# Rating Engine v1

Offline experimental engine for [Issue #100](https://github.com/Dmitry312/tennis-predictor/issues/100).
Read a pinned Match Data Store snapshot and its exact canonical snapshot
for gender, level and surface. The canonical token must match the approved
registry revision. The engine consumes only verified player IDs and unique
canonical completed results with two complementary valid win facts. Missing
statistics are immaterial. Duplicate, canceled and unresolved matches are
excluded. M and F have separate pools. Tour and Challenger share each pool.
Odds and official rank are absent from the input.

Each match sees the frozen UTC day's pre-match states. The engine aggregates
all that day's valid results and commits state once per player and rating
scope. Multi-match days contribute facts to later dates. A target counts only
if each participant has exactly one match in the Match Data Store index for
the day. Historical replay assumes validated prior dates are available, even
if original decision or availability timestamps are unknown. No live or
strict-as-of rating path exists in v1. Live use requires point-in-time evidence
and activation handling before these states can serve predictions.

## Equations and defaults

All models start at 1500. Put `q = ln(10)/400`, `L(x) = 1/(1+exp(-x))`
and `g(φ) = (1+3φ²/π²)^(-1/2)`. Logistic arguments are clipped to ±35
for finite logs and updates at extreme mismatches.

- Global and Fast Elo independently forecast `L(q(r_i − r_j))`.
  Their day update is `r_i += K Σ(y_ij − p_ij)`, using K 24 and 64 respectively.
- Surface Elo maintains a separate latent rating `s_i` and count `n_i` per
  known surface. Effective rating is `e_i = (n_i s_i + λ r_i)/(n_i + λ)`
  with λ 20 and global pre-day `r_i`. Predict `L(q(e_i − e_j))` and
  update `s_i += 24 Σ(y_ij − p_ij)`, increasing the surface count only
  for matches on that surface. Unknown surface uses Global Elo for the
  surface prediction and makes no surface-state update. The independent
  non-surface models still learn the result.
- Glicko starts with RD 350, capped at 350. Between active periods
  separated by `d` calendar days its pre-period RD is
  `min(cap, sqrt(RD² + c² d))`. Default `c=10` rating points per
  square root of a UTC day is an experimental setting. The directional
  expectation is `E_j=L(g(q RD_j)q(r_i−r_j))`.
  On scale `μ=q(r−1500), φ=q RD`, let
  `v⁻¹=Σg(q RD_j)² E_j(1−E_j)` and `S=Σg(q RD_j)(y_j−E_j)`.
  Then `φ'=(φ⁻²+v⁻¹)^(-1/2)` and `μ'=μ+φ'²S`.
- Glicko-2 uses the same scaled expectation and information. Initial
  volatility σ is .06 and τ is .5. At gap `d`, account for `d−1` empty
  periods by adding `(d−1)σ²` to scaled RD variance up to the RD cap.
  Solve the published volatility equation for σ′ using `Δ=vS`.
  Add σ′² for the active period with the same cap, then apply the
  Glicko-2 period update using that inflated φ. Cold starts use the
  configured RD and receive no pre-debut inflation.
- The reported Glicko/Glicko-2 prediction is symmetric:
  `L(g(q hypot(RD_i,RD_j))q(r_i−r_j))`. Reversing sides gives its
  complement. The original directional expectations remain in the updates.

Reference examples use the original single-period equations.
[Glicko](https://www.glicko.net/glicko/glicko.pdf) · [Glicko-2](https://www.glicko.net/glicko/glicko2.pdf)

## Commands and storage

```bash
uv run --locked python scripts/run_ratings.py \
  --match-data data/ml/match-data/historical_research_replay \
  --canonical data/ml/canonical/historical_research_replay \
  --registry data/ml/identity-import/registry.sqlite --registry-revision 1 \
  --output data/ml/ratings
```

Pass `--match-data-version` to reproduce an older input. A JSON `--config`
can override the parameters for a standalone run. Commands are local and
read the projected result facts once per invocation. For comparison add
`--compare --splits 2016-01-01 2018-01-01 2022-01-01 2024-01-01 2026-01-01`
and save stdout as a validation report. Dates are illustrative start
boundaries for warmup, train, validation, test and end. Then use
`--held-out path/to/frozen-validation.json` with the same splits and inputs.
Only validation-log-loss selections advance. Ties favor the baseline.
See [validation protocol](validation.md).

A snapshot binds algorithm, code, config, source revisions, registry token
and ordered daily inputs. Its manifest carries the SQLite checksum.
SQLite retains compact binary daily pre/post state and per-match pre ratings
for both competitors, RD, volatility, counts, prior gap, probability and
residual, with per-match source revision and snapshot provenance.
Unchanged daily prefixes are checkpoints. Append copies the checkpoint file
and computes only the added days. Correction or removal invalidates the
suffix from the first changed day and replays all players thereafter.
A version is immutable and `CURRENT` is replaced atomically under a lock.
A full build and incremental replay give the same logical version and rows.
SQLite layout and its physical checksum can depend on the build path.
Old snapshots require local storage retention planning. Reading and copying
the prefix still costs I/O, and a comparison stores a snapshot per variant.

Run `uv run --locked python scripts/benchmark_ratings.py` for the generated
176,000-match replay and 100-match append benchmark. The checked-in real
catalog has 20 matches and is a smoke test. No full-history grading or
ratings deployment has been run.

Measured on a local synthetic result stream of 176,000 matches across 1,760
days. Full replay took 27.171 s. Appending the final 100 matches took 2.361 s
and reused 1,759 days. SQLite size was 309,473,280 bytes. The append and full
runs produced the same logical version. The timing excludes Match Data Store
projection and measures neither full-history performance nor predictive quality.
