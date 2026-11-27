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
`data/ml/identity-review/player-review-v2/catalog/player-review.xlsx`. This workbook covers
40 match-side observations, 40 candidate groups and 40 alias rows.
The 20 matches are a smoke test. It contains no approved
mappings and gives no estimate of population coverage. The staging SQLite
file preserves exact names, match IDs, dates, tournament, source match IDs
and provenance. Other artifacts remain local under `data/`. Workbook creation
is atomic, and a repeated preparation preserves the original packet.

Send the completed packet to the owner using an approved private channel.
The coverage sheet gives dataset and parent revision fingerprints. The group
sheet prioritizes severe conflicts, then other uncertain groups. Alias rows
show one exact normalized alias per source and gender, with counts, dates
and an example match. Observation sheets contain exceptional evidence only,
including conflicting sides, suspicious overlap, transliteration groups or
changed evidence. Singleton, conflict and sample sheets provide shortcuts. Large sheets rotate after 500,000 rows.

### Owner decisions

Edit only `action`, `person_key` and `reason` in `Groups*` or
`Aliases*`, or the exceptional `Observations*` rows. Filter rows in Excel
and preserve their original order when saving. Exception decisions override
alias decisions, which override group decisions.

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
  --directory data/ml/identity-review/player-review-v2/catalog apply returned.xlsx \
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
are flagged. A verified person can occupy only one side of a match. Equal
normalized names can belong to distinct players in different gender scopes. Invalid names or unknown
gender remain quarantined. Transliteration, spelling hints, ranks and photos provide no automatic verified
assignment. Owner-reviewed groups and alias decisions can authorize their exact
aliases. A match-specific exception alone cannot authorize a rule for future matches.

Each internal UUID derives from an opaque creation occurrence and append-only
review sequence. Renaming or adding an alias does not renumber an existing
person. Changes to an existing assignment require explicit split, merge or
rejection, and old revisions remain queryable. Changed evidence requires renewed
approval. The transaction logs earlier and revised dates with previous and new
IDs for invalidating both affected histories. Unseen match sides and mismatched
evidence hashes return quarantine.

Consumers use `ReviewedMappings.mapping(match_id, side, observation_sha256=...,
revision=...)` for a manual mapping and `ReviewedMappings.resolve_match(row,
source="nbbet", revision=...)` for daily ingestion. Pass both sides
together and preserve the source reference. The evidence hash is
`fingerprint(observation_evidence(source_match_row, side))`.

On 2026-11-26 the owner confirmed that NBBet's approved names and initials
are stable across seasons. The registry interface uses the exact-alias rule
by default and records this approval in rule-resolved observations. Pass
`policy=None` for a manual-only lookup. The rule accepts
only a unique, exact normalized alias approved by a group or alias decision
in the NBBet and gender scope. It quarantines new or ambiguous aliases,
rejected or changed reviewed evidence, opposing sides and unknown
source or gender. Reviewed same-start overlaps appear in match quality and
allow alias resolution. A result carries `rule_resolved` provenance
and the pinned registry and alias evidence revisions. A manual match-side
decision carries separate `manual_review` provenance. Canonical ingestion activates this policy when given a registry.

`alias_candidates` remains a lookup of previously reviewed exact names.
`invalidations(revision)` streams affected dates and IDs.
Index automatic bindings by the returned alias source, gender and exact normalized
name. `alias_invalidations(revision)` exposes old and new keys so corrections
can invalidate downstream matches even if they were resolved without a manual event.
Consumers must pin a registry revision so later corrections cannot silently alter an
earlier output. Canonical ingestion and Match Data Store persist the
resolution type and revision through the existing PR #93 integration.

## One-button read-only export

The new **Export player registry review** workflow is manual, with no typed
inputs and no production dispatch in this change. It is restricted to `ML`
and uses the existing `player-identity-audit` environment, SSH secret names,
ED25519 host fingerprint, and server Python runtime under
`/root/Tennis_pelmen_model`. The workflow should be registered where GitHub
exposes manual workflows and the existing environment's review restrictions
must authorize the selected `ML` run. No new server, frozen replica, DSN or
credential is required. The server reads existing PostgreSQL settings locally.
The runner streams the reviewed script over SSH and stages only the projected
match identity fields. Source credentials stay on the server. The workflow
makes no production writes and does not install anything on the server.

The scan pins the highest visible UUID using the primary-key index and
uses a bounded forward keyset scan up to that UUID. It reads short validated
transactions instead of holding a single transaction over the whole history.
Inserts below a passed cursor and concurrent updates or deletions can affect
completeness or consistency. This is a live keyset scan, not a point-in-time
snapshot. Recheck coverage before treating the extract as authoritative.
The optional frozen-source CLI remains available separately.

| Bound | Value |
| --- | --- |
| Query page | At most 512 rows, exclusive lower UUID and inclusive saved upper UUID |
| DB statement / lock / idle timeout | 5 s / 500 ms / 10 s |
| Remote pacing | 100 ms between pages, one DB connection per stream |
| Internal window | 25 minutes or 20,000 pages per stream; up to four local extraction windows per dispatch |
| Recovery | Upper UUID and SQLite cursor persisted on the runner; retry opens a new stream from the committed cursor |
| Workbook import | 8 GiB expanded archive and 128 MiB shared strings safety limits |

A failed/incomplete run publishes no workbook. Checkpoints persist across
reconnections within one dispatch. A fresh GitHub-hosted run starts a fresh
scan because its runner disk is ephemeral. Completed runs publish a
one-day **player-registry-review-<run-id>** artifact with
`player-review.xlsx` and its minimal, content-checked `evidence.sqlite`
companion needed for validation and import. No SSH files, raw history,
credentials or private registry are uploaded. Before dispatch the owner
must approve both the read and the intended Artifacts audience under the
repository's sports-data policy. Retain the companion locally alongside
the returned workbook for import.

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

## Issue #96 bulk owner import and consumers

The owner approved all groups in the original evidence on 2026-11-27.
The importer validates the seal and obtains every group ID directly from
`evidence.sqlite`. Each group gets one permanent player ID. Exact aliases
are scoped by source and gender. The import checks 7,899 players (5,176 men
and 2,723 women), 8,131 aliases, 351,928 match-side observations and 175,964
matches. It verifies the mapping of every observation to its group and the
absence of ambiguous aliases or shared IDs across genders.
[Owner approval](https://github.com/Dmitry312/tennis-predictor/pull/97#issuecomment-7212660453)

The original GitHub run is 45866877459, artifact 15140813328. The importer
pins archive SHA-256 `11f62215eb8a8d4e9c3e26fefc26a17ce5248c88a8ee014e198e0a11034a41d7`
and dataset fingerprint `c01abdfa58a88bcf948283cbf8f1a24d1d3fe052556c0d203e2cd944c1a54d7c`.
Keep the original SQLite and workbook outside Git. With a saved copy, run:

```bash
uv run --locked python scripts/import_approved_registry.py \
  --evidence /path/to/evidence.sqlite --verify-repeat
uv run --locked python scripts/build_match_data.py --registry data/ml/identity-import/registry.sqlite
```

If the local evidence path is absent, the command downloads the original
artifact via the authenticated GitHub CLI. The manual **Verify full player
registry import** workflow accepts that artifact, an alternate same-repository
Actions artifact containing saved `evidence.sqlite`, or a private HTTPS URL
to the saved file configured in the `SAVED_PLAYER_EVIDENCE_URL` secret. Select
`use_saved_evidence_url` for the last option. All paths verify the same dataset
fingerprint. The workflow reads the existing copy and delivers only the compact
registry, manifest and acceptance JSON through Actions Artifacts. The report
includes coverage, repeat import, database size, import timing and an indexed
lookup benchmark for every alias.

An offline synthetic run at the approved dataset size covered 351,928
observations in 175,964 matches with 7,899 players, 8,131 aliases and zero
quarantined aliases. Import took 10.617 s, the byte-identical repeat took
8.590 s, and 8,131 scoped index lookups took 0.023 s. The SQLite registry
was 2,940,928 bytes. The original Actions artifact was unavailable from the
development environment, so its coverage and actual registry size still
require running the workflow against the saved evidence.

The registry keeps `people` and indexed `bulk_aliases` separately. For later
owner-approved corrections use an explicit `--approval` packet with `dataset`,
`parent`, `approval_ref`, offset-aware `recorded_at` and a `groups`
mapping from evidence group IDs to owner keys. Reuse a key when the same
person's aliases change. Each corrected packet replaces the active bulk alias
set. Omitting an alias retires it from the new revision and leaves the prior
revision queryable. Unchanged aliases retain their evidence revision.
Contradictions within an alias scope quarantine that alias. Both sides are
quarantined if they resolve to the same player.

Canonical and Match Data Store builds pin the revision and token. Use
`--registry-revision N` to select a historical revision. Unchanged matches can
reuse their shards after unrelated edits. Live callers project a bundle with
`aggregation.integration.live_match_input` and pass it to
`MatchDataStore.process` for incremental updates and corrections. Known
same-start overlaps remain match quality flags. Valid completed matches stay
in history. Canceled matches contribute no game statistics. Multiple matches
for the same player on one UTC date exclude training targets on that date.
Longitudinal features are computed when requested.

### S3 publication and local use

The S3 command uses the existing storage configuration and defaults to the
`ml/player-registry/v1` prefix. It uploads the SQLite snapshot and version
manifest under immutable keys, verifies SHA-256, size and registry token,
then updates `CURRENT.json` with a conditional write. Publication and rollback
require the expected current token and explicit confirmation. If the endpoint
cannot enforce conditional writes, the update fails safely.

```bash
uv run --locked python scripts/player_registry_s3.py fetch /path/to/local/registry.sqlite
uv run --locked python scripts/player_registry_s3.py fetch /path/to/local/registry.sqlite --token TOKEN
uv run --locked python scripts/player_registry_s3.py publish /path/to/registry.sqlite --expected-current root --confirm
uv run --locked python scripts/player_registry_s3.py rollback TOKEN --expected-current CURRENT_TOKEN --confirm
```

Fetch verifies the complete snapshot before atomically replacing the local
SQLite file. Pass that file to the historical or daily processor. Keep the
published cache read-only to consumers and apply owner corrections to a
separate working copy. Run publication or rollback only after owner approval.
