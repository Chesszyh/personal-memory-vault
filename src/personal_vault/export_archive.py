"""Bounded, human-readable exports derived from the canonical archive."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .extension_recovery import inspect_compatible_chrome_binary
from .reader import ReaderError, ReaderRepository


class ArchiveExportError(RuntimeError):
    """Raised when a bounded reading export cannot be produced safely."""


@dataclass(frozen=True, slots=True)
class StaticConversationExport:
    root: Path
    html_path: Path
    manifest_path: Path
    snapshot_id: int
    snapshot_key: str
    conversation_identity_key: str
    index_sha256: str
    asset_count: int
    asset_bytes: int


@dataclass(frozen=True, slots=True)
class PdfConversationExport:
    pdf_path: Path
    html_path: Path
    pdf_sha256: str
    pdf_bytes: int
    browser_version: str
    browser_binary_sha256: str


@dataclass(frozen=True, slots=True)
class StaticSnapshotExport:
    root: Path
    index_path: Path
    manifest_path: Path
    snapshot_id: int
    snapshot_key: str
    conversation_count: int
    asset_file_count: int
    asset_bytes: int
    tree_sha256: str


@dataclass(frozen=True, slots=True)
class StaticSnapshotVerification:
    root: Path
    ok: bool
    snapshot_id: int
    snapshot_key: str
    conversation_count: int
    asset_file_count: int
    asset_bytes: int
    tree_sha256: str


def _safe_suffix(name: Any) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


def _format_time(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return "时间未知"
    try:
        return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "时间未知"


def _text(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _asset_markup(attachment: Mapping[str, Any], local_path: str | None) -> str:
    name = _text(attachment.get("name") or "未命名附件")
    status = _text(attachment.get("status") or "unknown")
    if local_path is None:
        return f'<li class="asset missing"><span>{name}</span><small>{status}</small></li>'
    href = _text(local_path)
    mime = str(attachment.get("mime") or "application/octet-stream")
    preview = ""
    if mime.startswith("image/"):
        preview = f'<img src="{href}" alt="{name}">'
    elif mime.startswith("audio/"):
        preview = f'<audio controls preload="metadata" src="{href}"></audio>'
    elif mime.startswith("video/"):
        preview = f'<video controls preload="metadata" src="{href}"></video>'
    return (
        '<li class="asset">'
        f'{preview}<a href="{href}" download>{name}</a>'
        f'<small>{status}</small></li>'
    )


def _message_markup(node: Mapping[str, Any], asset_paths: Mapping[str, str]) -> str:
    message = node.get("message")
    if not isinstance(message, Mapping):
        return ""
    role = str(message.get("role") or "unknown")
    model = message.get("model")
    meta = " · ".join(
        part
        for part in (
            _format_time(message.get("create_time")),
            str(message.get("content_type") or "unknown"),
            str(model) if model else "",
        )
        if part
    )
    attachments = []
    for attachment in message.get("attachments") or []:
        sha256 = attachment.get("sha256") if isinstance(attachment, Mapping) else None
        attachments.append(
            _asset_markup(
                attachment,
                asset_paths.get(str(sha256)) if isinstance(sha256, str) else None,
            )
        )
    attachment_block = (
        '<ul class="assets">' + "".join(attachments) + "</ul>" if attachments else ""
    )
    return (
        f'<article class="message role-{_text(role)}">'
        f'<header><strong>{_text(role)}</strong><span>{_text(meta)}</span></header>'
        f'<pre>{_text(message.get("text"))}</pre>{attachment_block}'
        f'<footer>node {_text(node.get("native_id"))}</footer>'
        "</article>"
    )


def _render_html(payload: Mapping[str, Any], asset_paths: Mapping[str, str]) -> str:
    snapshot = payload["snapshot"]
    conversation = payload["conversation"]
    current = "".join(
        _message_markup(node, asset_paths) for node in conversation["current_branch"]
    )
    alternatives = "".join(
        _message_markup(node, asset_paths) for node in conversation["alternative_nodes"]
    )
    diagnostic_items = "".join(
        f'<li><strong>{_text(item.get("severity"))}</strong> {_text(item.get("code"))}</li>'
        for item in conversation.get("diagnostics") or []
    )
    alternative_block = (
        '<details><summary>替代分支节点 '
        f'{len(conversation["alternative_nodes"])}</summary>{alternatives}</details>'
        if alternatives
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; media-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>{_text(conversation.get('title') or '未命名会话')}</title>
<style>
:root{{--paper:#f6f4ed;--ink:#17201f;--muted:#64706d;--line:#ccd5d1;--user:#e8f4ef;--assistant:#fff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.65 system-ui,sans-serif}}
main{{max-width:920px;margin:auto;padding:32px 20px 80px}}h1{{font-size:clamp(28px,5vw,48px);line-height:1.15}}
.meta,header,footer,small{{color:var(--muted)}}.message{{margin:18px 0;padding:18px;border:1px solid var(--line);border-radius:16px;background:var(--assistant);break-inside:avoid}}
.role-user{{background:var(--user)}}header{{display:flex;gap:12px;justify-content:space-between}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:14px 0}}
.assets{{padding:0;list-style:none}}.asset{{margin:10px 0;display:grid;gap:6px}}.asset img,.asset video{{max-width:100%;max-height:560px;border-radius:10px}}.asset audio{{width:100%}}
details{{margin-top:32px;padding:16px;border:1px dashed var(--line);border-radius:14px}}code{{overflow-wrap:anywhere}}@media print{{body{{background:white}}main{{max-width:none;padding:0}}.message{{break-inside:auto}}.asset img,.asset video{{max-height:260px}}details{{break-before:page}}}}
</style></head><body><main>
<p class="meta">PERSONAL MEMORY VAULT · {_text(snapshot.get('snapshot_key'))}</p>
<h1>{_text(conversation.get('title') or '未命名会话')}</h1>
<p class="meta">创建：{_text(_format_time(conversation.get('create_time')))} · 更新：{_text(_format_time(conversation.get('update_time')))}</p>
<p class="meta">来源：<code>{_text(conversation['source'].get('source_file_path'))}</code> · 会话 ID：<code>{_text(conversation.get('native_id'))}</code></p>
<section>{current}</section>{alternative_block}
<details><summary>归档诊断 {len(conversation.get('diagnostics') or [])}</summary><ul>{diagnostic_items}</ul></details>
</main></body></html>"""


def export_conversation_html(
    *,
    vault_root: Path,
    snapshot_id: int,
    conversation_identity_key: str,
    output_directory: Path,
) -> StaticConversationExport:
    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.exists():
        raise ArchiveExportError("HTML export destination already exists")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    repository = ReaderRepository(vault_root / "canonical" / "archive.sqlite", vault_root)
    try:
        payload = repository.get_conversation(snapshot_id, conversation_identity_key)
    except ReaderError as error:
        raise ArchiveExportError(str(error)) from error

    staging = output_directory.parent / f".{output_directory.name}.staging-{uuid.uuid4().hex}"
    assets_root = staging / "assets"
    staging.mkdir(mode=0o700)
    assets_root.mkdir(mode=0o700)
    asset_paths: dict[str, str] = {}
    assets_manifest: list[dict[str, Any]] = []
    try:
        nodes: Iterable[Mapping[str, Any]] = (
            *payload["conversation"]["current_branch"],
            *payload["conversation"]["alternative_nodes"],
        )
        attachments = [
            attachment
            for node in nodes
            if isinstance(node.get("message"), Mapping)
            for attachment in node["message"].get("attachments") or []
            if isinstance(attachment, Mapping)
        ]
        for attachment in attachments:
            sha256 = attachment.get("sha256")
            if attachment.get("status") != "resolved" or not isinstance(sha256, str):
                continue
            if sha256 not in asset_paths:
                filename = f"{sha256}{_safe_suffix(attachment.get('name'))}"
                relative = f"assets/{filename}"
                target = assets_root / filename
                with repository.open_asset(sha256) as opened, target.open("xb") as destination:
                    shutil.copyfileobj(opened.file, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                asset_paths[sha256] = relative
                assets_manifest.append(
                    {
                        "sha256": sha256,
                        "path": relative,
                        "size_bytes": target.stat().st_size,
                        "name": attachment.get("name"),
                        "mime": attachment.get("mime"),
                    }
                )
        rendered = _render_html(payload, asset_paths).encode("utf-8")
        html_path = staging / "index.html"
        html_path.write_bytes(rendered)
        index_sha256 = hashlib.sha256(rendered).hexdigest()
        manifest = {
            "schema": "pmv.static-conversation-export.v1",
            "snapshot_id": snapshot_id,
            "snapshot_key": payload["snapshot"]["snapshot_key"],
            "conversation_identity_key": conversation_identity_key,
            "conversation_native_id": payload["conversation"]["native_id"],
            "index_sha256": index_sha256,
            "assets": sorted(assets_manifest, key=lambda item: item["sha256"]),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return StaticConversationExport(
        root=output_directory,
        html_path=output_directory / "index.html",
        manifest_path=output_directory / "manifest.json",
        snapshot_id=snapshot_id,
        snapshot_key=payload["snapshot"]["snapshot_key"],
        conversation_identity_key=conversation_identity_key,
        index_sha256=index_sha256,
        asset_count=len(assets_manifest),
        asset_bytes=sum(int(item["size_bytes"]) for item in assets_manifest),
    )


def export_conversation_pdf(
    *,
    html_path: Path,
    pdf_path: Path,
    chrome_binary: Path,
    timeout_seconds: int = 120,
) -> PdfConversationExport:
    html_path = Path(html_path).expanduser().resolve(strict=True)
    pdf_path = Path(pdf_path).expanduser().resolve()
    if html_path.is_symlink() or not html_path.is_file():
        raise ArchiveExportError("HTML input must be an existing regular file")
    if pdf_path.exists():
        raise ArchiveExportError("PDF export destination already exists")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    browser = inspect_compatible_chrome_binary(chrome_binary)
    try:
        sync_playwright, playwright_error = _load_playwright()
        with sync_playwright() as runtime:
            launched = runtime.chromium.launch(
                executable_path=str(browser.path),
                headless=True,
            )
            try:
                page = launched.new_page(service_workers="block")

                def route_local_only(route: Any) -> None:
                    url = str(route.request.url)
                    if url.startswith(("file:", "data:", "blob:")):
                        route.continue_()
                    else:
                        route.abort()

                page.route("**/*", route_local_only)
                page.goto(
                    html_path.as_uri(),
                    wait_until="load",
                    timeout=max(1, int(timeout_seconds)) * 1000,
                )
                page.wait_for_function(
                    "() => Array.from(document.images).every(image => image.complete)",
                    timeout=max(1, int(timeout_seconds)) * 1000,
                )
                page.emulate_media(media="print")
                page.pdf(
                    path=str(pdf_path),
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                launched.close()
    except (OSError, ValueError, playwright_error) as error:
        pdf_path.unlink(missing_ok=True)
        raise ArchiveExportError("isolated Chromium PDF rendering failed") from error
    if not pdf_path.is_file() or pdf_path.is_symlink():
        pdf_path.unlink(missing_ok=True)
        raise ArchiveExportError("Chromium did not produce a valid PDF output file")
    payload = pdf_path.read_bytes()
    if not payload.startswith(b"%PDF-"):
        pdf_path.unlink(missing_ok=True)
        raise ArchiveExportError("Chromium output is not a PDF file")
    return PdfConversationExport(
        pdf_path=pdf_path,
        html_path=html_path,
        pdf_sha256=hashlib.sha256(payload).hexdigest(),
        pdf_bytes=len(payload),
        browser_version=browser.version,
        browser_binary_sha256=browser.binary_sha256,
    )


def _safe_conversation_directory(native_id: Any, identity_key: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(native_id or "conversation")).strip("-.")
    stem = stem[:96] or "conversation"
    suffix = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{suffix}"


def export_snapshot_html_archive(
    *, vault_root: Path, snapshot_id: int, output_directory: Path
) -> StaticSnapshotExport:
    """Export every observed conversation in one Snapshot with an archive index."""

    vault_root = Path(vault_root).expanduser().resolve(strict=True)
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.exists():
        raise ArchiveExportError("snapshot HTML export destination already exists")
    repository = ReaderRepository(vault_root / "canonical" / "archive.sqlite", vault_root)
    page = repository.list_conversations(snapshot_id=snapshot_id, limit=200, offset=0)
    snapshot = page["snapshot"]
    items = list(page["items"])
    while page["pagination"]["next_offset"] is not None:
        page = repository.list_conversations(
            snapshot_id=snapshot_id,
            limit=200,
            offset=int(page["pagination"]["next_offset"]),
        )
        items.extend(page["items"])

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = output_directory.parent / f".{output_directory.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    conversations_root = staging / "conversations"
    conversations_root.mkdir(mode=0o700)
    exported: list[dict[str, Any]] = []
    try:
        for item in items:
            identity_key = str(item["identity_key"])
            directory_name = _safe_conversation_directory(item.get("native_id"), identity_key)
            result = export_conversation_html(
                vault_root=vault_root,
                snapshot_id=snapshot_id,
                conversation_identity_key=identity_key,
                output_directory=conversations_root / directory_name,
            )
            exported.append(
                {
                    "identity_key": identity_key,
                    "native_id": item.get("native_id"),
                    "title": item.get("title"),
                    "path": f"conversations/{directory_name}/index.html",
                    "index_sha256": result.index_sha256,
                    "asset_count": result.asset_count,
                    "asset_bytes": result.asset_bytes,
                    "update_time": item.get("update_time"),
                }
            )
        index_items = "".join(
            '<li><a href="{}"><strong>{}</strong><span>{}</span></a></li>'.format(
                _text(item["path"]),
                _text(item.get("title") or "未命名会话"),
                _text(_format_time(item.get("update_time"))),
            )
            for item in exported
        )
        index_payload = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'"><title>对话归档</title>
<style>:root{{--bg:#f4f2eb;--ink:#17201f;--card:#fff;--line:#ced6d2;--muted:#65716e}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}main{{max-width:1000px;margin:auto;padding:42px 20px}}h1{{font-size:48px}}ul{{padding:0;list-style:none}}li{{margin:10px 0}}a{{display:flex;justify-content:space-between;gap:16px;padding:15px 18px;background:var(--card);border:1px solid var(--line);border-radius:12px;color:inherit;text-decoration:none}}a:hover{{border-color:#43806f}}span{{color:var(--muted)}}</style>
</head><body><main><p>PERSONAL MEMORY VAULT</p><h1>对话归档</h1>
<p>{_text(snapshot['snapshot_key'])} · {len(exported)} 个会话</p><ul>{index_items}</ul></main></body></html>""".encode("utf-8")
        (staging / "index.html").write_bytes(index_payload)
        manifest_without_tree = {
            "schema": "pmv.static-snapshot-export.v1",
            "snapshot_id": snapshot_id,
            "snapshot_key": snapshot["snapshot_key"],
            "conversation_count": len(exported),
            "conversations": exported,
            "index_sha256": hashlib.sha256(index_payload).hexdigest(),
        }
        canonical = json.dumps(
            manifest_without_tree,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tree = hashlib.sha256(canonical).hexdigest()
        manifest = {**manifest_without_tree, "tree_sha256": tree}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output_directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return StaticSnapshotExport(
        root=output_directory,
        index_path=output_directory / "index.html",
        manifest_path=output_directory / "manifest.json",
        snapshot_id=snapshot_id,
        snapshot_key=snapshot["snapshot_key"],
        conversation_count=len(exported),
        asset_file_count=sum(int(item["asset_count"]) for item in exported),
        asset_bytes=sum(int(item["asset_bytes"]) for item in exported),
        tree_sha256=tree,
    )


def _archive_relative_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ArchiveExportError(f"invalid {label}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ArchiveExportError(f"unsafe archive path in {label}")
    return path


def _open_regular(path: Path, *, label: str) -> tuple[int, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ArchiveExportError("archive verification requires O_NOFOLLOW")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | nofollow)
    except OSError as error:
        raise ArchiveExportError(f"missing or unsafe {label}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ArchiveExportError(f"non-regular {label}")
        return descriptor, before
    except Exception:
        os.close(descriptor)
        raise


def _unchanged_identity(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def _read_regular_bytes(path: Path, *, label: str) -> bytes:
    descriptor, before = _open_regular(path, label=label)
    with os.fdopen(descriptor, "rb") as stream:
        payload = stream.read()
        after = os.fstat(stream.fileno())
    if not _unchanged_identity(before, after):
        raise ArchiveExportError(f"changed while reading {label}")
    return payload


def _hash_regular_file(path: Path, *, label: str) -> tuple[str, int]:
    descriptor, before = _open_regular(path, label=label)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(stream.fileno())
    if not _unchanged_identity(before, after):
        raise ArchiveExportError(f"changed while reading {label}")
    return digest.hexdigest(), size


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ArchiveExportError(f"duplicate JSON key in {label}")
            result[key] = value
        return result

    try:
        value = json.loads(
            _read_regular_bytes(path, label=label),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveExportError(f"invalid JSON in {label}") from error
    if not isinstance(value, dict):
        raise ArchiveExportError(f"invalid object in {label}")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ArchiveExportError(f"invalid SHA-256 in {label}")
    return value


def _verify_static_html(payload: bytes, *, label: str) -> None:
    try:
        rendered = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArchiveExportError(f"invalid UTF-8 in {label}") from error
    if not re.search(
        r'<meta\s+http-equiv="Content-Security-Policy"\s+content="[^"]*script-src \'none\'[^"]*">',
        rendered,
        flags=re.IGNORECASE,
    ):
        raise ArchiveExportError(f"missing hardened CSP in {label}")
    if re.search(r"<\s*(?:script|iframe|object|embed|form|base|link)\b", rendered, re.I):
        raise ArchiveExportError(f"active HTML tag in {label}")


def verify_snapshot_html_archive(root: Path) -> StaticSnapshotVerification:
    """Re-hash a portable Snapshot HTML archive without emitting private text."""

    supplied_root = Path(root).expanduser()
    if supplied_root.is_symlink():
        raise ArchiveExportError("snapshot archive root must not be a symlink")
    root = supplied_root.resolve(strict=True)
    if not root.is_dir():
        raise ArchiveExportError("snapshot archive root must be a regular directory")
    manifest = _read_json_object(root / "manifest.json", label="snapshot manifest")
    if manifest.get("schema") != "pmv.static-snapshot-export.v1":
        raise ArchiveExportError("unsupported snapshot archive schema")
    conversations = manifest.get("conversations")
    if not isinstance(conversations, list):
        raise ArchiveExportError("snapshot conversations must be an array")
    if manifest.get("conversation_count") != len(conversations):
        raise ArchiveExportError("snapshot conversation count mismatch")

    expected_files = {Path("index.html"), Path("manifest.json")}
    expected_directories = {Path("conversations")}
    root_index = _read_regular_bytes(root / "index.html", label="snapshot index")
    if hashlib.sha256(root_index).hexdigest() != _require_sha256(
        manifest.get("index_sha256"), label="snapshot index"
    ):
        raise ArchiveExportError("snapshot index hash mismatch")
    _verify_static_html(root_index, label="snapshot index")

    seen_paths: set[Path] = set()
    seen_identities: set[str] = set()
    asset_file_count = 0
    asset_bytes = 0
    for ordinal, item in enumerate(conversations):
        if not isinstance(item, dict):
            raise ArchiveExportError("snapshot conversation entry must be an object")
        relative = _archive_relative_path(
            item.get("path"), label=f"conversation[{ordinal}].path"
        )
        if (
            relative in seen_paths
            or len(relative.parts) != 3
            or relative.parts[0] != "conversations"
            or relative.name != "index.html"
        ):
            raise ArchiveExportError("invalid or duplicate conversation archive path")
        seen_paths.add(relative)
        identity_key = item.get("identity_key")
        if (
            not isinstance(identity_key, str)
            or not identity_key
            or identity_key in seen_identities
        ):
            raise ArchiveExportError("invalid or duplicate conversation identity")
        seen_identities.add(identity_key)
        page = root / relative
        page_payload = _read_regular_bytes(page, label="conversation index")
        expected_index_hash = _require_sha256(
            item.get("index_sha256"), label="conversation index"
        )
        if hashlib.sha256(page_payload).hexdigest() != expected_index_hash:
            raise ArchiveExportError("conversation index hash mismatch")
        _verify_static_html(page_payload, label="conversation index")
        expected_files.add(relative)
        expected_directories.add(relative.parent)
        expected_directories.add(relative.parent / "assets")

        inner_path = relative.parent / "manifest.json"
        expected_files.add(inner_path)
        inner = _read_json_object(root / inner_path, label="conversation manifest")
        if inner.get("schema") != "pmv.static-conversation-export.v1":
            raise ArchiveExportError("unsupported conversation archive schema")
        if inner.get("conversation_identity_key") != identity_key:
            raise ArchiveExportError("conversation identity mismatch")
        if inner.get("conversation_native_id") != item.get("native_id"):
            raise ArchiveExportError("conversation native identity mismatch")
        if inner.get("snapshot_id") != manifest.get("snapshot_id"):
            raise ArchiveExportError("conversation snapshot ID mismatch")
        if inner.get("snapshot_key") != manifest.get("snapshot_key"):
            raise ArchiveExportError("conversation snapshot key mismatch")
        if inner.get("index_sha256") != expected_index_hash:
            raise ArchiveExportError("conversation manifest index hash mismatch")
        assets = inner.get("assets")
        if not isinstance(assets, list):
            raise ArchiveExportError("conversation assets must be an array")
        seen_assets: set[Path] = set()
        for asset in assets:
            if not isinstance(asset, dict):
                raise ArchiveExportError("conversation asset entry must be an object")
            asset_relative = _archive_relative_path(
                asset.get("path"), label="conversation asset path"
            )
            if len(asset_relative.parts) != 2 or asset_relative.parts[0] != "assets":
                raise ArchiveExportError("asset path must stay inside conversation assets")
            full_relative = relative.parent / asset_relative
            if full_relative in seen_assets or full_relative in expected_files:
                raise ArchiveExportError("duplicate conversation asset path")
            seen_assets.add(full_relative)
            expected_files.add(full_relative)
            actual_hash, actual_size = _hash_regular_file(
                root / full_relative, label="conversation asset"
            )
            expected_size = asset.get("size_bytes")
            if (
                not isinstance(expected_size, int)
                or expected_size < 0
                or actual_size != expected_size
            ):
                raise ArchiveExportError("conversation asset size mismatch")
            expected_hash = _require_sha256(asset.get("sha256"), label="conversation asset")
            if actual_hash != expected_hash:
                raise ArchiveExportError("conversation asset hash mismatch")
            asset_file_count += 1
            asset_bytes += actual_size
        if item.get("asset_count") != len(assets):
            raise ArchiveExportError("conversation asset count mismatch")
        if item.get("asset_bytes") != sum(int(asset["size_bytes"]) for asset in assets):
            raise ArchiveExportError("conversation asset byte count mismatch")

    actual_files: set[Path] = set()
    actual_directories: set[Path] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ArchiveExportError("snapshot archive contains a symlink")
        if path.is_file():
            actual_files.add(relative)
        elif path.is_dir():
            actual_directories.add(relative)
        else:
            raise ArchiveExportError("snapshot archive contains a special file")
    if actual_files != expected_files:
        raise ArchiveExportError("snapshot archive file inventory mismatch")
    if actual_directories != expected_directories:
        raise ArchiveExportError("snapshot archive directory inventory mismatch")

    manifest_without_tree = {
        key: value for key, value in manifest.items() if key != "tree_sha256"
    }
    canonical = json.dumps(
        manifest_without_tree,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    tree_sha256 = _require_sha256(manifest.get("tree_sha256"), label="snapshot tree")
    if hashlib.sha256(canonical).hexdigest() != tree_sha256:
        raise ArchiveExportError("snapshot tree hash mismatch")
    snapshot_id = manifest.get("snapshot_id")
    snapshot_key = manifest.get("snapshot_key")
    if not isinstance(snapshot_id, int) or not isinstance(snapshot_key, str):
        raise ArchiveExportError("invalid snapshot identity")
    return StaticSnapshotVerification(
        root=root,
        ok=True,
        snapshot_id=snapshot_id,
        snapshot_key=snapshot_key,
        conversation_count=len(conversations),
        asset_file_count=asset_file_count,
        asset_bytes=asset_bytes,
        tree_sha256=tree_sha256,
    )


def _load_playwright() -> tuple[Any, type[Exception]]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ArchiveExportError(
            "PDF export requires the optional 'pdf' dependency: install with "
            "personal-memory-vault-tools[pdf]"
        ) from error
    return sync_playwright, PlaywrightError
