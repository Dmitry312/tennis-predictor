# Real data review and publication policy

Owner decisions, 2026-11-25 and 2026-11-26: use actual database and stored source
records by default for ML research, schema interpretation and design. Preserve
ordinary sports names, player/match IDs, scores, odds, original JSON, keys, values,
types, timestamps and links without anonymization. Synthetic fixtures are tests
and edge cases, never production semantic evidence.

The owner permits public ordinary sports records in tennis-predictor on ML under
docs/ml/data_catalog/samples. The 2026-11-26 decision removes repetitive per-batch
content_reviewed/content_evidence/rights_reviewed/rights_evidence attestations from
the default acquisition/publication process. No equivalent acknowledgement or
per-batch content/rights approval replaces them. Standing machine-readable policy:
docs/ml/data_catalog/publication-policy.json.

Automatic integrity and secret/contact checks must fail closed before any GitHub
content upload. Passwords, tokens, private keys, access credentials, connection
secrets and unrelated personal/contact data remain excluded. Original sports
identities and values are retained. Automatic detection is bounded defense in depth,
not a mathematical or legal guarantee about arbitrary free text.

Repository-owner permission does NOT establish a third-party source's licence or
redistribution rights. The project owner assumes responsibility for deciding which
content is eligible for public redistribution. The tool records that responsibility
and does not invent a legal certification or claim a human reviewed each batch.

The default is one manual workflow dispatch for one bounded batch, automatic JSON
validation and a dedicated PR into ML. There is no auto-merge. Existing protected
Environment, reviewed-code SHA, pinned SSH, read-only source access, load budgets,
timeouts and diagnostics remain. No new source permissions, users, certificates or
secrets are required. Existing two-step protected delivery remains optional, not a
default publication prerequisite.

Source/schema provenance, checksums, versions and missingness stay explicit.
Unknown semantics remain UNKNOWN. Production execution remains a separately chosen
manual action; implementing a workflow does not authorize Codex to dispatch it.
