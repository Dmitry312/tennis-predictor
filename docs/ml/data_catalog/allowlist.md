# Separate linked-review projection allowlist v1

This policy applies only to infra/audit/linked_record_review.py. It does not expand
or replace production_parquet_row_review.py or the existing anonymous K-label
protocol. Code is proposed for review, not permission to execute against production.

## Allowed structural keys
Only keys "1".."8" may be emitted **inside sets_json / raw.match["24"] structures**.
These are candidate structural paths for human interpretation, not established game/
point/duration meanings. Preserve input order, null, bool, int/float and short numeric
string types. Scalar numeric values are limited to 0..99; strings must be canonical
0..99 numeric strings. Other strings/large numbers/types become fixed redaction
markers. Unknown keys are counted, never echoed. At most five sets, depth three,
eight entries/container, 256 nodes, 4096 JSON bytes.

This narrow numeric-key exposure is separate from the previous raw-key prohibition.
It requires review before use. A key/value may still have unforeseen meaning; do
not widen to arbitrary numeric keys or small values under unrelated payloads.

## Field projection
matches: gender M/F, level tour/challenger, status 1..10/null, surface enum/null.
Join IDs, duplicate_of and pbp_s3_path remain private; no player names are queried.
details: status, has_coverage, details_version, sanitized sets_json and role-only
players_json. Name/photo/flag/ranking/profile values never leave a profile object.
stats: scope and the 28 documented numeric STAT_FIELDS side fields, bounded finite
numbers preserving integer/float/null. Never combine whole-match and set scopes.
pbp: set/game/point indices, side server/winner, source score strings, documented
booleans, four fixed point_type enums; unknown strings are redacted. No match winner
is inferred from point_winner. Preserve row ordering and expose omitted-row counts.
raw: projected match["23"] status, match["24"] sets and match["3"]/["5"] alias-only
profiles. No other raw branch is serialized; outside-match keys only contribute a
count. raw_json null remains null; oversized raw JSON is explicitly omitted.
Odds, arbitrary strings/keys, names, IDs, links, timestamps and infrastructure data
are never projected. Storage schemas include only allowlisted fields and fixed
primitive type categories, plus explicit absent-column lists.

## Link and winner evidence
Match alias is local M1..M3. Player aliases M1P1/M1P2 are local match roles, not
cross-match canonical identities. Cross-source P1/P2 aliases are populated only
from an explicit reviewed same/swapped orientation attestation with a private
reference. Otherwise source orientation/aliases are unknown; score order is not
silently inverted. Attestations are caller claims, not proven by this exporter.

No independent winner is available in Match ORM. Default evidence is unavailable.
An optional external reviewed independent_reference may supply canonical side 1/2;
only the resulting local alias and attestation label are output, never its private
reference. verified_by_exporter remains false. Score-derived or unreviewed claims
are rejected; no winner is fabricated or derived by this tool.
