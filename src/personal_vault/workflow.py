"""High-level, metadata-only workflows for recurring vault updates."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .archive_validation import ArchiveValidationResult, validate_archive
from .chatgpt_export import (
    ChatGPTDoctorResult,
    ChatGPTImportResult,
    doctor_chatgpt_export,
    import_chatgpt_export,
)
from .evidence import (
    EvidenceError,
    build_manifest,
    load_manifest,
    verify_manifest,
    write_manifest,
)


_SAFE_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")


class WorkflowError(RuntimeError):
    """Raised when a high-level workflow cannot preserve its evidence boundary."""


@dataclass(frozen=True, slots=True)
class ChatGPTUpdateResult:
    status: str
    source_id: str
    manifest_path: Path
    manifest_status: str
    evidence_tree_sha256: str
    doctor: ChatGPTDoctorResult
    import_result: ChatGPTImportResult
    validation: ArchiveValidationResult
    report_path: Path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _manifest_for_update(
    evidence_root: Path,
    manifest_path: Path,
    *,
    source_id: str,
    captured_at: str,
) -> tuple[dict[str, Any], str]:
    if manifest_path.exists():
        manifest = load_manifest(manifest_path)
        source = manifest.get("source")
        expected = {
            "id": source_id,
            "kind": "chatgpt_official_export",
            "captured_at": captured_at,
        }
        if not isinstance(source, dict) or any(
            source.get(key) != value for key, value in expected.items()
        ):
            raise WorkflowError(
                "existing update manifest does not match source id, kind, or capture time"
            )
        verification = verify_manifest(evidence_root, manifest)
        if not verification.ok:
            raise WorkflowError(
                "existing update manifest no longer matches the evidence tree: "
                + "; ".join(verification.differences)
            )
        return manifest, "reused"

    manifest = build_manifest(
        evidence_root,
        source_id=source_id,
        source_kind="chatgpt_official_export",
        captured_at=captured_at,
    )
    write_manifest(manifest, manifest_path, evidence_root=evidence_root)
    return manifest, "created"


def update_chatgpt_official(
    *,
    evidence_root: Path,
    vault_root: Path,
    identity_scope: str,
    source_id: str,
    captured_at: str,
    manifest_path: Path | None = None,
    progress=None,
) -> ChatGPTUpdateResult:
    """Capture, import, and independently validate one official-export snapshot."""

    if not _SAFE_SOURCE_ID.fullmatch(source_id):
        raise WorkflowError(
            "source_id must be 1-160 safe filename characters without path separators"
        )
    evidence_root = Path(evidence_root).expanduser().resolve(strict=True)
    vault_root = Path(vault_root).expanduser().resolve()
    if vault_root == evidence_root or vault_root.is_relative_to(evidence_root):
        raise WorkflowError("vault root cannot equal or be inside the evidence root")
    manifest_path = Path(
        manifest_path
        or vault_root / "manifests" / "sources" / source_id / "manifest.json"
    ).expanduser().resolve()
    if manifest_path == evidence_root or manifest_path.is_relative_to(evidence_root):
        raise WorkflowError("update manifest cannot be stored inside the evidence root")

    notify = progress or (lambda phase: None)
    notify("校验来源清单")
    try:
        manifest, manifest_status = _manifest_for_update(
            evidence_root,
            manifest_path,
            source_id=source_id,
            captured_at=captured_at,
        )
    except EvidenceError as error:
        raise WorkflowError(f"cannot prepare update evidence manifest: {error}") from error

    notify("检查导出结构")
    doctor = doctor_chatgpt_export(
        evidence_root=evidence_root, manifest_path=manifest_path
    )
    notify("增量导入")
    imported = import_chatgpt_export(
        evidence_root=evidence_root,
        manifest_path=manifest_path,
        output_root=vault_root,
        identity_scope=identity_scope,
    )
    notify("验收归档")
    validation = validate_archive(vault_root)
    report_path = (
        vault_root / "derived" / "reports" / "updates" / f"{source_id}.json"
    )
    report = {
        "schema": "pmv.chatgpt-official-update-report.v1",
        "status": "pass" if validation.ok else "validation_failed",
        "metadata_only": True,
        "source_id": source_id,
        "manifest": {
            "path": str(manifest_path),
            "status": manifest_status,
            "tree_sha256": manifest["tree_sha256"],
        },
        "doctor": asdict(doctor),
        "import": {
            **asdict(imported),
            "database_path": str(imported.database_path),
            "ndjson_path": str(imported.ndjson_path),
            "source_records_ndjson_path": str(imported.source_records_ndjson_path),
        },
        "validation": validation.as_dict(),
    }
    _write_json_atomic(report_path, report)
    return ChatGPTUpdateResult(
        status="pass" if validation.ok else "validation_failed",
        source_id=source_id,
        manifest_path=manifest_path,
        manifest_status=manifest_status,
        evidence_tree_sha256=manifest["tree_sha256"],
        doctor=doctor,
        import_result=imported,
        validation=validation,
        report_path=report_path,
    )
