"""Chronological E04 ablations on frozen rating and raw-form features."""

import math
from collections import defaultdict, deque
from itertools import groupby, pairwise
from pathlib import Path
from time import perf_counter

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from tennis_predictor.ml.baselines.accepted import accepted_matrix
from tennis_predictor.ml.baselines.evaluation import (
    FOLDS,
    HOLDOUT,
    TRAIN,
    WARMUP,
    model,
    rating_rows,
    summarize,
)
from tennis_predictor.ml.baselines.optimization import (
    fit_cat,
    labels,
    learn_bin,
    paired_labels,
    prediction,
    uncertainty,
)
from tennis_predictor.ml.baselines.parsimony import INNER, loss
from tennis_predictor.ml.canonical_layer.evidence import fingerprint
from tennis_predictor.ml.ratings.algorithms import Config

from .engine import METRICS, VERSION, WINDOWS, Features

GROUPS = {
    "result": {"win", "sets", "games", "points"},
    "serve_return": {
        "first_serve_in",
        "first_serve_won",
        "second_serve_won",
        "serve_points",
        "return_points",
        "first_return_won",
        "second_return_won",
        "aces",
        "double_faults",
    },
    "games_pressure": {"hold", "break", "break_points_saved", "break_points_converted"},
    "shot_counts": {"winners", "unforced_errors"},
}
MODES = ("5", "10", "15", "combined", "contrast", "dispersion")


def historical_vectors(projection, target_ids):
    """Replay facts by day and calculate vectors only for requested prematch targets."""
    histories = defaultdict(lambda: deque(maxlen=15))
    engine = Features(lambda pid, day, cutoff: histories[pid], max_cache=8192)
    result = {}
    ordered = sorted(projection, key=lambda item: (item[0].day, item[0].match_id))
    for day, items in groupby(ordered, key=lambda item: item[0].day):
        entries = list(items)
        for match, _ in entries:
            if match.match_id in target_ids:
                vectors = [
                    engine.get("historical-replay", pid, day) for pid in (match.left, match.right)
                ]
                result[match.match_id] = np.asarray(
                    [
                        [
                            [
                                [
                                    side[size]["metrics"][metric][stat]
                                    for stat in ("pooled", "mean", "stddev")
                                ]
                                for metric in METRICS
                            ]
                            for size in WINDOWS
                        ]
                        for side in vectors
                    ],
                    dtype=float,
                )
        for match, observations in entries:
            for pid, fact in zip((match.left, match.right), observations, strict=True):
                histories[pid].append(fact)
    return result


def schema(mode, drop=None):
    names, terms = [], []
    specs = {
        "5": ((5, "pooled"),),
        "10": ((10, "pooled"),),
        "15": ((15, "pooled"),),
        "combined": tuple((size, "pooled") for size in WINDOWS),
        "contrast": ((15, "pooled"), (5, "contrast")),
        "dispersion": ((10, "mean"), (10, "stddev")),
    }
    for size, agg in specs[mode]:
        for metric in METRICS:
            if drop is None or metric not in GROUPS[drop]:
                terms.append((size, metric, agg))
                names.append(f"{size}/{metric}/{agg}")
    return names, terms


def matrix(rows, history, mode, drop=None):
    names, terms = schema(mode, drop)
    tensors = np.stack([history[row[1]] for row in rows])
    vectors = []
    for size, metric, op in terms:
        slot = METRICS.index(metric)
        if op == "contrast":
            values = tensors[:, :, 0, slot, 0] - tensors[:, :, 2, slot, 0]
        else:
            values = tensors[
                :, :, WINDOWS.index(size), slot, ("pooled", "mean", "stddev").index(op)
            ]
        vectors.append(values)
    sides = np.stack(vectors, axis=-1)
    diff = sides[:, 0] - sides[:, 1]
    return np.stack((diff, -diff), axis=1), names


def candidates(family):
    yield ("rating-only", None, None, "l2")
    for mode in MODES:
        yield (mode, mode, None, "l2")
    for group in GROUPS:
        yield (f"10-without-{group}", "10", group, "l2")
    if family == "logistic":
        yield ("10-l1", "10", None, "l1")


def forecast(family, x, y, fit, check, penalty):
    train = x[fit]
    labels_ = paired_labels(y[fit])
    flat = train.reshape(-1, train.shape[-1])
    if family == "logistic":
        classifier = (
            model(family)
            if penalty == "l2"
            else make_pipeline(
                StandardScaler(),
                LogisticRegression(
                    C=0.5, solver="liblinear", penalty="l1", random_state=110, max_iter=600
                ),
            )
        )
        # Remove zero-coverage columns in this fit, then preserve a separate
        # missingness indicator. Predicted probabilities are symmetrized.
        imputer = SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)
        classifier = make_pipeline(imputer, classifier)
        classifier.fit(flat, labels_)
    else:
        classifier = fit_cat(train, y[fit], {"depth": 3, "iterations": 200})
    return prediction(classifier, x[check])


def diagnostics(values, names, y, dates):
    train = dates < "2024-01-01"
    valid = dates >= "2024-01-01"
    result = {}
    corr = []
    for i, name in enumerate(names):
        x = values[:, :, i]
        mapping = learn_bin(x[train], y[train], 7, signed=True)
        # Missingness has its own bucket. Compare using train-frozen edges.
        from tennis_predictor.ml.baselines.optimization import split_bins

        finite = np.isfinite(x)
        bins = np.where(finite, split_bins(np.where(finite, x, 0), mapping["edges"], True), 99)
        keys = np.unique(bins)

        def distribution(mask, bins=bins, keys=keys):
            counts = np.asarray([(bins[mask] == key).sum() + 0.5 for key in keys])
            return counts / counts.sum()

        base, later = distribution(train), distribution(valid)
        result[name] = {
            "iv": mapping["iv"],
            "psi": float(np.sum((later - base) * np.log(later / base))),
            "train_available_matches": int(finite[train].all(axis=1).sum()),
        }
        v = x[train, 0]
        observed = v[np.isfinite(v)]
        corr.append(np.nan_to_num(v, nan=float(np.median(observed)) if len(observed) else 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        matrix_ = np.atleast_2d(np.corrcoef(corr))
    return {
        "features": result,
        "correlation": [[float(v) if math.isfinite(v) else None for v in row] for row in matrix_],
        "correlation_order": names,
    }


def evaluate(matches, projection, source, output):
    if any(item.day >= HOLDOUT for item in matches):
        raise ValueError("E04 refuses holdout matches")
    output = Path(output)
    started = perf_counter()
    rows, rating = rating_rows(output, matches, source, "ratings", Config(), WARMUP, HOLDOUT, TRAIN)
    target_ids = {row[1] for row in rows}
    clock = perf_counter()
    key = fingerprint([VERSION, rating, source])
    cache = output / (key + ".npz")
    ids = [row[1] for row in rows]
    if cache.exists():
        with np.load(cache, allow_pickle=False) as saved:
            if saved["ids"].tolist() != ids:
                raise ValueError("Feature cache cohort mismatch")
            history = dict(zip(ids, saved["form"], strict=True))
    else:
        history = historical_vectors(projection, target_ids)
        pending = cache.with_suffix(".pending.npz")
        np.savez_compressed(pending, ids=ids, form=np.stack([history[mid] for mid in ids]))
        pending.replace(cache)
    elapsed = perf_counter() - clock
    dates = np.asarray([row[0] for row in rows])
    y = labels(rows)
    outer_rows = [row for row in rows if row[0] >= "2024-01-01"]
    report = {}
    for family in ("logistic", "catboost"):
        baseline = accepted_matrix(rows, family)
        kept, models, scores = {}, [], {}
        rated_n = baseline.shape[-1]
        for name, mode, drop, penalty in candidates(family):
            form, names = matrix(rows, history, mode, drop) if mode else (None, [])
            x = np.concatenate((baseline, form), axis=2) if mode else baseline
            folds, pooled, truth = [], [], []
            for start, end in pairwise(INNER):
                fit, check = dates < start, (dates >= start) & (dates < end)
                p = forecast(family, x, y, fit, check, penalty)
                pooled.extend(p)
                truth.extend(y[check])
                folds.append(loss(y[check], p))
            models.append(
                {
                    "id": name,
                    "recipe": mode,
                    "drop_group": drop,
                    "penalty": penalty,
                    "features": names,
                    "inner_loss": loss(truth, pooled),
                    "fold_losses": folds,
                }
            )
            kept[name] = x
        best = min(models, key=lambda m: m["inner_loss"])
        choice = {}
        for tolerance in (0.0005, 0.001):
            eligible = [
                m
                for m in models
                if m["inner_loss"] <= best["inner_loss"] + tolerance
                and all(
                    a <= b + 0.001
                    for a, b in zip(m["fold_losses"], best["fold_losses"], strict=True)
                )
            ]
            choice[str(tolerance)] = min(
                eligible, key=lambda item: (len(item["features"]), item["inner_loss"])
            )["id"]
        main = choice["0.0005"]
        selected = {"rating-only", main, choice["0.001"], best["id"]}
        outer = {}
        for config in models:
            name = config["id"]
            if name not in selected:
                continue
            x = kept[name]
            forecasts = []
            for start, end in FOLDS:
                fit = dates < str(start)
                check = (dates >= str(start)) & (dates < str(end))
                forecasts.extend(forecast(family, x, y, fit, check, config["penalty"]))
            scores[name] = np.asarray(forecasts)
            outer[name] = {"full": summarize(outer_rows, forecasts)}
        for name, summary in outer.items():
            summary["delta"] = uncertainty(outer_rows, scores["rating-only"], scores[name])
        chosen = kept[main][:, :, rated_n:]
        selected_spec = next(item for item in models if item["id"] == main)
        # Matched coverage uses the same mask for every scored candidate.
        form, names = matrix(rows, history, "10")
        observed = np.isfinite(
            form[:, :, [i for i, metric in enumerate(METRICS) if metric not in GROUPS["result"]]]
        ).any(axis=(1, 2)) & (dates >= "2024-01-01")
        outer_mask = observed[dates >= "2024-01-01"]
        if outer_mask.any():
            matched = [row for row, yes in zip(outer_rows, outer_mask, strict=True) if yes]
            for name, summary in outer.items():
                summary["matched"] = summarize(matched, scores[name][outer_mask])
        report[family] = {
            "selection": choice,
            "candidates": models,
            "outer": outer,
            "matched_targets": int(outer_mask.sum()),
            "selected_diagnostics": diagnostics(chosen, selected_spec["features"], y, dates)
            if chosen.shape[-1]
            else {},
            "coverage_diagnostics": diagnostics(form, names, y, dates),
        }
    return {
        "experiment": "E04",
        "feature_schema": VERSION,
        "source": source,
        "rating_version": rating,
        "cache_version": key,
        "cache_bytes": cache.stat().st_size,
        "window_replay_seconds": elapsed,
        "elapsed_seconds": perf_counter() - started,
        "targets": len(outer_rows),
        "holdout_opened": False,
        "models": report,
    }
