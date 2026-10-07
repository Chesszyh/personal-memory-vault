# Canonical archive schema (implemented v2)

This document describes the schema currently shipped in
`src/personal_vault/schema.sql`. The filename is retained as a historical link;
the database initializer sets `PRAGMA user_version = 2`. There is no migration
path yet: an existing non-empty version-0 database, or a database with any
version other than 2, is rejected instead of being changed in place.

The implemented importer handles verified ChatGPT official account exports.
The local reader, bounded HTML/PDF export, deterministic reports, review-gated
memory store, and provider migration packages are implemented as derived layers
outside this canonical schema. IndexedDB recovery remains deferred.

## Implemented layers

```text
immutable Evidence directory + v2 Evidence Manifest
→ source-preserving conversations.ndjson and source-records.ndjson
→ SQLite identities, versions, observations, relationships, and FTS5
→ reader, separate annotations, and rebuildable report/export/memory views
```

The Evidence directory remains external and immutable. SQLite stores the
verified tree hash, manifest hash, decoded-artifact hashes, and paths needed to
trace an import, but it is not a replacement for the original files.

## Evidence, source records, and file coverage

`sources`, `snapshots`, and `import_runs` record the provider, identity scope,
capture metadata, importer/schema/config versions, and import counters. The
Evidence Manifest is verified before import. When the provider's
`export_manifest.json` is present, its physical and logical inventories are
also checked against the Evidence Manifest.

The two decoded artifacts have different purposes:

- `conversations.ndjson` contains one full raw conversation envelope per
  conversation shard element.
- `source-records.ndjson` contains one `file_root` envelope for every JSON file
  in the Evidence Manifest and, for a list root, one `element` envelope per
  list item.

Each source record retains its provider kind, exact evidence-relative file
path, JSON pointer, optional array index/native ID, canonical raw JSON, and
SHA-256. SQLite integer IDs are internal implementation IDs; the current code
does not assign deterministic external UIDs to source records. The NDJSON
envelope's own `schema_version` is 1 and is independent of SQLite
`user_version` 2.

`source_file_coverage` accounts for every manifest file by path, size, and
hash. JSON files are marked `decoded`, `.dat` files are `cas_verified`, and
HTML or other formats are `preserved_opaque`. Preserving an opaque file is an
explicit handling outcome, not a claim that its internal records were parsed.

Reusing the same Source Snapshot ID with different evidence or capture metadata
is a hard failure. Re-importing the same snapshot/profile takes a no-op path
that verifies published NDJSON, database state, FTS integrity, and referenced
CAS objects before returning `no_op`.

## Identities, versions, and observations

Stable identity and snapshot-specific state are separate:

- conversation and group-thread identities are scoped by a `source_id`, whose
  key includes the caller-supplied account identity scope;
- node and ordinary-message identities are scoped by conversation;
- group-message identities are scoped by group thread;
- versions are keyed by identity plus the SHA-256 of the complete raw object;
- observations connect one version to one Snapshot and its source record.

Identity keys are readable scoped strings such as
`openai/chatgpt-official/<identity-scope>/conversations/<native-id>`; they are
not full-digest UIDs.
Conversation, node, message, group-thread, and group-message versions retain
their complete canonical raw JSON plus currently unknown top-level fields.
Selected scalar fields are projected for queries. There is no normalized
content-part table, normalized timestamp model, or identity-match-candidate
table in v2.

## Conversation graphs and group chats

For ordinary conversations, every `mapping` node and its parent pointer is
retained. Import requires node/message IDs to agree with their mapping key,
parents and `current_node` to resolve, and the parent graph to be acyclic.
Declared `children` are preserved in raw JSON; disagreement with parent
pointers is a warning rather than a rewrite.

`current_branch_nodes` materializes only the root-to-`current_node` path. All
alternative nodes and messages remain in their identity/version/observation
tables, but v2 does not materialize every alternative leaf path or a branch ID.
Multiple roots and a selected node with descendants are retained and reported
as warnings.

`group_chats.json` is projected into separate group-thread, group-message, and
group-attachment tables. It remains distinct from the ordinary conversation
DAG. Other auxiliary JSON families remain available through generic source
records even when they do not yet have normalized projections.

## Assets and references

Every physical `.dat` file becomes an `asset_observation` pointing to a shared
SHA-256 content-addressed object under `assets/sha256/<prefix>/<digest>`.
Physical evidence paths remain in their Snapshot; the CAS is a verified derived
copy.

Ordinary-message, group-message, and library references are stored separately
with their original value, normalized basename where applicable, candidate
list, resolution method, and `resolved`, `unresolved`, `ambiguous`, or (for a
group URL) `external` status. Unresolved soft references create diagnostics but
do not abort import.

The current schema does not sniff MIME types, create thumbnails, normalize
friendly names into a separate alias table, or parse opaque `.dat` contents.
`declared_name` is metadata only.

## Search

`search_documents` and the external-content `message_fts` table index extracted
text from ordinary message `content.text`/textual `parts`, plus the `text` field
of group messages. FTS5 uses the trigram tokenizer and retains unindexed
document kind, version ID, identity key, role, and content type alongside the
searchable text.

The FTS row does not directly contain Snapshot, Source, branch class, or
timestamp. The reader joins through observations to apply Snapshot scope. It
uses escaped literal `LIKE` for one- or two-character queries and trigram FTS5
for longer terms.

## Absence and integrity state

`source_absences` is recomputed in chronological Snapshot order for
conversations, nodes, messages, group threads, group messages, and content
assets. For each Snapshot it records identities seen in an earlier Snapshot of
the same Source but absent in this one, together with the most recent prior
Snapshot in which each identity was present. It does not assert provider-side
deletion.

`snapshot_state_digests` freezes a canonical digest of the Snapshot's persisted
rows and related identity/version/search-document state. It is a local
tamper/regression guard for no-op verification, not a cross-installation
reproducible database hash or a signature. The actual FTS index is checked
separately with FTS5's external-content integrity check.

## Persisted completeness claims

`snapshot_claims` uses `pass`, `partial`, `fail`, or `not_applicable`. The
current official-export importer defines the claims narrowly:

- `source_complete` passes when file/byte coverage equals the already verified
  Evidence Manifest.
- `extraction_complete` passes when Source coverage closes, every JSON file has
  one decoded root record, and every `.dat` file has one verified CAS
  observation. Opaque HTML/other files are counted as preserved, not decoded.
- `reconciliation_complete` is always `partial` until an independent full
  reconciliation report exists; unresolved/ambiguous diagnostic count is
  recorded in its details.
- `account_complete` is always `not_applicable`, because available captures
  cannot prove historical account completeness.

These are Source-Snapshot claims for this importer profile. In particular,
`extraction_complete=pass` does not mean every provider field has a normalized
table, every attachment reference resolves, extension recovery is complete, or
the user's account history is complete. The read-only `doctor` result is a
preflight validation result and is not a persisted reconciliation report.

## Derived layers now implemented outside schema v2

- loopback-only Reading Archive with current/alternative branches; ChatGPT archive
  reads remain read-only while annotations use a separate writable database;
- CAS-verified attachment serving and bounded static conversation HTML;
- selective PDF rendering from static HTML;
- deterministic JSON/Markdown analysis;
- separate review-gated `memory.sqlite`, a loopback-only interactive review
  workbench with source context/batch decisions/reviewed wording, confirmed-only
  profiles, and ChatGPT continuity packages;
- validation for the source-neutral adapter JSONL envelope;
- `memory/annotations.sqlite` for feedback, validity dates, bookmarks and reading positions;
- `canonical/pi.sqlite` for Pi session records;
- `derived/semantic.sqlite` for local semantic retrieval.

Retrieval and Pi integration are described in [pi-recall.md](pi-recall.md).

## Explicitly pending

The following design goals are not implemented by schema v2:

- ingestion or canonical projections for the IndexedDB recovery exporter;
- normalized alternative-branch/path records beyond the current branch;
- robust MIME signature detection, transcription, and rich previews for every
  attachment class;
- annual/topic collections and batch PDF exports (whole-Snapshot HTML is implemented);
- static HTML exports for normalized group chats (the local reader is implemented);
- provider-specific ingestion for Gemini, DeepSeek, Claude, WeChat, and QQ;
- automatic model-based memory extraction, conflict resolution, and
  cross-provider reconciliation.
