"""Provider-specific continuity packages derived only from confirmed memories."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .memory_store import MEMORY_SCHEMA_VERSION, MemoryStoreError
from .reader import ReaderError, connect_reader_database


class MigrationError(RuntimeError):
    """Raised when a continuity package cannot be generated safely."""


@dataclass(frozen=True, slots=True)
class MigrationPackResult:
    root: Path
    manifest_path: Path
    provider: str
    confirmed_memories: int
    file_count: int
    tree_sha256: str


def _memory_database(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise MigrationError("memory review database does not exist or is unsafe")
    database = sqlite3.connect(f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if version != MEMORY_SCHEMA_VERSION:
        database.close()
        raise MigrationError("memory review database schema is unsupported")
    return database


def _memory_lines(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        lines.extend(
            (
                f"- {row['effective_statement']}",
                f"  - status: confirmed",
                f"  - candidate_id: `{row['candidate_id']}`",
                f"  - source_ref: `{row['source_snapshot_key']} / {row['source_message_identity_key']}`",
                "",
            )
        )
    return lines


def _document(title: str, intro: str, rows: Iterable[Mapping[str, Any]]) -> str:
    rows = list(rows)
    lines = [f"# {title}", "", intro, ""]
    lines.extend(_memory_lines(rows) if rows else ["尚无已确认的条目。", ""])
    return "\n".join(lines)


def _snapshot_index(canonical: sqlite3.Connection) -> str:
    rows = canonical.execute(
        """SELECT s.snapshot_key, s.captured_at, so.provider, so.kind,
                  ir.conversations_count, ir.messages_count, ir.warnings_count
             FROM snapshots s JOIN sources so ON so.id = s.source_id
             LEFT JOIN import_runs ir ON ir.snapshot_id = s.id
            ORDER BY s.captured_at_us, s.id"""
    ).fetchall()
    lines = [
        "# 历史档案索引",
        "",
        "这里只列出档案覆盖范围，不把完整历史上传内容混入高层个人资料。",
        "",
    ]
    for row in rows:
        lines.extend(
            (
                f"## {row['snapshot_key']}",
                "",
                f"- captured_at: `{row['captured_at']}`",
                f"- source: `{row['provider']} / {row['kind']}`",
                f"- conversations: {int(row['conversations_count'] or 0)}",
                f"- messages: {int(row['messages_count'] or 0)}",
                f"- warnings: {int(row['warnings_count'] or 0)}",
                "",
            )
        )
    return "\n".join(lines)


def _tree_sha256(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, payload in sorted(files.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def build_chatgpt_migration_pack(
    *, vault_root: Path, output_directory: Path
) -> MigrationPackResult:
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.exists():
        raise MigrationError("migration package destination already exists")
    memory_path = vault_root / "memory" / "memory.sqlite"
    try:
        canonical = connect_reader_database(vault_root / "canonical" / "archive.sqlite")
    except ReaderError as error:
        raise MigrationError(str(error)) from error
    try:
        memory = _memory_database(memory_path)
    except (MemoryStoreError, ReaderError) as error:
        canonical.close()
        raise MigrationError(str(error)) from error
    try:
        rows = memory.execute(
            """SELECT *, COALESCE(reviewed_statement, statement) AS effective_statement
                 FROM candidates WHERE status='confirmed' ORDER BY kind,candidate_id"""
        ).fetchall()
        by_kind = {
            kind: [row for row in rows if row["kind"] == kind]
            for kind in ("identity", "preference", "boundary", "plan")
        }
        files_text = {
            "00_MIGRATION_README.md": """# ChatGPT 历史上下文迁移包

本目录是外部档案的派生视图，不会恢复旧账号的内部状态或侧栏会话。

使用规则：

1. 只有标记为 confirmed 的内容才能视为用户确认资料。
2. 每条资料都必须保留 candidate_id 和 source_ref。
3. 冲突或时间敏感内容不得静默覆盖，应向用户说明。
4. 不从旧助手回答、thoughts、reasoning_recap 推导用户事实。
5. 详细历史继续留在外部 archive，不要求模型永久记住全部对话。
""",
            "01_CORE_PROFILE.md": _document(
                "核心资料",
                "以下内容来自用户消息，并已由用户显式确认。",
                by_kind["identity"],
            ),
            "02_INTERACTION_PREFERENCES.md": _document(
                "交互偏好与边界",
                "偏好和协作边界均需要来源；旧偏好可能随时间变化。",
                (*by_kind["preference"], *by_kind["boundary"]),
            ),
            "03_CURRENT_STATE.md": _document(
                "当前计划与状态",
                "这些内容时间敏感，导入新账号后应优先复核是否仍然有效。",
                by_kind["plan"],
            ),
            "04_ARCHIVE_INDEX.md": _snapshot_index(canonical),
            "05_RECALL_TESTS.md": """# 迁移后回忆测试

1. 请分别列出已确认的身份、偏好、协作边界和当前计划，并引用 candidate_id。
2. 哪些资料是时间敏感的，迁移后需要再次向用户确认？
3. 如果两条历史资料冲突，请列出双方 source_ref，不要自行选择。
4. 哪些内容只是外部档案索引，并未写入长期记忆？
5. 当资料不足时，请明确回答未知，不要使用旧助手的推测补全。
""",
        }
        files = {name: text.encode("utf-8") for name, text in files_text.items()}
        tree = _tree_sha256(files)
        manifest = {
            "schema": "pmv.provider-migration-pack.v1",
            "provider": "chatgpt",
            "confirmed_memories": len(rows),
            "tree_sha256": tree,
            "files": [
                {
                    "path": name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in sorted(files.items())
            ],
        }
    finally:
        memory.close()
        canonical.close()

    staging = output_directory.parent / f".{output_directory.name}.staging-{uuid.uuid4().hex}"
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        for name, payload in files.items():
            target = staging / name
            target.write_bytes(payload)
            target.chmod(0o600)
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        os.replace(staging, output_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return MigrationPackResult(
        root=output_directory,
        manifest_path=output_directory / "manifest.json",
        provider="chatgpt",
        confirmed_memories=len(rows),
        file_count=len(files),
        tree_sha256=tree,
    )
