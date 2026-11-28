"""Validation-only, paired optimization of existing rating features."""

import math
from dataclasses import replace
from time import perf_counter

import numpy as np
from catboost import Pool
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from tennis_predictor.ml.baselines.evaluation import (
    FOLDS,
    HOLDOUT,
    MODEL_SPEC,
    TRAIN,
    WARMUP,
    model,
    rating_rows,
    summarize,
    vectors,
)
from tennis_predictor.ml.ratings.algorithms import MODELS, Config

INNER = "2023-01-01"
MIN_SUPPORT = 0.05
SEED = 109
SIDE = [f"{m}.{s}" for m in MODELS for s in ("rating", "rd", "volatility", "count")] + [
    "gap_days",
    "newcomer",
]
CONTEXT = ["female", "challenger", "hard", "clay", "grass", "carpet"]
RAW_NAMES = (
    [p + name for p in ("own/", "opponent/", "diff/") for name in SIDE]
    + [f"prob/{m}" for m in MODELS]
    + CONTEXT
)
PREVIOUS = {
    "run": 46149082580,
    "artifact": 15265372946,
    "targets": 35441,
    "logistic_log_loss": 0.61850982,
    "catboost_log_loss": 0.61783069,
}


def design(rows, recipe, drop=None):
    raw = np.asarray([vectors(row) for row in rows], dtype=np.float64)
    size = len(SIDE)
    if recipe == "raw":
        x, names = raw, RAW_NAMES
    else:
        left, right = raw[:, :, :size], raw[:, :, size : 2 * size]
        cols, names = [], []
        for i, name in enumerate(SIDE):
            if recipe in ("logged", "interactions") and (
                name.endswith(".count") or name == "gap_days"
            ):
                cols.append(np.log1p(left[:, :, i]) - np.log1p(right[:, :, i]))
                names.append("logdiff/" + name)
            else:
                cols.append(left[:, :, i] - right[:, :, i])
                names.append("diff/" + name)
        if recipe == "interactions":
            for m in ("glicko", "glicko2"):
                i = SIDE.index(f"{m}.rating")
                j = SIDE.index(f"{m}.rd")
                cols.append(cols[i] / (1 + np.hypot(left[:, :, j], right[:, :, j]) / 350))
                names.append(f"interaction/{m}.uncertainty")
            elo = cols[0]
            for i, name in enumerate(CONTEXT):
                cols.append(elo * raw[:, :, -len(CONTEXT) + i])
                names.append(f"interaction/elo.{name}")
        x = np.stack(cols, axis=-1)
    keep = [
        i
        for i, name in enumerate(names)
        if drop is None
        or not any(part.startswith(drop + ".") for part in name.split("/"))
        and name != f"prob/{drop}"
    ]
    return x[:, :, keep], [names[i] for i in keep]


def clean(x):
    """Stable first representative for exactly duplicate columns, based on past only."""
    flat = x.reshape(-1, x.shape[-1])
    keep, constants, duplicate, seen = [], [], {}, {}
    for i, col in enumerate(flat.T):
        if np.all(col == col[0]):
            constants.append(i)
            continue
        key = np.ascontiguousarray(col).tobytes()
        if key in seen:
            duplicate[i] = seen[key]
        else:
            keep.append(i)
            seen[key] = i
    return keep, constants, duplicate


def labels(rows):
    return np.asarray([row[7] for row in rows], dtype=int)


def paired_labels(y):
    return np.column_stack((y, 1 - y)).reshape(-1)


def prediction(estimator, x):
    pred = estimator.predict_proba(x.reshape(-1, x.shape[-1]))[:, 1].reshape(-1, 2)
    return np.clip((1 + pred[:, 0] - pred[:, 1]) / 2, 1e-15, 1 - 1e-15)


def split_bins(column, edges, signed):
    # Index zero is neutral for signed values and a normal first bin otherwise.
    if signed:
        return np.where(column == 0, 0, np.sign(column) * (1 + np.searchsorted(edges, abs(column))))
    return np.searchsorted(edges, column)


def learn_bin(x, y, bins, *, signed, monotonic=False):
    """Fit finite, mirrored maps on paired matches with minimum support."""
    if signed:
        nonzero = np.isfinite(x) & (x != 0)
        values = abs(x[nonzero])
        target = np.broadcast_to(np.column_stack((y, 1 - y)), x.shape)[nonzero]
        target = np.where(x[nonzero] > 0, target, 1 - target)
        # Mirrored bins have at most floor((bins-1)/2) positive buckets plus zero.
        k = max(1, (bins - 1) // 2)
    else:
        mask = np.isfinite(x)
        values = x[mask]
        target = np.column_stack((y, 1 - y))[mask]
        k = bins
    edges = np.unique(np.quantile(values, np.arange(1, k) / k)).tolist() if len(values) else []
    while edges:
        membership = np.searchsorted(edges, values)
        minimum = math.ceil(MIN_SUPPORT * len(values))
        count = np.bincount(membership, minlength=len(edges) + 1)
        small = np.flatnonzero(count < minimum)
        if len(small):
            edges.pop(min(int(small[0]), len(edges) - 1))
            continue
        if monotonic:
            rates = np.bincount(membership, weights=target, minlength=len(count)) / count
            trend = 1 if rates[-1] >= rates[0] else -1
            violations = np.flatnonzero(np.diff(rates) * trend < 0)
            if len(violations):
                edges.pop(int(violations[0]))
                continue
        break

    eligible = np.isfinite(x)
    buckets = split_bins(np.where(eligible, x, 0), edges, signed)
    event = np.column_stack((y, 1 - y))
    stats = {}
    for b in np.unique(buckets[eligible]):
        mask = eligible & (buckets == b)
        wins = int(event[mask].sum())
        stats[int(b)] = {
            "matches": int(mask.any(axis=1).sum()),
            "orientations": int(mask.sum()),
            "events": wins,
            "event_rate": wins / int(mask.sum()),
        }
    total = sum(item["orientations"] for item in stats.values()) / 2 + 0.5 * len(stats)
    iv = 0.0
    for item in stats.values():
        good = (item["events"] + 0.5) / total
        bad = (item["orientations"] - item["events"] + 0.5) / total
        item["woe"] = math.log(good / bad)
        item["iv"] = (good - bad) * item["woe"]
        iv += item["iv"]
    return {
        "edges": edges,
        "signed": signed,
        "bins": stats,
        "iv": iv,
        "missing_matches": int((~eligible).any(axis=1).sum()),
        "unknown_woe": 0.0,
    }


def apply_bin(x, mapping):
    valid = np.isfinite(x)
    result = np.zeros(x.shape)
    memberships = split_bins(np.where(valid, x, 0), mapping["edges"], mapping["signed"])
    for bin_id, stat in mapping["bins"].items():
        result[valid & (memberships == bin_id)] = stat["woe"]
    return result


def fit_maps(x, y, bins, mode):
    return [
        learn_bin(
            col,
            y,
            bins,
            signed=np.allclose(col[:, 0], -col[:, 1], equal_nan=True),
            monotonic=mode == "monotonic",
        )
        for col in x.transpose(2, 0, 1)
    ]


def apply_maps(x, maps):
    return np.stack([apply_bin(x[:, :, i], item) for i, item in enumerate(maps)], axis=-1)


def fit_logistic(x, y, dates, spec, names):
    keep, constant, duplicate = clean(x)
    if not keep:
        raise ValueError("All logistic inputs are constant")
    x = x[:, :, keep]
    maps = None
    initial = 0
    if spec.get("bins"):
        maps = fit_maps(x, y, spec["bins"], spec["bin_mode"])
        if spec.get("iv_top"):
            selected = sorted(range(len(keep)), key=lambda i: (-maps[i]["iv"], i))[: spec["iv_top"]]
            keep = [keep[i] for i in selected]
            x = x[:, :, selected]
            maps = [maps[i] for i in selected]
        if spec.get("oof"):
            # Fixed half-year blocks. First block only fits later maps, never a row's own label.
            boundary = np.asarray(
                [day[:4] + ("-01" if day[5:7] < "07" else "-07") for day in dates]
            )
            blocks, selected_indices = [], []
            for period in np.unique(boundary):
                past = boundary < period
                idx = np.flatnonzero(boundary == period)
                if not past.any():
                    initial += len(idx)
                    continue
                # IV selection is independent of OOF values. Keep every eligible
                # column for OOF candidates so future labels cannot select it.
                past_maps = fit_maps(x[past], y[past], spec["bins"], spec["bin_mode"])
                blocks.append(apply_maps(x[idx], past_maps))
                selected_indices.extend(idx)
            if not blocks:
                raise ValueError("OOF needs a later chronological training block")
            x, y = np.concatenate(blocks), y[selected_indices]
        else:
            x = apply_maps(x, maps)
    params = {"max_iter": 600, "random_state": SEED, "C": spec.get("C", 1.0)}
    if spec["penalty"] != "l2":
        params.update(solver="saga", penalty=spec["penalty"], tol=1e-3)
        if spec["penalty"] == "elasticnet":
            params["l1_ratio"] = 0.5
    estimator = make_pipeline(StandardScaler(), LogisticRegression(**params))
    estimator.fit(x.reshape(-1, x.shape[-1]), paired_labels(y))
    audit = {
        "features": [names[i] for i in keep],
        "constants": [names[i] for i in constant],
        "duplicates": {names[i]: names[j] for i, j in duplicate.items()},
        "coefficients": estimator[-1].coef_[0].tolist(),
        "oof_initial_matches": initial,
        "bin_maps": maps,
    }
    return estimator, keep, maps, audit


def raw_logistic(x, y, names):
    estimator = model("logistic").fit(x.reshape(-1, x.shape[-1]), paired_labels(y))
    return (
        estimator,
        list(range(len(names))),
        None,
        {
            "features": names,
            "coefficients": estimator[-1].coef_[0].tolist(),
            "bin_maps": None,
            "oof_initial_matches": 0,
        },
    )


def logistic_options():
    yield {"id": "original", "recipe": "raw", "penalty": "l2", "C": 1.0}
    for recipe in ("compact", "logged", "interactions"):
        yield {"id": recipe, "recipe": recipe, "penalty": "l2", "C": 1.0}
    for penalty in ("l1", "elasticnet"):
        yield {"id": penalty, "recipe": "interactions", "penalty": penalty, "C": 0.3}
    for family in MODELS:
        yield {"id": "drop-" + family, "recipe": "interactions", "penalty": "l2", "drop": family}
    for bins in (3, 5, 7, 10):
        for mode in ("quantile", "monotonic"):
            yield {
                "id": f"woe-{mode}-{bins}",
                "recipe": "interactions",
                "penalty": "l2",
                "bins": bins,
                "bin_mode": mode,
                "iv_top": 14,
            }
    for mode in ("quantile", "monotonic"):
        yield {
            "id": "woe-oof-" + mode,
            "recipe": "interactions",
            "penalty": "l2",
            "bins": 7,
            "bin_mode": mode,
            "oof": True,
        }


def diagnostic(train, y, scored):
    keep, constants, duplicate = clean(train)
    flat = train.reshape(-1, train.shape[-1])
    matrix = flat[:, keep]
    correlation = np.corrcoef(matrix, rowvar=False)
    correlation = np.atleast_2d(correlation) if len(keep) else np.empty((0, 0))
    if len(keep) == 1:
        correlation = np.ones((1, 1))
    vals, vec = np.linalg.eigh(correlation)
    positive = vals > 1e-10
    null = np.sum(vec[:, ~positive] ** 2, axis=1)
    vif = np.sum(vec[:, positive] ** 2 / vals[positive], axis=1)
    result = {
        "feature_names": RAW_NAMES,
        "rank": int(positive.sum()),
        "constants": [RAW_NAMES[i] for i in constants],
        "duplicates": {RAW_NAMES[i]: RAW_NAMES[j] for i, j in duplicate.items()},
        "correlation_features": [RAW_NAMES[i] for i in keep],
        "correlation": correlation.tolist(),
        "vif": {
            RAW_NAMES[i]: (float(value) if weight < 1e-9 else None)
            for i, value, weight in zip(keep, vif, null, strict=True)
        },
        "features": {},
    }
    for i, name in enumerate(RAW_NAMES):
        col = train[:, :, i]
        mapping = learn_bin(col, y, 7, signed=False)
        cut = mapping["edges"]
        count = len(cut) + 1
        origin = np.bincount(np.searchsorted(cut, col.reshape(-1)), minlength=count) + 0.5
        origin = origin / origin.sum()
        psi = {}
        for segment, array in scored.items():
            if len(array) < 100:
                continue
            observed = (
                np.bincount(np.searchsorted(cut, array[:, :, i].reshape(-1)), minlength=count) + 0.5
            )
            observed = observed / observed.sum()
            psi[segment] = float(np.sum((observed - origin) * np.log(observed / origin)))
        auc = float(roc_auc_score(paired_labels(y), col.reshape(-1)))
        result["features"][name] = {
            "quantiles": np.quantile(col, [0, 0.05, 0.25, 0.5, 0.75, 0.95, 1]).tolist(),
            "missing_matches": int((~np.isfinite(col)).any(axis=1).sum()),
            "bin_map": mapping,
            "auc": auc,
            "gini": 2 * auc - 1,
            "psi": psi,
        }
    return result


def fit_cat(train, y, spec, *, ev=None, exact=False):
    params = {**MODEL_SPEC["catboost"], **spec}
    learner = model("catboost") if exact else __import__("catboost").CatBoostClassifier(**params)
    x = train.reshape(-1, train.shape[-1])
    if ev is None:
        learner.fit(x, paired_labels(y))
    else:
        ex, ey = ev
        learner.fit(
            x,
            paired_labels(y),
            eval_set=(ex.reshape(-1, ex.shape[-1]), paired_labels(ey)),
            early_stopping_rounds=30,
            use_best_model=True,
        )
    return learner


def select_cat(rows, fit, valid, alternative):
    registry = []
    cache = {}

    def attempt(recipe="raw", drop=None, ratings="baseline", **params):
        spec = {"recipe": recipe, "drop": drop, "ratings": ratings, **params}
        if spec in [r["config"] for r in registry]:
            return next(r for r in registry if r["config"] == spec)
        source = rows if ratings == "baseline" else alternative
        x, names = design(source, recipe, drop)
        keep, _, _ = clean(x[fit])
        x = x[:, :, keep]
        start = perf_counter()
        estimator = fit_cat(
            x[fit], labels(source)[fit], params, ev=(x[valid], labels(source)[valid])
        )
        score = log_loss(labels(source)[valid], prediction(estimator, x[valid]), labels=[0, 1])
        entry = {
            "config": spec,
            "inner_log_loss": float(score),
            "iterations": estimator.tree_count_,
            "features": [names[i] for i in keep],
            "seconds": round(perf_counter() - start, 3),
        }
        registry.append(entry)
        cache[len(registry) - 1] = (estimator, x[valid], labels(source)[valid])
        return entry

    default = {"depth": 3, "learning_rate": 0.03, "iterations": 320, "l2_leaf_reg": 3}
    for recipe in ("raw", "compact", "logged", "interactions"):
        attempt(recipe, **default)
    best = min(registry, key=lambda r: r["inner_log_loss"])
    recipe = best["config"]["recipe"]
    for family in MODELS:
        attempt(recipe, drop=family, **default)
    if alternative is not rows:
        attempt(recipe, ratings="inner-selected", **default)
    best = min(registry, key=lambda r: r["inner_log_loss"])
    for param, candidates in {
        "depth": (3, 4, 5, 6),
        "learning_rate": (0.02, 0.03, 0.05, 0.1),
        "l2_leaf_reg": (3, 6, 12),
    }.items():
        frozen = best["config"]
        for value in candidates:
            attempt(**{**frozen, param: value})
        best = min(registry, key=lambda r: r["inner_log_loss"])
    estimator, x, y = cache[registry.index(best)]
    return registry, best, cat_diagnostics(estimator, x, y, best["features"])


def cat_diagnostics(estimator, x, y, names):
    rng = np.random.default_rng(SEED)
    idx = np.sort(rng.choice(len(x), min(1800, len(x)), replace=False))
    x, y = x[idx], y[idx]
    baseline = log_loss(y, prediction(estimator, x), labels=[0, 1])
    donor = rng.permutation(len(x))
    importance = {}
    groups = [*MODELS, "context", "activity"]
    for family in groups:
        columns = [
            i
            for i, name in enumerate(names)
            if ("/" not in name and family == "context")
            or any(part.startswith(family + ".") for part in name.split("/"))
            or (family == "activity" and ("gap_days" in name or "newcomer" in name))
            or name == f"prob/{family}"
        ]
        if not columns:
            continue
        shuffled = x.copy()
        shuffled[:, :, columns] = x[donor][:, :, columns]
        importance[family] = float(
            log_loss(y, prediction(estimator, shuffled), labels=[0, 1]) - baseline
        )
    subset = x[:512]
    shap = estimator.get_feature_importance(
        Pool(subset.reshape(-1, subset.shape[-1])), type="ShapValues", thread_count=2
    )
    return {
        "matches": len(x),
        "grouped_permutation_log_loss_delta": importance,
        "shap_matches": len(subset),
        "mean_abs_shap": dict(zip(names, np.mean(abs(shap[:, :-1]), axis=0).tolist(), strict=True)),
    }


def masks(rows):
    dates = np.asarray([row[0] for row in rows])
    return dates, dates < INNER, (dates >= INNER) & (dates < "2024-01-01")


def select_rating_config(output, matches, source, rows, original_version):
    _, _, valid = masks(rows)
    candidates = [
        ("baseline", Config()),
        ("elo-16", replace(Config(), elo_k=16)),
        ("elo-32", replace(Config(), elo_k=32)),
        ("fast-48", replace(Config(), fast_k=48)),
        ("rd-200", replace(Config(), rd=200)),
    ]
    original = [(row[0], row[1], row[7]) for row in rows]
    report = []
    leader, alternative = (math.inf, False), rows
    for name, config in candidates:
        if name == "baseline":
            projected, version = rows, original_version
        else:
            projected, version = rating_rows(
                output, matches, source, name, config, WARMUP, HOLDOUT, TRAIN
            )
            if [(row[0], row[1], row[7]) for row in projected] != original:
                raise ValueError("Rating variant changed the target cohort")
        p = np.asarray(
            [vectors(row)[0][-len(CONTEXT) - len(MODELS) : -len(CONTEXT)] for row in projected]
        )
        losses = {
            m: float(log_loss(labels(projected)[valid], p[valid, i], labels=[0, 1]))
            for i, m in enumerate(MODELS)
        }
        score = sum(losses.values()) / len(losses)
        rank = (score, name != "baseline")
        report.append(
            {
                "id": name,
                "config": vars(config),
                "rating_version": version,
                "inner_log_loss_by_family": losses,
                "mean_inner_log_loss": score,
            }
        )
        if rank < leader:
            leader, alternative = rank, projected
    return alternative, report


def log_fit_predict(rows, fit, valid, spec):
    x, names = design(rows, spec["recipe"], spec.get("drop"))
    dates = np.asarray([row[0] for row in rows])
    y = labels(rows)
    if spec["id"] == "original":
        est, keep, maps, audit = raw_logistic(x[fit], y[fit], names)
    else:
        est, keep, maps, audit = fit_logistic(x[fit], y[fit], dates[fit], spec, names)
    hold = x[valid][:, :, keep]
    return prediction(est, apply_maps(hold, maps) if maps else hold), audit


def choose(candidates, baseline, tolerance=0.0002):
    best = min(candidates, key=lambda item: item["inner_log_loss"])
    return baseline if baseline["inner_log_loss"] - best["inner_log_loss"] <= tolerance else best


def outer_log(rows, specs):
    dates, _, _ = masks(rows)
    result, forecasts, actual = {}, {}, []
    for spec in specs:
        scores, selected_rows, audits = [], [], {}
        for start, stop in FOLDS:
            valid = (dates >= str(start)) & (dates < str(stop))
            values, audit = log_fit_predict(rows, dates < str(start), valid, spec)
            selected_rows.extend(row for row, eligible in zip(rows, valid, strict=True) if eligible)
            scores.extend(values.tolist())
            audit["train_matches"] = int((dates < str(start)).sum())
            audits[str(start.year)] = audit
        result[spec["id"]] = {"outer": summarize(selected_rows, scores), "folds": audits}
        forecasts[spec["id"]] = np.asarray(scores)
        actual = selected_rows
    return result, forecasts, actual


def outer_cat(rows, alternative, chosen):
    validated, forecasts, actual = {}, {}, []
    for label in ("original", "selected"):
        recipe = "raw" if label == "original" else chosen["config"]["recipe"]
        spec = {} if label == "original" else dict(chosen["config"])
        source = rows
        if spec.pop("ratings", None) == "inner-selected":
            source = alternative
        x, names = design(source, recipe, spec.pop("drop", None))
        spec.pop("recipe", None)
        if label == "selected":
            spec["iterations"] = chosen["iterations"]
        received, selected_rows, records = [], [], {}
        dates, _, _ = masks(source)
        for start, stop in FOLDS:
            fit, valid = dates < str(start), (dates >= str(start)) & (dates < str(stop))
            keep = list(range(len(names))) if label == "original" else clean(x[fit])[0]
            learner = fit_cat(
                x[fit][:, :, keep], labels(source)[fit], spec, exact=label == "original"
            )
            received.extend(prediction(learner, x[valid][:, :, keep]).tolist())
            selected_rows.extend(row for row, yes in zip(source, valid, strict=True) if yes)
            records[str(start.year)] = {
                "features": [names[i] for i in keep],
                "train_matches": int(fit.sum()),
            }
        validated[label] = {"outer": summarize(selected_rows, received), "folds": records}
        forecasts[label] = np.asarray(received)
        actual = selected_rows
    return validated, forecasts, actual


def uncertainty(rows, baseline, candidate):
    y = labels(rows)
    diff = -np.log(np.where(y, candidate, 1 - candidate)) + np.log(
        np.where(y, baseline, 1 - baseline)
    )
    dates, idx = np.unique([row[0] for row in rows], return_inverse=True)
    totals, sizes = np.bincount(idx, weights=diff), np.bincount(idx)
    rng = np.random.default_rng(SEED)
    samples = rng.integers(0, len(dates), size=(1000, len(dates)))
    estimates = totals[samples].sum(axis=1) / sizes[samples].sum(axis=1)
    return {
        "delta_log_loss": float(diff.mean()),
        "days": len(dates),
        "day_bootstrap_95pct": np.quantile(estimates, [0.025, 0.975]).tolist(),
    }


def optimize(matches, source, output, publish):
    if any(match.day >= HOLDOUT for match in matches):
        raise ValueError("Optimization requires a validation-only cohort")
    began = perf_counter()
    rows, version = rating_rows(
        output, matches, source, "baseline", Config(), WARMUP, HOLDOUT, TRAIN
    )
    dates, fit, valid = masks(rows)
    if (
        min(fit.sum(), valid.sum(), *((dates[:, None] >= ["2024-01-01", "2025-01-01"]).sum(axis=0)))
        < 100
    ):
        raise ValueError("Optimization requires sufficient pre-2026 historical data in each year")
    raw, _ = design(rows, "raw")
    segments = {}
    for year in ("2023", "2024", "2025"):
        year_mask = np.char.startswith(dates, year)
        segments[f"year/{year}"] = raw[year_mask]
        for column, label in ((2, "gender"), (3, "level"), (4, "surface")):
            keys = np.asarray([r[column] for r in rows])
            for group in np.unique(keys):
                mask = year_mask & (keys == group)
                if mask.sum() >= 100:
                    segments[f"{year}/{label}/{group}"] = raw[mask]
    training = dates < "2024-01-01"
    evidence = diagnostic(raw[training], labels(rows)[training], segments)
    candidates = []
    for spec in logistic_options():
        start = perf_counter()
        scores, audit = log_fit_predict(rows, fit, valid, spec)
        candidates.append(
            {
                "config": spec,
                "inner_log_loss": float(log_loss(labels(rows)[valid], scores, labels=[0, 1])),
                "diagnostics": audit,
                "seconds": round(perf_counter() - start, 3),
            }
        )
    selected = choose(candidates, candidates[0])
    specs = [candidates[0]["config"]]
    if selected != candidates[0]:
        specs.append(selected["config"])
    outer, values, target = outer_log(rows, specs)
    log_id = selected["config"]["id"]
    shared = {
        "source": source,
        "rating_version": version,
        "historical_reference": PREVIOUS,
        "common_targets": len(target),
        "exploratory_outer": [2024, 2025],
        "inner_training": 2022,
        "inner_validation": 2023,
        "holdout_opened": False,
        "cohort_count_difference": len(target) - PREVIOUS["targets"],
    }
    first = {
        **shared,
        "experiment": "E03A",
        "selection": selected["config"],
        "candidates": candidates,
        "outer": outer,
        "paired_uncertainty": uncertainty(target, values["original"], values[log_id]),
        "feature_audit": evidence,
        "seconds": round(perf_counter() - began, 3),
    }
    publish("E03A", first)

    began = perf_counter()
    alternative, ratings = select_rating_config(output, matches, source, rows, version)
    configurations, chosen, feature = select_cat(rows, fit, valid, alternative)
    outer, values, scored = outer_cat(rows, alternative, chosen)
    if [(r[0], r[1], r[7]) for r in target] != [(r[0], r[1], r[7]) for r in scored]:
        raise ValueError("E03B outer cohort differs from E03A")
    second = {
        **shared,
        "experiment": "E03B",
        "selection": chosen,
        "candidates": configurations,
        "rating_config_comparison": ratings,
        "outer": outer,
        "feature_audit": feature,
        "paired_uncertainty": uncertainty(scored, values["original"], values["selected"]),
        "seconds": round(perf_counter() - began, 3),
    }
    publish("E03B", second)
