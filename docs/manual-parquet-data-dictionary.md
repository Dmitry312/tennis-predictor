# Manual Parquet data dictionary — review protocol

Goal: use a tiny, read-only, anonymized diagnostic export to document actual stored fields, not to infer ML labels prematurely.

Sources: `pbp/`, `nbbet_stats/`, `nbbet_details/`, and PostgreSQL `matches.surface`. The existing production workflow bounds S3 requests, object size, row count, time and output; it samples only a partial set of schemas.

For `sets_json`, export an anonymous nested shape with local positional labels K1..K8, primitive types, small integer scores (including numeric strings), booleans and nulls. Never export raw keys, arbitrary strings, player names, identifiers, odds, or raw JSON. K-labels are **not semantic field names**; a separate controlled inspection is needed to map them to actual source keys. Shape is limited to depth 3, 8 entries per object/list, 5 sets and 4096 input characters. If output exceeds 24 KiB, the sampler drops examples.

Manual dictionary columns: source, schema fingerprint, field or anonymous path, storage type, nullable, example, proposed meaning, evidence, confidence, validation rule, training availability and leakage risk. Record unknown meanings as UNKNOWN.

Validation gates: compare completed vs retired/walkover examples, verify player1/player2 score orientation, distinguish set points from match result, confirm status enum from `status.py`, verify `game_complete` semantics and never use final outcome as a pre-match feature.

No production execution or merge is part of this PR. First inspect CI, then authorize one read-only workflow run after merge.
