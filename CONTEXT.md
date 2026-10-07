# Personal Memory Vault

This context describes a vendor-independent personal archive that preserves source evidence, derives reviewable memories, and produces provider-specific continuity packages.

## Evidence and archive

**Evidence Artifact**:
An immutable source item captured from a provider or messaging system, including its original structure and associated files.
_Avoid_: Memory, imported truth

**Evidence Manifest**:
A deterministic inventory of a captured source, recording relative paths, sizes, and content hashes so that preservation can be verified without interpreting the files.
_Avoid_: Archive index, conversation list

**Master Evidence Copy**:
A verified copy of a frozen source capture that recovery tools and parsers never open for mutation; all risky or format-aware work uses a separate working copy.
_Avoid_: Working directory, latest export

**Recovery Working Copy**:
A disposable, checksum-linked copy of a Master Evidence Copy used for browser recovery, decoding, and other operations that may alter storage files.
_Avoid_: Backup, source of truth

**Snapshot**:
A bounded capture of the Evidence Artifacts visible from one Source at a particular time. Absence from a later Snapshot does not by itself mean deletion.
_Avoid_: Backup version, latest truth

**Source**:
The origin and capture mechanism of an Evidence Artifact, such as an official account export or an extension observation.
_Avoid_: Provider, authority

**Provenance**:
The trace from a derived record or assertion back to the Source, Snapshot, and exact Evidence Artifact that supports it.
_Avoid_: Citation text, metadata

**Canonical Record**:
A source-neutral representation of an archived conversation, message, participant, relation, or asset, retaining its Provenance.
_Avoid_: Raw record, final truth

**Reading Archive**:
A human-readable, searchable presentation derived from Canonical Records and linked Evidence Artifacts.
_Avoid_: Evidence Archive, source of truth

**Source Complete**:
A coverage claim that every file present in a frozen Source capture has been preserved and accounted for.
_Avoid_: Fully recovered, account complete

**Extraction Complete**:
A coverage claim that every stored record in the available Evidence Artifacts has either been decoded or reported as a specific extraction failure.
_Avoid_: Source complete, account complete

**Reconciliation Complete**:
A coverage claim that every expected cross-record reference, native-identity match, overlap, and unresolved conflict among the ingested Sources has been accounted for in a report.
_Avoid_: Extraction complete, account complete

**Account Complete**:
An unprovable claim that every item ever held by a provider account has been recovered, including items no available Source captured.
_Avoid_: Extraction complete, fully recovered

**Identity Match Candidate**:
A proposed link between records that lack sufficient native identity evidence to be merged automatically.
_Avoid_: Duplicate, matched record

**Source Absence**:
The condition in which an item is not present in a particular Snapshot without evidence that it was deleted.
_Avoid_: Deletion, purge

**Source Deletion**:
An explicit observation that a Source considers an item deleted; the prior Evidence Artifact remains preserved.
_Avoid_: Source absence, purge

**Purge**:
An explicit user-authorized physical destruction of private evidence, distinct from a provider's deletion state.
_Avoid_: Delete flag, source deletion

**Secret**:
An active credential or recovery value that grants access, such as a password, token, private key, session cookie, or recovery code.
_Avoid_: Sensitive memory, ordinary private data

**Current Branch**:
The conversation path selected by the Source for default reading at the time of a Snapshot; alternative branches remain Evidence Artifacts.
_Avoid_: Complete conversation, only valid branch

**Content-addressed Asset**:
A derived copy of an attachment identified by its cryptographic content hash while retaining every original filename, reference, Source, and Snapshot in Provenance.
_Avoid_: Original attachment, filename identity

**Derived Provider View**:
A compatibility export that resembles a provider format but is explicitly labeled as vault-generated and never represented as an original provider export.
_Avoid_: Official export, merged source

## Personal memory

Historical retrieval reads source messages without candidate approval. The retrieval
profile also exposes explicitly saved quotes and observed statement groups, labeled
separately from the confirmed Current Profile defined below.

**Memory Candidate**:
An unconfirmed assertion about the user, their preferences, relationships, projects, or state, derived from one or more pieces of evidence.
_Avoid_: Fact, memory

**Confirmed Memory**:
A Memory Candidate that the user has accepted for use in profiles, retrieval, and migration.
_Avoid_: Candidate, inferred fact

**Current Profile**:
A present-time view assembled from applicable Confirmed Memories; it is not the authoritative store of historical states.
_Avoid_: Master memory, permanent profile

**Stated Time**:
The time at which a source message expressed an assertion.
_Avoid_: Observed time, validity start

**Observed Time**:
The time at which the vault captured or learned an assertion.
_Avoid_: Stated time, validity start

**Validity Period**:
The interval during which an assertion is understood to hold in the world, which may remain unknown.
_Avoid_: Message time, import time

**Memory Scope**:
The domain in which a memory applies, such as globally or within a project, relationship, device, or conversation.
_Avoid_: Provider destination, folder

**Superseded Memory**:
A previously Confirmed Memory whose Validity Period or applicability ended after a later confirmed assertion; it remains part of history.
_Avoid_: Deleted memory, corrected in place

**Conflict**:
Two or more assertions whose scopes or validity periods overlap and cannot all be accepted without adjudication.
_Avoid_: Duplicate, overwrite

## Continuity

**Migration Package**:
A reviewed, provider-specific selection of Confirmed Memories and archive indexes prepared within that provider's limits.
_Avoid_: Full backup, account restore

**Cloud Processing Task**:
A user-authorized purpose, provider, and bounded input set that may be processed in automatic batches after one explicit approval.
_Avoid_: General cloud permission, background upload

**Continuity Reconstruction**:
The measurable recreation of useful personal context in a new system without claiming to restore the former provider's internal account state.
_Avoid_: Memory restore, account clone
