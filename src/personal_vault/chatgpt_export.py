from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from personal_vault.database import (
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    DatabaseSchemaError,
    connect_database,
)
from personal_vault.evidence import EvidenceError, load_manifest, verify_manifest


class ChatGPTImportError(RuntimeError):
    """Raised when an official ChatGPT export cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class ChatGPTImportResult:
    status: str
    source_key: str
    snapshot_key: str
    database_path: Path
    ndjson_path: Path
    source_records_ndjson_path: Path
    source_records: int
    conversations: int
    nodes: int
    messages: int
    group_threads: int
    group_messages: int
    warnings: int
    claims: dict[str, str]


@dataclass(frozen=True, slots=True)
class ChatGPTDoctorResult:
    source_complete: bool
    extraction_complete: bool
    snapshot_key: str
    evidence_tree_sha256: str
    source_records: int
    conversations: int
    nodes: int
    messages: int
    group_threads: int
    group_messages: int
    graph_warnings: int
    physical_assets: int


@dataclass(frozen=True, slots=True)
class _ValidatedConversation:
    native_id: str
    current_node: str
    current_branch: tuple[str, ...]
    node_count: int
    message_count: int
    warnings: tuple[tuple[str, dict[str, Any]], ...]


CONVERSATION_KNOWN_FIELDS = frozenset(
    {
        "id",
        "conversation_id",
        "title",
        "create_time",
        "update_time",
        "mapping",
        "current_node",
        "conversation_template_id",
        "gizmo_id",
        "gizmo_type",
        "is_archived",
        "is_starred",
        "is_read_only",
        "is_study_mode",
        "default_model_slug",
        "safe_urls",
        "blocked_urls",
        "moderation_results",
        "plugin_ids",
        "async_status",
        "disabled_tool_ids",
        "is_do_not_remember",
        "memory_scope",
        "context_scopes",
        "sugar_item_id",
        "sugar_item_visible",
        "pinned_time",
        "voice",
    }
)
NODE_KNOWN_FIELDS = frozenset({"id", "message", "parent", "children"})
MESSAGE_KNOWN_FIELDS = frozenset(
    {
        "id",
        "author",
        "create_time",
        "update_time",
        "content",
        "status",
        "end_turn",
        "weight",
        "metadata",
        "recipient",
        "channel",
    }
)
CONVERSATION_SHARD = re.compile(r"^conversations(?:-(\d+))?\.json$")
IMPORTER_VERSION = 2
DATABASE_SCHEMA_VERSION = SCHEMA_VERSION
IMPORT_CONFIG_SHA256 = hashlib.sha256(
    (
        "chatgpt-official-v2|fts5-trigram|source-preserving-raw-json|"
        f"asset-cas-sha256|schema-sha256:{SCHEMA_SHA256}"
    ).encode("ascii")
).hexdigest()

_SNAPSHOT_DIRECT_STATE_TABLES = (
    "source_records",
    "conversation_observations",
    "node_observations",
    "message_observations",
    "current_branch_nodes",
    "asset_observations",
    "message_asset_refs",
    "library_asset_refs",
    "library_source_refs",
    "diagnostics",
    "snapshot_claims",
    "source_file_coverage",
    "source_absences",
    "group_thread_observations",
    "group_message_observations",
    "group_message_asset_refs",
)

_SNAPSHOT_RELATED_STATE_QUERIES = (
    (
        "sources",
        "SELECT src.* FROM sources src JOIN snapshots s ON s.source_id = src.id WHERE s.id = ?",
    ),
    ("snapshots", "SELECT * FROM snapshots WHERE id = ?"),
    ("import_runs", "SELECT * FROM import_runs WHERE snapshot_id = ?"),
    (
        "conversation_identities",
        """SELECT DISTINCT i.* FROM conversation_identities i
              JOIN conversation_observations o ON o.conversation_identity_id = i.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "conversation_versions",
        """SELECT DISTINCT v.* FROM conversation_versions v
              JOIN conversation_observations o ON o.conversation_version_id = v.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "node_identities",
        """SELECT DISTINCT i.* FROM node_identities i
              JOIN node_observations o ON o.node_identity_id = i.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "node_versions",
        """SELECT DISTINCT v.* FROM node_versions v
              JOIN node_observations o ON o.node_version_id = v.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "message_identities",
        """SELECT DISTINCT i.* FROM message_identities i
              JOIN message_observations o ON o.message_identity_id = i.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "message_versions",
        """SELECT DISTINCT v.* FROM message_versions v
              JOIN message_observations o ON o.message_version_id = v.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "assets",
        """SELECT DISTINCT a.* FROM assets a
              JOIN asset_observations o ON o.asset_id = a.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "group_thread_identities",
        """SELECT DISTINCT i.* FROM group_thread_identities i
              JOIN group_thread_observations o ON o.group_thread_identity_id = i.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "group_thread_versions",
        """SELECT DISTINCT v.* FROM group_thread_versions v
              JOIN group_thread_observations o ON o.group_thread_version_id = v.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "group_message_identities",
        """SELECT DISTINCT i.* FROM group_message_identities i
              JOIN group_message_observations o ON o.group_message_identity_id = i.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "group_message_versions",
        """SELECT DISTINCT v.* FROM group_message_versions v
              JOIN group_message_observations o ON o.group_message_version_id = v.id
             WHERE o.snapshot_id = ?""",
    ),
    (
        "search_documents",
        """SELECT DISTINCT d.* FROM search_documents d
             WHERE d.message_version_id IN (
                       SELECT o.message_version_id FROM message_observations o
                        WHERE o.snapshot_id = ?
                   )
                OR d.group_message_version_id IN (
                       SELECT o.group_message_version_id FROM group_message_observations o
                        WHERE o.snapshot_id = ?
                   )""",
    ),
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return component or "snapshot"


def _unknown(value: dict[str, Any], known: frozenset[str]) -> str:
    return _canonical_json({key: item for key, item in value.items() if key not in known})


def _identity_prefix(identity_scope: str, conversation_id: str) -> str:
    return f"openai/chatgpt-official/{identity_scope}/conversations/{conversation_id}"


def _load_shards(evidence_root: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    export_manifest = evidence_root / "export_manifest.json"
    shard_names: list[str] = []
    if export_manifest.is_file():
        try:
            exported = json.loads(export_manifest.read_text(encoding="utf-8"))
            logical = exported.get("logical_files", {}).get("conversations.json", {})
            files = logical.get("files")
        except (OSError, json.JSONDecodeError, AttributeError) as error:
            raise ChatGPTImportError(f"cannot read export_manifest.json: {error}") from error
        if isinstance(files, list) and all(isinstance(item, str) for item in files):
            shard_names = list(files)
    if not shard_names:
        for entry in manifest["entries"]:
            path = entry.get("path")
            if (
                entry.get("kind") == "file"
                and isinstance(path, str)
                and CONVERSATION_SHARD.fullmatch(path)
            ):
                shard_names.append(path)

        def shard_order(name: str) -> tuple[int, int, str]:
            match = CONVERSATION_SHARD.fullmatch(name)
            assert match is not None
            suffix = match.group(1)
            return (0 if suffix is None else 1, -1 if suffix is None else int(suffix), name)

        shard_names.sort(key=shard_order)
    if not shard_names:
        raise ChatGPTImportError("no conversations JSON shards found in verified evidence")

    manifest_files = {
        entry["path"]
        for entry in manifest["entries"]
        if entry.get("kind") == "file" and isinstance(entry.get("path"), str)
    }
    for name in shard_names:
        if name not in manifest_files:
            raise ChatGPTImportError(f"conversation shard is not in evidence manifest: {name}")
        if not CONVERSATION_SHARD.fullmatch(name):
            raise ChatGPTImportError(f"invalid conversation shard path: {name}")
    if len(shard_names) != len(set(shard_names)):
        raise ChatGPTImportError("export manifest contains duplicate conversation shards")
    return tuple(shard_names)


def _validate_provider_export_manifest(
    evidence_root: Path, evidence_manifest: dict[str, Any]
) -> None:
    path = evidence_root / "export_manifest.json"
    if not path.is_file():
        return
    try:
        provider_manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChatGPTImportError(f"cannot read export_manifest.json: {error}") from error
    if not isinstance(provider_manifest, dict):
        raise ChatGPTImportError("export_manifest.json must be an object")
    export_files = provider_manifest.get("export_files")
    logical_files = provider_manifest.get("logical_files")
    if not isinstance(export_files, list) or not isinstance(logical_files, dict):
        raise ChatGPTImportError("export_manifest.json has invalid file inventories")
    evidence_files = {
        entry["path"]: entry["size"]
        for entry in evidence_manifest["entries"]
        if entry.get("kind") == "file" and isinstance(entry.get("path"), str)
    }
    provider_files: dict[str, int] = {}
    for item in export_files:
        if not isinstance(item, dict):
            raise ChatGPTImportError("provider export file entry is invalid")
        relative = item.get("path")
        size = item.get("size_bytes")
        if not isinstance(relative, str) or not isinstance(size, int):
            raise ChatGPTImportError("provider export file entry is incomplete")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ChatGPTImportError(f"unsafe provider export path: {relative}")
        if relative in provider_files:
            raise ChatGPTImportError(f"duplicate provider export path: {relative}")
        provider_files[relative] = size
    expected_provider_files = {
        relative: size
        for relative, size in evidence_files.items()
        if relative != "export_manifest.json"
    }
    if provider_files != expected_provider_files:
        raise ChatGPTImportError("provider export file inventory does not match evidence")
    for logical_name, logical in logical_files.items():
        if not isinstance(logical_name, str) or not isinstance(logical, dict):
            raise ChatGPTImportError("provider logical file entry is invalid")
        files = logical.get("files")
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise ChatGPTImportError(f"provider logical file list is invalid: {logical_name}")
        if len(files) != len(set(files)) or any(item not in provider_files for item in files):
            raise ChatGPTImportError(f"provider logical file does not resolve: {logical_name}")


def _validate_conversation(raw: Any, source_file: str, array_index: int) -> _ValidatedConversation:
    location = f"{source_file}[{array_index}]"
    if not isinstance(raw, dict):
        raise ChatGPTImportError(f"conversation is not an object: {location}")
    conversation_id = raw.get("id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ChatGPTImportError(f"conversation has no native id: {location}")
    alternate_id = raw.get("conversation_id")
    if alternate_id is not None and alternate_id != conversation_id:
        raise ChatGPTImportError(f"conversation id mismatch: {conversation_id}")
    mapping = raw.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise ChatGPTImportError(f"conversation mapping is empty or invalid: {conversation_id}")
    current_node = raw.get("current_node")
    if not isinstance(current_node, str) or current_node not in mapping:
        raise ChatGPTImportError(f"current_node does not resolve: {conversation_id}")

    parents: dict[str, str | None] = {}
    messages = 0
    warnings: list[tuple[str, dict[str, Any]]] = []
    for mapping_key, node in mapping.items():
        if not isinstance(mapping_key, str) or not isinstance(node, dict):
            raise ChatGPTImportError(f"invalid mapping entry: {conversation_id}")
        if node.get("id") != mapping_key:
            raise ChatGPTImportError(f"node id mismatch: {conversation_id}/{mapping_key}")
        parent = node.get("parent")
        if parent is not None and (not isinstance(parent, str) or parent not in mapping):
            raise ChatGPTImportError(f"parent does not resolve: {conversation_id}/{mapping_key}")
        parents[mapping_key] = parent
        message = node.get("message")
        if message is not None:
            if not isinstance(message, dict) or message.get("id") != mapping_key:
                raise ChatGPTImportError(f"message id mismatch: {conversation_id}/{mapping_key}")
            messages += 1
        if "children" in node:
            children = node["children"]
            if not isinstance(children, list):
                raise ChatGPTImportError(
                    f"node children are invalid: {conversation_id}/{mapping_key}"
                )
            for child in children:
                if not isinstance(child, str) or child not in mapping:
                    warnings.append(("child_does_not_resolve", {"node_id": mapping_key}))
                elif mapping[child].get("parent") != mapping_key:
                    warnings.append(("child_parent_disagreement", {"node_id": mapping_key}))

    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        status = state.get(node_id, 0)
        if status == 1:
            raise ChatGPTImportError(f"cycle in conversation graph: {conversation_id}")
        if status == 2:
            return
        state[node_id] = 1
        parent = parents[node_id]
        if parent is not None:
            visit(parent)
        state[node_id] = 2

    for node_id in mapping:
        visit(node_id)

    roots = tuple(node_id for node_id, parent in parents.items() if parent is None)
    if len(roots) != 1:
        warnings.append(("root_count_anomaly", {"root_count": len(roots)}))
    current_children = sum(parent == current_node for parent in parents.values())
    if current_children:
        warnings.append(
            ("current_node_not_leaf", {"derived_child_count": current_children})
        )

    branch_reversed: list[str] = []
    cursor: str | None = current_node
    while cursor is not None:
        branch_reversed.append(cursor)
        cursor = parents[cursor]
    return _ValidatedConversation(
        native_id=conversation_id,
        current_node=current_node,
        current_branch=tuple(reversed(branch_reversed)),
        node_count=len(mapping),
        message_count=messages,
        warnings=tuple(warnings),
    )


def _write_staging_ndjson(
    *,
    evidence_root: Path,
    shard_names: Iterable[str],
    temporary_file: BinaryIO | None,
    source_context: dict[str, str] | None = None,
) -> tuple[int, int, int, int]:
    seen_conversations: dict[str, str] = {}
    conversations = nodes = messages = warnings = 0
    for shard_name in shard_names:
        try:
            with (evidence_root / shard_name).open("r", encoding="utf-8") as handle:
                shard = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ChatGPTImportError(f"cannot read conversation shard {shard_name}: {error}") from error
        if not isinstance(shard, list):
            raise ChatGPTImportError(f"conversation shard is not an array: {shard_name}")
        for array_index, raw in enumerate(shard):
            validated = _validate_conversation(raw, shard_name, array_index)
            raw_sha256 = _json_hash(raw)
            prior = seen_conversations.get(validated.native_id)
            if prior is not None:
                detail = "different payload" if prior != raw_sha256 else "duplicate payload"
                raise ChatGPTImportError(
                    f"duplicate conversation native id ({detail}): {validated.native_id}"
                )
            seen_conversations[validated.native_id] = raw_sha256
            envelope = {
                "schema_version": 2,
                "record_type": "chatgpt_official_conversation",
                "native_id": validated.native_id,
                "raw_sha256": raw_sha256,
                "provenance": {
                    **(source_context or {}),
                    "source_file": shard_name,
                    "array_index": array_index,
                },
                "raw": raw,
            }
            if temporary_file is not None:
                temporary_file.write(_canonical_json(envelope).encode("utf-8"))
                temporary_file.write(b"\n")
            conversations += 1
            nodes += validated.node_count
            messages += validated.message_count
            warnings += len(validated.warnings)
    if temporary_file is not None:
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
    return conversations, nodes, messages, warnings


def _record_kind_for_file(path: str) -> str:
    name = Path(path).name
    if CONVERSATION_SHARD.fullmatch(name):
        return "conversation"
    return {
        "group_chats.json": "group_chats",
        "shared_conversations.json": "shared_conversation",
        "message_feedback.json": "message_feedback",
        "library_files.json": "library_file",
        "conversation_asset_file_names.json": "conversation_asset_registry",
        "user_settings.json": "user_settings",
        "user.json": "user",
        "ads.json": "ads",
        "export_manifest.json": "provider_export_manifest",
    }.get(name, f"provider_json:{Path(name).stem}")


def _native_id_for_source_record(kind: str, raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    candidate_keys = {
        "conversation": ("id", "conversation_id"),
        "shared_conversation": ("id", "conversation_id"),
        "message_feedback": ("id", "message_id"),
        "library_file": ("file_id",),
    }.get(kind, ("id",))
    for key in candidate_keys:
        candidate = raw.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _write_source_records_ndjson(
    *,
    evidence_root: Path,
    manifest: dict[str, Any],
    temporary_file: BinaryIO,
    source_context: dict[str, str],
) -> int:
    line_number = 0
    json_entry_by_path = {
        entry["path"]: entry
        for entry in manifest["entries"]
        if entry.get("kind") == "file"
        and isinstance(entry.get("path"), str)
        and entry["path"].endswith(".json")
    }
    json_entries = sorted(json_entry_by_path, key=os.fsencode)
    for source_file in json_entries:
        try:
            with (evidence_root / source_file).open("r", encoding="utf-8") as handle:
                root = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ChatGPTImportError(f"cannot decode provider JSON {source_file}: {error}") from error
        provider_kind = _record_kind_for_file(source_file)
        file_entry = json_entry_by_path[source_file]
        root_record = (
            {
                "$provider_json_file_root": {
                    "json_type": "array",
                    "element_count": len(root),
                    "evidence_sha256": file_entry["sha256"],
                    "evidence_size_bytes": file_entry["size"],
                }
            }
            if isinstance(root, list)
            else root
        )
        records: list[tuple[int | None, str, str, str, Any]] = [
            (None, "", "file_root", "provider_json_file_root", root_record)
        ]
        if isinstance(root, list):
            records.extend(
                (index, f"/{index}", "element", provider_kind, value)
                for index, value in enumerate(root)
            )
        for array_index, json_pointer, record_scope, kind, raw in records:
            envelope = {
                "schema_version": 2,
                "record_type": "provider_source_record",
                "record_kind": kind,
                "provider_record_kind": provider_kind,
                "record_scope": record_scope,
                "native_id": None
                if record_scope == "file_root"
                else _native_id_for_source_record(kind, raw),
                "raw_sha256": _json_hash(raw),
                "provenance": {
                    **source_context,
                    "source_file": source_file,
                    "array_index": array_index,
                    "json_pointer": json_pointer,
                    "line_number": line_number,
                },
                "raw": raw,
            }
            temporary_file.write(_canonical_json(envelope).encode("utf-8"))
            temporary_file.write(b"\n")
            line_number += 1
    temporary_file.flush()
    os.fsync(temporary_file.fileno())
    return line_number


def _integer_id(database: Any, table: str, key_column: str, key: str) -> int:
    row = database.execute(
        f"SELECT id FROM {table} WHERE {key_column} = ?", (key,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    fragments: list[str] = []
    text = content.get("text")
    if isinstance(text, str):
        fragments.append(text)
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                fragments.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                fragments.append(part["text"])
    return "\n".join(fragments)


@dataclass(frozen=True, slots=True)
class _AssetResolution:
    normalized: str | None
    status: str
    method: str | None
    observation_id: int | None
    candidates: tuple[str, ...]


class _AssetResolver:
    def __init__(self, observations: dict[str, int]) -> None:
        self._observations = observations

    def resolve(self, raw_reference: str) -> _AssetResolution:
        normalized = _normalize_asset_reference(raw_reference)
        if normalized is None:
            return _AssetResolution(None, "unresolved", None, None, ())
        attempts: list[tuple[str, str]] = [("exact", normalized)]
        if normalized.endswith(".dat"):
            attempts.append(("remove_dat", normalized[:-4]))
        else:
            attempts.append(("append_dat", f"{normalized}.dat"))
        matched: dict[str, tuple[int, str]] = {}
        for method, candidate in attempts:
            for evidence_path, observation_id in self._observations.items():
                if evidence_path == candidate or Path(evidence_path).name == candidate:
                    matched[evidence_path] = (observation_id, method)
        candidates = tuple(sorted(matched))
        if not candidates:
            return _AssetResolution(normalized, "unresolved", None, None, ())
        if len(candidates) > 1:
            return _AssetResolution(normalized, "ambiguous", None, None, candidates)
        evidence_path = candidates[0]
        observation_id, method = matched[evidence_path]
        return _AssetResolution(
            normalized, "resolved", method, observation_id, candidates
        )


def _normalize_asset_reference(reference: str) -> str | None:
    value = reference.strip()
    if not value:
        return None
    value = value.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.rstrip("/").rsplit("/", 1)[-1]
    return value or None


def _load_asset_friendly_names(evidence_root: Path) -> dict[str, str]:
    path = evidence_root / "conversation_asset_file_names.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChatGPTImportError(f"cannot read conversation asset registry: {error}") from error
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ChatGPTImportError("conversation asset registry must map strings to strings")
    names: dict[str, str] = {}
    for key, value in raw.items():
        names[key] = value
        if key.endswith(".dat"):
            names[key[:-4]] = value
        else:
            names[f"{key}.dat"] = value
    return names


def _copy_to_content_store(
    *, source: Path, output_root: Path, sha256: str, size_bytes: int
) -> str:
    relative = Path("assets") / "sha256" / sha256[:2] / sha256
    target = output_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    def verify_target() -> None:
        if target.stat().st_size != size_bytes or _file_hash(target) != sha256:
            raise ChatGPTImportError(f"content-addressed asset conflict: {relative.as_posix()}")

    if target.exists():
        if not target.is_file():
            raise ChatGPTImportError(f"content-addressed asset is not a file: {relative.as_posix()}")
        verify_target()
        return relative.as_posix()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{sha256}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        copied = 0
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                output_file.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if copied != size_bytes or digest.hexdigest() != sha256:
            raise ChatGPTImportError(f"asset changed while copying: {source.name}")
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        verify_target()
    finally:
        temporary.unlink(missing_ok=True)
    return relative.as_posix()


def _publish_no_clobber(staging: Path, target: Path, expected_sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or _file_hash(target) != expected_sha256:
            raise ChatGPTImportError(f"published artifact conflict: {target.name}")
        staging.unlink()
        return
    try:
        os.link(staging, target)
    except FileExistsError:
        if not target.is_file() or _file_hash(target) != expected_sha256:
            raise ChatGPTImportError(f"published artifact conflict: {target.name}")
    staging.unlink()


def _verify_snapshot_assets(database: Any, *, snapshot_id: int, output_root: Path) -> None:
    rows = database.execute(
        """SELECT DISTINCT a.sha256, a.size_bytes, a.storage_path
             FROM asset_observations ao
             JOIN assets a ON a.id = ao.asset_id
            WHERE ao.snapshot_id = ?""",
        (snapshot_id,),
    )
    for row in rows:
        sha256 = str(row["sha256"])
        expected_storage_path = f"assets/sha256/{sha256[:2]}/{sha256}"
        storage_path = row["storage_path"]
        if (
            not isinstance(storage_path, str)
            or storage_path != expected_storage_path
            or re.fullmatch(r"assets/sha256/[0-9a-f]{2}/[0-9a-f]{64}", storage_path)
            is None
        ):
            raise ChatGPTImportError("stored CAS asset path is not canonical")
        pure = PurePosixPath(storage_path)
        if pure.is_absolute() or ".." in pure.parts:
            raise ChatGPTImportError("stored CAS asset path escapes the vault")
        path = output_root / Path(*pure.parts)
        if path.is_symlink():
            raise ChatGPTImportError("stored CAS asset path is a symlink")
        try:
            resolved_path = path.resolve(strict=True)
        except OSError as error:
            raise ChatGPTImportError(f"existing snapshot has a missing CAS asset: {sha256}") from error
        if not resolved_path.is_relative_to(output_root.resolve()):
            raise ChatGPTImportError("stored CAS asset path resolves outside the vault")
        if (
            not resolved_path.is_file()
            or resolved_path.stat().st_size != row["size_bytes"]
            or _file_hash(resolved_path) != sha256
        ):
            raise ChatGPTImportError(f"existing snapshot has a missing or changed CAS asset: {sha256}")


def _canonical_state_row_digests(rows: Iterable[Any]) -> tuple[str, ...]:
    digests: list[str] = []
    for row in rows:
        canonical_row = _canonical_json({key: row[key] for key in row.keys()})
        digests.append(hashlib.sha256(canonical_row.encode("utf-8")).hexdigest())
    return tuple(sorted(digests))


def _snapshot_state_digest(database: Any, *, snapshot_id: int) -> tuple[str, dict[str, int]]:
    sections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for table in _SNAPSHOT_DIRECT_STATE_TABLES:
        row_digests = _canonical_state_row_digests(
            database.execute(f"SELECT * FROM {table} WHERE snapshot_id = ?", (snapshot_id,))
        )
        counts[table] = len(row_digests)
        sections.append({"name": table, "row_sha256": row_digests})
    for name, query in _SNAPSHOT_RELATED_STATE_QUERIES:
        row_digests = _canonical_state_row_digests(
            database.execute(query, (snapshot_id,) * query.count("?"))
        )
        counts[name] = len(row_digests)
        sections.append({"name": name, "row_sha256": row_digests})
    payload = {
        "digest_format": "snapshot-state-v1",
        "schema_version": DATABASE_SCHEMA_VERSION,
        "sections": sections,
    }
    return _json_hash(payload), counts


def _store_snapshot_state_digest(database: Any, *, snapshot_id: int) -> None:
    state_sha256, counts = _snapshot_state_digest(database, snapshot_id=snapshot_id)
    database.execute(
        """INSERT INTO snapshot_state_digests
               (snapshot_id, algorithm, state_sha256, details_json)
             VALUES (?, 'sha256-canonical-json-v1', ?, ?)
             ON CONFLICT(snapshot_id) DO UPDATE SET
                 algorithm = excluded.algorithm,
                 state_sha256 = excluded.state_sha256,
                 details_json = excluded.details_json""",
        (
            snapshot_id,
            state_sha256,
            _canonical_json(
                {
                    "digest_format": "snapshot-state-v1",
                    "schema_version": DATABASE_SCHEMA_VERSION,
                    "section_row_counts": counts,
                }
            ),
        ),
    )


def _store_source_snapshot_state_digests(database: Any, *, source_id: int) -> None:
    for row in database.execute(
        "SELECT id FROM snapshots WHERE source_id = ? ORDER BY captured_at_us, snapshot_key",
        (source_id,),
    ):
        _store_snapshot_state_digest(database, snapshot_id=int(row[0]))


def _verify_snapshot_state_digest(database: Any, *, snapshot_id: int) -> None:
    stored = database.execute(
        """SELECT algorithm, state_sha256, details_json FROM snapshot_state_digests
             WHERE snapshot_id = ?""",
        (snapshot_id,),
    ).fetchone()
    if stored is None or stored["algorithm"] != "sha256-canonical-json-v1":
        raise ChatGPTImportError("snapshot state digest is missing or unsupported")
    actual, counts = _snapshot_state_digest(database, snapshot_id=snapshot_id)
    if actual != stored["state_sha256"]:
        raise ChatGPTImportError("snapshot state digest changed")
    expected_details = _canonical_json(
        {
            "digest_format": "snapshot-state-v1",
            "schema_version": DATABASE_SCHEMA_VERSION,
            "section_row_counts": counts,
        }
    )
    if stored["details_json"] != expected_details:
        raise ChatGPTImportError("snapshot state digest details changed")


def _verify_message_fts_integrity(database: Any) -> None:
    """Verify the external-content FTS index without leaking a transaction."""

    if database.in_transaction:
        raise ChatGPTImportError(
            "message FTS integrity check must run outside an import transaction"
        )
    try:
        database.execute(
            "INSERT INTO message_fts(message_fts, rank) VALUES('integrity-check', 1)"
        )
    except sqlite3.DatabaseError as error:
        raise ChatGPTImportError("message FTS index integrity check failed") from error
    finally:
        if database.in_transaction:
            database.rollback()


def _verify_snapshot_database(
    database: Any,
    *,
    snapshot_id: int,
    expected_source_records: int,
    expected_conversations: int,
    expected_nodes: int,
    expected_messages: int,
    expected_group_threads: int,
    expected_group_messages: int,
    expected_warnings: int,
) -> None:
    quick_check = database.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise ChatGPTImportError(f"vault database integrity check failed: {quick_check}")
    foreign_key_violation = database.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_violation is not None:
        raise ChatGPTImportError("vault database foreign-key integrity check failed")
    count_queries = {
        "source_record": "SELECT count(*) FROM source_records WHERE snapshot_id = ?",
        "conversation": "SELECT count(*) FROM conversation_observations WHERE snapshot_id = ?",
        "node": "SELECT count(*) FROM node_observations WHERE snapshot_id = ?",
        "message": "SELECT count(*) FROM message_observations WHERE snapshot_id = ?",
        "group_thread": "SELECT count(*) FROM group_thread_observations WHERE snapshot_id = ?",
        "group_message": "SELECT count(*) FROM group_message_observations WHERE snapshot_id = ?",
        "warning": "SELECT count(*) FROM diagnostics WHERE snapshot_id = ?",
    }
    expected = {
        "source_record": expected_source_records,
        "conversation": expected_conversations,
        "node": expected_nodes,
        "message": expected_messages,
        "group_thread": expected_group_threads,
        "group_message": expected_group_messages,
        "warning": expected_warnings,
    }
    for kind, query in count_queries.items():
        actual = database.execute(query, (snapshot_id,)).fetchone()[0]
        if actual != expected[kind]:
            raise ChatGPTImportError(
                f"existing snapshot database count changed: {kind} expected {expected[kind]}, got {actual}"
            )
    _verify_snapshot_invariants(database, snapshot_id=snapshot_id)
    for row in database.execute(
        "SELECT raw_sha256, raw_json FROM source_records WHERE snapshot_id = ?",
        (snapshot_id,),
    ):
        try:
            raw = json.loads(row["raw_json"])
        except json.JSONDecodeError as error:
            raise ChatGPTImportError("stored source-record JSON is invalid") from error
        if _json_hash(raw) != row["raw_sha256"]:
            raise ChatGPTImportError("stored source-record JSON hash changed")
    raw_tables = (
        (
            "conversation_versions",
            "conversation_observations",
            "conversation_version_id",
        ),
        ("node_versions", "node_observations", "node_version_id"),
        ("message_versions", "message_observations", "message_version_id"),
        (
            "group_thread_versions",
            "group_thread_observations",
            "group_thread_version_id",
        ),
        (
            "group_message_versions",
            "group_message_observations",
            "group_message_version_id",
        ),
    )
    for table, observation_table, version_column in raw_tables:
        for row in database.execute(
            f"""SELECT DISTINCT v.raw_sha256, v.raw_json
                  FROM {table} v
                  JOIN {observation_table} o ON o.{version_column} = v.id
                 WHERE o.snapshot_id = ?""",
            (snapshot_id,),
        ):
            try:
                raw = json.loads(row["raw_json"])
            except json.JSONDecodeError as error:
                raise ChatGPTImportError(f"stored raw JSON is invalid in {table}") from error
            if _json_hash(raw) != row["raw_sha256"]:
                raise ChatGPTImportError(f"stored raw JSON hash changed in {table}")
    fts_rows = database.execute(
        """SELECT mv.raw_json, sd.content
             FROM message_observations mo
             JOIN message_versions mv ON mv.id = mo.message_version_id
             LEFT JOIN search_documents sd ON sd.message_version_id = mv.id
            WHERE mo.snapshot_id = ?""",
        (snapshot_id,),
    ).fetchall()
    if len(fts_rows) != expected_messages:
        raise ChatGPTImportError("existing snapshot FTS row count changed")
    for row in fts_rows:
        if row["content"] is None or _message_text(json.loads(row["raw_json"])) != row["content"]:
            raise ChatGPTImportError("existing snapshot FTS content changed")
    group_fts_rows = database.execute(
        """SELECT gv.raw_json, sd.content
             FROM group_message_observations go
             JOIN group_message_versions gv ON gv.id = go.group_message_version_id
             LEFT JOIN search_documents sd
               ON sd.group_message_version_id = gv.id
            WHERE go.snapshot_id = ?""",
        (snapshot_id,),
    ).fetchall()
    if len(group_fts_rows) != expected_group_messages:
        raise ChatGPTImportError("existing snapshot group FTS row count changed")
    for row in group_fts_rows:
        raw = json.loads(row["raw_json"])
        expected_text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        if row["content"] is None or row["content"] != expected_text:
            raise ChatGPTImportError("existing snapshot group FTS content changed")


def _verify_snapshot_invariants(database: Any, *, snapshot_id: int) -> None:
    invariant_queries = {
        "import run cardinality": """SELECT CASE WHEN count(*) = 1 THEN 0 ELSE 1 END
             FROM import_runs WHERE snapshot_id = ?""",
        "conversation links": """SELECT count(*)
             FROM conversation_observations co
             JOIN snapshots s ON s.id = co.snapshot_id
             JOIN conversation_identities ci ON ci.id = co.conversation_identity_id
             JOIN conversation_versions cv ON cv.id = co.conversation_version_id
             JOIN source_records sr ON sr.id = co.source_record_id
             LEFT JOIN node_identities cni
               ON cni.identity_key = co.current_node_identity_key
             LEFT JOIN node_observations cno
               ON cno.snapshot_id = co.snapshot_id AND cno.node_identity_id = cni.id
            WHERE co.snapshot_id = ? AND (
                  ci.source_id <> s.source_id
               OR cv.conversation_identity_id <> co.conversation_identity_id
               OR sr.snapshot_id <> co.snapshot_id
               OR cni.id IS NULL
               OR cni.conversation_identity_id <> co.conversation_identity_id
               OR cno.id IS NULL)""",
        "node links": """SELECT count(*)
             FROM node_observations no
             JOIN node_identities ni ON ni.id = no.node_identity_id
             JOIN node_versions nv ON nv.id = no.node_version_id
             JOIN source_records sr ON sr.id = no.source_record_id
             LEFT JOIN conversation_observations co
               ON co.snapshot_id = no.snapshot_id
              AND co.conversation_identity_id = ni.conversation_identity_id
             LEFT JOIN node_identities pi ON pi.id = no.parent_node_identity_id
             LEFT JOIN message_identities mi ON mi.id = no.message_identity_id
            WHERE no.snapshot_id = ? AND (
                  nv.node_identity_id <> no.node_identity_id
               OR sr.snapshot_id <> no.snapshot_id
               OR co.id IS NULL
               OR (pi.id IS NOT NULL AND pi.conversation_identity_id <> ni.conversation_identity_id)
               OR (mi.id IS NOT NULL AND mi.conversation_identity_id <> ni.conversation_identity_id))""",
        "message links": """SELECT count(*)
             FROM message_observations mo
             JOIN message_identities mi ON mi.id = mo.message_identity_id
             JOIN message_versions mv ON mv.id = mo.message_version_id
             JOIN node_observations no ON no.id = mo.node_observation_id
             JOIN source_records sr ON sr.id = mo.source_record_id
            WHERE mo.snapshot_id = ? AND (
                  mv.message_identity_id <> mo.message_identity_id
               OR no.snapshot_id <> mo.snapshot_id
               OR no.message_identity_id <> mo.message_identity_id
               OR sr.snapshot_id <> mo.snapshot_id)""",
        "current branch links": """SELECT count(*)
             FROM current_branch_nodes cb
             JOIN conversation_observations co ON co.id = cb.conversation_observation_id
             JOIN node_identities ni ON ni.id = cb.node_identity_id
             LEFT JOIN node_observations no
               ON no.snapshot_id = cb.snapshot_id AND no.node_identity_id = cb.node_identity_id
            WHERE cb.snapshot_id = ? AND (
                  co.snapshot_id <> cb.snapshot_id
               OR co.conversation_identity_id <> cb.conversation_identity_id
               OR ni.conversation_identity_id <> cb.conversation_identity_id
               OR no.id IS NULL)""",
        "current branch depth": """SELECT count(*) FROM (
             SELECT conversation_identity_id
               FROM current_branch_nodes
              WHERE snapshot_id = ?
              GROUP BY conversation_identity_id
             HAVING min(depth) <> 0 OR max(depth) + 1 <> count(*)
        )""",
        "current branch parent order": """SELECT count(*)
             FROM current_branch_nodes cb
             JOIN current_branch_nodes prior
               ON prior.snapshot_id = cb.snapshot_id
              AND prior.conversation_identity_id = cb.conversation_identity_id
              AND prior.depth = cb.depth - 1
             JOIN node_observations no
               ON no.snapshot_id = cb.snapshot_id AND no.node_identity_id = cb.node_identity_id
            WHERE cb.snapshot_id = ? AND cb.depth > 0
              AND no.parent_node_identity_id <> prior.node_identity_id""",
        "current branch tip": """SELECT count(*)
             FROM conversation_observations co
             LEFT JOIN current_branch_nodes cb
               ON cb.snapshot_id = co.snapshot_id
              AND cb.conversation_identity_id = co.conversation_identity_id
              AND cb.depth = (
                    SELECT max(x.depth) FROM current_branch_nodes x
                     WHERE x.snapshot_id = co.snapshot_id
                       AND x.conversation_identity_id = co.conversation_identity_id)
             LEFT JOIN node_identities ni ON ni.id = cb.node_identity_id
            WHERE co.snapshot_id = ?
              AND (ni.identity_key IS NULL OR ni.identity_key <> co.current_node_identity_key)""",
        "message asset references": """SELECT count(*)
             FROM message_asset_refs r
             JOIN message_observations mo ON mo.id = r.message_observation_id
             LEFT JOIN asset_observations ao ON ao.id = r.asset_observation_id
            WHERE r.snapshot_id = ? AND (
                  mo.snapshot_id <> r.snapshot_id
               OR (ao.id IS NOT NULL AND ao.snapshot_id <> r.snapshot_id)
               OR (r.resolution_status = 'resolved'
                   AND (r.asset_observation_id IS NULL OR r.candidate_count <> 1))
               OR (r.resolution_status = 'unresolved'
                   AND (r.asset_observation_id IS NOT NULL OR r.candidate_count <> 0))
               OR (r.resolution_status = 'ambiguous'
                   AND (r.asset_observation_id IS NOT NULL OR r.candidate_count < 2)))""",
        "group thread links": """SELECT count(*)
             FROM group_thread_observations go
             JOIN group_thread_identities gi ON gi.id = go.group_thread_identity_id
             JOIN group_thread_versions gv ON gv.id = go.group_thread_version_id
             JOIN snapshots s ON s.id = go.snapshot_id
             JOIN source_records sr ON sr.id = go.source_record_id
            WHERE go.snapshot_id = ? AND (
                  gi.source_id <> s.source_id
               OR gv.group_thread_identity_id <> go.group_thread_identity_id
               OR sr.snapshot_id <> go.snapshot_id)""",
        "group message links": """SELECT count(*)
             FROM group_message_observations go
             JOIN group_message_identities gi ON gi.id = go.group_message_identity_id
             JOIN group_message_versions gv ON gv.id = go.group_message_version_id
             JOIN group_thread_observations gt ON gt.id = go.group_thread_observation_id
             JOIN source_records sr ON sr.id = go.source_record_id
            WHERE go.snapshot_id = ? AND (
                  gv.group_message_identity_id <> go.group_message_identity_id
               OR gt.snapshot_id <> go.snapshot_id
               OR gt.group_thread_identity_id <> gi.group_thread_identity_id
               OR sr.snapshot_id <> go.snapshot_id)""",
        "source file coverage": """SELECT count(*)
             FROM source_file_coverage c
            WHERE c.snapshot_id = ? AND (
                  (c.media_class = 'json' AND (
                       c.handling_status <> 'decoded'
                    OR c.source_record_count <> (
                         SELECT count(*) FROM source_records sr
                          WHERE sr.snapshot_id = c.snapshot_id
                            AND sr.source_file_path = c.evidence_path)
                    OR 1 <> (SELECT count(*) FROM source_records sr
                              WHERE sr.snapshot_id = c.snapshot_id
                                AND sr.source_file_path = c.evidence_path
                                AND sr.record_scope = 'file_root')))
               OR (c.media_class = 'dat' AND (
                       c.handling_status <> 'cas_verified'
                    OR c.asset_observation_count <> 1)))""",
    }
    for name, query in invariant_queries.items():
        violations = int(database.execute(query, (snapshot_id,)).fetchone()[0])
        if violations:
            raise ChatGPTImportError(
                f"snapshot invariant failed: {name} ({violations} violation(s))"
            )
    claim_rows = {
        row["claim"]: row["status"]
        for row in database.execute(
            "SELECT claim, status FROM snapshot_claims WHERE snapshot_id = ?",
            (snapshot_id,),
        )
    }
    expected_claims = {
        "source_complete": "pass",
        "extraction_complete": "pass",
        "reconciliation_complete": "partial",
        "account_complete": "not_applicable",
    }
    if claim_rows != expected_claims:
        raise ChatGPTImportError("snapshot completeness claims changed or are incomplete")


def _stored_path(path: Path, output_root: Path) -> str:
    try:
        return path.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _insert_physical_assets(
    database: Any,
    *,
    manifest: dict[str, Any],
    evidence_root: Path,
    output_root: Path,
    snapshot_id: int,
) -> _AssetResolver:
    friendly_names = _load_asset_friendly_names(evidence_root)
    observations: dict[str, int] = {}
    for entry in manifest["entries"]:
        evidence_path = entry.get("path")
        if (
            entry.get("kind") != "file"
            or not isinstance(evidence_path, str)
            or not evidence_path.endswith(".dat")
        ):
            continue
        sha256 = entry.get("sha256")
        size_bytes = entry.get("size")
        if not isinstance(sha256, str) or not isinstance(size_bytes, int):
            raise ChatGPTImportError(f"asset manifest entry is incomplete: {evidence_path}")
        storage_path = _copy_to_content_store(
            source=evidence_root / evidence_path,
            output_root=output_root,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        database.execute(
            """INSERT INTO assets (sha256, size_bytes, storage_path)
                 VALUES (?, ?, ?) ON CONFLICT(sha256) DO NOTHING""",
            (sha256, size_bytes, storage_path),
        )
        asset_row = database.execute(
            "SELECT id, size_bytes, storage_path FROM assets WHERE sha256 = ?", (sha256,)
        ).fetchone()
        assert asset_row is not None
        if asset_row["size_bytes"] != size_bytes or asset_row["storage_path"] != storage_path:
            raise ChatGPTImportError(f"content asset identity conflict: {sha256}")
        cursor = database.execute(
            """INSERT INTO asset_observations
                   (snapshot_id, asset_id, evidence_path, sha256, size_bytes,
                    declared_name, observation_kind, raw_json)
                 VALUES (?, ?, ?, ?, ?, ?, 'physical_export_file', ?)""",
            (
                snapshot_id,
                asset_row["id"],
                evidence_path,
                sha256,
                size_bytes,
                friendly_names.get(evidence_path)
                or friendly_names.get(evidence_path[:-4]),
                _canonical_json(entry),
            ),
        )
        observations[evidence_path] = int(cursor.lastrowid)
    return _AssetResolver(observations)


def _insert_source_file_coverage(
    database: Any,
    *,
    manifest: dict[str, Any],
    snapshot_id: int,
) -> None:
    for entry in manifest["entries"]:
        evidence_path = entry.get("path")
        if entry.get("kind") != "file" or not isinstance(evidence_path, str):
            continue
        suffix = Path(evidence_path).suffix.lower()
        if suffix == ".json":
            media_class = "json"
            handling_status = "decoded"
        elif suffix == ".dat":
            media_class = "dat"
            handling_status = "cas_verified"
        elif suffix in {".html", ".htm"}:
            media_class = "html"
            handling_status = "preserved_opaque"
        else:
            media_class = "other"
            handling_status = "preserved_opaque"
        source_record_count = int(
            database.execute(
                """SELECT count(*) FROM source_records
                     WHERE snapshot_id = ? AND source_file_path = ?""",
                (snapshot_id, evidence_path),
            ).fetchone()[0]
        )
        root_records = int(
            database.execute(
                """SELECT count(*) FROM source_records
                     WHERE snapshot_id = ? AND source_file_path = ?
                       AND record_scope = 'file_root' AND json_pointer = ''""",
                (snapshot_id, evidence_path),
            ).fetchone()[0]
        )
        element_records = int(
            database.execute(
                """SELECT count(*) FROM source_records
                     WHERE snapshot_id = ? AND source_file_path = ?
                       AND record_scope = 'element'""",
                (snapshot_id, evidence_path),
            ).fetchone()[0]
        )
        asset_observation_count = int(
            database.execute(
                """SELECT count(*) FROM asset_observations
                     WHERE snapshot_id = ? AND evidence_path = ?
                       AND observation_kind = 'physical_export_file'""",
                (snapshot_id, evidence_path),
            ).fetchone()[0]
        )
        if media_class == "json" and root_records != 1:
            raise ChatGPTImportError(
                f"JSON file does not have exactly one decoded root record: {evidence_path}"
            )
        if media_class == "json":
            root_raw = database.execute(
                """SELECT raw_json FROM source_records
                     WHERE snapshot_id = ? AND source_file_path = ?
                       AND record_scope = 'file_root'""",
                (snapshot_id, evidence_path),
            ).fetchone()[0]
            root_value = json.loads(root_raw)
            root_descriptor = (
                root_value.get("$provider_json_file_root")
                if isinstance(root_value, dict)
                else None
            )
            if isinstance(root_descriptor, dict) and root_descriptor.get("json_type") == "array":
                expected_elements = root_descriptor.get("element_count")
                if not isinstance(expected_elements, int) or expected_elements != element_records:
                    raise ChatGPTImportError(
                        f"JSON array element records are incomplete: {evidence_path}"
                    )
        if media_class == "dat" and asset_observation_count != 1:
            raise ChatGPTImportError(
                f"DAT file does not have exactly one verified CAS observation: {evidence_path}"
            )
        database.execute(
            """INSERT INTO source_file_coverage
                   (snapshot_id, evidence_path, evidence_kind, size_bytes, sha256,
                    media_class, handling_status, source_record_count,
                    asset_observation_count, details_json)
                 VALUES (?, ?, 'file', ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                evidence_path,
                entry["size"],
                entry["sha256"],
                media_class,
                handling_status,
                source_record_count,
                asset_observation_count,
                _canonical_json(
                    {
                        "json_root_records": root_records,
                        "json_element_records": element_records,
                        "opaque_reason": "provider-rendered HTML retained as evidence"
                        if media_class == "html"
                        else (
                            "unparsed provider artifact retained as evidence"
                            if media_class == "other"
                            else None
                        ),
                    }
                ),
            ),
        )


def _build_snapshot_claims(
    database: Any,
    *,
    manifest: dict[str, Any],
    snapshot_id: int,
    conversations: int,
    nodes: int,
    messages: int,
    group_threads: int,
    group_messages: int,
    unresolved: int,
) -> dict[str, tuple[str, dict[str, Any]]]:
    manifest_files = tuple(
        entry for entry in manifest["entries"] if entry.get("kind") == "file"
    )
    manifest_bytes = sum(int(entry["size"]) for entry in manifest_files)
    coverage = database.execute(
        """SELECT count(*) AS files, coalesce(sum(size_bytes), 0) AS bytes,
                  sum(media_class = 'json') AS json_files,
                  sum(media_class = 'dat') AS dat_files,
                  sum(handling_status = 'cas_verified') AS cas_verified_files,
                  sum(handling_status = 'preserved_opaque') AS opaque_preserved_files
             FROM source_file_coverage WHERE snapshot_id = ?""",
        (snapshot_id,),
    ).fetchone()
    json_root_records = int(
        database.execute(
            """SELECT count(*) FROM source_records
                 WHERE snapshot_id = ? AND record_scope = 'file_root'""",
            (snapshot_id,),
        ).fetchone()[0]
    )
    covered_files = int(coverage["files"] or 0)
    covered_bytes = int(coverage["bytes"] or 0)
    json_files = int(coverage["json_files"] or 0)
    dat_files = int(coverage["dat_files"] or 0)
    cas_verified_files = int(coverage["cas_verified_files"] or 0)
    opaque_preserved_files = int(coverage["opaque_preserved_files"] or 0)
    source_closed = covered_files == len(manifest_files) and covered_bytes == manifest_bytes
    extraction_closed = (
        source_closed
        and json_root_records == json_files
        and cas_verified_files == dat_files
    )
    return {
        "source_complete": (
            "pass" if source_closed else "fail",
            {
                "evidence_tree_sha256": manifest["tree_sha256"],
                "manifest_files": len(manifest_files),
                "covered_files": covered_files,
                "manifest_bytes": manifest_bytes,
                "covered_bytes": covered_bytes,
                "provider_inventory_status": "validated"
                if any(entry.get("path") == "export_manifest.json" for entry in manifest_files)
                else "not_present",
            },
        ),
        "extraction_complete": (
            "pass" if extraction_closed else "fail",
            {
                "json_files": json_files,
                "json_root_records": json_root_records,
                "dat_files": dat_files,
                "cas_verified_files": cas_verified_files,
                "opaque_preserved_files": opaque_preserved_files,
                "conversations": conversations,
                "nodes": nodes,
                "messages": messages,
                "group_threads": group_threads,
                "group_messages": group_messages,
                "explicit_failures": 0 if extraction_closed else 1,
            },
        ),
        "reconciliation_complete": (
            "partial",
            {
                "unresolved_or_ambiguous_findings": unresolved,
                "reason": "independent full reconciliation report has not been produced",
            },
        ),
        "account_complete": (
            "not_applicable",
            {"reason": "available snapshots cannot prove historical account completeness"},
        ),
    }


def _extract_message_asset_references(
    message: dict[str, Any],
) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    references: list[tuple[str, str, dict[str, Any]]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                item_path = f"{path}.{key}"
                if key.endswith("asset_pointer"):
                    if isinstance(item, str):
                        references.append((key, item, {"path": item_path, "value": item}))
                        continue
                    if isinstance(item, dict) and isinstance(item.get("asset_pointer"), str):
                        references.append(
                            (key, item["asset_pointer"], {"path": item_path, "value": item})
                        )
                        continue
                if key == "frames_asset_pointers" and isinstance(item, list):
                    for index, pointer in enumerate(item):
                        if isinstance(pointer, str):
                            references.append(
                                (
                                    "frames_asset_pointer",
                                    pointer,
                                    {"path": f"{item_path}[{index}]", "value": pointer},
                                )
                            )
                    continue
                if key == "asset_pointer_links" and isinstance(item, list):
                    for index, pointer in enumerate(item):
                        if isinstance(pointer, str):
                            references.append(
                                (
                                    "asset_pointer_link",
                                    pointer,
                                    {"path": f"{item_path}[{index}]", "value": pointer},
                                )
                            )
                    continue
                walk(item, item_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(message.get("content"), "content")
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        walk(metadata.get("content_references"), "metadata.content_references")
    attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
    if isinstance(attachments, list):
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            key = "file_id" if isinstance(attachment.get("file_id"), str) else "id"
            reference = attachment.get(key)
            if isinstance(reference, str):
                references.append(
                    (
                        f"attachment_{key}",
                        reference,
                        {"path": f"metadata.attachments[{index}]", "value": attachment},
                    )
                )
    return tuple(references)


def _record_diagnostic(
    database: Any,
    *,
    import_run_id: str,
    snapshot_id: int,
    conversation_identity_id: int | None,
    code: str,
    details: dict[str, Any],
) -> None:
    database.execute(
        """INSERT INTO diagnostics
               (import_run_id, snapshot_id, conversation_identity_id,
                severity, code, details_json)
             VALUES (?, ?, ?, 'warning', ?, ?)""",
        (
            import_run_id,
            snapshot_id,
            conversation_identity_id,
            code,
            _canonical_json(details),
        ),
    )


def _load_library_records(evidence_root: Path) -> tuple[dict[str, Any], ...]:
    path = evidence_root / "library_files.json"
    if not path.is_file():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChatGPTImportError(f"cannot read library_files.json: {error}") from error
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ChatGPTImportError("library_files.json must be an array of objects")
    return tuple(raw)


def _resolve_library_source_reference(
    database: Any,
    *,
    snapshot_id: int,
    reference_kind: str,
    target_native_id: str,
    related_thread_native_id: str | None,
) -> tuple[str, str | None]:
    if reference_kind in {"origination_thread_id", "initiating_conversation_id"}:
        rows = database.execute(
            """SELECT ci.identity_key
                 FROM conversation_observations cv
                 JOIN conversation_identities ci
                   ON ci.id = cv.conversation_identity_id
                WHERE cv.snapshot_id = ? AND ci.native_id = ?""",
            (snapshot_id, target_native_id),
        ).fetchall()
    elif related_thread_native_id is not None:
        rows = database.execute(
            """SELECT mi.identity_key
                 FROM message_observations mv
                 JOIN message_identities mi ON mi.id = mv.message_identity_id
                 JOIN conversation_identities ci
                   ON ci.id = mi.conversation_identity_id
                WHERE mv.snapshot_id = ? AND mi.native_id = ? AND ci.native_id = ?""",
            (snapshot_id, target_native_id, related_thread_native_id),
        ).fetchall()
    else:
        rows = database.execute(
            """SELECT mi.identity_key
                 FROM message_observations mv
                 JOIN message_identities mi ON mi.id = mv.message_identity_id
                WHERE mv.snapshot_id = ? AND mi.native_id = ?""",
            (snapshot_id, target_native_id),
        ).fetchall()
    if not rows:
        return "unresolved", None
    if len(rows) > 1:
        return "ambiguous", None
    return "resolved", str(rows[0][0])


def _insert_library_records(
    database: Any,
    *,
    evidence_root: Path,
    snapshot_id: int,
    import_run_id: str,
    asset_resolver: _AssetResolver,
) -> int:
    warnings = 0
    for array_index, raw in enumerate(_load_library_records(evidence_root)):
        file_id = raw.get("file_id")
        library_reference = file_id if isinstance(file_id, str) else f"row-{array_index}"
        resolution = (
            asset_resolver.resolve(file_id)
            if isinstance(file_id, str)
            else _AssetResolution(None, "unresolved", None, None, ())
        )
        database.execute(
            """INSERT INTO library_asset_refs
                   (snapshot_id, evidence_path, declared_name, asset_observation_id,
                    resolution_status, resolution_method, candidate_count,
                    candidates_json, raw_json)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                library_reference,
                raw.get("file_name"),
                resolution.observation_id,
                resolution.status,
                resolution.method,
                len(resolution.candidates),
                _canonical_json(resolution.candidates),
                _canonical_json(raw),
            ),
        )
        if resolution.status != "resolved":
            warnings += 1
            _record_diagnostic(
                database,
                import_run_id=import_run_id,
                snapshot_id=snapshot_id,
                conversation_identity_id=None,
                code=f"library_asset_{resolution.status}",
                details={
                    "library_file_reference": library_reference,
                    "normalized_reference": resolution.normalized,
                    "state": raw.get("state"),
                    "candidates": resolution.candidates,
                },
            )

        related_thread = raw.get("origination_thread_id")
        if not isinstance(related_thread, str):
            related_thread = None
        for reference_kind in (
            "origination_thread_id",
            "initiating_conversation_id",
            "origination_message_id",
        ):
            target = raw.get(reference_kind)
            if not isinstance(target, str) or not target:
                continue
            status, resolved_identity_key = _resolve_library_source_reference(
                database,
                snapshot_id=snapshot_id,
                reference_kind=reference_kind,
                target_native_id=target,
                related_thread_native_id=related_thread,
            )
            database.execute(
                """INSERT INTO library_source_refs
                       (snapshot_id, library_file_reference, reference_kind,
                        target_native_id, related_thread_native_id, resolution_status,
                        resolved_identity_key, raw_json)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    library_reference,
                    reference_kind,
                    target,
                    related_thread,
                    status,
                    resolved_identity_key,
                    _canonical_json(
                        {
                            "reference_kind": reference_kind,
                            "target_native_id": target,
                            "related_thread_native_id": related_thread,
                        }
                    ),
                ),
            )
            if status != "resolved":
                warnings += 1
                code_subject = "message" if reference_kind == "origination_message_id" else "thread"
                _record_diagnostic(
                    database,
                    import_run_id=import_run_id,
                    snapshot_id=snapshot_id,
                    conversation_identity_id=None,
                    code=f"library_{code_subject}_reference_{status}",
                    details={
                        "library_file_reference": library_reference,
                        "reference_kind": reference_kind,
                        "related_thread_native_id": related_thread,
                    },
                )
    return warnings


def _insert_identity(
    database: Any,
    *,
    table: str,
    identity_key: str,
    native_id: str,
    source_id: int | None = None,
    conversation_identity_id: int | None = None,
) -> int:
    if table == "conversation_identities":
        database.execute(
            """INSERT OR IGNORE INTO conversation_identities
                   (source_id, identity_key, native_id) VALUES (?, ?, ?)""",
            (source_id, identity_key, native_id),
        )
    else:
        database.execute(
            f"""INSERT OR IGNORE INTO {table}
                    (conversation_identity_id, identity_key, native_id)
                  VALUES (?, ?, ?)""",
            (conversation_identity_id, identity_key, native_id),
        )
    row = database.execute(
        f"SELECT * FROM {table} WHERE identity_key = ?", (identity_key,)
    ).fetchone()
    assert row is not None
    if table == "conversation_identities":
        if row["source_id"] != source_id or row["native_id"] != native_id:
            raise ChatGPTImportError("conversation identity conflict")
    elif (
        row["conversation_identity_id"] != conversation_identity_id
        or row["native_id"] != native_id
    ):
        raise ChatGPTImportError(f"{table} identity conflict")
    return int(row["id"])


def _insert_source_records(
    database: Any, *, snapshot_id: int, source_records_path: Path
) -> dict[tuple[str, str], int]:
    records: dict[tuple[str, str], int] = {}
    with source_records_path.open("r", encoding="utf-8") as source_records:
        for line in source_records:
            envelope = json.loads(line)
            provenance = envelope["provenance"]
            cursor = database.execute(
                """INSERT INTO source_records
                       (snapshot_id, record_kind, provider_record_kind, record_scope,
                        native_id, source_file_path,
                        array_index, json_pointer, raw_sha256, raw_json,
                        ndjson_line_number)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    envelope["record_kind"],
                    envelope["provider_record_kind"],
                    envelope["record_scope"],
                    envelope.get("native_id"),
                    provenance["source_file"],
                    provenance.get("array_index"),
                    provenance["json_pointer"],
                    envelope["raw_sha256"],
                    _canonical_json(envelope["raw"]),
                    provenance["line_number"],
                ),
            )
            records[(provenance["source_file"], provenance["json_pointer"])] = int(
                cursor.lastrowid
            )
    return records


def _json_pointer_component(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _insert_conversation(
    database: Any,
    *,
    envelope: dict[str, Any],
    source_id: int,
    snapshot_id: int,
    identity_scope: str,
    import_run_id: str,
    asset_resolver: _AssetResolver,
    source_records: dict[tuple[str, str], int],
) -> int:
    raw = envelope["raw"]
    validated = _validate_conversation(
        raw, envelope["provenance"]["source_file"], envelope["provenance"]["array_index"]
    )
    prefix = _identity_prefix(identity_scope, validated.native_id)
    conversation_identity_id = _insert_identity(
        database,
        table="conversation_identities",
        identity_key=prefix,
        native_id=validated.native_id,
        source_id=source_id,
    )
    source_file = envelope["provenance"]["source_file"]
    array_index = envelope["provenance"]["array_index"]
    source_record_id = source_records[(source_file, f"/{array_index}")]

    node_ids: dict[str, int] = {}
    message_ids: dict[str, int] = {}
    mapping = raw["mapping"]
    for node_native_id, node in mapping.items():
        node_key = f"{prefix}/nodes/{node_native_id}"
        node_ids[node_native_id] = _insert_identity(
            database,
            table="node_identities",
            identity_key=node_key,
            native_id=node_native_id,
            conversation_identity_id=conversation_identity_id,
        )
        message = node.get("message")
        if message is not None:
            message_key = f"{prefix}/messages/{message['id']}"
            message_ids[message["id"]] = _insert_identity(
                database,
                table="message_identities",
                identity_key=message_key,
                native_id=message["id"],
                conversation_identity_id=conversation_identity_id,
            )

    conversation_raw_json = _canonical_json(raw)
    database.execute(
        """INSERT INTO conversation_versions
               (conversation_identity_id, raw_sha256, raw_json, unknown_json,
                title, create_time, update_time)
             VALUES (?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT(conversation_identity_id, raw_sha256) DO NOTHING""",
        (
            conversation_identity_id,
            envelope["raw_sha256"],
            conversation_raw_json,
            _unknown(raw, CONVERSATION_KNOWN_FIELDS),
            raw.get("title"),
            raw.get("create_time"),
            raw.get("update_time"),
        ),
    )
    conversation_version = database.execute(
        """SELECT id, raw_json FROM conversation_versions
             WHERE conversation_identity_id = ? AND raw_sha256 = ?""",
        (conversation_identity_id, envelope["raw_sha256"]),
    ).fetchone()
    assert conversation_version is not None
    if conversation_version["raw_json"] != conversation_raw_json:
        raise ChatGPTImportError("conversation version hash collision")
    conversation_cursor = database.execute(
        """INSERT INTO conversation_observations
               (snapshot_id, conversation_identity_id, conversation_version_id,
                source_record_id, current_node_identity_key)
             VALUES (?, ?, ?, ?, ?)""",
        (
            snapshot_id,
            conversation_identity_id,
            conversation_version["id"],
            source_record_id,
            f"{prefix}/nodes/{validated.current_node}",
        ),
    )
    conversation_observation_id = int(conversation_cursor.lastrowid)

    for node_native_id, node in mapping.items():
        message = node.get("message")
        message_identity_id = None if message is None else message_ids[message["id"]]
        parent = node.get("parent")
        node_raw_json = _canonical_json(node)
        node_raw_sha256 = _json_hash(node)
        database.execute(
            """INSERT INTO node_versions
                   (node_identity_id, raw_sha256, raw_json, unknown_json,
                    declared_children_json)
                 VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(node_identity_id, raw_sha256) DO NOTHING""",
            (
                node_ids[node_native_id],
                node_raw_sha256,
                node_raw_json,
                _unknown(node, NODE_KNOWN_FIELDS),
                _canonical_json(node["children"]) if "children" in node else None,
            ),
        )
        node_version = database.execute(
            """SELECT id, raw_json FROM node_versions
                 WHERE node_identity_id = ? AND raw_sha256 = ?""",
            (node_ids[node_native_id], node_raw_sha256),
        ).fetchone()
        assert node_version is not None
        if node_version["raw_json"] != node_raw_json:
            raise ChatGPTImportError("node version hash collision")
        node_pointer = (
            f"/{array_index}/mapping/{_json_pointer_component(node_native_id)}"
        )
        node_cursor = database.execute(
            """INSERT INTO node_observations
                   (snapshot_id, node_identity_id, node_version_id, source_record_id,
                    json_pointer, parent_node_identity_id, message_identity_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                node_ids[node_native_id],
                node_version["id"],
                source_record_id,
                node_pointer,
                None if parent is None else node_ids[parent],
                message_identity_id,
            ),
        )
        node_observation_id = int(node_cursor.lastrowid)
        if message is not None:
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            message_raw_json = _canonical_json(message)
            message_raw_sha256 = _json_hash(message)
            database.execute(
                """INSERT INTO message_versions
                       (message_identity_id, raw_sha256,
                        raw_json, unknown_json, author_role, author_name, content_type,
                        create_time, update_time, status, recipient)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(message_identity_id, raw_sha256) DO NOTHING""",
                (
                    message_identity_id,
                    message_raw_sha256,
                    message_raw_json,
                    _unknown(message, MESSAGE_KNOWN_FIELDS),
                    author.get("role"),
                    author.get("name"),
                    content.get("content_type"),
                    message.get("create_time"),
                    message.get("update_time"),
                    message.get("status"),
                    message.get("recipient"),
                ),
            )
            message_version = database.execute(
                """SELECT id, raw_json FROM message_versions
                     WHERE message_identity_id = ? AND raw_sha256 = ?""",
                (message_identity_id, message_raw_sha256),
            ).fetchone()
            assert message_version is not None
            if message_version["raw_json"] != message_raw_json:
                raise ChatGPTImportError("message version hash collision")
            message_version_id = int(message_version["id"])
            message_pointer = f"{node_pointer}/message"
            message_cursor = database.execute(
                """INSERT INTO message_observations
                       (snapshot_id, message_identity_id, message_version_id,
                        node_observation_id, source_record_id, json_pointer)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    message_identity_id,
                    message_version_id,
                    node_observation_id,
                    source_record_id,
                    message_pointer,
                ),
            )
            message_observation_id = int(message_cursor.lastrowid)
            database.execute(
                """INSERT INTO search_documents
                       (document_kind, message_version_id, group_message_version_id,
                        identity_key, author_role, content_type, content)
                     VALUES ('conversation_message', ?, NULL, ?, ?, ?, ?)
                     ON CONFLICT(message_version_id) DO NOTHING""",
                (
                    message_version_id,
                    f"{prefix}/messages/{message['id']}",
                    author.get("role"),
                    content.get("content_type"),
                    _message_text(message),
                ),
            )
            search_document = database.execute(
                """SELECT document_kind, identity_key, author_role, content_type, content
                     FROM search_documents
                     WHERE message_version_id = ?""",
                (message_version_id,),
            ).fetchone()
            assert search_document is not None
            if tuple(search_document) != (
                "conversation_message",
                f"{prefix}/messages/{message['id']}",
                author.get("role"),
                content.get("content_type"),
                _message_text(message),
            ):
                raise ChatGPTImportError("search document conflict")
            for ordinal, (kind, reference, raw_reference) in enumerate(
                _extract_message_asset_references(message)
            ):
                resolution = asset_resolver.resolve(reference)
                database.execute(
                    """INSERT INTO message_asset_refs
                           (snapshot_id, message_observation_id, ordinal, reference_kind,
                            reference_value, normalized_reference, asset_observation_id,
                            resolution_status, resolution_method, candidate_count,
                            candidates_json, raw_json)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        message_observation_id,
                        ordinal,
                        kind,
                        reference,
                        resolution.normalized,
                        resolution.observation_id,
                        resolution.status,
                        resolution.method,
                        len(resolution.candidates),
                        _canonical_json(resolution.candidates),
                        _canonical_json(raw_reference),
                    ),
                )
                if resolution.status != "resolved":
                    _record_diagnostic(
                        database,
                        import_run_id=import_run_id,
                        snapshot_id=snapshot_id,
                        conversation_identity_id=conversation_identity_id,
                        code=f"asset_reference_{resolution.status}",
                        details={
                            "message_id": message["id"],
                            "reference_kind": kind,
                            "normalized_reference": resolution.normalized,
                            "candidates": resolution.candidates,
                        },
                    )

    for depth, node_native_id in enumerate(validated.current_branch):
        database.execute(
            """INSERT INTO current_branch_nodes
                   (snapshot_id, conversation_identity_id, conversation_observation_id,
                    node_identity_id, depth)
                 VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                conversation_identity_id,
                conversation_observation_id,
                node_ids[node_native_id],
                depth,
            ),
        )
    for code, details in validated.warnings:
        _record_diagnostic(
            database,
            import_run_id=import_run_id,
            snapshot_id=snapshot_id,
            conversation_identity_id=conversation_identity_id,
            code=code,
            details=details,
        )
    asset_warnings = database.execute(
        """SELECT count(*) FROM diagnostics
             WHERE import_run_id = ? AND conversation_identity_id = ?
               AND code LIKE 'asset_reference_%'""",
        (import_run_id, conversation_identity_id),
    ).fetchone()[0]
    return int(asset_warnings)


def _record_source_absences(
    database: Any, *, source_id: int
) -> None:
    queries = {
        "conversation": """SELECT ci.identity_key FROM conversation_observations cv
                              JOIN conversation_identities ci
                                ON ci.id = cv.conversation_identity_id
                             WHERE cv.snapshot_id = ?""",
        "node": """SELECT ni.identity_key FROM node_observations nv
                      JOIN node_identities ni ON ni.id = nv.node_identity_id
                     WHERE nv.snapshot_id = ?""",
        "message": """SELECT mi.identity_key FROM message_observations mv
                         JOIN message_identities mi ON mi.id = mv.message_identity_id
                        WHERE mv.snapshot_id = ?""",
        "group_thread": """SELECT gi.identity_key FROM group_thread_observations go
                                JOIN group_thread_identities gi
                                  ON gi.id = go.group_thread_identity_id
                               WHERE go.snapshot_id = ?""",
        "group_message": """SELECT gi.identity_key FROM group_message_observations go
                                 JOIN group_message_identities gi
                                   ON gi.id = go.group_message_identity_id
                                WHERE go.snapshot_id = ?""",
        "asset": """SELECT 'asset:sha256:' || a.sha256 FROM asset_observations ao
                        JOIN assets a ON a.id = ao.asset_id
                       WHERE ao.snapshot_id = ?""",
    }
    database.execute("DELETE FROM source_absences WHERE source_id = ?", (source_id,))
    snapshots = tuple(
        int(row[0])
        for row in database.execute(
            """SELECT id FROM snapshots WHERE source_id = ?
                 ORDER BY captured_at_us, snapshot_key""",
            (source_id,),
        )
    )
    for kind, query in queries.items():
        last_present_snapshot: dict[str, int] = {}
        for snapshot_id in snapshots:
            current = {str(row[0]) for row in database.execute(query, (snapshot_id,))}
            database.executemany(
                """INSERT INTO source_absences
                       (source_id, snapshot_id, prior_snapshot_id, entity_kind, identity_key)
                     VALUES (?, ?, ?, ?, ?)""",
                (
                    (
                        source_id,
                        snapshot_id,
                        last_present_snapshot[identity_key],
                        kind,
                        identity_key,
                    )
                    for identity_key in sorted(last_present_snapshot.keys() - current)
                ),
            )
            for identity_key in current:
                last_present_snapshot[identity_key] = snapshot_id


def _load_group_chats(evidence_root: Path) -> tuple[dict[str, Any], ...]:
    path = evidence_root / "group_chats.json"
    if not path.is_file():
        return ()
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChatGPTImportError(f"cannot read group_chats.json: {error}") from error
    chats = root.get("chats") if isinstance(root, dict) else None
    if not isinstance(chats, list):
        raise ChatGPTImportError("group_chats.json must contain a chats array")
    seen_chat_ids: set[str] = set()
    for chat_index, chat in enumerate(chats):
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), str):
            raise ChatGPTImportError(f"group chat has no native id: chats[{chat_index}]")
        chat_id = chat["id"]
        if not chat_id or chat_id in seen_chat_ids:
            raise ChatGPTImportError(f"group chat native id is empty or duplicated: chats[{chat_index}]")
        seen_chat_ids.add(chat_id)
        messages = chat.get("messages")
        if not isinstance(messages, list):
            raise ChatGPTImportError(f"group chat messages are invalid: {chat_id}")
        seen_message_ids: set[str] = set()
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise ChatGPTImportError(
                    f"group message has no native id: {chat_id}/{message_index}"
                )
            message_id = message["id"]
            if not message_id or message_id in seen_message_ids:
                raise ChatGPTImportError(
                    f"group message native id is empty or duplicated: {chat_id}/{message_index}"
                )
            seen_message_ids.add(message_id)
            attachments = message.get("attachments")
            if not isinstance(attachments, list) or not all(
                isinstance(attachment, dict) for attachment in attachments
            ):
                raise ChatGPTImportError(f"group message attachments are invalid: {message_id}")
    return tuple(chats)


def _insert_group_chats(
    database: Any,
    *,
    evidence_root: Path,
    source_id: int,
    snapshot_id: int,
    identity_scope: str,
    import_run_id: str,
    source_records: dict[tuple[str, str], int],
    asset_resolver: _AssetResolver,
) -> tuple[int, int, int]:
    chats = _load_group_chats(evidence_root)
    if not chats:
        return 0, 0, 0
    source_record_id = source_records[("group_chats.json", "")]
    thread_count = message_count = warnings = 0
    for chat_index, chat in enumerate(chats):
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), str):
            raise ChatGPTImportError(f"group chat has no native id: chats[{chat_index}]")
        chat_id = chat["id"]
        thread_key = f"openai/chatgpt-official/{identity_scope}/group-chats/{chat_id}"
        database.execute(
            """INSERT INTO group_thread_identities
                   (source_id, identity_key, native_id) VALUES (?, ?, ?)
                 ON CONFLICT(identity_key) DO NOTHING""",
            (source_id, thread_key, chat_id),
        )
        thread_identity = database.execute(
            """SELECT id, source_id, native_id FROM group_thread_identities
                 WHERE identity_key = ?""",
            (thread_key,),
        ).fetchone()
        assert thread_identity is not None
        if thread_identity["source_id"] != source_id or thread_identity["native_id"] != chat_id:
            raise ChatGPTImportError("group thread identity conflict")
        thread_identity_id = int(thread_identity["id"])
        chat_raw_json = _canonical_json(chat)
        chat_hash = _json_hash(chat)
        database.execute(
            """INSERT INTO group_thread_versions
                   (group_thread_identity_id, raw_sha256, raw_json, unknown_json, name)
                 VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(group_thread_identity_id, raw_sha256) DO NOTHING""",
            (
                thread_identity_id,
                chat_hash,
                chat_raw_json,
                _unknown(
                    chat,
                    frozenset(
                        {
                            "id",
                            "name",
                            "assistant_name",
                            "created_at",
                            "updated_at",
                            "last_action_at",
                            "last_read_at",
                            "members",
                            "messages",
                            "should_auto_respond",
                            "workspace_id",
                        }
                    ),
                ),
                chat.get("name"),
            ),
        )
        thread_version = database.execute(
            """SELECT id, raw_json FROM group_thread_versions
                 WHERE group_thread_identity_id = ? AND raw_sha256 = ?""",
            (thread_identity_id, chat_hash),
        ).fetchone()
        assert thread_version is not None
        if thread_version["raw_json"] != chat_raw_json:
            raise ChatGPTImportError("group thread version hash collision")
        thread_observation_cursor = database.execute(
            """INSERT INTO group_thread_observations
                   (snapshot_id, group_thread_identity_id, group_thread_version_id,
                    source_record_id, json_pointer)
                 VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                thread_identity_id,
                thread_version["id"],
                source_record_id,
                f"/chats/{chat_index}",
            ),
        )
        thread_observation_id = int(thread_observation_cursor.lastrowid)
        messages = chat.get("messages")
        if not isinstance(messages, list):
            raise ChatGPTImportError(f"group chat messages are invalid: {chat_id}")
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise ChatGPTImportError(
                    f"group message has no native id: {chat_id}/{message_index}"
                )
            message_id = message["id"]
            message_key = f"{thread_key}/messages/{message_id}"
            database.execute(
                """INSERT INTO group_message_identities
                       (group_thread_identity_id, identity_key, native_id)
                     VALUES (?, ?, ?)
                     ON CONFLICT(identity_key) DO NOTHING""",
                (thread_identity_id, message_key, message_id),
            )
            message_identity = database.execute(
                """SELECT id, group_thread_identity_id, native_id
                     FROM group_message_identities WHERE identity_key = ?""",
                (message_key,),
            ).fetchone()
            assert message_identity is not None
            if (
                message_identity["group_thread_identity_id"] != thread_identity_id
                or message_identity["native_id"] != message_id
            ):
                raise ChatGPTImportError("group message identity conflict")
            message_identity_id = int(message_identity["id"])
            message_raw_json = _canonical_json(message)
            message_hash = _json_hash(message)
            database.execute(
                """INSERT INTO group_message_versions
                       (group_message_identity_id, raw_sha256, raw_json, unknown_json,
                        role, text, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(group_message_identity_id, raw_sha256) DO NOTHING""",
                (
                    message_identity_id,
                    message_hash,
                    message_raw_json,
                    _unknown(
                        message,
                        frozenset(
                            {"id", "role", "text", "created_at", "updated_at", "attachments"}
                        ),
                    ),
                    message.get("role"),
                    message.get("text"),
                    message.get("created_at"),
                ),
            )
            message_version = database.execute(
                """SELECT id, raw_json FROM group_message_versions
                     WHERE group_message_identity_id = ? AND raw_sha256 = ?""",
                (message_identity_id, message_hash),
            ).fetchone()
            assert message_version is not None
            if message_version["raw_json"] != message_raw_json:
                raise ChatGPTImportError("group message version hash collision")
            group_message_version_id = int(message_version["id"])
            group_text = message.get("text") if isinstance(message.get("text"), str) else ""
            database.execute(
                """INSERT INTO search_documents
                       (document_kind, message_version_id, group_message_version_id,
                        identity_key, author_role, content_type, content)
                     VALUES ('group_message', NULL, ?, ?, ?, 'group_text', ?)
                     ON CONFLICT(group_message_version_id) DO NOTHING""",
                (group_message_version_id, message_key, message.get("role"), group_text),
            )
            group_search_document = database.execute(
                """SELECT document_kind, identity_key, author_role, content_type, content
                     FROM search_documents WHERE group_message_version_id = ?""",
                (group_message_version_id,),
            ).fetchone()
            assert group_search_document is not None
            if tuple(group_search_document) != (
                "group_message",
                message_key,
                message.get("role"),
                "group_text",
                group_text,
            ):
                raise ChatGPTImportError("group search document conflict")
            message_observation_cursor = database.execute(
                """INSERT INTO group_message_observations
                       (snapshot_id, group_message_identity_id, group_message_version_id,
                        group_thread_observation_id, source_record_id, json_pointer)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    message_identity_id,
                    group_message_version_id,
                    thread_observation_id,
                    source_record_id,
                    f"/chats/{chat_index}/messages/{message_index}",
                ),
            )
            message_observation_id = int(message_observation_cursor.lastrowid)
            attachments = message.get("attachments")
            if not isinstance(attachments, list):
                raise ChatGPTImportError(f"group message attachments are invalid: {message_id}")
            for ordinal, attachment in enumerate(attachments):
                if not isinstance(attachment, dict):
                    raise ChatGPTImportError(
                        f"group message attachment is invalid: {message_id}/{ordinal}"
                    )
                reference = attachment.get("target_id")
                url = attachment.get("url")
                if isinstance(reference, str):
                    resolution = asset_resolver.resolve(reference)
                    status = resolution.status
                elif isinstance(url, str) and url.startswith(("https://", "http://")):
                    resolution = _AssetResolution(None, "external", None, None, ())
                    status = "external"
                    reference = url
                else:
                    resolution = _AssetResolution(None, "unresolved", None, None, ())
                    status = "unresolved"
                    reference = None
                database.execute(
                    """INSERT INTO group_message_asset_refs
                           (snapshot_id, group_message_observation_id, ordinal,
                            reference_value, resolution_status, asset_observation_id,
                            candidates_json, raw_json)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        message_observation_id,
                        ordinal,
                        reference,
                        status,
                        resolution.observation_id,
                        _canonical_json(resolution.candidates),
                        _canonical_json(attachment),
                    ),
                )
                if status in {"unresolved", "ambiguous"}:
                    warnings += 1
                    _record_diagnostic(
                        database,
                        import_run_id=import_run_id,
                        snapshot_id=snapshot_id,
                        conversation_identity_id=None,
                        code=f"group_asset_reference_{status}",
                        details={
                            "group_chat_id": chat_id,
                            "message_id": message_id,
                            "ordinal": ordinal,
                            "candidates": resolution.candidates,
                        },
                    )
            message_count += 1
        thread_count += 1
    return thread_count, message_count, warnings


def _captured_at_microseconds(value: str) -> int:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ChatGPTImportError(f"captured_at is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1_000_000)


def doctor_chatgpt_export(
    *, evidence_root: Path, manifest_path: Path
) -> ChatGPTDoctorResult:
    """Read and validate a Snapshot without creating vault artifacts."""

    evidence_root = evidence_root.expanduser().resolve(strict=True)
    manifest_path = manifest_path.expanduser().resolve(strict=True)
    try:
        manifest = load_manifest(manifest_path)
        verification = verify_manifest(evidence_root, manifest)
    except EvidenceError as error:
        raise ChatGPTImportError(f"cannot verify evidence manifest: {error}") from error
    if not verification.ok:
        raise ChatGPTImportError(
            "manifest verification failed: " + "; ".join(verification.differences)
        )
    _validate_provider_export_manifest(evidence_root, manifest)
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("kind") != "chatgpt_official_export":
        raise ChatGPTImportError("evidence manifest is not a ChatGPT official export")
    snapshot_key = source.get("id")
    if not isinstance(snapshot_key, str) or not snapshot_key:
        raise ChatGPTImportError("evidence manifest source id is not a valid snapshot key")
    _load_asset_friendly_names(evidence_root)
    _load_library_records(evidence_root)
    group_chats = _load_group_chats(evidence_root)
    group_threads = len(group_chats)
    group_messages = sum(len(chat["messages"]) for chat in group_chats)
    source_context = {
        "source_kind": "chatgpt_official_export",
        "snapshot_key": snapshot_key,
        "evidence_tree_sha256": manifest["tree_sha256"],
    }
    conversations, nodes, messages, warnings = _write_staging_ndjson(
        evidence_root=evidence_root,
        shard_names=_load_shards(evidence_root, manifest),
        temporary_file=None,
        source_context=source_context,
    )
    with tempfile.TemporaryFile(mode="w+b") as source_records_file:
        source_records = _write_source_records_ndjson(
            evidence_root=evidence_root,
            manifest=manifest,
            temporary_file=source_records_file,
            source_context=source_context,
        )
    physical_assets = sum(
        entry.get("kind") == "file"
        and isinstance(entry.get("path"), str)
        and entry["path"].endswith(".dat")
        for entry in manifest["entries"]
    )
    return ChatGPTDoctorResult(
        source_complete=True,
        extraction_complete=True,
        snapshot_key=snapshot_key,
        evidence_tree_sha256=manifest["tree_sha256"],
        source_records=source_records,
        conversations=conversations,
        nodes=nodes,
        messages=messages,
        group_threads=group_threads,
        group_messages=group_messages,
        graph_warnings=warnings,
        physical_assets=physical_assets,
    )


def import_chatgpt_export(
    *,
    evidence_root: Path,
    manifest_path: Path,
    output_root: Path,
    identity_scope: str,
) -> ChatGPTImportResult:
    """Import one verified official ChatGPT export Snapshot.

    The verified Evidence Manifest is the trust boundary. Identity rows are stable across
    snapshots, while every mutable field and graph relation is stored as a Snapshot version.
    """

    evidence_root = evidence_root.expanduser().resolve(strict=True)
    manifest_path = manifest_path.expanduser().resolve(strict=True)
    output_root = output_root.expanduser().resolve()
    if output_root == evidence_root or output_root.is_relative_to(evidence_root):
        raise ChatGPTImportError(
            "output_root cannot equal or be inside the evidence root"
        )
    if not identity_scope or any(character.isspace() for character in identity_scope):
        raise ChatGPTImportError("identity_scope must be non-empty and contain no whitespace")
    try:
        manifest = load_manifest(manifest_path)
        verification = verify_manifest(evidence_root, manifest)
    except EvidenceError as error:
        raise ChatGPTImportError(f"cannot verify evidence manifest: {error}") from error
    if not verification.ok:
        raise ChatGPTImportError(
            "manifest verification failed: " + "; ".join(verification.differences)
        )
    _validate_provider_export_manifest(evidence_root, manifest)

    source_metadata = manifest.get("source")
    if not isinstance(source_metadata, dict):
        raise ChatGPTImportError("evidence manifest source metadata is invalid")
    if source_metadata.get("kind") != "chatgpt_official_export":
        raise ChatGPTImportError("evidence manifest is not a ChatGPT official export")
    snapshot_key = source_metadata.get("id")
    captured_at = source_metadata.get("captured_at")
    if not isinstance(snapshot_key, str) or not snapshot_key:
        raise ChatGPTImportError("evidence manifest source id is not a valid snapshot key")
    if not isinstance(captured_at, str) or not captured_at:
        raise ChatGPTImportError("evidence manifest captured_at is invalid")
    captured_at_us = _captured_at_microseconds(captured_at)

    source_key = f"chatgpt-official-export:{identity_scope}"
    database_path = output_root / "canonical" / "archive.sqlite"
    decoded_snapshot_dir = (
        output_root
        / "decoded"
        / "chatgpt-official"
        / f"v{IMPORTER_VERSION}"
        / f"{_safe_component(snapshot_key)}-{manifest['tree_sha256']}"
    )
    ndjson_path = decoded_snapshot_dir / "conversations.ndjson"
    source_records_ndjson_path = decoded_snapshot_dir / "source-records.ndjson"
    shard_names = _load_shards(evidence_root, manifest)
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        database = connect_database(database_path)
    except DatabaseSchemaError as error:
        raise ChatGPTImportError(f"cannot open canonical database: {error}") from error
    try:
        _verify_message_fts_integrity(database)
        existing = database.execute(
            """SELECT s.id AS snapshot_id, s.evidence_tree_sha256,
                      s.raw_source_json, s.decoded_ndjson_path,
                      s.decoded_ndjson_sha256, s.source_records_ndjson_path,
                      s.source_records_ndjson_sha256,
                      s.evidence_manifest_path, s.evidence_manifest_sha256,
                      r.importer_version, r.schema_version, r.config_sha256,
                      r.source_records_count, r.conversations_count,
                      r.nodes_count, r.messages_count,
                      r.group_threads_count, r.group_messages_count,
                      r.warnings_count
                 FROM snapshots s
                 JOIN sources src ON src.id = s.source_id
                 JOIN import_runs r ON r.snapshot_id = s.id
                WHERE src.source_key = ? AND s.snapshot_key = ?""",
            (source_key, snapshot_key),
        ).fetchone()
        if existing is not None:
            if existing["evidence_tree_sha256"] != manifest["tree_sha256"]:
                raise ChatGPTImportError(
                    "snapshot id already exists with a different evidence hash"
                )
            if existing["raw_source_json"] != _canonical_json(source_metadata):
                raise ChatGPTImportError(
                    "snapshot id already exists with different capture metadata"
                )
            if (
                existing["importer_version"] != IMPORTER_VERSION
                or existing["schema_version"] != DATABASE_SCHEMA_VERSION
                or existing["config_sha256"] != IMPORT_CONFIG_SHA256
            ):
                raise ChatGPTImportError(
                    "snapshot was imported with a different importer profile"
                )
            expected_decoded_path = _stored_path(ndjson_path, output_root)
            if existing["decoded_ndjson_path"] != expected_decoded_path:
                raise ChatGPTImportError("stored decoded NDJSON path changed")
            expected_source_records_path = _stored_path(
                source_records_ndjson_path, output_root
            )
            if existing["source_records_ndjson_path"] != expected_source_records_path:
                raise ChatGPTImportError("stored source-records NDJSON path changed")
            expected_manifest_path = _stored_path(manifest_path, output_root)
            if existing["evidence_manifest_path"] != expected_manifest_path:
                raise ChatGPTImportError("stored evidence manifest path changed")
            if existing["evidence_manifest_sha256"] != _file_hash(manifest_path):
                raise ChatGPTImportError("stored evidence manifest hash changed")
            published = ndjson_path
            if not published.is_file() or _file_hash(published) != existing["decoded_ndjson_sha256"]:
                raise ChatGPTImportError("existing snapshot has a missing or changed NDJSON artifact")
            published_source_records = source_records_ndjson_path
            if (
                not published_source_records.is_file()
                or _file_hash(published_source_records)
                != existing["source_records_ndjson_sha256"]
            ):
                raise ChatGPTImportError(
                    "existing snapshot has a missing or changed source-records NDJSON artifact"
                )
            _verify_snapshot_database(
                database,
                snapshot_id=existing["snapshot_id"],
                expected_source_records=existing["source_records_count"],
                expected_conversations=existing["conversations_count"],
                expected_nodes=existing["nodes_count"],
                expected_messages=existing["messages_count"],
                expected_group_threads=existing["group_threads_count"],
                expected_group_messages=existing["group_messages_count"],
                expected_warnings=existing["warnings_count"],
            )
            _verify_snapshot_state_digest(
                database, snapshot_id=existing["snapshot_id"]
            )
            _verify_snapshot_assets(
                database, snapshot_id=existing["snapshot_id"], output_root=output_root
            )
            claims = {
                row["claim"]: row["status"]
                for row in database.execute(
                    "SELECT claim, status FROM snapshot_claims WHERE snapshot_id = ?",
                    (existing["snapshot_id"],),
                )
            }
            return ChatGPTImportResult(
                status="no_op",
                source_key=source_key,
                snapshot_key=snapshot_key,
                database_path=database_path,
                ndjson_path=published,
                source_records_ndjson_path=published_source_records,
                source_records=existing["source_records_count"],
                conversations=existing["conversations_count"],
                nodes=existing["nodes_count"],
                messages=existing["messages_count"],
                group_threads=existing["group_threads_count"],
                group_messages=existing["group_messages_count"],
                warnings=existing["warnings_count"],
                claims=claims,
            )

        prior_source = database.execute(
            "SELECT id FROM sources WHERE source_key = ?", (source_key,)
        ).fetchone()
        if prior_source is not None:
            for prior_snapshot in database.execute(
                "SELECT id FROM snapshots WHERE source_id = ? ORDER BY captured_at_us, snapshot_key",
                (prior_source["id"],),
            ):
                _verify_snapshot_state_digest(
                    database, snapshot_id=int(prior_snapshot["id"])
                )

        staging_dir = output_root / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="chatgpt-official-", suffix=".ndjson.tmp", dir=staging_dir
        )
        temporary_path = Path(temporary_name)
        source_descriptor, source_temporary_name = tempfile.mkstemp(
            prefix="chatgpt-source-records-", suffix=".ndjson.tmp", dir=staging_dir
        )
        source_temporary_path = Path(source_temporary_name)
        try:
            source_context = {
                "source_kind": "chatgpt_official_export",
                "snapshot_key": snapshot_key,
                "evidence_tree_sha256": manifest["tree_sha256"],
            }
            with os.fdopen(descriptor, "w+b") as temporary_file:
                conversations, nodes, messages, warnings = _write_staging_ndjson(
                    evidence_root=evidence_root,
                    shard_names=shard_names,
                    temporary_file=temporary_file,
                    source_context=source_context,
                )
            with os.fdopen(source_descriptor, "w+b") as source_temporary_file:
                source_records_count = _write_source_records_ndjson(
                    evidence_root=evidence_root,
                    manifest=manifest,
                    temporary_file=source_temporary_file,
                    source_context=source_context,
                )
            decoded_sha256 = _file_hash(temporary_path)
            source_records_sha256 = _file_hash(source_temporary_path)
            manifest_sha256 = _file_hash(manifest_path)
            started_at = _now()
            import_run_id = str(uuid.uuid4())

            database.execute("BEGIN IMMEDIATE")
            try:
                database.execute(
                    """INSERT INTO sources
                           (source_key, provider, kind, identity_scope)
                         VALUES (?, 'openai', 'chatgpt_official_export', ?)
                         ON CONFLICT(source_key) DO NOTHING""",
                    (source_key, identity_scope),
                )
                source_id = _integer_id(database, "sources", "source_key", source_key)
                source_row = database.execute(
                    "SELECT provider, kind, identity_scope FROM sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if tuple(source_row) != (
                    "openai",
                    "chatgpt_official_export",
                    identity_scope,
                ):
                    raise ChatGPTImportError("source identity conflict")
                database.execute(
                    """INSERT INTO snapshots
                           (source_id, snapshot_key, captured_at, captured_at_us,
                            raw_source_json, evidence_tree_sha256,
                            evidence_manifest_sha256, evidence_manifest_path,
                            decoded_ndjson_path, decoded_ndjson_sha256,
                            source_records_ndjson_path, source_records_ndjson_sha256,
                            imported_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        snapshot_key,
                        captured_at,
                        captured_at_us,
                        _canonical_json(source_metadata),
                        manifest["tree_sha256"],
                        manifest_sha256,
                        _stored_path(manifest_path, output_root),
                        _stored_path(ndjson_path, output_root),
                        decoded_sha256,
                        _stored_path(source_records_ndjson_path, output_root),
                        source_records_sha256,
                        started_at,
                    ),
                )
                snapshot_id = int(database.execute("SELECT last_insert_rowid()").fetchone()[0])
                database.execute(
                    """INSERT INTO import_runs
                           (id, source_id, snapshot_id, started_at, completed_at, status,
                            importer_version, schema_version, config_sha256,
                            source_records_count, conversations_count, nodes_count,
                            messages_count, group_threads_count, group_messages_count,
                            warnings_count)
                         VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        import_run_id,
                        source_id,
                        snapshot_id,
                        started_at,
                        started_at,
                        IMPORTER_VERSION,
                        DATABASE_SCHEMA_VERSION,
                        IMPORT_CONFIG_SHA256,
                        source_records_count,
                        conversations,
                        nodes,
                        messages,
                        0,
                        0,
                        warnings,
                    ),
                )
                source_records = _insert_source_records(
                    database,
                    snapshot_id=snapshot_id,
                    source_records_path=source_temporary_path,
                )
                asset_resolver = _insert_physical_assets(
                    database,
                    manifest=manifest,
                    evidence_root=evidence_root,
                    output_root=output_root,
                    snapshot_id=snapshot_id,
                )
                _insert_source_file_coverage(
                    database,
                    manifest=manifest,
                    snapshot_id=snapshot_id,
                )
                asset_reference_warnings = 0
                with temporary_path.open("r", encoding="utf-8") as staged:
                    for line in staged:
                        asset_reference_warnings += _insert_conversation(
                            database,
                            envelope=json.loads(line),
                            source_id=source_id,
                            snapshot_id=snapshot_id,
                            identity_scope=identity_scope,
                            import_run_id=import_run_id,
                            asset_resolver=asset_resolver,
                            source_records=source_records,
                        )
                group_threads, group_messages, group_warnings = _insert_group_chats(
                    database,
                    evidence_root=evidence_root,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    identity_scope=identity_scope,
                    import_run_id=import_run_id,
                    source_records=source_records,
                    asset_resolver=asset_resolver,
                )
                library_warnings = _insert_library_records(
                    database,
                    evidence_root=evidence_root,
                    snapshot_id=snapshot_id,
                    import_run_id=import_run_id,
                    asset_resolver=asset_resolver,
                )
                warnings += asset_reference_warnings + group_warnings + library_warnings
                database.execute(
                    """UPDATE import_runs
                          SET group_threads_count = ?, group_messages_count = ?,
                              warnings_count = ?, completed_at = ?
                        WHERE id = ?""",
                    (
                        group_threads,
                        group_messages,
                        warnings,
                        _now(),
                        import_run_id,
                    ),
                )
                _record_source_absences(database, source_id=source_id)
                unresolved = database.execute(
                    """SELECT count(*) FROM diagnostics
                         WHERE snapshot_id = ?
                           AND (code LIKE '%unresolved' OR code LIKE '%ambiguous')""",
                    (snapshot_id,),
                ).fetchone()[0]
                claim_rows = _build_snapshot_claims(
                    database,
                    manifest=manifest,
                    snapshot_id=snapshot_id,
                    conversations=conversations,
                    nodes=nodes,
                    messages=messages,
                    group_threads=group_threads,
                    group_messages=group_messages,
                    unresolved=unresolved,
                )
                claims = {
                    claim: status for claim, (status, _) in claim_rows.items()
                }
                database.executemany(
                    """INSERT INTO snapshot_claims
                           (snapshot_id, claim, status, details_json)
                         VALUES (?, ?, ?, ?)""",
                    (
                        (
                            snapshot_id,
                            claim,
                            status,
                            _canonical_json(details),
                        )
                        for claim, (status, details) in claim_rows.items()
                    ),
                )
                _verify_snapshot_database(
                    database,
                    snapshot_id=snapshot_id,
                    expected_source_records=source_records_count,
                    expected_conversations=conversations,
                    expected_nodes=nodes,
                    expected_messages=messages,
                    expected_group_threads=group_threads,
                    expected_group_messages=group_messages,
                    expected_warnings=warnings,
                )
                _store_source_snapshot_state_digests(database, source_id=source_id)
                _publish_no_clobber(temporary_path, ndjson_path, decoded_sha256)
                _publish_no_clobber(
                    source_temporary_path,
                    source_records_ndjson_path,
                    source_records_sha256,
                )
                database.commit()
            except Exception:
                database.rollback()
                raise
        finally:
            temporary_path.unlink(missing_ok=True)
            source_temporary_path.unlink(missing_ok=True)
    except ChatGPTImportError:
        raise
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ChatGPTImportError(f"ChatGPT import failed: {error}") from error
    finally:
        database.close()

    return ChatGPTImportResult(
        status="imported",
        source_key=source_key,
        snapshot_key=snapshot_key,
        database_path=database_path,
        ndjson_path=ndjson_path,
        source_records_ndjson_path=source_records_ndjson_path,
        source_records=source_records_count,
        conversations=conversations,
        nodes=nodes,
        messages=messages,
        group_threads=group_threads,
        group_messages=group_messages,
        warnings=warnings,
        claims=claims,
    )
