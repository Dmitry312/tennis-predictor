# Persistent sanitized data catalog — Issue #62

This directory is the reusable review home for source schemas, linked examples and
semantic decisions. It contains **synthetic examples only**, not production exports.

- examples.synthetic.json: fabricated linked examples, two batches of at most three
  matches, with straight sets, deciding tiebreak, BO5, retirement, walkover and
  legacy/current examples. Numeric set key "1" is intentionally fictional; do not
  use it as an NB-Bet score mapping. Local M/P aliases are roles, not people.
- dictionary.md: writer evidence, storage/availability/leakage and open questions.
- allowlist.md: separate fixed structural-key policy proposed for review.
- execution.md: exact-input export API, safety budgets, feasibility and blockers.
- index.json: versioned catalog entries and review state.

No real examples may be committed to this public repository before separate review.
A future production sample must first remain in a protected private location; review
the exact projected output and publication scope before adding any descriptor/example.
The exporter returns bytes and does not write files, print payloads or upload artifacts.
Sports scores can still allow indirect reidentification: this is data minimization
and local pseudonymization, not a mathematical anonymity guarantee.

Keep every future example entry linked to its dictionary version, projection policy
and review decision. Public descriptors must not contain object keys, real match/
player IDs, URLs, names, credentials, odds or full raw payloads. Unknown semantics
remain UNKNOWN. Do not derive a winner merely from a plausible score shape.

The existing manual-parquet protocol and existing production workflows are unchanged.
The older full-match export plan allowed names/raw payloads and its driver can print
a traceback; it is **not** a safe implementation of this catalog's policy.
