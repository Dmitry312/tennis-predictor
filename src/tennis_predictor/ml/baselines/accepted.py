"""Owner-approved rating-only research baselines from E03C."""

from .optimization import design

LOGISTIC_FEATURES = (
    "diff/elo.rating",
    "logdiff/elo.count",
    "diff/fast.rating",
    "diff/surface.rating",
    "logdiff/surface.count",
    "diff/glicko.rating",
    "diff/glicko.rd",
    "diff/glicko2.rating",
    "diff/glicko2.rd",
    "diff/glicko2.volatility",
    "logdiff/gap_days",
    "diff/newcomer",
    "interaction/glicko.uncertainty",
    "interaction/glicko2.uncertainty",
)
CATBOOST_FEATURES = tuple(
    name for name in LOGISTIC_FEATURES if "/fast." not in name and "/glicko2." not in name
)
FEATURES = {"logistic": LOGISTIC_FEATURES, "catboost": CATBOOST_FEATURES}


def accepted_matrix(rows, model):
    values, names = design(rows, "interactions")
    return values[:, :, [names.index(name) for name in FEATURES[model]]]
