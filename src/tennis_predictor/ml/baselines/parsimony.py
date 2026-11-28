"""E03C compact rating comparison. Training and selection stop before 2024."""

from itertools import pairwise
from time import perf_counter

import numpy as np
from sklearn.metrics import log_loss

from tennis_predictor.ml.baselines.evaluation import (
    FOLDS,
    HOLDOUT,
    TRAIN,
    WARMUP,
    rating_rows,
    summarize,
)
from tennis_predictor.ml.baselines.optimization import (
    CONTEXT,
    SEED,
    clean,
    design,
    fit_cat,
    fit_logistic,
    labels,
    learn_bin,
    prediction,
    uncertainty,
)
from tennis_predictor.ml.ratings.algorithms import Config

INNER = ("2023-01-01", "2023-07-01", "2024-01-01")
TOLERANCES = (0.0005, 0.001)
REFERENCE = {
    "run": 46160158114,
    "artifact": 15270078301,
    "logistic_log_loss": 0.615343,
    "catboost_log_loss": 0.617351,
    "original_catboost_log_loss": 0.617831,
}


def feature_subset(name, subset):
    context = any(name == f"interaction/elo.{x}" for x in CONTEXT)
    fast = "/fast." in name
    glicko2 = "/glicko2." in name
    redundant = name.endswith(".volatility") or (
        name.startswith("logdiff/") and name.endswith(".count") and name != "logdiff/elo.count"
    )
    strong = not (context or fast or glicko2 or redundant)
    if subset == "interactions":
        return True
    if subset == "no-context":
        return not context
    if subset == "joint":
        return not (context or fast or glicko2 or name.endswith(".volatility"))
    if not strong:
        return False
    if subset == "no-glicko":
        return "/glicko." not in name
    if subset == "no-rd":
        return not name.endswith((".rd", ".uncertainty"))
    if subset == "no-surface":
        return "/surface." not in name
    if subset == "no-experience":
        return not any(key in name for key in (".count", "gap_days", "newcomer"))
    return True


def specifications():
    for subset in (
        "interactions",
        "no-context",
        "joint",
        "compact",
        "no-glicko",
        "no-rd",
        "no-surface",
        "no-experience",
    ):
        yield {"family": "logistic", "id": subset, "subset": subset, "recipe": "interactions"}
    yield {"family": "catboost", "id": "original", "recipe": "raw", "depth": 3, "iterations": 200}
    for subset in ("joint", "compact", "no-experience"):
        yield {
            "family": "catboost",
            "id": subset,
            "subset": subset,
            "recipe": "interactions",
            "depth": 3,
            "iterations": 200,
        }
    for recipe in ("compact", "logged", "interactions"):
        yield {
            "family": "catboost",
            "id": f"depth4-{recipe}",
            "recipe": recipe,
            "drop": "glicko2",
            "depth": 4,
            "iterations": 280,
        }


def matrix(rows, spec):
    x, names = design(rows, spec["recipe"], spec.get("drop"))
    if "subset" in spec:
        keep = [i for i, name in enumerate(names) if feature_subset(name, spec["subset"])]
        return x[:, :, keep], [names[i] for i in keep]
    return x, names


def fit_predict(x, names, y, dates, fit, score, spec):
    if spec["family"] == "logistic":
        estimator, keep, _, audit = fit_logistic(
            x[fit],
            y[fit],
            dates[fit],
            {"penalty": "l2", "C": 1.0},
            names,
        )
        coefficients = dict(zip(audit["features"], audit["coefficients"], strict=True))
    else:
        exact = spec["id"] == "original"
        keep = list(range(len(names))) if exact else clean(x[fit])[0]
        estimator = fit_cat(
            x[fit][:, :, keep],
            y[fit],
            {
                "depth": spec["depth"],
                "iterations": spec["iterations"],
            },
            exact=exact,
        )
        coefficients = {}
    values = prediction(estimator, x[score][:, :, keep])
    return values, {
        "features": [names[i] for i in keep],
        "coefficients": coefficients,
        "train_matches": int(fit.sum()),
    }


def loss(y, probabilities):
    return float(log_loss(y, probabilities, labels=[0, 1]))


def complexity(candidate):
    spec = candidate["config"]
    counts = max(len(fold["features"]) for fold in candidate["folds"].values())
    return (spec.get("depth", 0), spec.get("iterations", 0), counts, candidate["inner_log_loss"])


def select(candidates, tolerance):
    best = min(candidates, key=lambda item: item["inner_log_loss"])
    qualifying = [
        item
        for item in candidates
        if item["inner_log_loss"] <= best["inner_log_loss"] + tolerance
        and all(
            fold["log_loss"] <= best["folds"][key]["log_loss"] + 0.001
            for key, fold in item["folds"].items()
        )
    ]
    return min(qualifying, key=complexity)


def count_drift(x, names, dates, y):
    report = {}
    reference = dates < "2024-01-01"
    for i, name in enumerate(names):
        if not name.endswith(".count"):
            continue
        pairs = x[:, :, i]
        plan = learn_bin(pairs[reference], y[reference], 10, signed=False)
        cuts = plan["edges"]
        k = len(cuts) + 1

        def shares(mask, cuts=cuts, pairs=pairs, k=k):
            counts = np.bincount(np.searchsorted(cuts, pairs[mask].ravel()), minlength=k) + 0.5
            return counts / counts.sum()

        base = shares(reference)
        years = {}
        for year in ("2024", "2025"):
            mask = np.char.startswith(dates, year)
            target = shares(mask)
            years[year] = {
                "n": int(mask.sum()),
                "psi": float(np.sum((target - base) * np.log(target / base))),
            }
        report[name] = {"edges": cuts, "years": years}
    return report


def compare(rows):
    dates = np.asarray([row[0] for row in rows])
    if (dates >= "2026-01-01").any():
        raise ValueError("E03C refuses holdout rows")
    y = labels(rows)
    predictions, audit = {}, {"logistic": [], "catboost": []}
    for spec in specifications():
        began = perf_counter()
        x, names = matrix(rows, spec)
        parts, truth, folds = [], [], {}
        for start, end in pairwise(INNER):
            fit, check = dates < start, (dates >= start) & (dates < end)
            if min(fit.sum(), check.sum()) < 20:
                raise ValueError("E03C needs at least 20 matches in each inner temporal fold")
            p, meta = fit_predict(x, names, y, dates, fit, check, spec)
            parts.extend(p)
            truth.extend(y[check])
            folds[start] = {
                **meta,
                "log_loss": loss(y[check], p),
                "validation_matches": int(check.sum()),
            }
        audit[spec["family"]].append(
            {
                "config": spec,
                "folds": folds,
                "inner_log_loss": loss(truth, parts),
                "seconds": perf_counter() - began,
            }
        )
    target = [row for row in rows if row[0] >= "2024-01-01"]
    results = {}
    for family, candidates in audit.items():
        reference = candidates[0]
        winners = {str(tol): select(candidates, tol) for tol in TOLERANCES}
        raw_best = min(candidates, key=lambda item: item["inner_log_loss"])
        evaluation = [reference, raw_best, *winners.values()]
        results[family] = {
            "candidates": candidates,
            "selected": winners[str(TOLERANCES[0])]["config"],
            "sensitivity": winners[str(TOLERANCES[1])]["config"],
            "outer": {},
        }
        for item in evaluation:
            spec = item["config"]
            identifier = spec["id"]
            if identifier in results[family]["outer"]:
                continue
            x, names = matrix(rows, spec)
            fold_report, parts = {}, []
            for start, end in FOLDS:
                fit = dates < str(start)
                check = (dates >= str(start)) & (dates < str(end))
                if check.sum() < 20:
                    raise ValueError("E03C needs at least 20 matches in both outer folds")
                p, meta = fit_predict(x, names, y, dates, fit, check, spec)
                parts.extend(p)
                fold_report[str(start.year)] = meta
            predictions[identifier] = np.asarray(parts)
            report = {"config": spec, "metrics": summarize(target, parts), "fits": fold_report}
            if family == "logistic":
                c1, c2 = (fold_report[year]["coefficients"] for year in ("2024", "2025"))
                report["coefficient_stability"] = {
                    name: {
                        "change": c2[name] - c1[name],
                        "sign_changed": bool(c1[name] * c2[name] < 0),
                    }
                    for name in c1.keys() & c2.keys()
                }
                report["count_psi"] = count_drift(x, names, dates, y)
            results[family]["outer"][identifier] = report
        for identifier, report in results[family]["outer"].items():
            report["versus_reference"] = uncertainty(
                target, predictions[reference["config"]["id"]], predictions[identifier]
            )
        results[family]["reference"] = reference["config"]
    return results, len(target)


def run(matches, source, output):
    if any(m.day >= HOLDOUT for m in matches):
        raise ValueError("E03C requires a pre-2026 snapshot")
    started = perf_counter()
    rows, rating = rating_rows(output, matches, source, "e03c", Config(), WARMUP, HOLDOUT, TRAIN)
    families, n = compare(rows)
    return {
        "experiment": "E03C",
        "source": source,
        "rating_version": rating,
        "common_targets": n,
        "holdout_opened": False,
        "outer_is_exploratory": True,
        "inner_folds": list(INNER),
        "tolerances": list(TOLERANCES),
        "seed": SEED,
        "references": REFERENCE,
        "families": families,
        "seconds": perf_counter() - started,
    }
