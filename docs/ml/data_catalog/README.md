# Persistent data catalog — Issues #62 and #65

The owner approved public storage of real sports samples on **2026-11-25** in
Issue #65. Ordinary sports names, IDs, original keys, scores, odds, timestamps
and raw sports payloads may be retained without anonymization. This supersedes
Issue #62's blanket ban on publishing those sports fields. Credentials, access
tokens, private keys, connection secrets and unrelated personal information
remain excluded. Review redistribution rights and actual content before committing.

The catalog currently contains synthetic examples only. Approval is not evidence
that production acquisition or content/rights review has happened.

- [real_samples.md](real_samples.md): new original-record export, selection,
  versions, provenance, review and offline reuse.
- [samples/README.md](samples/README.md): actual bundle layout and availability.
- examples.synthetic.json: fabricated structural examples from Issue #62.
  Numeric set key "1" is fictional and proves no NB-Bet score mapping.
- dictionary.md: writer evidence, availability, leakage and unresolved semantics.
- allowlist.md and execution.md: the separate legacy sanitized projection policy
  and API. Their restrictions apply to that exporter, not the newly approved
  original-sports-record tool.
- index.json: separate synthetic entries and real_examples with checksums and
  review status. Empty real_examples means no real records stored.

scripts/export_match_samples.py reuses safe JSON and part-layout helpers from
infra/audit/full_match_export.py, not the older production driver that can print
tracebacks. Existing sanitized exporters and production workflows remain unchanged.
Do not infer a winner or numeric score-key meaning from synthetic fixtures.
