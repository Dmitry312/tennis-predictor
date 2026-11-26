# Cross-match player registry and owner review

The [Issue #92 workflow](https://github.com/Dmitry312/tennis-predictor/issues/92)
stages both match sides, proposes identity groups, exports an Excel workbook,
and publishes only explicitly reviewed mappings. The aggregation layer can
continue storing match-local facts while registry review is pending.

## Offline preparation and delivery

```bash
uv run --locked --group review python scripts/prepare_player_review.py
```

The default reads the repository's 20-match sample and writes
`data/ml/identity-review/catalog/player-review.xlsx`. This workbook covers
40 match-side observations and 40 candidate groups. It contains no approved
mappings and gives no estimate of population coverage. The staging SQLite
file preserves exact names, match IDs, dates, tournament, source match IDs
and provenance. Other artifacts remain local under `data/`. Workbook creation
is atomic, and a repeated preparation preserves the original packet.

Send the completed packet to the owner using an approved private channel.
The coverage sheet gives dataset and parent revision fingerprints. The group
sheet prioritizes severe conflicts, then other uncertain groups. All sides
appear in the observation sheets. Singleton, conflict and sample sheets
provide shortcuts. Large sheets rotate after 500,000 rows.

### Owner decisions

Edit only `action`, `person_key` and `reason` in `Groups*` or
`Observations*`. Filter rows in Excel as needed and preserve original order
when saving. Decisions on observations override group decisions.

| Action | Usage |
| --- | --- |
| `approve` | Assign an explicitly confirmed observation or group to an opaque key such as `P0001`. |
| `merge` | Explicitly assign observations or groups to the same person key with evidence. |
| `split` | On observation rows, assign distinct keys to identified homonyms or separate people. |
| `reject` | Quarantine the selected observations without a person key. |
| blank | Keep the observation unresolved, or preserve a prior binding if its evidence is unchanged. |

A reason is mandatory for each decision. Escalate ambiguous source evidence
rather than inferring a person. Imported rows are checked against staged
values and fingerprints. Formula and error cells are rejected. Strings that
look like formulas are written as literal Excel text. XML-incompatible
source characters are displayed escaped, with exact values retained in staging.

A designated reviewer applies the returned workbook explicitly:

```bash
uv run --locked --group review python scripts/apply_player_review.py \
  --directory data/ml/identity-review/catalog apply returned.xlsx \
  --reviewer owner --recorded-at '<actual ISO-8601 decision time with offset>'
```

The actor and timestamp record the offline review. They are supplied by the
authorized operator, and the workbook hash identifies content rather than
authenticating that operator. The same decisions for the same parent replay
idempotently. A different stale packet requires export against the current
registry. Use the `packet` subcommand instead of `apply` to export a fresh
workbook. For a new frozen source, retain the prior registry alongside the
new extract. Pass its path with `--registry` when preparing and importing
the new packet, or use `create_workbook(stage, registry, destination)`
and `import_review(stage, registry, workbook, ...)`.

## Matching and consumer contract

Groups use the existing Unicode NFC, lower-case and whitespace normalization.
A deliberately lossy transliteration and accent-folding key proposes Cyrillic
and Latin variants for review only. Every repeated key is a potential
homonym. Equal UTC starts across different matches are suspicious, and a
same-day recurrence alone is allowed. Opposing observations within one match
and conflicting gender context are flagged. One verified person cannot occupy
both sides or cross conflicting gender contexts. Invalid names or unknown
gender remain quarantined. Names, spelling hints, ranks and photos provide
no automatic verified assignment.

Each internal UUID derives from an opaque creation occurrence and append-only
review sequence. Renaming or adding an alias does not renumber an existing
person. Changes to an existing assignment require explicit split, merge or
rejection, and old revisions remain queryable. Changed evidence requires renewed
approval. The transaction logs earlier and revised dates with previous and new
IDs for invalidating both affected histories. Unseen match sides and mismatched
evidence hashes return quarantine.

Consumers use `ReviewedMappings.mapping(match_id, side, observation_sha256=...,
revision=...)`. The evidence hash is
`fingerprint(observation_evidence(source_match_row, side))`, with the same source
reference as the extraction. A verified record contains its player ID,
journal revision, token and actual decision time. `alias_candidates(gender, name)`
returns possible **previously reviewed** IDs for future observation review,
including multiple candidates; it cannot verify a new match-side mapping.
`invalidations(revision)` streams affected match-side dates and IDs. This forms the
interface for later canonical and Match Data Store integration, with the
aggregation and canonical implementations kept unchanged in this branch.

## Future protected read-only extraction

The SQL is implemented and tested with a stub of the read-only transport. No
production query, deployment, database migration or full-history export was
performed. The existing `matches` table holds source **match** IDs and no
documented source player ID. The extraction uses that minimal projection;
it makes no per-player queries and downloads no raw history, photos or S3 files.

A future owner-approved button should configure a restricted read-only account
and an immutable database snapshot or frozen copy. Pin the snapshot identifier
and persistent local staging directory before dispatch. The job sets
`IDENTITY_SOURCE=postgres`, `IDENTITY_FROZEN_SOURCE=yes`,
`IDENTITY_SOURCE_SNAPSHOT` and the protected `IDENTITY_PG_DSN`, then invokes the
same preparation command. Users need no typed parameters. A resumed run must
use the same frozen source; a label alone cannot freeze a mutable primary.
The transport checks the unfiltered `matches_pkey` plan before each page
and filters Tour/Challenger and the existing slash doubles marker locally.
Rows with missing names or uncertain gender remain review evidence.

| Bound | Value |
| --- | --- |
| Query page | 512 rows, exclusive UUID keyset cursor, one short read-only transaction per page |
| Statement / lock / idle timeout | 5 s / 500 ms / 10 s |
| Delay between pages | 100 ms in the configured PostgreSQL CLI path |
| Local invocation | 25-minute extraction window or 20,000 pages, whichever occurs first |
| Staging checkpoint | Match UUID, row counters and page committed together; failed pages are replayable |
| Workbook import | 8 GiB expanded archive and 128 MiB shared strings safety limits |

An incomplete run exits with a checkpoint and no workbook. The future protected
runner resumes the same logical operation from durable staging within its
approved total budget. Provision and measure disk space for the frozen source,
SQLite evidence, disk sorts and workbook spill files during a separate pilot.
At completion, validate scanned/filtered counts against the frozen source's
approved inventory before asserting full coverage. Keep all review files out of
release artifacts and deliver the workbook only to the owner. The production
dispatch and its credentials remain a separate approval.

## Questions for the owner

1. Is there an immutable NBBet player ID? Which field and namespace identify it?
2. How stable are display names, initials and transliterations across seasons,
   tours and source revisions? Which verified variants are first-class aliases?
3. Are photo paths replaceable media references, and can any of their segments
   be linked to a player with an explicit source contract?
4. What is the nested boolean, and what are the time semantics of ranks?
5. Which source evidence distinguishes singles, homonyms and gender-context
   anomalies? Can it be extracted without scanning raw match history?

Until confirmed, these fields are supporting context or absent. They do not
establish identity.
