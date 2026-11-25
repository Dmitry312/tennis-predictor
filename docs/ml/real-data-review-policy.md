# Real data review policy

Owner decision, 2026-11-25: use actual database and stored source records by default for ML research, schema interpretation and design. Preserve ordinary sports names, IDs, scores, original values, keys, types and links without anonymization. Synthetic fixtures are for tests and edge cases, never evidence of production semantics.

Owner explicitly approved public storage in tennis-predictor, branch ML, under docs/ml/data_catalog/samples. Follow the catalog README and real_samples.md for content and redistribution-rights review. Never publish credentials, tokens, private keys, connection secrets or unrelated personal data. Production access remains bounded and read-only, with separate approval for execution. Record source/schema provenance and distinguish observed facts from interpretations.
