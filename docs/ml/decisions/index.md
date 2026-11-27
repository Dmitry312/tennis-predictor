# Model decisions

Record the evidence, candidate alternatives and rationale once a configuration
is accepted or rejected. Keep linked experiment results, including negative
outcomes. Update [the accepted model](../model.md) only after a decision.

| ID | Status | Planned comparison |
| --- | --- | --- |
| [D001](D001.json) | planned | Dataset Builder training window of all eligible history, 7, 5, 3, 2 or 1 year. Rating warmup stays fixed. |
| [D002](D002.json) | planned | Dataset Builder sample weighting, uniform versus recency half-life of 0.5, 1, 2, 3 or 5 years. |

D001 and D002 have no measurements or accepted outcomes. Use rolling-origin
validation and an untouched final holdout. Neither experiment trains CatBoost
within Issue #100.
