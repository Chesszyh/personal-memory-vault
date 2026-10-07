PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    identity_scope TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    snapshot_key TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    captured_at_us INTEGER NOT NULL,
    raw_source_json TEXT NOT NULL,
    evidence_tree_sha256 TEXT NOT NULL,
    evidence_manifest_sha256 TEXT NOT NULL,
    evidence_manifest_path TEXT NOT NULL,
    decoded_ndjson_path TEXT NOT NULL,
    decoded_ndjson_sha256 TEXT NOT NULL,
    source_records_ndjson_path TEXT NOT NULL,
    source_records_ndjson_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (source_id, snapshot_key)
);

CREATE TABLE IF NOT EXISTS import_runs (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed')),
    importer_version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    config_sha256 TEXT NOT NULL,
    source_records_count INTEGER NOT NULL,
    conversations_count INTEGER NOT NULL,
    nodes_count INTEGER NOT NULL,
    messages_count INTEGER NOT NULL,
    group_threads_count INTEGER NOT NULL,
    group_messages_count INTEGER NOT NULL,
    warnings_count INTEGER NOT NULL,
    UNIQUE (snapshot_id)
);

CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    record_kind TEXT NOT NULL,
    provider_record_kind TEXT NOT NULL,
    record_scope TEXT NOT NULL CHECK (record_scope IN ('file_root', 'element')),
    native_id TEXT,
    source_file_path TEXT NOT NULL,
    array_index INTEGER,
    json_pointer TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    ndjson_line_number INTEGER NOT NULL,
    UNIQUE (snapshot_id, source_file_path, json_pointer),
    UNIQUE (snapshot_id, ndjson_line_number)
);

CREATE TABLE IF NOT EXISTS conversation_identities (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    identity_key TEXT NOT NULL UNIQUE,
    native_id TEXT NOT NULL,
    UNIQUE (source_id, native_id)
);

CREATE TABLE IF NOT EXISTS conversation_versions (
    id INTEGER PRIMARY KEY,
    conversation_identity_id INTEGER NOT NULL REFERENCES conversation_identities(id),
    raw_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    unknown_json TEXT NOT NULL,
    title TEXT,
    create_time REAL,
    update_time REAL,
    UNIQUE (conversation_identity_id, raw_sha256)
);

CREATE TABLE IF NOT EXISTS conversation_observations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    conversation_identity_id INTEGER NOT NULL REFERENCES conversation_identities(id),
    conversation_version_id INTEGER NOT NULL REFERENCES conversation_versions(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    current_node_identity_key TEXT NOT NULL,
    UNIQUE (snapshot_id, conversation_identity_id),
    UNIQUE (snapshot_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS node_identities (
    id INTEGER PRIMARY KEY,
    conversation_identity_id INTEGER NOT NULL REFERENCES conversation_identities(id),
    identity_key TEXT NOT NULL UNIQUE,
    native_id TEXT NOT NULL,
    UNIQUE (conversation_identity_id, native_id)
);

CREATE TABLE IF NOT EXISTS message_identities (
    id INTEGER PRIMARY KEY,
    conversation_identity_id INTEGER NOT NULL REFERENCES conversation_identities(id),
    identity_key TEXT NOT NULL UNIQUE,
    native_id TEXT NOT NULL,
    UNIQUE (conversation_identity_id, native_id)
);

CREATE TABLE IF NOT EXISTS node_versions (
    id INTEGER PRIMARY KEY,
    node_identity_id INTEGER NOT NULL REFERENCES node_identities(id),
    raw_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    unknown_json TEXT NOT NULL,
    declared_children_json TEXT,
    UNIQUE (node_identity_id, raw_sha256)
);

CREATE TABLE IF NOT EXISTS node_observations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    node_identity_id INTEGER NOT NULL REFERENCES node_identities(id),
    node_version_id INTEGER NOT NULL REFERENCES node_versions(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    json_pointer TEXT NOT NULL,
    parent_node_identity_id INTEGER REFERENCES node_identities(id),
    message_identity_id INTEGER REFERENCES message_identities(id),
    UNIQUE (snapshot_id, node_identity_id),
    UNIQUE (snapshot_id, source_record_id, json_pointer)
);

CREATE TABLE IF NOT EXISTS message_versions (
    id INTEGER PRIMARY KEY,
    message_identity_id INTEGER NOT NULL REFERENCES message_identities(id),
    raw_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    unknown_json TEXT NOT NULL,
    author_role TEXT,
    author_name TEXT,
    content_type TEXT,
    create_time REAL,
    update_time REAL,
    status TEXT,
    recipient TEXT,
    UNIQUE (message_identity_id, raw_sha256)
);

CREATE TABLE IF NOT EXISTS message_observations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    message_identity_id INTEGER NOT NULL REFERENCES message_identities(id),
    message_version_id INTEGER NOT NULL REFERENCES message_versions(id),
    node_observation_id INTEGER NOT NULL REFERENCES node_observations(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    json_pointer TEXT NOT NULL,
    UNIQUE (snapshot_id, message_identity_id),
    UNIQUE (snapshot_id, source_record_id, json_pointer)
);

CREATE TABLE IF NOT EXISTS current_branch_nodes (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    conversation_identity_id INTEGER NOT NULL REFERENCES conversation_identities(id),
    conversation_observation_id INTEGER NOT NULL REFERENCES conversation_observations(id),
    node_identity_id INTEGER NOT NULL REFERENCES node_identities(id),
    depth INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, conversation_identity_id, depth),
    UNIQUE (snapshot_id, conversation_identity_id, node_identity_id)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS asset_observations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    evidence_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    declared_name TEXT,
    observation_kind TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (snapshot_id, evidence_path, observation_kind)
);

CREATE TABLE IF NOT EXISTS message_asset_refs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    message_observation_id INTEGER NOT NULL REFERENCES message_observations(id),
    ordinal INTEGER NOT NULL,
    reference_kind TEXT NOT NULL,
    reference_value TEXT NOT NULL,
    normalized_reference TEXT,
    asset_observation_id INTEGER REFERENCES asset_observations(id),
    resolution_status TEXT NOT NULL CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous')),
    resolution_method TEXT,
    candidate_count INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (snapshot_id, message_observation_id, ordinal)
);

CREATE TABLE IF NOT EXISTS library_asset_refs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    evidence_path TEXT NOT NULL,
    declared_name TEXT,
    asset_observation_id INTEGER REFERENCES asset_observations(id),
    resolution_status TEXT NOT NULL CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous')),
    resolution_method TEXT,
    candidate_count INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (snapshot_id, evidence_path)
);

CREATE TABLE IF NOT EXISTS library_source_refs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    library_file_reference TEXT NOT NULL,
    reference_kind TEXT NOT NULL,
    target_native_id TEXT NOT NULL,
    related_thread_native_id TEXT,
    resolution_status TEXT NOT NULL CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous')),
    resolved_identity_key TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE (snapshot_id, library_file_reference, reference_kind)
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY,
    import_run_id TEXT NOT NULL REFERENCES import_runs(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    conversation_identity_id INTEGER REFERENCES conversation_identities(id),
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
    code TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_claims (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    claim TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pass', 'partial', 'fail', 'not_applicable')),
    details_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, claim)
);

CREATE TABLE IF NOT EXISTS source_file_coverage (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    evidence_path TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind = 'file'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    media_class TEXT NOT NULL CHECK (media_class IN ('json', 'dat', 'html', 'other')),
    handling_status TEXT NOT NULL CHECK (
        handling_status IN ('decoded', 'cas_verified', 'preserved_opaque')
    ),
    source_record_count INTEGER NOT NULL CHECK (source_record_count >= 0),
    asset_observation_count INTEGER NOT NULL CHECK (asset_observation_count >= 0),
    details_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, evidence_path)
);

CREATE TABLE IF NOT EXISTS snapshot_state_digests (
    snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id),
    algorithm TEXT NOT NULL CHECK (algorithm = 'sha256-canonical-json-v1'),
    state_sha256 TEXT NOT NULL CHECK (length(state_sha256) = 64),
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_absences (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    prior_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    entity_kind TEXT NOT NULL CHECK (
        entity_kind IN (
            'conversation', 'node', 'message', 'group_thread', 'group_message', 'asset'
        )
    ),
    identity_key TEXT NOT NULL,
    UNIQUE (snapshot_id, entity_kind, identity_key)
);

CREATE TABLE IF NOT EXISTS group_thread_identities (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    identity_key TEXT NOT NULL UNIQUE,
    native_id TEXT NOT NULL,
    UNIQUE (source_id, native_id)
);

CREATE TABLE IF NOT EXISTS group_thread_versions (
    id INTEGER PRIMARY KEY,
    group_thread_identity_id INTEGER NOT NULL REFERENCES group_thread_identities(id),
    raw_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    unknown_json TEXT NOT NULL,
    name TEXT,
    UNIQUE (group_thread_identity_id, raw_sha256)
);

CREATE TABLE IF NOT EXISTS group_thread_observations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    group_thread_identity_id INTEGER NOT NULL REFERENCES group_thread_identities(id),
    group_thread_version_id INTEGER NOT NULL REFERENCES group_thread_versions(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    json_pointer TEXT NOT NULL,
    UNIQUE (snapshot_id, group_thread_identity_id)
);

CREATE TABLE IF NOT EXISTS group_message_identities (
    id INTEGER PRIMARY KEY,
    group_thread_identity_id INTEGER NOT NULL REFERENCES group_thread_identities(id),
    identity_key TEXT NOT NULL UNIQUE,
    native_id TEXT NOT NULL,
    UNIQUE (group_thread_identity_id, native_id)
);

CREATE TABLE IF NOT EXISTS group_message_versions (
    id INTEGER PRIMARY KEY,
    group_message_identity_id INTEGER NOT NULL REFERENCES group_message_identities(id),
    raw_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    unknown_json TEXT NOT NULL,
    role TEXT,
    text TEXT,
    created_at TEXT,
    UNIQUE (group_message_identity_id, raw_sha256)
);

CREATE TABLE IF NOT EXISTS group_message_observations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    group_message_identity_id INTEGER NOT NULL REFERENCES group_message_identities(id),
    group_message_version_id INTEGER NOT NULL REFERENCES group_message_versions(id),
    group_thread_observation_id INTEGER NOT NULL REFERENCES group_thread_observations(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    json_pointer TEXT NOT NULL,
    UNIQUE (snapshot_id, group_message_identity_id)
);

CREATE TABLE IF NOT EXISTS group_message_asset_refs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    group_message_observation_id INTEGER NOT NULL REFERENCES group_message_observations(id),
    ordinal INTEGER NOT NULL,
    reference_value TEXT,
    resolution_status TEXT NOT NULL CHECK (resolution_status IN ('resolved', 'unresolved', 'ambiguous', 'external')),
    asset_observation_id INTEGER REFERENCES asset_observations(id),
    candidates_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (snapshot_id, group_message_observation_id, ordinal)
);

CREATE TABLE IF NOT EXISTS search_documents (
    id INTEGER PRIMARY KEY,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN ('conversation_message', 'group_message')
    ),
    message_version_id INTEGER UNIQUE REFERENCES message_versions(id),
    group_message_version_id INTEGER UNIQUE REFERENCES group_message_versions(id),
    identity_key TEXT NOT NULL,
    author_role TEXT,
    content_type TEXT,
    content TEXT NOT NULL,
    CHECK (
        (message_version_id IS NOT NULL AND group_message_version_id IS NULL)
        OR (message_version_id IS NULL AND group_message_version_id IS NOT NULL)
    )
);

CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    document_kind UNINDEXED,
    message_version_id UNINDEXED,
    group_message_version_id UNINDEXED,
    identity_key UNINDEXED,
    author_role UNINDEXED,
    content_type UNINDEXED,
    content,
    content = 'search_documents',
    content_rowid = 'id',
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS search_documents_ai AFTER INSERT ON search_documents BEGIN
    INSERT INTO message_fts(
        rowid, document_kind, message_version_id, group_message_version_id,
        identity_key, author_role, content_type, content
    ) VALUES (
        new.id, new.document_kind, new.message_version_id, new.group_message_version_id,
        new.identity_key, new.author_role, new.content_type, new.content
    );
END;

CREATE TRIGGER IF NOT EXISTS search_documents_ad AFTER DELETE ON search_documents BEGIN
    INSERT INTO message_fts(
        message_fts, rowid, document_kind, message_version_id,
        group_message_version_id, identity_key, author_role, content_type, content
    ) VALUES (
        'delete', old.id, old.document_kind, old.message_version_id,
        old.group_message_version_id, old.identity_key, old.author_role,
        old.content_type, old.content
    );
END;

CREATE TRIGGER IF NOT EXISTS search_documents_au AFTER UPDATE ON search_documents BEGIN
    INSERT INTO message_fts(
        message_fts, rowid, document_kind, message_version_id,
        group_message_version_id, identity_key, author_role, content_type, content
    ) VALUES (
        'delete', old.id, old.document_kind, old.message_version_id,
        old.group_message_version_id, old.identity_key, old.author_role,
        old.content_type, old.content
    );
    INSERT INTO message_fts(
        rowid, document_kind, message_version_id, group_message_version_id,
        identity_key, author_role, content_type, content
    ) VALUES (
        new.id, new.document_kind, new.message_version_id, new.group_message_version_id,
        new.identity_key, new.author_role, new.content_type, new.content
    );
END;

CREATE INDEX IF NOT EXISTS conversation_versions_snapshot
    ON conversation_observations(snapshot_id);
CREATE INDEX IF NOT EXISTS node_versions_snapshot
    ON node_observations(snapshot_id);
CREATE INDEX IF NOT EXISTS message_versions_snapshot
    ON message_observations(snapshot_id);
CREATE INDEX IF NOT EXISTS asset_observations_snapshot
    ON asset_observations(snapshot_id);
CREATE INDEX IF NOT EXISTS diagnostics_snapshot
    ON diagnostics(snapshot_id, severity, code);
