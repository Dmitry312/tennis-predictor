# Persistent data catalog — Issues #62, #65, #67 and #73

The owner approved public original sports data on 2026-11-25 and simplified the
default process on **2026-11-26**: no repeated per-batch content/rights attestations.
Real names, IDs, original keys/values, raw sports JSON, scores, odds and timestamps
are retained without anonymization. Automatic integrity/secret/contact checks fail
closed. Credentials and unrelated personal information remain excluded.

The owner's repository permission is not proof of a third-party source licence.
Content eligibility remains the project owner's responsibility; tooling does not
certify redistribution rights. See [publication-policy.json](publication-policy.json)
and ../real-data-review-policy.md for the standing decision.

- [one-dispatch-samples.md](one-dispatch-samples.md): default one-run acquisition,
  validation and automatic sample PR, first-20 continuation and PR #72 migration.
- [first20_acquisition.md](first20_acquisition.md): optional historical two-step
  staging/review/publication and protected delivery.
- [sample-diagnostics-70.md](sample-diagnostics-70.md): safe diagnostics and the
  verified PostgreSQL UUID-ordering fix, retained by the default workflow.
- real_samples.md: original-record export/reuse contract. Its manual publication
  procedure describes the older optional path, superseded by Issue #73 by default.
- examples.synthetic.json: fabricated structural examples only; numeric key "1"
  does not prove an NB-Bet score mapping.
- dictionary.md: writer evidence, availability, leakage and UNKNOWN semantics.
- allowlist.md/execution.md: separate legacy sanitized exporter policy.
- index.json, acquisition-plans and samples: committed catalogs, exact records and
  immutable provenance. Pending PR #72 already holds two real examples; this
  implementation neither copies nor changes those sample files.

Use the existing exporter/publisher; do not run the older traceback-printing driver.
This implementation PR does not execute production acquisition or publish new records.
