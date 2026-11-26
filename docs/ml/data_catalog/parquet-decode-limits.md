# Bounded Parquet coverage

Incident: production run 45816054258 at ML b04628be356b5d2b9943ee51b16eed86e3c7cd9f failed at offset 12, batch 5 after successful PostgreSQL and S3 access. The historical decode_limit combines six checks; the exact violated check cannot be recovered from that log. This is not evidence of corruption.

The decoder emits only fixed diagnostic reasons: metadata_row_limit, metadata_columns, row_groups, total_rows, uncompressed_bytes, batch_bytes. No object keys, identifiers, raw rows or exception text enter diagnostics.

After the existing conditional HEAD/GET and integrity checks, overly wide (>128 columns) or fragmented (>128 groups) files are quarantined without walking their metadata. Otherwise groups are visited in file order and decoded only if the whole group's declared rows and uncompressed bytes fit the unchanged file/shared budgets. No predicate pruning is claimed. Every chosen group reserves rows before iteration, without refunds; actual batch bytes are also bounded before conversion to Python rows. An oversized actual batch discards its whole group and stops that object. Remaining sources and matches continue. No additional S3 calls or retries are introduced.

Catalog versions retain key, requested version, ETag, content hash, schema and bounded metadata, with decode_reasons, row_groups_read, row_groups_skipped and object_scan_complete. Coverage is decode_limit (none read) or partial_row_groups. Neither means missing, zero source rows, or full history. matching_rows_observed and rows_truncated concern inspected groups only; unread groups have unknown matching counts. Source reports carry the same coverage details. Existing coverage_complete remains false.

Integrity failures, duplicate columns, invalid Parquet, security checks, deadlines and transport/read/output budgets remain fatal. They are not converted into successful skips. Unclassified decoder exceptions still fail closed; safely continuing them requires a separately validated representation.

One operation still completes the persisted plan from its committed prefix (12→20 as 5+3), validates all archives, and creates one final PR. Explicit partial source coverage is a valid result; failed integrity is not. No existing 12 sample bundles are rewritten. Existing cleanup, duplicate and stale-ML guards remain.

Resource limits and read-only PostgreSQL/S3 permissions are unchanged. Group statistics are not trusted as sufficient protection: actual batch bytes, process address space and deadlines still apply. A single malicious allocation can hit the existing process cap before a batch can be inspected; that remains a fatal safe stop.

Deployment: no workflow YAML changes are needed for this fix. After reviewed merge to ML the pinned payload includes the updated exporter/diagnostics. If main still has an older operation workflow, synchronize the previously reviewed workflow through normal review; this PR does not modify main, Environment, permissions or production data.
