# Rating experiments

| ID | Candidate | Historical status |
| --- | --- | --- |
| [R001](R001.md) | Global Elo | planned |
| [R002](R002.md) | Fast Elo | planned |
| [R003](R003.md) | Surface Elo | planned |
| [R004](R004.md) | Glicko | planned |
| [R005](R005.md) | Glicko-2 | planned |

Keep the stable ID and its JSON manifest when moving to `run`, `accepted` or
`rejected`. Add actual code, data and registry versions, chronological split
boundaries, metrics, segment counts, artifact pointers, limitations and a
decision rationale. Preserve prior and negative outcomes. Keep large outputs
and credentials outside Git. Future ML work maintains these records and the
[decision log](../decisions/index.md).

## Planned Dataset Builder experiments

| ID | Candidate experiment | Status |
| --- | --- | --- |
| [D001](D001.json) | Compare training windows of all eligible history, 7, 5, 3, 2 or 1 year with fixed rating warmup. | planned |
| [D002](D002.json) | Compare uniform weighting and recency half-lives of 0.5, 1, 2, 3 or 5 years. | planned |

These plans have no measurements or accepted outcomes.

## Historical result baselines

| ID | Experiment | Full-history status |
| --- | --- | --- |
| [E00](E00.json) | Default Global Elo | planned |
| [E01](E01.json) | Five rating systems, bounded one-factor variants and warmup comparison | planned |
| [E02](E02.json) | Logistic Regression | planned |
| [E03](E03.json) | CatBoost | planned |

These manifests preserve the completed real-catalog smoke and its limits.
The [historical baseline protocol](../historical-baselines.md) governs full
validation and the later holdout. No model or rating variant was accepted.

- [E03A Logistic Regression optimization](E03A.md)
- [E03B CatBoost optimization](E03B.md)
