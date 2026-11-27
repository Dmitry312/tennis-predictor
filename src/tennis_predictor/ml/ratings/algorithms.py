"""Daily frozen-state Elo, Glicko and Glicko-2."""

import math
from dataclasses import dataclass

BASE = 1500.0
Q = math.log(10) / 400
SCALE = 1 / Q
MODELS = ("elo", "fast", "surface", "glicko", "glicko2")


@dataclass(frozen=True)
class Config:
    elo_k: float = 24
    fast_k: float = 64
    surface_k: float = 24
    shrink: float = 20
    rd: float = 350
    rd_cap: float = 350
    diffusion: float = 10
    sigma: float = 0.06
    tau: float = 0.5

    def __post_init__(self):
        for key, value in vars(self).items():
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            if not valid or not 1e-6 <= value <= 10000:
                raise ValueError(f"Rating parameter outside supported range: {key}")
        if self.rd > self.rd_cap or self.sigma > 1 or self.tau > 2:
            raise ValueError("Invalid initial uncertainty or volatility parameters")


@dataclass(frozen=True)
class State:
    rating: float = BASE
    rd: float = 0
    sigma: float = 0
    count: int = 0


def logistic(x):
    # Keep evaluation and update formulas finite even after extreme mismatches.
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, x))))


def g(phi):
    return 1 / math.sqrt(1 + 3 * (phi / math.pi) ** 2)


def probability(left, right):
    combined = math.hypot(left.rd, right.rd) / SCALE
    return logistic(g(combined) * (left.rating - right.rating) / SCALE)


def initial(model, config):
    return State(
        rd=config.rd if model.startswith("glicko") else 0,
        sigma=config.sigma if model == "glicko2" else 0,
    )


def inactive(model, state, days, config):
    if not model.startswith("glicko"):
        return state
    step = config.diffusion if model == "glicko" else SCALE * state.sigma
    rd = min(config.rd_cap, math.hypot(state.rd, step * math.sqrt(days)))
    return State(state.rating, rd, state.sigma, state.count)


def volatility(phi, sigma, variance, delta, tau):
    a = math.log(sigma**2)

    def f(x):
        ex = math.exp(x)
        denominator = phi**2 + variance + ex
        return ex * (delta**2 - denominator) / (2 * denominator**2) - (x - a) / tau**2

    lower = a
    upper = a - tau
    if delta**2 > phi**2 + variance:
        upper = math.log(delta**2 - phi**2 - variance)
    else:
        while f(upper) < 0:
            upper -= tau
    a_value, b_value = f(lower), f(upper)
    for _ in range(200):
        if abs(upper - lower) <= 1e-6:
            return math.exp(lower / 2)
        center = lower + (lower - upper) * a_value / (b_value - a_value)
        c_value = f(center)
        if c_value * b_value <= 0:
            lower, a_value = upper, b_value
        else:
            a_value /= 2
        upper, b_value = center, c_value
    raise ArithmeticError("Glicko-2 volatility failed to converge")


def update(model, state, games, config):
    """Games are (frozen opponent state, score); a whole period updates once."""
    count = state.count + len(games)
    if not model.startswith("glicko"):
        k = {"elo": config.elo_k, "fast": config.fast_k, "surface": config.surface_k}[model]
        return State(
            state.rating + k * math.fsum(s - probability(state, o) for o, s in games), count=count
        )

    phi = state.rd / SCALE
    residuals, information = [], []
    for opponent, score in games:
        weight = g(opponent.rd / SCALE)
        expected = logistic(weight * (state.rating - opponent.rating) / SCALE)
        residuals.append(weight * (score - expected))
        information.append(weight**2 * expected * (1 - expected))
    residual = math.fsum(residuals)
    variance = 1 / math.fsum(information)
    sigma = state.sigma
    if model == "glicko2":
        sigma = volatility(phi, sigma, variance, variance * residual, config.tau)
        phi = min(config.rd_cap / SCALE, math.hypot(phi, sigma))
    new_phi = 1 / math.sqrt(1 / phi**2 + 1 / variance)
    return State(state.rating + SCALE * new_phi**2 * residual, SCALE * new_phi, sigma, count)


def surface_state(surface, global_state, config):
    weight = surface.count / (surface.count + config.shrink)
    return State(
        rating=weight * surface.rating + (1 - weight) * global_state.rating,
        count=surface.count,
    )
