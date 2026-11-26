# Potential features (no training)

Each row is a proposal conditional on confirmed input contracts. Current20 examples establish shapes/limitations, not sufficient training history. All features are paired/differenced P1/P2 after identity and orientation resolution. All windows end strictly before prediction_cutoff; persist state/version/coverage and sample size. Odds are excluded entirely.

| Priority | Category / feature | Required real data | Formula / principle | Coverage and leakage constraints |
|---|---|---|---|---|
| P0 | Elo / player strength | resolved players + past eligible results | E=1/(1+10^((Ropp-R)/400)); R+=K*(y-E) | fixed initialization/K, no future fitted state |
| P0 | Surface Elo | past results + surface | separate ratings per surface; shrink to overall Elo | missing surface explicit; no future partition knowledge |
| P0 | Current form | past results | rolling/exponentially decayed win rate, N and time window | exclude same-day ambiguous order; not source status as input |
| P0 | Previous opponent strength | prior as-of opponent Elo | average previous opponents' pre-match Elo | never use current/future opponent final-season rating |
| P1 | Rank gap / log rank | timestamped rank snapshots | log(rank_p2)-log(rank_p1) | currently no proven publication dates; gated |
| P1 | Serve quality | past serve won/all counts | sum(first_won+second_won)/sum(first_all+second_all) | no average percentages; count semantics and coverage gates |
| P1 | Return quality | prior opponent serve counts | 1-opponent serve points won/played | requires opponent orientation, compatible denominators |
| P1 | BP conversion / saving | BP won/all for both sides | sum(converted)/sum(chances); saved=1-opponent converted/chances | NULL BP fields not0; pressure timing not needed for source counts |
| P1 | Ace and DF rates | prior aces/DF + validated denominator | sums per service point or game with unit explicit | fault denominator unknown; compare like coverage only |
| P1 | Tiebreak results | validated set scores/TB and result rules | historical TB win rate with N/shrinkage | do not infer TB points from winner-only sequence |
| P1 | Stability | past per-match rates/results | rolling variance/std of compatible rates | small sample and selection bias; no future windows |
| P1 | Physical load / recovery | past starts/ends/durations | days since last eligible event, prior7/14day games/minutes | end/duration/availability unknown; overlap handling explicit |
| P1 | Result dynamics | past eligible state | EWMA win/strength trends and surface transitions | time decay computed at cutoff |
| P1 | Head-to-head | resolved repeated opponents | prior meetings W/L and surface subsets with shrinkage | identity unresolved in current20; avoid sparse overfit |
| P1 | Tournament context | edition/round/surface/time known at cutoff | categorical level/round, schedule context | no unique ID/edition leakage; unknown availability blocked |
| P2 | Pressure performance | complete validated PBP + prepoint pressure state | BP/setball/matchball opportunities and success counts | source flags timing unknown; no setball field; terminal gaps |
| P2 | Point/game runs | complete PBP or validated source maxima | max/quantile runs, deuce frequency | maxima nonadditive, missing terminal points bias |
| P2 | Serve/return TB patterns | complete side scores + actual TB server | rates by point/serve rotation | current TB scores/server NULL: unavailable |
| P3 | Style | validated serve/return/shot mix across history | latent style clustering on past-only standardized rates | no shot/serve-type evidence, forbid invented labels |
| P3 | Weather/venue sensitivity | timestamped forecast/venue observations | past performance by conditions | current snapshots retrospective, no availability proof |

P0 starts with result-based strength after identity/result/time gates, not with all observed columns. P1 requires canonical counts and as-of rankings; P2 is blocked on completeness validation, P3 explicitly experimental. Fit priors, scalers and feature selection only on training history. Do not include target, current score/statistics/PBP/status, odds, media/IDs, ingestion/capture artifacts or coverage flags as strength features. No models or feature-store service are created here.
