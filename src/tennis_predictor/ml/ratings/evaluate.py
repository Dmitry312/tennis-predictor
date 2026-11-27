"""Chronological, offline probability evaluation on a common target cohort."""

import math
import struct
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from pathlib import Path

from tennis_predictor.ml.canonical_layer.storage import checksum

from .algorithms import MODELS, Config

CODE = checksum(Path(__file__))
FACTORS = {
    "elo_k": ("elo", (16, 24, 32)),
    "fast_k": ("fast", (48, 64, 96)),
    "shrink": ("surface", (10, 20, 40)),
    "tau": ("glicko2", (0.3, 0.5, 1.0)),
    "rd": ("glicko", (200, 350)),
}


def variants():
    baseline = Config()
    yield "baseline", baseline, MODELS
    for field, (model, values) in FACTORS.items():
        for value in values:
            if value != getattr(baseline, field):
                models = ("glicko", "glicko2") if field == "rd" else (model,)
                yield f"{field}-{value}", replace(baseline, **{field: value}), models


@dataclass(frozen=True)
class Splits:
    warmup: date
    train: date
    validation: date
    test: date
    end: date

    def __post_init__(self):
        dates = (self.warmup, self.train, self.validation, self.test, self.end)
        if any(a >= b for a, b in pairwise(dates)):
            raise ValueError("Split boundaries must be strictly increasing")

    def part(self, day):
        if day < self.warmup or day >= self.end:
            return None
        if day < self.train:
            return "warmup"
        if day < self.validation:
            return "train"
        if day < self.test:
            return "validation"
        return "test"


class Metrics:
    def __init__(self):
        self.count = 0
        self.candidates = 0
        self.log = self.brier = self.correct = 0.0
        self.bins = [[0, 0.0, 0] for _ in range(10)]

    def add(self, p, y, eligible):
        self.candidates += 1
        if not eligible:
            return
        self.count += 1
        self.log -= math.log(p if y else 1 - p)
        self.brier += (p - y) ** 2
        self.correct += 0.5 if p == 0.5 else int((p > 0.5) == bool(y))
        bucket = self.bins[min(9, int(p * 10))]
        bucket[0] += 1
        bucket[1] += p
        bucket[2] += y

    def report(self):
        n = self.count
        return {
            "n": n,
            "candidates": self.candidates,
            "coverage": n / self.candidates,
            "log_loss": self.log / n if n else None,
            "brier": self.brier / n if n else None,
            "accuracy": self.correct / n if n else None,
            "calibration": [
                {"bin": i, "n": k, "mean_probability": p / k, "win_rate": y / k}
                for i, (k, p, y) in enumerate(self.bins)
                if k
            ],
        }


def evaluate(db, splits, *, models=MODELS, held_out=False):
    totals = defaultdict(Metrics)
    rows = db.execute(
        "SELECT day,pool,level,surface,score,target,probabilities,left_idle,right_idle "
        "FROM predictions ORDER BY day,match_id"
    )
    for day, gender, level, surface, score, target, values, left, right in rows:
        phase = splits.part(date.fromisoformat(day))
        if phase is None or phase == "warmup" or (phase == "test") != held_out:
            continue
        segments = (
            "all",
            f"gender/{gender}",
            f"level/{level}",
            f"surface/{surface or 'unknown'}",
            f"newcomer/{'yes' if min(left, right) < 0 else 'no'}",
            f"inactivity/{'long' if max(left, right) >= 30 else 'recent' if min(left, right) >= 0 else 'unknown'}",
        )
        for model, value in zip(MODELS, struct.unpack("<5d", values), strict=True):
            if model in models:
                for segment in segments:
                    totals[phase, model, segment].add(value, score, target)
    report = {}
    for (phase, model, segment), metric in sorted(totals.items()):
        report.setdefault(phase, {}).setdefault(model, {})[segment] = metric.report()
    return report
