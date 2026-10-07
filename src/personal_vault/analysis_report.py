"""Deterministic, non-LLM archive analysis reports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .reader import ReaderError, ReaderRepository


class AnalysisReportError(RuntimeError):
    """Raised when a deterministic analysis report cannot be written safely."""


@dataclass(frozen=True, slots=True)
class AnalysisReportResult:
    root: Path
    json_path: Path
    markdown_path: Path
    snapshot_id: int
    snapshot_key: str
    json_sha256: str
    markdown_sha256: str


def _table(rows: list[dict[str, Any]], key_label: str = "类别") -> list[str]:
    lines = [f"| {key_label} | 数量 |", "|---|---:|"]
    lines.extend(f"| {row['key']} | {int(row['count'])} |" for row in rows)
    return lines


def build_snapshot_analysis_report(
    *, vault_root: Path, snapshot_id: int, output_directory: Path
) -> AnalysisReportResult:
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.exists():
        raise AnalysisReportError("analysis report destination already exists")
    repository = ReaderRepository(vault_root / "canonical" / "archive.sqlite", vault_root)
    try:
        analytics = repository.analytics(snapshot_id)
    except ReaderError as error:
        raise AnalysisReportError(str(error)) from error
    snapshot = analytics["snapshot"]
    branches = analytics["branches"]
    attachments = analytics["attachments"]
    quality = analytics["snapshot_quality"]
    lines = [
        "# 对话档案确定性分析",
        "",
        f"- snapshot: `{snapshot['snapshot_key']}`",
        f"- captured_at: `{snapshot['captured_at']}`",
        "- generated_from: `deterministic_sql`",
        "- llm_inference: `disabled`",
        "- profile_writeback: `disabled`",
        "",
        "## 会话结构",
        "",
        f"- 节点：{branches['nodes']}",
        f"- 当前主线节点：{branches['current_nodes']}",
        f"- 替代节点：{branches['alternative_nodes']}",
        f"- 分叉点：{branches['branch_points']}",
        f"- 含分支会话：{branches['conversations_with_branches']}",
        "",
        "## 附件",
        "",
        f"- 引用：{attachments['references']}",
        f"- 已解析：{attachments['resolved']}",
        f"- 未解析：{attachments['unresolved']}",
        f"- 歧义：{attachments['ambiguous']}",
        f"- 唯一资产：{attachments['unique_assets']}",
        f"- 总字节：{attachments['total_bytes']}",
        "",
        "## 角色分布",
        "",
        *_table(analytics["roles"]),
        "",
        "## 模型分布",
        "",
        *_table(analytics["models"], "模型"),
        "",
        "## 数据质量",
        "",
        f"- warnings: {quality['diagnostics']['warnings']}",
        f"- errors: {quality['diagnostics']['errors']}",
        f"- source_absences: {quality['source_absences']}",
        "- source_deletions: unknown（导出没有明确删除事件）",
        "",
        "详细月份、内容类型、快照差异和 claims 见 `analytics.json`。",
        "",
    ]
    json_payload = (
        json.dumps(analytics, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    markdown_payload = "\n".join(lines).encode("utf-8")
    staging = output_directory.parent / f".{output_directory.name}.staging-{uuid.uuid4().hex}"
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        (staging / "analytics.json").write_bytes(json_payload)
        (staging / "report.md").write_bytes(markdown_payload)
        os.replace(staging, output_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return AnalysisReportResult(
        root=output_directory,
        json_path=output_directory / "analytics.json",
        markdown_path=output_directory / "report.md",
        snapshot_id=snapshot_id,
        snapshot_key=snapshot["snapshot_key"],
        json_sha256=hashlib.sha256(json_payload).hexdigest(),
        markdown_sha256=hashlib.sha256(markdown_payload).hexdigest(),
    )
