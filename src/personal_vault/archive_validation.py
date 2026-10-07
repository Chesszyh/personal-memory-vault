from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Callable
from typing import Any, Iterable, Mapping

from personal_vault.database import SCHEMA_VERSION


CAS_ROOT = "assets/sha256"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_LABEL = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One metadata-only archive validation result."""

    name: str
    status: str
    count: int | None = None
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveValidationResult:
    ok: bool
    checks: tuple[ValidationCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "metadata_only": True,
            "checks": [asdict(check) for check in self.checks],
        }


ValidationExpectations = Mapping[str, int | str]


# These are optional regression expectations for the two frozen official exports.
# The validator itself derives its invariants from the database and stored evidence.
CHATGPT_OFFICIAL_2026_EXPECTATIONS: dict[str, int | str] = {
    "schema.version": 2,
    "snapshots.count": 2,
    "global.assets.count": 1_007,
    "global.assets.bytes": 1_059_737_220,
    "global.source_file_coverage.count": 1_573,
    "global.conversation_identities.count": 1_269,
    "global.node_identities.count": 36_360,
    "global.message_identities.count": 35_091,
    "global.conversation_versions.count": 2_329,
    "global.node_versions.count": 41_154,
    "global.message_versions.count": 35_093,
    "global.search_documents.count": 36_089,
    "snapshot.chatgpt-official-2026-05-31.source_records.count": 1_098,
    "snapshot.chatgpt-official-2026-05-31.conversations.count": 1_066,
    "snapshot.chatgpt-official-2026-05-31.nodes.count": 27_578,
    "snapshot.chatgpt-official-2026-05-31.messages.count": 26_512,
    "snapshot.chatgpt-official-2026-05-31.group_threads.count": 2,
    "snapshot.chatgpt-official-2026-05-31.group_messages.count": 984,
    "snapshot.chatgpt-official-2026-05-31.covered_files.count": 501,
    "snapshot.chatgpt-official-2026-05-31.covered_bytes.count": 393_317_975,
    "snapshot.chatgpt-official-2026-05-31.current_branch_nodes.count": 26_190,
    "snapshot.chatgpt-official-2026-05-31.current_branch_messages.count": 25_124,
    "snapshot.chatgpt-official-2026-05-31.roots.count": 1_066,
    "snapshot.chatgpt-official-2026-05-31.branch_points.count": 321,
    "snapshot.chatgpt-official-2026-05-31.extra_alternative_edges.count": 389,
    "snapshot.chatgpt-official-2026-05-31.physical_assets.count": 481,
    "snapshot.chatgpt-official-2026-05-31.unique_assets.count": 478,
    "snapshot.chatgpt-official-2026-07-15.source_records.count": 2_084,
    "snapshot.chatgpt-official-2026-07-15.conversations.count": 1_263,
    "snapshot.chatgpt-official-2026-07-15.nodes.count": 24_871,
    "snapshot.chatgpt-official-2026-07-15.messages.count": 23_608,
    "snapshot.chatgpt-official-2026-07-15.group_threads.count": 2,
    "snapshot.chatgpt-official-2026-07-15.group_messages.count": 996,
    "snapshot.chatgpt-official-2026-07-15.covered_files.count": 1_072,
    "snapshot.chatgpt-official-2026-07-15.covered_bytes.count": 1_409_022_633,
    "snapshot.chatgpt-official-2026-07-15.current_branch_nodes.count": 23_758,
    "snapshot.chatgpt-official-2026-07-15.current_branch_messages.count": 22_496,
    "snapshot.chatgpt-official-2026-07-15.roots.count": 1_264,
    "snapshot.chatgpt-official-2026-07-15.branch_points.count": 355,
    "snapshot.chatgpt-official-2026-07-15.extra_alternative_edges.count": 421,
    "snapshot.chatgpt-official-2026-07-15.physical_assets.count": 1_049,
    "snapshot.chatgpt-official-2026-07-15.unique_assets.count": 972,
    "snapshot.chatgpt-official-2026-07-15.absence.conversation.count": 6,
    "snapshot.chatgpt-official-2026-07-15.absence.node.count": 11_489,
    "snapshot.chatgpt-official-2026-07-15.absence.message.count": 11_483,
    "snapshot.chatgpt-official-2026-07-15.absence.group_thread.count": 0,
    "snapshot.chatgpt-official-2026-07-15.absence.group_message.count": 0,
    "snapshot.chatgpt-official-2026-07-15.absence.asset.count": 35,
}


_DIRECT_STATE_TABLES = (
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

_RELATED_STATE_QUERIES = (
    (
        "sources",
        "SELECT src.* FROM sources src JOIN snapshots s ON s.source_id = src.id "
        "WHERE s.id = ?",
    ),
    ("snapshots", "SELECT * FROM snapshots WHERE id = ?"),
    ("import_runs", "SELECT * FROM import_runs WHERE snapshot_id = ?"),
    (
        "conversation_identities",
        "SELECT DISTINCT i.* FROM conversation_identities i "
        "JOIN conversation_observations o ON o.conversation_identity_id = i.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "conversation_versions",
        "SELECT DISTINCT v.* FROM conversation_versions v "
        "JOIN conversation_observations o ON o.conversation_version_id = v.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "node_identities",
        "SELECT DISTINCT i.* FROM node_identities i "
        "JOIN node_observations o ON o.node_identity_id = i.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "node_versions",
        "SELECT DISTINCT v.* FROM node_versions v "
        "JOIN node_observations o ON o.node_version_id = v.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "message_identities",
        "SELECT DISTINCT i.* FROM message_identities i "
        "JOIN message_observations o ON o.message_identity_id = i.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "message_versions",
        "SELECT DISTINCT v.* FROM message_versions v "
        "JOIN message_observations o ON o.message_version_id = v.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "assets",
        "SELECT DISTINCT a.* FROM assets a "
        "JOIN asset_observations o ON o.asset_id = a.id WHERE o.snapshot_id = ?",
    ),
    (
        "group_thread_identities",
        "SELECT DISTINCT i.* FROM group_thread_identities i "
        "JOIN group_thread_observations o ON o.group_thread_identity_id = i.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "group_thread_versions",
        "SELECT DISTINCT v.* FROM group_thread_versions v "
        "JOIN group_thread_observations o ON o.group_thread_version_id = v.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "group_message_identities",
        "SELECT DISTINCT i.* FROM group_message_identities i "
        "JOIN group_message_observations o ON o.group_message_identity_id = i.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "group_message_versions",
        "SELECT DISTINCT v.* FROM group_message_versions v "
        "JOIN group_message_observations o ON o.group_message_version_id = v.id "
        "WHERE o.snapshot_id = ?",
    ),
    (
        "search_documents",
        "SELECT DISTINCT d.* FROM search_documents d "
        "WHERE d.message_version_id IN (SELECT o.message_version_id "
        "FROM message_observations o WHERE o.snapshot_id = ?) "
        "OR d.group_message_version_id IN (SELECT o.group_message_version_id "
        "FROM group_message_observations o WHERE o.snapshot_id = ?)",
    ),
)


class _Checks:
    def __init__(self, expectations: ValidationExpectations | None) -> None:
        self._expectations = dict(expectations or {})
        self.items: list[ValidationCheck] = []

    def metric(
        self, name: str, *, count: int | None = None, digest: str | None = None
    ) -> None:
        expected = self._expectations.get(name)
        expected_count = expected if isinstance(expected, int) else None
        expected_digest = expected if isinstance(expected, str) else None
        if expected is None:
            matches = True
        elif isinstance(expected, int):
            matches = expected == count
        else:
            matches = expected == digest
        self.items.append(
            ValidationCheck(
                name=name,
                status="pass" if matches else "fail",
                count=count,
                digest=digest,
            )
        )

    def violations(self, name: str, count: int) -> None:
        self.items.append(
            ValidationCheck(
                name=name,
                status="pass" if count == 0 else "fail",
                count=count,
            )
        )

    def failure(self, name: str, _error: BaseException) -> None:
        self.items.append(
            ValidationCheck(
                name=name,
                status="fail",
                count=1,
            )
        )

    def finish(self) -> ArchiveValidationResult:
        produced = {item.name for item in self.items}
        for name, expected in sorted(self._expectations.items()):
            if name in produced:
                continue
            self.items.append(
                ValidationCheck(
                    name=name,
                    status="fail",
                    count=0 if isinstance(expected, int) else None,
                )
            )
        return ArchiveValidationResult(
            ok=all(item.status == "pass" for item in self.items),
            checks=tuple(self.items),
        )


@dataclass(frozen=True, slots=True)
class _FileDigest:
    size: int
    sha256: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_snapshot_label(snapshot_key: str) -> str:
    if _SAFE_LABEL.fullmatch(snapshot_key):
        return snapshot_key
    return f"sha256-{hashlib.sha256(snapshot_key.encode('utf-8')).hexdigest()[:16]}"


def _require_open_flags() -> None:
    for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"):
        if not hasattr(os, name):
            raise NotImplementedError(f"secure archive validation requires os.{name}")


def _relative_parts(value: str | os.PathLike[str]) -> tuple[str, ...]:
    raw = os.fspath(value)
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
    ):
        raise ValueError("archive paths must be canonical relative POSIX paths")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("archive paths must be canonical relative POSIX paths")
    return PurePosixPath(raw).parts


def _dir_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_root(vault_root: Path) -> int:
    _require_open_flags()
    descriptor = os.open(vault_root, _dir_flags())
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("vault root is not a directory")
    return descriptor


def _open_dir_at(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            following = os.open(part, _dir_flags(), dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_regular_at(root_fd: int, relative_path: str) -> tuple[int, os.stat_result]:
    parts = _relative_parts(relative_path)
    parent = _open_dir_at(root_fd, parts[:-1])
    try:
        descriptor = os.open(parts[-1], _file_flags(), dir_fd=parent)
    finally:
        os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("archive entry is not a regular file")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _stable_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def _digest_vault_file(root_fd: int, relative_path: str) -> _FileDigest:
    descriptor, before = _open_regular_at(root_fd, relative_path)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not _stable_file_state(before, after) or total != after.st_size:
        raise RuntimeError("archive file changed while hashing")
    return _FileDigest(total, digest.hexdigest())


def _read_json_vault_file(root_fd: int, relative_path: str) -> tuple[Any, _FileDigest]:
    descriptor, before = _open_regular_at(root_fd, relative_path)
    digest = hashlib.sha256()
    payload = bytearray()
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            payload.extend(block)
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not _stable_file_state(before, after) or len(payload) != after.st_size:
        raise RuntimeError("archive file changed while reading")
    return json.loads(payload), _FileDigest(len(payload), digest.hexdigest())


def _walk_regular_files(
    directory_fd: int, prefix: tuple[str, ...], result: set[str]
) -> None:
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        relative_parts = prefix + (name,)
        relative = PurePosixPath(*relative_parts).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("symlink found in content-addressed store")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, _dir_flags(), dir_fd=directory_fd)
            try:
                _walk_regular_files(child, relative_parts, result)
            finally:
                os.close(child)
            continue
        descriptor = os.open(name, _file_flags(), dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("special entry found in content-addressed store")
        finally:
            os.close(descriptor)
        result.add(relative)


def _enumerate_cas(root_fd: int, *, allow_missing: bool) -> frozenset[str]:
    parts = _relative_parts(CAS_ROOT)
    try:
        directory = _open_dir_at(root_fd, parts)
    except FileNotFoundError:
        if allow_missing:
            return frozenset()
        raise
    try:
        result: set[str] = set()
        _walk_regular_files(directory, parts, result)
        return frozenset(result)
    finally:
        os.close(directory)


def _relative_database_path(vault_root: Path, database_path: Path) -> str:
    root_absolute = Path(os.path.abspath(vault_root))
    candidate = database_path if database_path.is_absolute() else root_absolute / database_path
    candidate_absolute = Path(os.path.abspath(candidate))
    try:
        relative = candidate_absolute.relative_to(root_absolute)
    except ValueError as error:
        raise ValueError("database path must be inside the vault") from error
    return PurePosixPath(*relative.parts).as_posix()


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri = database_path.absolute().as_uri() + "?mode=ro"
    database = sqlite3.connect(uri, uri=True)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    database.execute("PRAGMA foreign_keys = ON")
    return database


def _count(database: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()) -> int:
    return int(database.execute(query, parameters).fetchone()[0])


def _canonical_state_row_digests(rows: Iterable[sqlite3.Row]) -> tuple[str, ...]:
    digests = []
    for row in rows:
        canonical = _canonical_json({key: row[key] for key in row.keys()})
        digests.append(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
    return tuple(sorted(digests))


def _snapshot_state_digest(
    database: sqlite3.Connection, snapshot_id: int
) -> tuple[str, dict[str, int]]:
    sections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for table in _DIRECT_STATE_TABLES:
        rows = _canonical_state_row_digests(
            database.execute(f"SELECT * FROM {table} WHERE snapshot_id = ?", (snapshot_id,))
        )
        counts[table] = len(rows)
        sections.append({"name": table, "row_sha256": rows})
    for name, query in _RELATED_STATE_QUERIES:
        rows = _canonical_state_row_digests(
            database.execute(query, (snapshot_id,) * query.count("?"))
        )
        counts[name] = len(rows)
        sections.append({"name": name, "row_sha256": rows})
    return (
        _json_hash(
            {
                "digest_format": "snapshot-state-v1",
                "schema_version": SCHEMA_VERSION,
                "sections": sections,
            }
        ),
        counts,
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    fragments: list[str] = []
    if isinstance(content.get("text"), str):
        fragments.append(content["text"])
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                fragments.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                fragments.append(part["text"])
    return "\n".join(fragments)


def _validate_raw_payloads(database: sqlite3.Connection, checks: _Checks) -> None:
    mismatches = 0
    normalized_mismatches = 0
    for row in database.execute("SELECT raw_sha256, raw_json FROM source_records"):
        try:
            raw = json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            mismatches += 1
            continue
        mismatches += _json_hash(raw) != row["raw_sha256"]

    version_specs = (
        ("conversation_versions", None),
        ("node_versions", None),
        ("message_versions", "message"),
        ("group_thread_versions", None),
        ("group_message_versions", "group_message"),
    )
    for table, normalized_kind in version_specs:
        for row in database.execute(f"SELECT * FROM {table}"):
            try:
                raw = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                mismatches += 1
                continue
            mismatches += _json_hash(raw) != row["raw_sha256"]
            if normalized_kind == "message":
                author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
                content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
                expected = (
                    author.get("role"),
                    author.get("name"),
                    content.get("content_type"),
                    raw.get("create_time"),
                    raw.get("update_time"),
                    raw.get("status"),
                    raw.get("recipient"),
                )
                actual = tuple(
                    row[key]
                    for key in (
                        "author_role",
                        "author_name",
                        "content_type",
                        "create_time",
                        "update_time",
                        "status",
                        "recipient",
                    )
                )
                normalized_mismatches += actual != expected
            elif normalized_kind == "group_message":
                expected_text = raw.get("text") if isinstance(raw.get("text"), str) else None
                normalized_mismatches += (
                    row["role"], row["text"], row["created_at"]
                ) != (raw.get("role"), expected_text, raw.get("created_at"))
    checks.violations("raw_payload_hash.violations", int(mismatches))
    checks.violations("normalized_payload.violations", int(normalized_mismatches))


def _validate_json_columns(database: sqlite3.Connection, checks: _Checks) -> None:
    columns = (
        ("source_records", "raw_json"),
        ("conversation_versions", "raw_json"),
        ("conversation_versions", "unknown_json"),
        ("node_versions", "raw_json"),
        ("node_versions", "unknown_json"),
        ("message_versions", "raw_json"),
        ("message_versions", "unknown_json"),
        ("group_thread_versions", "raw_json"),
        ("group_thread_versions", "unknown_json"),
        ("group_message_versions", "raw_json"),
        ("group_message_versions", "unknown_json"),
        ("diagnostics", "details_json"),
        ("snapshot_claims", "details_json"),
        ("source_file_coverage", "details_json"),
        ("snapshot_state_digests", "details_json"),
        ("message_asset_refs", "raw_json"),
        ("message_asset_refs", "candidates_json"),
        ("group_message_asset_refs", "raw_json"),
        ("group_message_asset_refs", "candidates_json"),
        ("library_asset_refs", "raw_json"),
        ("library_asset_refs", "candidates_json"),
        ("library_source_refs", "raw_json"),
    )
    invalid = sum(
        _count(database, f"SELECT count(*) FROM {table} WHERE NOT json_valid({column})")
        for table, column in columns
    )
    checks.violations("json_columns.violations", invalid)


def _validate_import_counts(database: sqlite3.Connection, checks: _Checks) -> None:
    checks.violations(
        "import_runs.cardinality.violations",
        _count(
            database,
            "SELECT count(*) FROM (SELECT s.id FROM snapshots s "
            "LEFT JOIN import_runs r ON r.snapshot_id = s.id GROUP BY s.id "
            "HAVING count(r.id) <> 1)",
        ),
    )
    checks.violations(
        "import_runs.count_closure.violations",
        _count(
            database,
            "SELECT count(*) FROM import_runs r WHERE "
            "r.source_records_count <> (SELECT count(*) FROM source_records x WHERE x.snapshot_id=r.snapshot_id) OR "
            "r.conversations_count <> (SELECT count(*) FROM conversation_observations x WHERE x.snapshot_id=r.snapshot_id) OR "
            "r.nodes_count <> (SELECT count(*) FROM node_observations x WHERE x.snapshot_id=r.snapshot_id) OR "
            "r.messages_count <> (SELECT count(*) FROM message_observations x WHERE x.snapshot_id=r.snapshot_id) OR "
            "r.group_threads_count <> (SELECT count(*) FROM group_thread_observations x WHERE x.snapshot_id=r.snapshot_id) OR "
            "r.group_messages_count <> (SELECT count(*) FROM group_message_observations x WHERE x.snapshot_id=r.snapshot_id) OR "
            "r.warnings_count <> (SELECT count(*) FROM diagnostics x WHERE x.snapshot_id=r.snapshot_id)",
        ),
    )


def _validate_absences(database: sqlite3.Connection, checks: _Checks) -> None:
    checks.violations(
        "source_absences.semantic.violations",
        _count(
            database,
            """WITH presence(snapshot_id,source_id,entity_kind,identity_key) AS (
                SELECT o.snapshot_id,i.source_id,'conversation',i.identity_key
                  FROM conversation_observations o JOIN conversation_identities i
                    ON i.id=o.conversation_identity_id
                UNION ALL
                SELECT o.snapshot_id,ci.source_id,'node',i.identity_key
                  FROM node_observations o JOIN node_identities i ON i.id=o.node_identity_id
                  JOIN conversation_identities ci ON ci.id=i.conversation_identity_id
                UNION ALL
                SELECT o.snapshot_id,ci.source_id,'message',i.identity_key
                  FROM message_observations o JOIN message_identities i
                    ON i.id=o.message_identity_id
                  JOIN conversation_identities ci ON ci.id=i.conversation_identity_id
                UNION ALL
                SELECT o.snapshot_id,i.source_id,'group_thread',i.identity_key
                  FROM group_thread_observations o JOIN group_thread_identities i
                    ON i.id=o.group_thread_identity_id
                UNION ALL
                SELECT o.snapshot_id,ti.source_id,'group_message',i.identity_key
                  FROM group_message_observations o JOIN group_message_identities i
                    ON i.id=o.group_message_identity_id
                  JOIN group_thread_identities ti ON ti.id=i.group_thread_identity_id
                UNION ALL
                SELECT o.snapshot_id,s.source_id,'asset','asset:sha256:'||a.sha256
                  FROM asset_observations o JOIN assets a ON a.id=o.asset_id
                  JOIN snapshots s ON s.id=o.snapshot_id
            )
            SELECT count(*) FROM source_absences a
              JOIN snapshots current ON current.id=a.snapshot_id
              JOIN snapshots prior ON prior.id=a.prior_snapshot_id
              LEFT JOIN presence prior_presence
                ON prior_presence.snapshot_id=a.prior_snapshot_id
               AND prior_presence.source_id=a.source_id
               AND prior_presence.entity_kind=a.entity_kind
               AND prior_presence.identity_key=a.identity_key
              LEFT JOIN presence current_presence
                ON current_presence.snapshot_id=a.snapshot_id
               AND current_presence.source_id=a.source_id
               AND current_presence.entity_kind=a.entity_kind
               AND current_presence.identity_key=a.identity_key
             WHERE current.source_id<>a.source_id OR prior.source_id<>a.source_id
                OR (prior.captured_at_us,prior.snapshot_key)>=(current.captured_at_us,current.snapshot_key)
                OR prior_presence.identity_key IS NULL
                OR current_presence.identity_key IS NOT NULL""",
        ),
    )


_SNAPSHOT_INVARIANTS = {
    "conversation_links": """SELECT count(*) FROM conversation_observations co
        JOIN snapshots s ON s.id=co.snapshot_id
        JOIN conversation_identities ci ON ci.id=co.conversation_identity_id
        JOIN conversation_versions cv ON cv.id=co.conversation_version_id
        JOIN source_records sr ON sr.id=co.source_record_id
        LEFT JOIN node_identities cni ON cni.identity_key=co.current_node_identity_key
        LEFT JOIN node_observations cno ON cno.snapshot_id=co.snapshot_id
             AND cno.node_identity_id=cni.id
        WHERE co.snapshot_id=? AND (ci.source_id<>s.source_id
          OR cv.conversation_identity_id<>co.conversation_identity_id
          OR sr.snapshot_id<>co.snapshot_id OR cni.id IS NULL
          OR cni.conversation_identity_id<>co.conversation_identity_id OR cno.id IS NULL)""",
    "node_links": """SELECT count(*) FROM node_observations no
        JOIN node_identities ni ON ni.id=no.node_identity_id
        JOIN node_versions nv ON nv.id=no.node_version_id
        JOIN source_records sr ON sr.id=no.source_record_id
        LEFT JOIN conversation_observations co ON co.snapshot_id=no.snapshot_id
             AND co.conversation_identity_id=ni.conversation_identity_id
        LEFT JOIN node_identities pi ON pi.id=no.parent_node_identity_id
        LEFT JOIN node_observations po ON po.snapshot_id=no.snapshot_id
             AND po.node_identity_id=no.parent_node_identity_id
        LEFT JOIN message_identities mi ON mi.id=no.message_identity_id
        WHERE no.snapshot_id=? AND (nv.node_identity_id<>no.node_identity_id
          OR sr.snapshot_id<>no.snapshot_id OR co.id IS NULL
          OR (pi.id IS NOT NULL AND pi.conversation_identity_id<>ni.conversation_identity_id)
          OR (no.parent_node_identity_id IS NOT NULL AND po.id IS NULL)
          OR (mi.id IS NOT NULL AND mi.conversation_identity_id<>ni.conversation_identity_id))""",
    "message_links": """SELECT count(*) FROM message_observations mo
        JOIN message_versions mv ON mv.id=mo.message_version_id
        JOIN node_observations no ON no.id=mo.node_observation_id
        JOIN source_records sr ON sr.id=mo.source_record_id
        WHERE mo.snapshot_id=? AND (mv.message_identity_id<>mo.message_identity_id
          OR no.snapshot_id<>mo.snapshot_id OR no.message_identity_id<>mo.message_identity_id
          OR sr.snapshot_id<>mo.snapshot_id)""",
    "current_branch_links": """SELECT count(*) FROM current_branch_nodes cb
        JOIN conversation_observations co ON co.id=cb.conversation_observation_id
        JOIN node_identities ni ON ni.id=cb.node_identity_id
        LEFT JOIN node_observations no ON no.snapshot_id=cb.snapshot_id
             AND no.node_identity_id=cb.node_identity_id
        WHERE cb.snapshot_id=? AND (co.snapshot_id<>cb.snapshot_id
          OR co.conversation_identity_id<>cb.conversation_identity_id
          OR ni.conversation_identity_id<>cb.conversation_identity_id OR no.id IS NULL)""",
    "current_branch_depth": """SELECT count(*) FROM (
        SELECT conversation_identity_id FROM current_branch_nodes WHERE snapshot_id=?
        GROUP BY conversation_identity_id HAVING min(depth)<>0 OR max(depth)+1<>count(*))""",
    "current_branch_root": """SELECT count(*) FROM current_branch_nodes cb
        JOIN node_observations no ON no.snapshot_id=cb.snapshot_id
             AND no.node_identity_id=cb.node_identity_id
        WHERE cb.snapshot_id=? AND cb.depth=0 AND no.parent_node_identity_id IS NOT NULL""",
    "current_branch_parent_order": """SELECT count(*) FROM current_branch_nodes cb
        LEFT JOIN current_branch_nodes prior ON prior.snapshot_id=cb.snapshot_id
             AND prior.conversation_identity_id=cb.conversation_identity_id
             AND prior.depth=cb.depth-1
        JOIN node_observations no ON no.snapshot_id=cb.snapshot_id
             AND no.node_identity_id=cb.node_identity_id
        WHERE cb.snapshot_id=? AND cb.depth>0
          AND (prior.node_identity_id IS NULL OR no.parent_node_identity_id IS NOT prior.node_identity_id)""",
    "current_branch_tip": """SELECT count(*) FROM conversation_observations co
        LEFT JOIN current_branch_nodes cb ON cb.snapshot_id=co.snapshot_id
             AND cb.conversation_identity_id=co.conversation_identity_id
             AND cb.depth=(SELECT max(x.depth) FROM current_branch_nodes x
                 WHERE x.snapshot_id=co.snapshot_id
                   AND x.conversation_identity_id=co.conversation_identity_id)
        LEFT JOIN node_identities ni ON ni.id=cb.node_identity_id
        WHERE co.snapshot_id=? AND (ni.identity_key IS NULL
          OR ni.identity_key<>co.current_node_identity_key)""",
    "dag_reachability": """WITH RECURSIVE reachable(node_identity_id) AS (
        SELECT node_identity_id FROM node_observations
         WHERE snapshot_id=? AND parent_node_identity_id IS NULL
        UNION
        SELECT child.node_identity_id FROM node_observations child
        JOIN reachable parent ON parent.node_identity_id=child.parent_node_identity_id
         WHERE child.snapshot_id=?
        )
        SELECT count(*) FROM node_observations no LEFT JOIN reachable r
          ON r.node_identity_id=no.node_identity_id
         WHERE no.snapshot_id=? AND r.node_identity_id IS NULL""",
    "group_thread_links": """SELECT count(*) FROM group_thread_observations go
        JOIN group_thread_identities gi ON gi.id=go.group_thread_identity_id
        JOIN group_thread_versions gv ON gv.id=go.group_thread_version_id
        JOIN snapshots s ON s.id=go.snapshot_id
        JOIN source_records sr ON sr.id=go.source_record_id
        WHERE go.snapshot_id=? AND (gi.source_id<>s.source_id
          OR gv.group_thread_identity_id<>go.group_thread_identity_id
          OR sr.snapshot_id<>go.snapshot_id)""",
    "group_message_links": """SELECT count(*) FROM group_message_observations go
        JOIN group_message_identities gi ON gi.id=go.group_message_identity_id
        JOIN group_message_versions gv ON gv.id=go.group_message_version_id
        JOIN group_thread_observations gt ON gt.id=go.group_thread_observation_id
        JOIN source_records sr ON sr.id=go.source_record_id
        WHERE go.snapshot_id=? AND (gv.group_message_identity_id<>go.group_message_identity_id
          OR gt.snapshot_id<>go.snapshot_id
          OR gt.group_thread_identity_id<>gi.group_thread_identity_id
          OR sr.snapshot_id<>go.snapshot_id)""",
    "message_asset_refs": """SELECT count(*) FROM message_asset_refs r
        JOIN message_observations mo ON mo.id=r.message_observation_id
        LEFT JOIN asset_observations ao ON ao.id=r.asset_observation_id
        WHERE r.snapshot_id=? AND (mo.snapshot_id<>r.snapshot_id
          OR (ao.id IS NOT NULL AND ao.snapshot_id<>r.snapshot_id)
          OR NOT json_valid(r.candidates_json)
          OR r.candidate_count<>json_array_length(r.candidates_json)
          OR (r.resolution_status='resolved' AND
              (r.asset_observation_id IS NULL OR r.candidate_count<>1))
          OR (r.resolution_status='unresolved' AND
              (r.asset_observation_id IS NOT NULL OR r.candidate_count<>0))
          OR (r.resolution_status='ambiguous' AND
              (r.asset_observation_id IS NOT NULL OR r.candidate_count<2)))""",
    "asset_observations": """SELECT count(*) FROM asset_observations ao
        JOIN assets a ON a.id=ao.asset_id WHERE ao.snapshot_id=? AND
        (ao.sha256 IS NOT a.sha256 OR ao.size_bytes IS NOT a.size_bytes
         OR ao.observation_kind<>'physical_export_file')""",
    "group_message_asset_refs": """SELECT count(*) FROM group_message_asset_refs r
        JOIN group_message_observations mo ON mo.id=r.group_message_observation_id
        LEFT JOIN asset_observations ao ON ao.id=r.asset_observation_id
        WHERE r.snapshot_id=? AND (mo.snapshot_id<>r.snapshot_id
          OR NOT json_valid(r.candidates_json)
          OR (ao.id IS NOT NULL AND ao.snapshot_id<>r.snapshot_id)
          OR (r.resolution_status='resolved' AND r.asset_observation_id IS NULL)
          OR (r.resolution_status IN ('unresolved','ambiguous','external')
              AND r.asset_observation_id IS NOT NULL)
          OR (r.resolution_status='unresolved' AND json_array_length(r.candidates_json)<>0)
          OR (r.resolution_status='ambiguous' AND json_array_length(r.candidates_json)<2)
          OR (r.resolution_status='external' AND json_array_length(r.candidates_json)<>0))""",
    "library_asset_refs": """SELECT count(*) FROM library_asset_refs r
        LEFT JOIN asset_observations ao ON ao.id=r.asset_observation_id
        WHERE r.snapshot_id=? AND (NOT json_valid(r.candidates_json)
          OR r.candidate_count<>json_array_length(r.candidates_json)
          OR (ao.id IS NOT NULL AND ao.snapshot_id<>r.snapshot_id)
          OR (r.resolution_status='resolved' AND
              (r.asset_observation_id IS NULL OR r.candidate_count<>1))
          OR (r.resolution_status='unresolved' AND
              (r.asset_observation_id IS NOT NULL OR r.candidate_count<>0))
          OR (r.resolution_status='ambiguous' AND
              (r.asset_observation_id IS NOT NULL OR r.candidate_count<2)))""",
    "library_source_refs": """SELECT count(*) FROM library_source_refs r
        WHERE r.snapshot_id=? AND ((r.resolution_status='resolved'
             AND r.resolved_identity_key IS NULL)
          OR (r.resolution_status IN ('unresolved','ambiguous')
             AND r.resolved_identity_key IS NOT NULL))""",
    "diagnostic_links": """SELECT count(*) FROM diagnostics d
        JOIN import_runs r ON r.id=d.import_run_id
        LEFT JOIN conversation_identities ci ON ci.id=d.conversation_identity_id
        LEFT JOIN snapshots s ON s.id=d.snapshot_id
        WHERE d.snapshot_id=? AND (r.snapshot_id<>d.snapshot_id
          OR s.id IS NULL OR (ci.id IS NOT NULL AND ci.source_id<>s.source_id))""",
    "claims": """SELECT count(*) FROM (
        SELECT 'source_complete' claim,'pass' status UNION ALL
        SELECT 'extraction_complete','pass' UNION ALL
        SELECT 'reconciliation_complete','partial' UNION ALL
        SELECT 'account_complete','not_applicable') expected
        LEFT JOIN snapshot_claims actual ON actual.snapshot_id=?
          AND actual.claim=expected.claim AND actual.status=expected.status
        WHERE actual.claim IS NULL""",
    "claims_extra": """SELECT count(*) FROM snapshot_claims WHERE snapshot_id=?
        AND claim NOT IN ('source_complete','extraction_complete',
                          'reconciliation_complete','account_complete')""",
    "source_file_coverage": """SELECT count(*) FROM source_file_coverage c
        WHERE c.snapshot_id=? AND ((c.media_class='json' AND
          (c.handling_status<>'decoded' OR c.source_record_count<>(SELECT count(*)
             FROM source_records sr WHERE sr.snapshot_id=c.snapshot_id
               AND sr.source_file_path=c.evidence_path)
           OR 1<>(SELECT count(*) FROM source_records sr
             WHERE sr.snapshot_id=c.snapshot_id AND sr.source_file_path=c.evidence_path
               AND sr.record_scope='file_root' AND sr.json_pointer='')))
          OR (c.media_class='dat' AND
             (c.handling_status<>'cas_verified' OR c.asset_observation_count<>1))
          OR (c.media_class IN ('html','other') AND c.handling_status<>'preserved_opaque'))""",
}


def _validate_snapshot_invariants(
    database: sqlite3.Connection,
    checks: _Checks,
    snapshot_id: int,
    label: str,
) -> None:
    for name, query in _SNAPSHOT_INVARIANTS.items():
        parameters = (snapshot_id,) * query.count("?")
        checks.violations(
            f"snapshot.{label}.invariant.{name}.violations",
            _count(database, query, parameters),
        )


def _validate_state_digests(
    database: sqlite3.Connection,
    checks: _Checks,
    snapshots: tuple[sqlite3.Row, ...],
) -> None:
    missing = 0
    for snapshot in snapshots:
        label = _safe_snapshot_label(str(snapshot["snapshot_key"]))
        stored = database.execute(
            "SELECT algorithm,state_sha256,details_json FROM snapshot_state_digests "
            "WHERE snapshot_id=?",
            (snapshot["id"],),
        ).fetchone()
        if stored is None or stored["algorithm"] != "sha256-canonical-json-v1":
            missing += 1
            continue
        actual, counts = _snapshot_state_digest(database, int(snapshot["id"]))
        expected_details = _canonical_json(
            {
                "digest_format": "snapshot-state-v1",
                "schema_version": SCHEMA_VERSION,
                "section_row_counts": counts,
            }
        )
        matches = actual == stored["state_sha256"] and expected_details == stored["details_json"]
        checks.items.append(
            ValidationCheck(
                name=f"snapshot.{label}.state_digest",
                status="pass" if matches else "fail",
                count=0 if matches else 1,
                digest=actual,
            )
        )
    checks.violations("snapshot_state_digests.missing.violations", missing)


def _validate_snapshot_metrics(
    database: sqlite3.Connection,
    checks: _Checks,
    snapshots: tuple[sqlite3.Row, ...],
) -> None:
    table_metrics = {
        "source_records": "source_records",
        "conversations": "conversation_observations",
        "nodes": "node_observations",
        "messages": "message_observations",
        "group_threads": "group_thread_observations",
        "group_messages": "group_message_observations",
        "covered_files": "source_file_coverage",
        "physical_assets": "asset_observations",
    }
    for snapshot in snapshots:
        snapshot_id = int(snapshot["id"])
        label = _safe_snapshot_label(str(snapshot["snapshot_key"]))
        for metric, table in table_metrics.items():
            checks.metric(
                f"snapshot.{label}.{metric}.count",
                count=_count(database, f"SELECT count(*) FROM {table} WHERE snapshot_id=?", (snapshot_id,)),
            )
        checks.metric(
            f"snapshot.{label}.covered_bytes.count",
            count=_count(
                database,
                "SELECT coalesce(sum(size_bytes),0) FROM source_file_coverage WHERE snapshot_id=?",
                (snapshot_id,),
            ),
        )
        checks.metric(
            f"snapshot.{label}.current_branch_nodes.count",
            count=_count(database, "SELECT count(*) FROM current_branch_nodes WHERE snapshot_id=?", (snapshot_id,)),
        )
        checks.metric(
            f"snapshot.{label}.current_branch_messages.count",
            count=_count(
                database,
                "SELECT count(*) FROM current_branch_nodes cb JOIN node_observations no "
                "ON no.snapshot_id=cb.snapshot_id AND no.node_identity_id=cb.node_identity_id "
                "WHERE cb.snapshot_id=? AND no.message_identity_id IS NOT NULL",
                (snapshot_id,),
            ),
        )
        checks.metric(
            f"snapshot.{label}.roots.count",
            count=_count(
                database,
                "SELECT count(*) FROM node_observations WHERE snapshot_id=? "
                "AND parent_node_identity_id IS NULL",
                (snapshot_id,),
            ),
        )
        checks.metric(
            f"snapshot.{label}.branch_points.count",
            count=_count(
                database,
                "SELECT count(*) FROM (SELECT parent_node_identity_id FROM node_observations "
                "WHERE snapshot_id=? AND parent_node_identity_id IS NOT NULL "
                "GROUP BY parent_node_identity_id HAVING count(*)>1)",
                (snapshot_id,),
            ),
        )
        checks.metric(
            f"snapshot.{label}.extra_alternative_edges.count",
            count=_count(
                database,
                "SELECT coalesce(sum(children-1),0) FROM (SELECT count(*) children "
                "FROM node_observations WHERE snapshot_id=? AND parent_node_identity_id IS NOT NULL "
                "GROUP BY parent_node_identity_id HAVING count(*)>1)",
                (snapshot_id,),
            ),
        )
        checks.metric(
            f"snapshot.{label}.unique_assets.count",
            count=_count(database, "SELECT count(DISTINCT asset_id) FROM asset_observations WHERE snapshot_id=?", (snapshot_id,)),
        )
        for entity_kind in (
            "conversation",
            "node",
            "message",
            "group_thread",
            "group_message",
            "asset",
        ):
            checks.metric(
                f"snapshot.{label}.absence.{entity_kind}.count",
                count=_count(
                    database,
                    "SELECT count(*) FROM source_absences WHERE snapshot_id=? AND entity_kind=?",
                    (snapshot_id, entity_kind),
                ),
            )


def _validate_manifest_and_ndjson(
    database: sqlite3.Connection,
    root_fd: int,
    checks: _Checks,
    snapshots: tuple[sqlite3.Row, ...],
) -> None:
    artifact_failures = 0
    ndjson_failures = 0
    coverage_failures = 0
    for snapshot in snapshots:
        snapshot_id = int(snapshot["id"])
        paths_and_hashes = (
            (snapshot["evidence_manifest_path"], snapshot["evidence_manifest_sha256"]),
            (snapshot["decoded_ndjson_path"], snapshot["decoded_ndjson_sha256"]),
            (snapshot["source_records_ndjson_path"], snapshot["source_records_ndjson_sha256"]),
        )
        for stored_path, expected_hash in paths_and_hashes:
            try:
                actual = _digest_vault_file(root_fd, str(stored_path))
                artifact_failures += actual.sha256 != expected_hash
            except (OSError, ValueError, RuntimeError):
                artifact_failures += 1

        try:
            manifest, manifest_digest = _read_json_vault_file(
                root_fd, str(snapshot["evidence_manifest_path"])
            )
            if manifest_digest.sha256 != snapshot["evidence_manifest_sha256"]:
                coverage_failures += 1
                continue
            if (
                not isinstance(manifest, dict)
                or manifest.get("tree_sha256") != snapshot["evidence_tree_sha256"]
                or _canonical_json(manifest.get("source")) != snapshot["raw_source_json"]
            ):
                coverage_failures += 1
                continue
            manifest_files = {
                entry["path"]: (entry["size"], entry["sha256"])
                for entry in manifest.get("entries", [])
                if isinstance(entry, dict)
                and entry.get("kind") == "file"
                and isinstance(entry.get("path"), str)
            }
            coverage_rows = {
                str(row["evidence_path"]): (int(row["size_bytes"]), str(row["sha256"]))
                for row in database.execute(
                    "SELECT evidence_path,size_bytes,sha256 FROM source_file_coverage "
                    "WHERE snapshot_id=?",
                    (snapshot_id,),
                )
            }
            coverage_failures += manifest_files != coverage_rows
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError, TypeError, KeyError):
            coverage_failures += 1

        try:
            ndjson_failures += _validate_source_records_ndjson(
                database,
                root_fd,
                snapshot_id,
                str(snapshot["source_records_ndjson_path"]),
                str(snapshot["source_records_ndjson_sha256"]),
            )
            ndjson_failures += _validate_conversations_ndjson(
                database,
                root_fd,
                snapshot_id,
                str(snapshot["decoded_ndjson_path"]),
                str(snapshot["decoded_ndjson_sha256"]),
            )
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
            ndjson_failures += 1
    checks.violations("snapshot_artifacts.hash.violations", artifact_failures)
    checks.violations("source_file_coverage.manifest_closure.violations", coverage_failures)
    checks.violations("decoded_ndjson.closure.violations", ndjson_failures)


def _scan_ndjson(
    root_fd: int,
    relative_path: str,
    consume: Callable[[int, dict[str, Any]], None],
) -> tuple[int, _FileDigest]:
    descriptor, before = _open_regular_at(root_fd, relative_path)
    digest = hashlib.sha256()
    total = 0
    record_count = 0
    pending = bytearray()
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
            pending.extend(block)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                line = bytes(pending[:newline])
                del pending[: newline + 1]
                if not line:
                    raise ValueError("blank NDJSON record")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("NDJSON record is not an object")
                consume(record_count, value)
                record_count += 1
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if pending:
        raise ValueError("NDJSON file lacks a final newline")
    if not _stable_file_state(before, after) or total != after.st_size:
        raise RuntimeError("NDJSON changed while validating")
    return record_count, _FileDigest(total, digest.hexdigest())


def _validate_source_records_ndjson(
    database: sqlite3.Connection,
    root_fd: int,
    snapshot_id: int,
    relative_path: str,
    expected_file_hash: str,
) -> int:
    failures = 0

    def consume(line_number: int, envelope: dict[str, Any]) -> None:
        nonlocal failures
        row = database.execute(
            "SELECT * FROM source_records WHERE snapshot_id=? AND ndjson_line_number=?",
            (snapshot_id, line_number),
        ).fetchone()
        if row is None:
            failures += 1
            return
        try:
            provenance = envelope["provenance"]
            raw = envelope["raw"]
            raw_hash = _json_hash(raw)
            failures += raw_hash != envelope["raw_sha256"]
            failures += raw_hash != row["raw_sha256"]
            failures += _canonical_json(raw) != row["raw_json"]
            failures += envelope.get("record_kind") != row["record_kind"]
            failures += envelope.get("provider_record_kind") != row["provider_record_kind"]
            failures += envelope.get("record_scope") != row["record_scope"]
            failures += envelope.get("native_id") != row["native_id"]
            failures += provenance.get("source_file") != row["source_file_path"]
            failures += provenance.get("array_index") != row["array_index"]
            failures += provenance.get("json_pointer") != row["json_pointer"]
            failures += provenance.get("line_number") != line_number
        except (KeyError, TypeError):
            failures += 1
    record_count, file_digest = _scan_ndjson(root_fd, relative_path, consume)
    failures += file_digest.sha256 != expected_file_hash
    failures += record_count != _count(
        database, "SELECT count(*) FROM source_records WHERE snapshot_id=?", (snapshot_id,)
    )
    return int(failures)


def _validate_conversations_ndjson(
    database: sqlite3.Connection,
    root_fd: int,
    snapshot_id: int,
    relative_path: str,
    expected_file_hash: str,
) -> int:
    failures = 0

    def consume(_line_number: int, envelope: dict[str, Any]) -> None:
        nonlocal failures
        try:
            provenance = envelope["provenance"]
            pointer = f"/{provenance['array_index']}"
            row = database.execute(
                "SELECT * FROM source_records WHERE snapshot_id=? "
                "AND source_file_path=? AND json_pointer=? AND record_kind='conversation'",
                (snapshot_id, str(provenance["source_file"]), pointer),
            ).fetchone()
            if row is None:
                failures += 1
                return
            raw = envelope["raw"]
            raw_hash = _json_hash(raw)
            failures += envelope.get("record_type") != "chatgpt_official_conversation"
            failures += raw_hash != envelope["raw_sha256"]
            failures += raw_hash != row["raw_sha256"]
            failures += _canonical_json(raw) != row["raw_json"]
            failures += envelope.get("native_id") != row["native_id"]
        except (KeyError, TypeError):
            failures += 1
    record_count, file_digest = _scan_ndjson(root_fd, relative_path, consume)
    failures += file_digest.sha256 != expected_file_hash
    failures += record_count != _count(
        database,
        "SELECT count(*) FROM source_records WHERE snapshot_id=? AND record_kind='conversation'",
        (snapshot_id,),
    )
    return int(failures)


def _validate_cas(
    database: sqlite3.Connection, root_fd: int, checks: _Checks
) -> None:
    rows = tuple(database.execute("SELECT sha256,size_bytes,storage_path FROM assets"))
    expected_paths: set[str] = set()
    failures = 0
    total_bytes = 0
    for row in rows:
        sha256 = str(row["sha256"])
        size = int(row["size_bytes"])
        storage_path = str(row["storage_path"])
        canonical_path = f"{CAS_ROOT}/{sha256[:2]}/{sha256}"
        total_bytes += size
        if not _SHA256.fullmatch(sha256) or storage_path != canonical_path:
            failures += 1
            continue
        expected_paths.add(storage_path)
    try:
        actual_paths = _enumerate_cas(root_fd, allow_missing=not expected_paths)
    except (OSError, ValueError):
        actual_paths = frozenset()
        failures += 1
    failures += len(expected_paths - actual_paths) + len(actual_paths - expected_paths)
    for row in rows:
        storage_path = str(row["storage_path"])
        if storage_path not in expected_paths or storage_path not in actual_paths:
            continue
        try:
            actual = _digest_vault_file(root_fd, storage_path)
            failures += actual.size != int(row["size_bytes"])
            failures += actual.sha256 != row["sha256"]
        except (OSError, ValueError, RuntimeError):
            failures += 1
    checks.metric("global.assets.count", count=len(rows))
    checks.metric("global.assets.bytes", count=total_bytes)
    checks.violations("cas.closure.violations", int(failures))
    checks.violations(
        "cas.orphan_assets.violations",
        _count(
            database,
            "SELECT count(*) FROM assets a LEFT JOIN asset_observations ao ON ao.asset_id=a.id "
            "WHERE ao.id IS NULL",
        ),
    )


def _validate_search(database: sqlite3.Connection, checks: _Checks) -> None:
    mismatches = 0
    for row in database.execute(
        "SELECT sd.*,mv.raw_json,mi.identity_key expected_identity,mv.author_role expected_role,"
        "mv.content_type expected_content_type FROM search_documents sd "
        "JOIN message_versions mv ON mv.id=sd.message_version_id "
        "JOIN message_identities mi ON mi.id=mv.message_identity_id "
        "WHERE sd.document_kind='conversation_message'"
    ):
        try:
            expected_content = _message_text(json.loads(row["raw_json"]))
        except (json.JSONDecodeError, TypeError):
            mismatches += 1
            continue
        mismatches += (
            row["group_message_version_id"] is not None
            or row["identity_key"] != row["expected_identity"]
            or row["author_role"] != row["expected_role"]
            or row["content_type"] != row["expected_content_type"]
            or row["content"] != expected_content
        )
    for row in database.execute(
        "SELECT sd.*,gv.raw_json,gi.identity_key expected_identity,gv.role expected_role "
        "FROM search_documents sd JOIN group_message_versions gv "
        "ON gv.id=sd.group_message_version_id JOIN group_message_identities gi "
        "ON gi.id=gv.group_message_identity_id WHERE sd.document_kind='group_message'"
    ):
        try:
            raw = json.loads(row["raw_json"])
            expected_content = raw.get("text") if isinstance(raw.get("text"), str) else ""
        except (json.JSONDecodeError, TypeError):
            mismatches += 1
            continue
        mismatches += (
            row["message_version_id"] is not None
            or row["identity_key"] != row["expected_identity"]
            or row["author_role"] != row["expected_role"]
            or row["content_type"] != "group_text"
            or row["content"] != expected_content
        )
    expected_documents = _count(database, "SELECT count(*) FROM message_versions") + _count(
        database, "SELECT count(*) FROM group_message_versions"
    )
    actual_documents = _count(database, "SELECT count(*) FROM search_documents")
    mismatches += expected_documents != actual_documents
    checks.metric("global.search_documents.count", count=actual_documents)
    checks.violations("search_documents.projection.violations", int(mismatches))

    try:
        with tempfile.TemporaryDirectory(prefix="personal-vault-fts-") as temporary:
            temporary_database = Path(temporary) / "archive.sqlite"
            with closing(sqlite3.connect(temporary_database)) as copy:
                database.backup(copy)
                try:
                    copy.execute(
                        "INSERT INTO message_fts(message_fts,rank) VALUES(?,?)",
                        ("integrity-check", 1),
                    )
                finally:
                    copy.rollback()
        checks.violations("fts.external_content_integrity.violations", 0)
    except sqlite3.DatabaseError as error:
        checks.failure("fts.external_content_integrity.violations", error)


def _validate_staging(root_fd: int, checks: _Checks) -> None:
    try:
        staging = _open_dir_at(root_fd, (".staging",))
    except FileNotFoundError:
        checks.violations("staging.entries.violations", 0)
        return
    try:
        with os.scandir(staging) as entries:
            count = sum(1 for _ in entries)
    finally:
        os.close(staging)
    checks.violations("staging.entries.violations", count)


def _global_metrics(database: sqlite3.Connection, checks: _Checks) -> None:
    checks.metric("snapshots.count", count=_count(database, "SELECT count(*) FROM snapshots"))
    for table in (
        "source_file_coverage",
        "conversation_identities",
        "node_identities",
        "message_identities",
        "conversation_versions",
        "node_versions",
        "message_versions",
    ):
        checks.metric(f"global.{table}.count", count=_count(database, f"SELECT count(*) FROM {table}"))


def validate_archive(
    vault_root: Path,
    database_path: Path | None = None,
    *,
    expectations: ValidationExpectations | None = None,
) -> ArchiveValidationResult:
    """Validate a canonical archive without writing to it or returning private content.

    The source database is opened with SQLite ``mode=ro`` and ``query_only``. FTS5's
    write-shaped integrity command runs only against a temporary SQLite backup.
    """

    checks = _Checks(expectations)
    vault_root = Path(vault_root)
    database_path = Path(database_path or "canonical/archive.sqlite")
    try:
        root_fd = _open_root(vault_root)
    except (OSError, ValueError, NotImplementedError) as error:
        checks.failure("vault.open", error)
        return checks.finish()
    try:
        try:
            database_relative = _relative_database_path(vault_root, database_path)
            descriptor, _ = _open_regular_at(root_fd, database_relative)
            os.close(descriptor)
            database_absolute = Path(os.path.abspath(vault_root)) / Path(
                *_relative_parts(database_relative)
            )
            database = _connect_read_only(database_absolute)
        except (OSError, ValueError, sqlite3.Error) as error:
            checks.failure("database.open", error)
            return checks.finish()

        with closing(database):
            try:
                integrity_rows = tuple(database.execute("PRAGMA integrity_check"))
                integrity_failures = sum(str(row[0]) != "ok" for row in integrity_rows)
                checks.violations("sqlite.integrity.violations", integrity_failures)
                checks.violations(
                    "sqlite.foreign_keys.violations",
                    sum(1 for _ in database.execute("PRAGMA foreign_key_check")),
                )
                schema_version = _count(database, "PRAGMA user_version")
                checks.metric("schema.version", count=schema_version)
                if schema_version != SCHEMA_VERSION:
                    checks.items.append(
                        ValidationCheck(
                            name="schema.compatibility",
                            status="fail",
                            count=schema_version,
                        )
                    )
                    return checks.finish()
                checks.violations("schema.compatibility", 0)

                snapshots = tuple(
                    database.execute(
                        "SELECT * FROM snapshots ORDER BY source_id,captured_at_us,snapshot_key"
                    )
                )
                _global_metrics(database, checks)
                _validate_import_counts(database, checks)
                _validate_absences(database, checks)
                _validate_json_columns(database, checks)
                _validate_raw_payloads(database, checks)
                _validate_snapshot_metrics(database, checks, snapshots)
                for snapshot in snapshots:
                    _validate_snapshot_invariants(
                        database,
                        checks,
                        int(snapshot["id"]),
                        _safe_snapshot_label(str(snapshot["snapshot_key"])),
                    )
                _validate_state_digests(database, checks, snapshots)
                _validate_manifest_and_ndjson(database, root_fd, checks, snapshots)
                _validate_cas(database, root_fd, checks)
                _validate_search(database, checks)
                _validate_staging(root_fd, checks)
            except (sqlite3.Error, OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
                checks.failure("validation.execution", error)
    finally:
        os.close(root_fd)
    return checks.finish()
