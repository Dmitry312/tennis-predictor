"""Past-only evaluation of ratings and two supervised baselines."""

import math
import struct
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict
from datetime import date

import numpy as np
from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from tennis_predictor.ml.ratings.algorithms import MODELS
from tennis_predictor.ml.ratings.engine import PACK, Reader, build
from tennis_predictor.ml.ratings.evaluate import Metrics, variants

TRAIN = date(2022, 1, 1)
VALIDATION = date(2024, 1, 1)
HOLDOUT = date(2026, 1, 1)
WARMUP = date(2021, 1, 1)
FOLDS = ((VALIDATION, date(2025, 1, 1)), (date(2025, 1, 1), HOLDOUT))
SUPERVISED = ("logistic", "catboost")
MODEL_SPEC = {
    "logistic": {"C": 1.0, "max_iter": 500, "random_state": 107},
    "catboost": {
        "iterations": 200,
        "depth": 3,
        "learning_rate": 0.03,
        "random_seed": 107,
        "thread_count": 2,
        "allow_writing_files": False,
        "verbose": False,
    },
}


def vectors(row):
    _, _, gender, level, surface, _, _, _, _, _, left, right, probs, _, left_gap, right_gap = row
    context = [
        gender == "F",
        level == "challenger",
        *(surface == item for item in ("hard", "clay", "grass", "carpet")),
    ]
    sides = [
        [x for state in PACK.iter_unpack(blob) for x in state] + [max(0, gap), gap < 0]
        for blob, gap in ((left, left_gap), (right, right_gap))
    ]
    p = struct.unpack("<5d", probs)
    return tuple(
        [*own, *other, *(a - b for a, b in zip(own, other, strict=True)), *chances, *context]
        for own, other, chances in (
            (sides[0], sides[1], p),
            (sides[1], sides[0], [1 - x for x in p]),
        )
    )


def model(name):
    if name == "logistic":
        return make_pipeline(StandardScaler(), LogisticRegression(**MODEL_SPEC[name]))
    return CatBoostClassifier(**MODEL_SPEC[name])


def supervised(name, train, test):
    if not train or not test:
        return []
    training = np.asarray([vectors(row) for row in train], dtype=np.float64)
    labels = np.asarray([(r[7], 1 - r[7]) for r in train]).reshape(-1)
    estimator = model(name).fit(training.reshape(-1, training.shape[-1]), labels)
    scoring = np.asarray([vectors(row) for row in test], dtype=np.float64)
    probabilities = estimator.predict_proba(scoring.reshape(-1, scoring.shape[-1]))[:, 1]
    paired = probabilities.reshape(-1, 2)
    return np.clip((paired[:, 0] + 1 - paired[:, 1]) / 2, 1e-15, 1 - 1e-15).tolist()


def summarize(rows, predictions):
    groups = defaultdict(lambda: (Metrics(), [], []))
    for row, p in zip(rows, predictions, strict=True):
        day, _, gender, level, surface, _, _, y, *_ = row
        for key in (
            "all",
            f"year/{day[:4]}",
            f"gender/{gender}",
            f"level/{level}",
            f"surface/{surface}",
        ):
            metrics, scores, labels = groups[key]
            metrics.add(p, y, True)
            scores.append(p)
            labels.append(y)
    return {
        key: {
            **metrics.report(),
            "roc_auc": float(roc_auc_score(labels, scores)) if len(set(labels)) == 2 else None,
        }
        for key, (metrics, scores, labels) in sorted(groups.items())
    }


def options():
    registered = list(variants())
    for name, config, models in registered:
        yield name, config, models, WARMUP
    yield "warmup-2022", registered[0][1], MODELS, TRAIN


def rating_rows(output, matches, source, name, config, warmup, end, train_start):
    info = build(output / name, [m for m in matches if warmup <= m.day < end], source, config)
    with closing(Reader(output / name, info["version"]).connect()) as db:
        rows = db.execute(
            "SELECT * FROM predictions WHERE target=1 AND day>=? ORDER BY day,match_id",
            (train_start.isoformat(),),
        ).fetchall()
    return rows, info["version"]


def run(matches, source, output, as_of, *, frozen=None, train_start=TRAIN):
    heldout = frozen is not None
    end = min(as_of, HOLDOUT) if not heldout else as_of
    cuts = (
        tuple((a, min(b, end)) for a, b in FOLDS if a < end) if not heldout else ((HOLDOUT, end),)
    )
    if heldout:
        allowed = {m: {name for name, _, models, _ in options() if m in models} for m in MODELS}
        choices = frozen.get("selected_ratings", {})
        if set(choices) != set(allowed) or any(choices[m] not in allowed[m] for m in allowed):
            raise ValueError("Frozen report has invalid rating choices")
    report = {
        "snapshot": source,
        "assessment": "heldout" if heldout else "validation",
        "split": {
            "warmup": str(WARMUP),
            "train": str(train_start),
            "validation": str(VALIDATION),
            "holdout": str(HOLDOUT),
            "end_exclusive": str(end),
        },
        "runs": {},
        "supervised_parameters": MODEL_SPEC,
    }
    best = {}
    common = None
    for name, config, models, warmup in options():
        if heldout and name != "baseline":
            models = tuple(m for m in models if frozen["selected_ratings"][m] == name)
            if not models:
                continue
        rows, version = rating_rows(output, matches, source, name, config, warmup, end, train_start)
        target = [row for row in rows if any(str(a) <= row[0] < str(b) for a, b in cuts)]
        cohort = tuple((r[1], r[7]) for r in target)
        if common is None:
            common = cohort
        elif common != cohort:
            raise ValueError("Rating variants did not score identical targets")
        scores = [struct.unpack("<5d", r[12]) for r in target]
        metrics = {}
        for algorithm in models:
            metrics[algorithm] = summarize(target, [p[MODELS.index(algorithm)] for p in scores])
            key = metrics[algorithm].get("all", {}).get("log_loss", math.inf)
            rank = (key, name != "baseline", name)
            if algorithm not in best or rank < best[algorithm]:
                best[algorithm] = rank
        report["runs"][name] = {
            "rating_version": version,
            "config": asdict(config),
            "warmup": str(warmup),
            "ratings": metrics,
        }
        if name == "baseline":
            for learner in SUPERVISED:
                predicted = []
                predicted_rows = []
                folds = []
                for lower, upper in cuts:
                    training = [r for r in rows if r[0] < str(lower)]
                    testing = [r for r in rows if str(lower) <= r[0] < str(upper)]
                    values = supervised(learner, training, testing)
                    if values:
                        predicted.extend(values)
                        predicted_rows.extend(testing)
                    folds.append(
                        {
                            "from": str(lower),
                            "until_exclusive": str(upper),
                            "train": len(training),
                            "scored": len(values),
                        }
                    )
                if tuple((r[1], r[7]) for r in predicted_rows) != cohort:
                    raise ValueError("Insufficient past training data to compare identical targets")
                report["runs"][name][learner] = {
                    "folds": folds,
                    "metrics": summarize(predicted_rows, predicted),
                }
    if not common:
        raise ValueError("The frozen time split contains no eligible evaluation targets")
    report["common_targets"] = len(common)
    if not heldout:
        report["selected_ratings"] = {algorithm: rank[2] for algorithm, rank in best.items()}
        baseline = report["runs"]["baseline"]
        report["leading_supervised"] = min(
            SUPERVISED, key=lambda name: baseline[name]["metrics"]["all"]["log_loss"]
        )
    return report
