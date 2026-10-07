"""Conservative, review-gated personal memory candidates."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .reader import ReaderError, connect_reader_database


MEMORY_SCHEMA_VERSION = 2
EXTRACTOR_VERSION = "explicit-user-statement-v1"
_CANDIDATE_ID = re.compile(r"memc_[0-9a-f]{32}\Z")
_KINDS = {
    "preference": re.compile(r"(?:我|本人).{0,12}(?:喜欢|偏好|习惯|希望|倾向|不喜欢|讨厌)|\bI (?:prefer|like|want)\b", re.I),
    "identity": re.compile(r"(?:我是|我叫|我的职业|我的专业|我目前是)|\bI am\b|\bmy (?:name|job|major) is\b", re.I),
    "plan": re.compile(r"(?:我|本人).{0,8}(?:打算|计划|正在|准备|想要)|\bI (?:plan|intend|am working|want to)\b", re.I),
    "boundary": re.compile(r"(?:请|以后|回答时)?.{0,6}(?:不要|别再|不需要)|\b(?:do not|don't|never)\b", re.I),
}


class MemoryStoreError(RuntimeError):
    """Raised when memory review data cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class MemoryScanResult:
    memory_database_path: Path
    snapshot_id: int
    scanned_user_messages: int
    matched_messages: int
    inserted_candidates: int
    existing_candidates: int
    pending_candidates: int
    confirmed_candidates: int
    rejected_candidates: int


@dataclass(frozen=True, slots=True)
class MemoryDecisionResult:
    candidate_id: str
    prior_status: str
    status: str
    decided_at: str


@dataclass(frozen=True, slots=True)
class MemoryBatchDecisionResult:
    candidate_ids: tuple[str, ...]
    status: str
    updated_count: int
    decided_at: str


@dataclass(frozen=True, slots=True)
class MemoryEditResult:
    candidate_id: str
    statement: str
    edited: bool
    reviewed_at: str


@dataclass(frozen=True, slots=True)
class ProfileExportResult:
    path: Path
    confirmed_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MemoryReviewExportResult:
    path: Path
    pending_count: int
    sha256: str


_SCHEMA = f"""
PRAGMA user_version = {MEMORY_SCHEMA_VERSION};
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  extractor_version TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('preference','identity','plan','boundary')),
  statement TEXT NOT NULL,
  normalized_statement TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  sensitivity TEXT NOT NULL DEFAULT 'private',
  source_snapshot_id INTEGER NOT NULL,
  source_snapshot_key TEXT NOT NULL,
  source_conversation_identity_key TEXT NOT NULL,
  source_message_identity_key TEXT NOT NULL,
  source_message_raw_sha256 TEXT NOT NULL,
  message_create_time REAL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  reviewed_statement TEXT,
  reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS candidates_status_kind ON candidates(status, kind, candidate_id);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  prior_status TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('confirmed','rejected','pending')),
  note TEXT,
  decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_edits (
  id INTEGER PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  prior_statement TEXT NOT NULL,
  statement TEXT NOT NULL,
  edited_at TEXT NOT NULL
);
"""


def _upgrade_memory_v1_to_v2(database: sqlite3.Connection) -> None:
    """Upgrade the one real on-disk v1 shape without rebuilding candidates."""

    with database:
        database.execute("ALTER TABLE candidates ADD COLUMN reviewed_statement TEXT")
        database.execute("ALTER TABLE candidates ADD COLUMN reviewed_at TEXT")
        database.execute(
            """CREATE TABLE candidate_edits (
                 id INTEGER PRIMARY KEY,
                 candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                 prior_statement TEXT NOT NULL,
                 statement TEXT NOT NULL,
                 edited_at TEXT NOT NULL
               )"""
        )
        database.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")


def _connect_memory(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise MemoryStoreError("memory database path must not be a symlink")
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if version == 0 and not database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone():
        database.executescript(_SCHEMA)
    elif version == 1:
        _upgrade_memory_v1_to_v2(database)
    elif version != MEMORY_SCHEMA_VERSION:
        database.close()
        raise MemoryStoreError(
            f"memory schema version {version} is unsupported; expected {MEMORY_SCHEMA_VERSION}"
        )
    return database


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _classify(value: str) -> str | None:
    for kind in ("boundary", "preference", "identity", "plan"):
        if _KINDS[kind].search(value):
            return kind
    return None


def _candidate_id(message_identity_key: str, raw_sha256: str, kind: str) -> str:
    payload = "\0".join((EXTRACTOR_VERSION, message_identity_key, raw_sha256, kind))
    return "memc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def scan_memory_candidates(
    *, vault_root: Path, snapshot_id: int, memory_database_path: Path | None = None
) -> MemoryScanResult:
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    canonical = vault_root / "canonical" / "archive.sqlite"
    memory_path = Path(
        memory_database_path or vault_root / "memory" / "memory.sqlite"
    ).expanduser().resolve()
    if memory_path == canonical or memory_path.is_relative_to(vault_root / "canonical"):
        raise MemoryStoreError("memory review database must be separate from canonical data")
    try:
        source = connect_reader_database(canonical)
    except ReaderError as error:
        raise MemoryStoreError(str(error)) from error
    memory = _connect_memory(memory_path)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    scanned = matched = inserted = existing = 0
    try:
        snapshot = source.execute(
            "SELECT snapshot_key FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise MemoryStoreError("snapshot was not found")
        rows = source.execute(
            """SELECT ci.identity_key AS conversation_identity_key,
                      mi.identity_key AS message_identity_key,
                      mv.raw_sha256, mv.create_time, sd.content
                 FROM current_branch_nodes cb
                 JOIN node_observations no
                   ON no.snapshot_id = cb.snapshot_id
                  AND no.node_identity_id = cb.node_identity_id
                 JOIN node_identities ni ON ni.id = no.node_identity_id
                 JOIN conversation_identities ci ON ci.id = ni.conversation_identity_id
                 JOIN message_observations mo ON mo.node_observation_id = no.id
                 JOIN message_identities mi ON mi.id = mo.message_identity_id
                 JOIN message_versions mv ON mv.id = mo.message_version_id
                 JOIN search_documents sd ON sd.message_version_id = mv.id
                WHERE cb.snapshot_id = ? AND mv.author_role = 'user'
                  AND length(trim(sd.content)) >= 8
                ORDER BY ci.identity_key, cb.depth, mi.identity_key""",
            (snapshot_id,),
        )
        with memory:
            for row in rows:
                scanned += 1
                normalized = _normalize(str(row["content"]))
                kind = _classify(normalized)
                if kind is None:
                    continue
                matched += 1
                candidate_id = _candidate_id(
                    str(row["message_identity_key"]), str(row["raw_sha256"]), kind
                )
                cursor = memory.execute(
                    """INSERT OR IGNORE INTO candidates(
                           candidate_id, extractor_version, kind, statement,
                           normalized_statement, status, confidence, sensitivity,
                           source_snapshot_id, source_snapshot_key,
                           source_conversation_identity_key, source_message_identity_key,
                           source_message_raw_sha256, message_create_time,
                           first_seen_at, last_seen_at)
                         VALUES (?, ?, ?, ?, ?, 'pending', 0.55, 'private',
                                 ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate_id,
                        EXTRACTOR_VERSION,
                        kind,
                        normalized[:4000],
                        normalized.casefold()[:4000],
                        snapshot_id,
                        snapshot["snapshot_key"],
                        row["conversation_identity_key"],
                        row["message_identity_key"],
                        row["raw_sha256"],
                        row["create_time"],
                        now,
                        now,
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    existing += 1
                    memory.execute(
                        "UPDATE candidates SET last_seen_at = ? WHERE candidate_id = ?",
                        (now, candidate_id),
                    )
        counts = {
            str(row["status"]): int(row["count"])
            for row in memory.execute(
                "SELECT status, count(*) AS count FROM candidates GROUP BY status"
            )
        }
    finally:
        source.close()
        memory.close()
    return MemoryScanResult(
        memory_database_path=memory_path,
        snapshot_id=snapshot_id,
        scanned_user_messages=scanned,
        matched_messages=matched,
        inserted_candidates=inserted,
        existing_candidates=existing,
        pending_candidates=counts.get("pending", 0),
        confirmed_candidates=counts.get("confirmed", 0),
        rejected_candidates=counts.get("rejected", 0),
    )


def decide_memory_candidate(
    *,
    vault_root: Path,
    candidate_id: str,
    status: str,
    note: str | None = None,
    memory_database_path: Path | None = None,
) -> MemoryDecisionResult:
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise MemoryStoreError("candidate ID is invalid")
    if status not in {"confirmed", "rejected", "pending"}:
        raise MemoryStoreError("memory decision status is invalid")
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    memory_path = Path(
        memory_database_path or vault_root / "memory" / "memory.sqlite"
    ).expanduser().resolve()
    database = _connect_memory(memory_path)
    decided_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        with database:
            row = database.execute(
                "SELECT status FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise MemoryStoreError("memory candidate was not found")
            prior = str(row["status"])
            database.execute(
                "UPDATE candidates SET status = ? WHERE candidate_id = ?",
                (status, candidate_id),
            )
            database.execute(
                "INSERT INTO decisions(candidate_id, prior_status, status, note, decided_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (candidate_id, prior, status, note, decided_at),
            )
    finally:
        database.close()
    return MemoryDecisionResult(candidate_id, prior, status, decided_at)


def decide_memory_candidates(
    *,
    vault_root: Path,
    candidate_ids: list[str] | tuple[str, ...],
    status: str,
    note: str | None = None,
    memory_database_path: Path | None = None,
) -> MemoryBatchDecisionResult:
    """Apply one explicit decision atomically to a non-empty candidate set."""

    unique_ids = tuple(dict.fromkeys(candidate_ids))
    if not unique_ids:
        raise MemoryStoreError("at least one memory candidate is required")
    if len(unique_ids) > 1000:
        raise MemoryStoreError("a batch decision may contain at most 1000 candidates")
    if any(not _CANDIDATE_ID.fullmatch(value) for value in unique_ids):
        raise MemoryStoreError("candidate ID is invalid")
    if status not in {"confirmed", "rejected", "pending"}:
        raise MemoryStoreError("memory decision status is invalid")
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    memory_path = Path(
        memory_database_path or vault_root / "memory" / "memory.sqlite"
    ).expanduser().resolve()
    database = _connect_memory(memory_path)
    decided_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        with database:
            prior_by_id: dict[str, str] = {}
            for candidate_id in unique_ids:
                row = database.execute(
                    "SELECT status FROM candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                if row is None:
                    raise MemoryStoreError("one or more memory candidates were not found")
                prior_by_id[candidate_id] = str(row["status"])
            for candidate_id in unique_ids:
                database.execute(
                    "UPDATE candidates SET status = ? WHERE candidate_id = ?",
                    (status, candidate_id),
                )
                database.execute(
                    "INSERT INTO decisions(candidate_id, prior_status, status, note, decided_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (candidate_id, prior_by_id[candidate_id], status, note, decided_at),
                )
    finally:
        database.close()
    return MemoryBatchDecisionResult(unique_ids, status, len(unique_ids), decided_at)


def edit_memory_candidate(
    *,
    vault_root: Path,
    candidate_id: str,
    statement: str,
    memory_database_path: Path | None = None,
) -> MemoryEditResult:
    """Persist a concise reviewed wording while retaining the extracted original."""

    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise MemoryStoreError("candidate ID is invalid")
    reviewed = _normalize(statement)
    if not reviewed:
        raise MemoryStoreError("reviewed statement cannot be empty")
    if len(reviewed) > 4000:
        raise MemoryStoreError("reviewed statement is longer than 4000 characters")
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    memory_path = Path(
        memory_database_path or vault_root / "memory" / "memory.sqlite"
    ).expanduser().resolve()
    database = _connect_memory(memory_path)
    reviewed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        with database:
            row = database.execute(
                """SELECT statement, COALESCE(reviewed_statement, statement) AS effective_statement
                     FROM candidates WHERE candidate_id = ?""",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise MemoryStoreError("memory candidate was not found")
            original = str(row["statement"])
            prior = str(row["effective_statement"])
            stored = None if reviewed == original else reviewed
            database.execute(
                "UPDATE candidates SET reviewed_statement = ?, reviewed_at = ? WHERE candidate_id = ?",
                (stored, reviewed_at, candidate_id),
            )
            if prior != reviewed:
                database.execute(
                    "INSERT INTO candidate_edits(candidate_id, prior_statement, statement, edited_at) "
                    "VALUES (?, ?, ?, ?)",
                    (candidate_id, prior, reviewed, reviewed_at),
                )
    finally:
        database.close()
    return MemoryEditResult(candidate_id, reviewed, reviewed != original, reviewed_at)


def export_confirmed_profile(
    *,
    vault_root: Path,
    output_path: Path | None = None,
    memory_database_path: Path | None = None,
) -> ProfileExportResult:
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    memory_path = Path(
        memory_database_path or vault_root / "memory" / "memory.sqlite"
    ).expanduser().resolve(strict=True)
    output = Path(output_path or vault_root / "memory" / "profile.md").expanduser().resolve()
    if output == memory_path:
        raise MemoryStoreError("profile output cannot replace the memory database")
    database = _connect_memory(memory_path)
    try:
        rows = database.execute(
            """SELECT *, COALESCE(reviewed_statement, statement) AS effective_statement
                 FROM candidates WHERE status = 'confirmed'
                 ORDER BY kind, candidate_id"""
        ).fetchall()
    finally:
        database.close()
    labels = {"identity": "身份", "preference": "偏好", "boundary": "协作边界", "plan": "计划与当前状态"}
    lines = [
        "# 已确认个人资料",
        "",
        "> 仅包含已确认的候选。原话不等于永久事实；时间敏感内容应定期复审。",
        "",
    ]
    for kind in ("identity", "preference", "boundary", "plan"):
        selected = [row for row in rows if row["kind"] == kind]
        if not selected:
            continue
        lines.extend((f"## {labels[kind]}", ""))
        for row in selected:
            source_ref = f"{row['source_snapshot_key']} / {row['source_message_identity_key']}"
            lines.extend(
                (
                    f"- {row['effective_statement']}",
                    f"  - candidate_id: `{row['candidate_id']}`",
                    f"  - source_ref: `{source_ref}`",
                    "",
                )
            )
    if not rows:
        lines.extend(("尚无已确认的记忆。", ""))
    payload = "\n".join(lines).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return ProfileExportResult(output, len(rows), hashlib.sha256(payload).hexdigest())


def export_memory_review_html(
    *,
    vault_root: Path,
    output_path: Path | None = None,
    memory_database_path: Path | None = None,
) -> MemoryReviewExportResult:
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    memory_path = Path(
        memory_database_path or vault_root / "memory" / "memory.sqlite"
    ).expanduser().resolve(strict=True)
    output = Path(output_path or vault_root / "memory" / "review.html").expanduser().resolve()
    database = _connect_memory(memory_path)
    try:
        rows = database.execute(
            "SELECT * FROM candidates WHERE status='pending' ORDER BY kind,candidate_id"
        ).fetchall()
    finally:
        database.close()
    cards = []
    for row in rows:
        candidate_id = html.escape(str(row["candidate_id"]), quote=True)
        source_ref = html.escape(
            f"{row['source_snapshot_key']} / {row['source_message_identity_key']}",
            quote=True,
        )
        cards.append(
            '<article class="candidate">'
            f'<header><span>{html.escape(str(row["kind"]))}</span><code>{candidate_id}</code></header>'
            f'<p>{html.escape(str(row["statement"]))}</p>'
            f'<footer><code>{source_ref}</code></footer>'
            '</article>'
        )
    payload = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>记忆候选审核</title>
<style>:root{{--bg:#f4f2eb;--ink:#17201f;--card:#fff;--line:#ced6d2;--muted:#65716e;--accent:#17623f}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,sans-serif}}main{{max-width:960px;margin:auto;padding:40px 20px}}h1{{font-size:42px}}.workbench{{padding:18px;border:1px solid var(--line);border-radius:14px;background:#e5f1e9}}.workbench a{{display:inline-block;margin-top:8px;padding:9px 14px;border-radius:9px;background:var(--accent);color:#fff;text-decoration:none;font-weight:700}}.candidate{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}}header{{display:flex;justify-content:space-between;gap:12px;color:var(--muted)}}p{{white-space:pre-wrap}}footer{{color:var(--muted);overflow-wrap:anywhere}}code{{overflow-wrap:anywhere}}</style>
</head><body><main><p>PERSONAL MEMORY VAULT</p><h1>待审核记忆候选</h1>
<section class="workbench"><strong>这是只读便携快照</strong><p>真正的批量审核、上下文、改写和自动保存请使用本地交互工作台。</p><a href="http://127.0.0.1:8766/">打开交互式审核工作台</a></section>
<p>共 {len(rows)} 条。这里展示的是用户原话候选，不是已经确认的事实。</p>
{''.join(cards)}</main></body></html>""".encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return MemoryReviewExportResult(output, len(rows), hashlib.sha256(payload).hexdigest())
