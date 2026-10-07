from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode

from personal_vault.evidence import (
    EvidenceError,
    scan_evidence,
    tree_sha256,
    verify_manifest,
)


DATABASE_NAME = "chatgpt-web-usage-observatory"
DATABASE_VERSION = 2
EXPORT_MANIFEST_SCHEMA = "pmv.extension-idb-export-manifest.v1"
ROW_ENVELOPE_SCHEMA = "pmv.extension-idb-row.v1"
RECOVERY_STATE_SCHEMA = "pmv.extension-recovery-state.v1"
SOURCE_KIND = "extension_indexeddb_state_snapshot"
EXPECTED_STORE_NAMES = (
    "turn-events",
    "turn-records",
    "conversation-records",
    "daily-aggregates",
    "raw-artifacts",
    "sync-outbox",
    "meta",
)
EXPECTED_DATABASE_SCHEMA: dict[str, dict[str, Any]] = {
    "turn-events": {
        "key_path": "sequence",
        "auto_increment": True,
        "indexes": (
            {"name": "timestamp", "key_path": "timestamp", "unique": False, "multi_entry": False},
            {"name": "turnId", "key_path": "turnId", "unique": False, "multi_entry": False},
            {"name": "type", "key_path": "type", "unique": False, "multi_entry": False},
            {"name": "eventId", "key_path": "eventId", "unique": False, "multi_entry": False},
            {"name": "conversationId", "key_path": "conversationId", "unique": False, "multi_entry": False},
        ),
    },
    "turn-records": {
        "key_path": "recordId",
        "auto_increment": False,
        "indexes": (
            {"name": "turnId", "key_path": "turnId", "unique": False, "multi_entry": False},
            {"name": "conversationId", "key_path": "conversationId", "unique": False, "multi_entry": False},
            {"name": "startedAt", "key_path": "startedAt", "unique": False, "multi_entry": False},
            {"name": "updatedAt", "key_path": "updatedAt", "unique": False, "multi_entry": False},
            {"name": "status", "key_path": "status", "unique": False, "multi_entry": False},
            {
                "name": "conversationUser",
                "key_path": ["conversationId", "nativeUserMessageId"],
                "unique": False,
                "multi_entry": False,
            },
        ),
    },
    "conversation-records": {
        "key_path": "conversationId",
        "auto_increment": False,
        "indexes": (
            {"name": "updatedAt", "key_path": "updatedAt", "unique": False, "multi_entry": False},
            {"name": "projectId", "key_path": "projectId", "unique": False, "multi_entry": False},
            {"name": "isArchived", "key_path": "isArchived", "unique": False, "multi_entry": False},
        ),
    },
    "daily-aggregates": {
        "key_path": ["date", "timezone"],
        "auto_increment": False,
        "indexes": (),
    },
    "raw-artifacts": {
        "key_path": "artifactId",
        "auto_increment": False,
        "indexes": (
            {"name": "conversationId", "key_path": "conversationId", "unique": False, "multi_entry": False},
            {"name": "turnId", "key_path": "turnId", "unique": False, "multi_entry": False},
            {"name": "updatedAt", "key_path": "updatedAt", "unique": False, "multi_entry": False},
        ),
    },
    "sync-outbox": {
        "key_path": "mutationId",
        "auto_increment": False,
        "indexes": (
            {"name": "createdAt", "key_path": "createdAt", "unique": False, "multi_entry": False},
            {"name": "recordId", "key_path": "recordId", "unique": False, "multi_entry": False},
        ),
    },
    "meta": {"key_path": "key", "auto_increment": False, "indexes": ()},
}
PRODUCTION_EXTENSION_FILES = (
    "manifest.json",
    "export.html",
    "export.css",
    "export.js",
    "tagged-json.js",
)
ARCHIVE_EXTENSION_ID = "ainoobmdpanhopangobnggdkpljnpmgl"

_ORIGIN_DIRECTORY = re.compile(
    r"^chrome-extension_([a-p]{32})_0\.indexeddb\.(leveldb|blob)$"
)
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_REPLAY_NAMES = ("replay-1", "replay-2")
_ACTIVE_PROFILE_MARKERS = (
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "DevToolsActivePort",
)


class ExtensionRecoveryError(RuntimeError):
    """Raised when recovery cannot proceed without weakening an evidence boundary."""


@dataclass(frozen=True, slots=True)
class TreeFingerprint:
    sha256: str
    file_count: int
    directory_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class ProductionExtensionBundle:
    root: Path
    extension_id: str
    tree_sha256: str
    file_count: int
    byte_count: int
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserIdentity:
    path: Path
    flavor: str
    version: str
    binary_sha256: str


@dataclass(frozen=True, slots=True)
class RecoverySource:
    root: Path
    indexeddb_directory: Path
    source_extension_id: str
    tree: TreeFingerprint
    payload_sha256: str
    working_copy_evidence_tree_sha256: str
    evidence_manifest_sha256: str | None


@dataclass(frozen=True, slots=True)
class ReplayWorkspace:
    root: Path
    preparation_root: Path
    user_data_directory: Path
    staged_indexeddb_directory: Path
    production_extension_directory: Path
    output_directory: Path
    state_path: Path
    replay_id: str
    replay_challenge: str
    source_extension_id: str
    target_extension_id: str
    phase: str
    working_copy_evidence_tree_sha256: str
    source_payload_sha256: str
    production_bundle_sha256: str
    preparation_sha256: str
    recovery_state_sha256: str
    browser_path: Path
    browser_flavor: str
    browser_version: str
    browser_binary_sha256: str


@dataclass(frozen=True, slots=True)
class ReplayPreparation:
    root: Path
    source_extension_id: str
    target_extension_id: str
    source_tree_sha256: str
    source_payload_sha256: str
    production_bundle_sha256: str
    browser_binary_sha256: str
    replays: tuple[ReplayWorkspace, ReplayWorkspace]


@dataclass(frozen=True, slots=True)
class FileDigest:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RowFingerprint:
    store_name: str
    cursor_ordinal: int
    line_sha256: str
    canonical_row_sha256: str
    decoded_value_graph_sha256: str
    binary_reference_sha256: str


@dataclass(frozen=True, slots=True)
class StoreFingerprint:
    name: str
    row_count: int
    row_sha256: str
    ndjson_sha256: str
    ndjson_bytes: int
    binary_part_count: int
    binary_bytes: int
    binary_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedExport:
    root: Path
    replay_id: str
    replay_challenge: str
    working_copy_evidence_tree_sha256: str
    source_payload_sha256: str
    production_bundle_sha256: str
    preparation_sha256: str
    recovery_state_sha256: str
    browser_flavor: str
    browser_version: str
    browser_binary_sha256: str
    snapshot_id: str
    extension_id: str
    manifest_sha256: str
    file_count: int
    byte_count: int
    row_count: int
    row_sha256: str
    store_sha256: str
    binary_sha256: str
    rows: tuple[RowFingerprint, ...]
    stores: tuple[StoreFingerprint, ...]
    files: tuple[FileDigest, ...]


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    deterministic: bool
    extraction_complete: bool
    snapshot_id: str | None
    first_store_sha256: str
    second_store_sha256: str
    first_binary_sha256: str
    second_binary_sha256: str
    differences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChromeRunResult:
    """Metadata-only result from one isolated, interactive Chrome run."""

    replay_id: str
    phase: str
    returncode: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _resolved_existing_directory(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ExtensionRecoveryError(f"{label} must not be a symlink: {expanded}")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as error:
        raise ExtensionRecoveryError(f"cannot resolve {label}: {expanded}: {error}") from error
    if not resolved.is_dir():
        raise ExtensionRecoveryError(f"{label} is not a directory: {resolved}")
    return resolved


def _hash_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ExtensionRecoveryError(f"cannot safely open regular file: {path}") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExtensionRecoveryError(f"not a regular file: {path}")
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_after = os.lstat(path)
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            any(getattr(before, field) != getattr(after, field) for field in fields)
            or any(getattr(after, field) != getattr(path_after, field) for field in fields)
            or byte_count != after.st_size
        ):
            raise ExtensionRecoveryError(f"file changed while hashing: {path}")
        return after.st_size, digest.hexdigest()
    except OSError as error:
        raise ExtensionRecoveryError(f"cannot safely hash regular file: {path}") from error
    finally:
        os.close(descriptor)


def _walk_tree(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    try:
        scanned = scan_evidence(root)
    except EvidenceError as error:
        raise ExtensionRecoveryError(f"cannot safely scan recovery tree: {error}") from error
    entries: list[tuple[str, str, int, str]] = []
    for entry in scanned:
        relative = PurePosixPath(entry.path)
        if any(part.casefold() == "extensionstorage" for part in relative.parts):
            raise ExtensionRecoveryError("ExtensionStorage is forbidden in recovery input")
        if entry.kind == "symlink":
            raise ExtensionRecoveryError(
                f"symlink is forbidden in recovery input: {entry.path}"
            )
        if entry.kind == "directory":
            entries.append(("directory", entry.path, 0, ""))
        elif entry.kind == "file":
            entries.append(("file", entry.path, entry.size or 0, entry.sha256 or ""))
        else:
            raise ExtensionRecoveryError(
                f"special filesystem entry is forbidden in recovery input: {entry.path}"
            )
    return tuple(sorted(entries, key=lambda entry: os.fsencode(entry[1])))


def _fingerprint_tree(root: Path) -> TreeFingerprint:
    entries = _walk_tree(root)
    digest = hashlib.sha256()
    for kind, path, size, file_digest in entries:
        digest.update(f"{kind}\0{path}\0{size}\0{file_digest}\0".encode("utf-8"))
    return TreeFingerprint(
        sha256=digest.hexdigest(),
        file_count=sum(kind == "file" for kind, *_ in entries),
        directory_count=sum(kind == "directory" for kind, *_ in entries),
        byte_count=sum(size for kind, _, size, _ in entries if kind == "file"),
    )


def _directory_is_empty(path: Path) -> bool:
    with os.scandir(path) as entries:
        return next(entries, None) is None


def _payload_fingerprint(indexeddb_directory: Path, extension_id: str) -> str:
    digest = hashlib.sha256()
    expected_prefix = f"chrome-extension_{extension_id}_0.indexeddb."
    for kind, path, size, file_digest in _walk_tree(indexeddb_directory):
        first, separator, remainder = path.partition("/")
        if not first.startswith(expected_prefix):
            raise ExtensionRecoveryError(f"unexpected IndexedDB origin path: {first}")
        origin_kind = first.removeprefix(expected_prefix)
        normalized = origin_kind if not separator else f"{origin_kind}/{remainder}"
        digest.update(
            f"{kind}\0{normalized}\0{size}\0{file_digest}\0".encode("utf-8")
        )
    return digest.hexdigest()


def derive_extension_id(extension_manifest: Path) -> str:
    """Derive Chromium's stable extension ID from a Manifest V3 public key."""

    try:
        manifest = json.loads(extension_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExtensionRecoveryError(
            f"cannot read extension manifest: {extension_manifest}: {error}"
        ) from error
    if manifest.get("manifest_version") != 3:
        raise ExtensionRecoveryError("archive reader must use Manifest V3")
    encoded_key = manifest.get("key")
    if not isinstance(encoded_key, str) or not encoded_key:
        raise ExtensionRecoveryError("extension manifest is missing a fixed public key")
    return _extension_id_from_encoded_key(encoded_key)


def _extension_id_from_encoded_key(encoded_key: str) -> str:
    try:
        public_key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ExtensionRecoveryError("extension manifest public key is not valid base64") from error
    if not public_key:
        raise ExtensionRecoveryError("extension manifest public key is empty")
    hexadecimal = hashlib.sha256(public_key).hexdigest()[:32]
    extension_id = "".join(chr(ord("a") + int(character, 16)) for character in hexadecimal)
    if not _EXTENSION_ID.fullmatch(extension_id):
        raise AssertionError("derived extension ID is outside Chromium's alphabet")
    return extension_id


def _validate_archive_extension_manifest(extension_manifest: Path) -> str:
    try:
        manifest, _ = _load_json_object(
            extension_manifest, "archive extension manifest"
        )
    except ExtensionRecoveryError as error:
        raise ExtensionRecoveryError(
            f"cannot validate archive extension manifest: {error}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 3:
        raise ExtensionRecoveryError("archive extension manifest must be a Manifest V3 object")
    approved_fields = {
        "manifest_version",
        "name",
        "version",
        "description",
        "key",
        "content_security_policy",
    }
    unapproved = sorted(set(manifest) - approved_fields)
    if unapproved:
        raise ExtensionRecoveryError(
            f"archive extension manifest contains an unapproved field: {unapproved[0]}"
        )
    missing = sorted(approved_fields - set(manifest))
    if missing:
        raise ExtensionRecoveryError(
            f"archive extension manifest is missing an approved field: {missing[0]}"
        )
    forbidden_fields = {
        "action",
        "background",
        "commands",
        "content_scripts",
        "devtools_page",
        "externally_connectable",
        "host_permissions",
        "incognito",
        "optional_host_permissions",
        "optional_permissions",
        "permissions",
        "sandbox",
        "side_panel",
        "storage",
        "web_accessible_resources",
    }
    present = sorted(forbidden_fields & set(manifest))
    if present:
        raise ExtensionRecoveryError(
            f"archive extension manifest exposes a forbidden surface: {present[0]}"
        )
    csp = manifest.get("content_security_policy")
    if not isinstance(csp, dict) or set(csp) != {"extension_pages"}:
        raise ExtensionRecoveryError(
            "archive extension content_security_policy must contain only extension_pages"
        )
    extension_pages = csp.get("extension_pages") if isinstance(csp, dict) else None
    if not isinstance(extension_pages, str):
        raise ExtensionRecoveryError("archive extension manifest is missing extension-page CSP")
    directives: dict[str, tuple[str, ...]] = {}
    for raw_directive in extension_pages.split(";"):
        fields = raw_directive.strip().split()
        if not fields:
            continue
        if fields[0] in directives:
            raise ExtensionRecoveryError("archive extension CSP contains duplicate directives")
        directives[fields[0]] = tuple(fields[1:])
    required_directives = {
        "default-src": ("'none'",),
        "script-src": ("'self'",),
        "style-src": ("'self'",),
        "connect-src": ("'none'",),
        "object-src": ("'none'",),
        "base-uri": ("'none'",),
        "form-action": ("'none'",),
        "frame-ancestors": ("'none'",),
    }
    for directive, expected in required_directives.items():
        if directives.get(directive) != expected:
            raise ExtensionRecoveryError(
                f"archive extension CSP is not closed for directive: {directive}"
            )
    encoded_key = manifest.get("key")
    if not isinstance(encoded_key, str) or not encoded_key:
        raise ExtensionRecoveryError("extension manifest is missing a fixed public key")
    extension_id = _extension_id_from_encoded_key(encoded_key)
    if extension_id != ARCHIVE_EXTENSION_ID:
        raise ExtensionRecoveryError(
            "extension manifest does not derive the fixed archive extension ID"
        )
    return extension_id


def _copy_regular_file_between_directories(
    source_directory_fd: int,
    destination_directory_fd: int,
    name: str,
) -> None:
    read_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        source_fd = os.open(name, read_flags, dir_fd=source_directory_fd)
    except OSError as error:
        raise ExtensionRecoveryError(f"cannot open production extension file: {name}") from error
    destination_fd: int | None = None
    try:
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode):
            raise ExtensionRecoveryError(
                f"production extension entry is not a regular file: {name}"
            )
        try:
            destination_fd = os.open(
                name,
                write_flags,
                0o400,
                dir_fd=destination_directory_fd,
            )
        except OSError as error:
            raise ExtensionRecoveryError(
                f"cannot create production extension file: {name}"
            ) from error
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        source_after = os.fstat(source_fd)
        if (
            source_before.st_dev != source_after.st_dev
            or source_before.st_ino != source_after.st_ino
            or source_before.st_mode != source_after.st_mode
            or source_before.st_size != source_after.st_size
            or source_before.st_mtime_ns != source_after.st_mtime_ns
            or source_before.st_ctime_ns != source_after.st_ctime_ns
        ):
            raise ExtensionRecoveryError(
                f"production extension file changed while copying: {name}"
            )
        path_after = os.stat(name, dir_fd=source_directory_fd, follow_symlinks=False)
        if (
            path_after.st_dev != source_after.st_dev
            or path_after.st_ino != source_after.st_ino
            or path_after.st_mode != source_after.st_mode
            or path_after.st_size != source_after.st_size
            or path_after.st_mtime_ns != source_after.st_mtime_ns
            or path_after.st_ctime_ns != source_after.st_ctime_ns
        ):
            raise ExtensionRecoveryError(
                f"production extension path changed while copying: {name}"
            )
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def validate_production_extension_bundle(
    bundle_directory: Path,
) -> ProductionExtensionBundle:
    """Validate the exact read-only runtime surface loaded into recovery Chrome."""

    root = _resolved_existing_directory(bundle_directory, "production extension bundle")
    try:
        entries = scan_evidence(root)
    except EvidenceError as error:
        raise ExtensionRecoveryError(
            f"cannot safely scan production extension bundle: {error}"
        ) from error
    expected = set(PRODUCTION_EXTENSION_FILES)
    actual = {entry.path for entry in entries}
    if actual != expected or any(entry.kind != "file" for entry in entries):
        detail = sorted(actual ^ expected, key=os.fsencode)
        raise ExtensionRecoveryError(
            "production extension bundle must contain exactly five regular files"
            + (f": {detail[0]}" if detail else "")
        )
    for entry in entries:
        metadata = os.stat(root / entry.path, follow_symlinks=False)
        if metadata.st_mode & 0o222:
            raise ExtensionRecoveryError(
                f"production extension bundle file is writable: {entry.path}"
            )
    if root.stat().st_mode & 0o222:
        raise ExtensionRecoveryError("production extension bundle directory is writable")
    extension_id = _validate_archive_extension_manifest(root / "manifest.json")
    return ProductionExtensionBundle(
        root=root,
        extension_id=extension_id,
        tree_sha256=tree_sha256(entries),
        file_count=len(entries),
        byte_count=sum(entry.size or 0 for entry in entries),
        files=tuple(sorted(actual, key=os.fsencode)),
    )


def build_production_extension_bundle(
    source_directory: Path,
    destination: Path,
) -> ProductionExtensionBundle:
    """Copy the five audited runtime files into a new, closed production bundle."""

    source = _resolved_existing_directory(source_directory, "extension source directory")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ExtensionRecoveryError(
            f"production extension destination must not exist: {destination}"
        )
    resolved_destination = destination.resolve(strict=False)
    if _paths_overlap(source, resolved_destination):
        raise ExtensionRecoveryError("production extension destination overlaps its source")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.resolve(strict=True)
    staging = parent / f".{destination.name}.preparing-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(
            source,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        destination_fd = os.open(
            staging,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        for name in PRODUCTION_EXTENSION_FILES:
            _copy_regular_file_between_directories(source_fd, destination_fd, name)
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        os.chmod(staging, 0o500)
        bundle = validate_production_extension_bundle(staging)
        os.replace(staging, destination)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return ProductionExtensionBundle(
            root=destination.resolve(strict=True),
            extension_id=bundle.extension_id,
            tree_sha256=bundle.tree_sha256,
            file_count=bundle.file_count,
            byte_count=bundle.byte_count,
            files=bundle.files,
        )
    except BaseException:
        if staging.exists():
            try:
                _make_private_writable(staging)
            except ExtensionRecoveryError:
                pass
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def validate_recovery_working_copy(
    root: Path,
    *,
    master_roots: Iterable[Path],
    active_profile_roots: Iterable[Path],
    evidence_manifest: Path | None = None,
    unsafe_synthetic_only: bool = False,
) -> RecoverySource:
    """Validate the narrow IndexedDB-only recovery-copy contract."""

    master_roots = tuple(master_roots)
    active_profile_roots = tuple(active_profile_roots)
    if not unsafe_synthetic_only:
        if evidence_manifest is None:
            raise ExtensionRecoveryError(
                "production recovery requires a working-copy evidence manifest"
            )
        if not master_roots:
            raise ExtensionRecoveryError("production recovery requires protected master roots")
        if not active_profile_roots:
            raise ExtensionRecoveryError(
                "production recovery requires explicit active-profile roots"
            )
    resolved = _resolved_existing_directory(root, "recovery working copy")
    denied = [
        (_resolved_existing_directory(path, "master root"), "immutable master")
        for path in master_roots
    ]
    denied.extend(
        (_resolved_existing_directory(path, "active profile root"), "active Chrome profile")
        for path in active_profile_roots
    )
    for denied_root, label in denied:
        if _paths_overlap(resolved, denied_root):
            raise ExtensionRecoveryError(
                f"recovery working copy overlaps {label}: {denied_root}"
            )
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise ExtensionRecoveryError(
            "recovery input is not a writable working copy; immutable masters are forbidden"
        )

    evidence_manifest_sha256 = None
    working_copy_evidence_tree_sha256: str | None = None
    if evidence_manifest is not None:
        try:
            declared_manifest, manifest_digest = _load_json_object(
                evidence_manifest.expanduser().absolute(),
                "working-copy evidence manifest",
            )
            if declared_manifest.get("source", {}).get("kind") != "recovery_working_copy":
                raise ExtensionRecoveryError(
                    "working-copy evidence manifest has the wrong source kind"
                )
            verification = verify_manifest(resolved, declared_manifest)
        except (EvidenceError, ExtensionRecoveryError) as error:
            raise ExtensionRecoveryError(
                f"working-copy evidence manifest cannot be verified: {error}"
            ) from error
        if not verification.ok:
            locator = verification.differences[0] if verification.differences else "tree hash"
            raise ExtensionRecoveryError(
                f"working-copy evidence manifest does not match: {locator}"
            )
        working_copy_evidence_tree_sha256 = declared_manifest["tree_sha256"]
        evidence_manifest_sha256 = manifest_digest.sha256

    top_level = sorted(os.scandir(resolved), key=lambda item: os.fsencode(item.name))
    if [entry.name for entry in top_level] != ["IndexedDB"] or not top_level[0].is_dir(
        follow_symlinks=False
    ):
        raise ExtensionRecoveryError(
            "recovery working copy must contain only an IndexedDB directory"
        )
    if top_level[0].is_symlink():
        raise ExtensionRecoveryError("IndexedDB must not be a symlink")

    indexeddb = resolved / "IndexedDB"
    origins = sorted(os.scandir(indexeddb), key=lambda item: os.fsencode(item.name))
    if len(origins) != 2:
        raise ExtensionRecoveryError(
            "IndexedDB must contain exactly one LevelDB directory and one blob directory"
        )
    found: dict[str, str] = {}
    for origin in origins:
        if origin.is_symlink() or not origin.is_dir(follow_symlinks=False):
            raise ExtensionRecoveryError(
                f"unexpected non-directory IndexedDB entry: {origin.name}"
            )
        match = _ORIGIN_DIRECTORY.fullmatch(origin.name)
        if match is None:
            raise ExtensionRecoveryError(f"unexpected IndexedDB origin directory: {origin.name}")
        extension_id, kind = match.groups()
        if kind in found:
            raise ExtensionRecoveryError(f"duplicate IndexedDB {kind} directory")
        found[kind] = extension_id
    if set(found) != {"leveldb", "blob"} or len(set(found.values())) != 1:
        raise ExtensionRecoveryError("LevelDB and blob directories must share one extension origin")
    source_extension_id = found["leveldb"]

    tree = _fingerprint_tree(indexeddb)
    if tree.file_count == 0:
        raise ExtensionRecoveryError("recovery IndexedDB working copy contains no files")
    leveldb_name = f"chrome-extension_{source_extension_id}_0.indexeddb.leveldb"
    with os.scandir(indexeddb / leveldb_name) as leveldb_entries:
        has_leveldb_file = any(
            entry.is_file(follow_symlinks=False) for entry in leveldb_entries
        )
    if not has_leveldb_file:
        raise ExtensionRecoveryError("recovery LevelDB directory contains no files")
    if working_copy_evidence_tree_sha256 is None:
        try:
            working_copy_evidence_tree_sha256 = tree_sha256(scan_evidence(resolved))
        except EvidenceError as error:
            raise ExtensionRecoveryError(
                f"cannot safely fingerprint synthetic recovery input: {error}"
            ) from error
    return RecoverySource(
        root=resolved,
        indexeddb_directory=indexeddb,
        source_extension_id=source_extension_id,
        tree=tree,
        payload_sha256=_payload_fingerprint(indexeddb, source_extension_id),
        working_copy_evidence_tree_sha256=working_copy_evidence_tree_sha256,
        evidence_manifest_sha256=evidence_manifest_sha256,
    )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _make_private_writable(root: Path) -> None:
    for directory, directory_names, file_names in os.walk(root):
        os.chmod(directory, 0o700)
        for name in directory_names:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise ExtensionRecoveryError(f"copied recovery tree contains a symlink: {candidate}")
            os.chmod(candidate, 0o700)
        for name in file_names:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise ExtensionRecoveryError(f"copied recovery tree contains a symlink: {candidate}")
            os.chmod(candidate, 0o600)


def _state_for_replay(
    replay_name: str,
    source: RecoverySource,
    bundle: ProductionExtensionBundle,
    browser: BrowserIdentity,
) -> dict[str, Any]:
    replay_challenge = hashlib.sha256(os.urandom(32)).hexdigest()
    preparation_payload = {
        "replay_id": replay_name,
        "replay_challenge": replay_challenge,
        "source_extension_id": source.source_extension_id,
        "target_extension_id": bundle.extension_id,
        "working_copy_evidence_tree_sha256": source.working_copy_evidence_tree_sha256,
        "source_indexeddb_tree_sha256": source.tree.sha256,
        "source_payload_sha256": source.payload_sha256,
        "working_copy_evidence_manifest_sha256": source.evidence_manifest_sha256,
        "production_bundle_sha256": bundle.tree_sha256,
        "browser": {
            "path": os.fspath(browser.path),
            "flavor": browser.flavor,
            "version": browser.version,
            "binary_sha256": browser.binary_sha256,
        },
        "user_data_directory": "chrome-user-data",
        "staged_indexeddb_directory": "recovery-copy/IndexedDB",
        "production_extension_directory": "runtime/archive-extension",
        "output_directory": f"outputs/{replay_name}",
        "sandbox_home_directory": "sandbox-home",
    }
    return {
        "schema": RECOVERY_STATE_SCHEMA,
        "phase": "awaiting_missing_database_preflight",
        "replay_id": replay_name,
        "replay_challenge": replay_challenge,
        "created_at": _now(),
        "source_extension_id": source.source_extension_id,
        "target_extension_id": bundle.extension_id,
        "database_name": DATABASE_NAME,
        "database_version": DATABASE_VERSION,
        "working_copy_evidence_tree_sha256": source.working_copy_evidence_tree_sha256,
        "source_indexeddb_tree_sha256": source.tree.sha256,
        "source_payload_sha256": source.payload_sha256,
        "working_copy_evidence_manifest_sha256": source.evidence_manifest_sha256,
        "production_bundle_sha256": bundle.tree_sha256,
        "browser": preparation_payload["browser"],
        "preparation_sha256": _sha256_json(preparation_payload),
        "user_data_directory": "chrome-user-data",
        "staged_indexeddb_directory": "recovery-copy/IndexedDB",
        "production_extension_directory": "runtime/archive-extension",
        "output_directory": f"outputs/{replay_name}",
        "sandbox_home_directory": "sandbox-home",
        "missing_database_preflight": None,
        "installed_indexeddb_directory": None,
    }


def prepare_replay_workspaces(
    recovery_working_copy: Path,
    destination: Path,
    extension_manifest: Path,
    *,
    chrome_binary: Path | str | None = None,
    master_roots: Iterable[Path],
    active_profile_roots: Iterable[Path],
    evidence_manifest: Path | None = None,
    unsafe_synthetic_only: bool = False,
) -> ReplayPreparation:
    """Create two independent replay copies without launching or touching Chrome."""

    master_roots = tuple(master_roots)
    active_profile_roots = tuple(active_profile_roots)
    if not unsafe_synthetic_only:
        if evidence_manifest is None:
            raise ExtensionRecoveryError(
                "production recovery requires a verified working-copy evidence manifest"
            )
        if not master_roots:
            raise ExtensionRecoveryError("production recovery requires protected master roots")
        if not active_profile_roots:
            raise ExtensionRecoveryError(
                "production recovery requires explicit active-profile roots"
            )
    if chrome_binary is None:
        raise ExtensionRecoveryError("recovery preparation requires a compatible Chrome binary")
    browser = inspect_compatible_chrome_binary(chrome_binary)
    source = validate_recovery_working_copy(
        recovery_working_copy,
        master_roots=master_roots,
        active_profile_roots=active_profile_roots,
        evidence_manifest=evidence_manifest,
        unsafe_synthetic_only=unsafe_synthetic_only,
    )
    extension_manifest = extension_manifest.expanduser().absolute()
    if extension_manifest.name != "manifest.json":
        raise ExtensionRecoveryError("extension manifest must be named manifest.json")
    extension_source = _resolved_existing_directory(
        extension_manifest.parent, "extension source directory"
    )
    target_extension_id = _validate_archive_extension_manifest(
        extension_source / "manifest.json"
    )
    destination = destination.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ExtensionRecoveryError(f"replay destination must not already exist: {destination}")
    if _paths_overlap(destination, source.root):
        raise ExtensionRecoveryError("replay destination must not overlap recovery source")
    if _paths_overlap(destination, extension_source):
        raise ExtensionRecoveryError("replay destination must not overlap extension source")
    for denied_root in (*master_roots, *active_profile_roots):
        resolved_denied = _resolved_existing_directory(denied_root, "denied root")
        if _paths_overlap(destination, resolved_denied):
            raise ExtensionRecoveryError(
                f"replay destination overlaps a protected root: {resolved_denied}"
            )

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = destination.parent / f".{destination.name}.preparing-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    production_bundle_sha256: str | None = None
    try:
        for replay_name in _REPLAY_NAMES:
            replay_root = staging / "workspaces" / replay_name
            copied_indexeddb = replay_root / "recovery-copy" / "IndexedDB"
            copied_indexeddb.parent.mkdir(parents=True, mode=0o700)
            shutil.copytree(source.indexeddb_directory, copied_indexeddb, symlinks=True)
            _make_private_writable(replay_root)
            copied_tree = _fingerprint_tree(copied_indexeddb)
            copied_payload = _payload_fingerprint(
                copied_indexeddb, source.source_extension_id
            )
            if copied_tree != source.tree or copied_payload != source.payload_sha256:
                raise ExtensionRecoveryError(
                    f"independent recovery copy did not verify: {replay_name}"
                )
            (replay_root / "chrome-user-data").mkdir(mode=0o700)
            sandbox_home = replay_root / "sandbox-home"
            for relative in (".cache", ".config", ".local/share"):
                (sandbox_home / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
            output_directory = staging / "outputs" / replay_name
            output_directory.mkdir(parents=True, mode=0o700)
            bundle = build_production_extension_bundle(
                extension_source,
                replay_root / "runtime" / "archive-extension",
            )
            if bundle.extension_id != target_extension_id:
                raise ExtensionRecoveryError(
                    "production extension ID changed while preparing replays"
                )
            if production_bundle_sha256 is None:
                production_bundle_sha256 = bundle.tree_sha256
            elif production_bundle_sha256 != bundle.tree_sha256:
                raise ExtensionRecoveryError(
                    "production extension bundle differs between independent replays"
                )
            _write_json_atomic(
                replay_root / "recovery-state.json",
                _state_for_replay(replay_name, source, bundle, browser),
            )
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    replays = tuple(
        load_replay_workspace(destination / "workspaces" / name)
        for name in _REPLAY_NAMES
    )
    if production_bundle_sha256 is None:
        raise AssertionError("replay preparation did not build a production extension")
    return ReplayPreparation(
        root=destination,
        source_extension_id=source.source_extension_id,
        target_extension_id=target_extension_id,
        source_tree_sha256=source.working_copy_evidence_tree_sha256,
        source_payload_sha256=source.payload_sha256,
        production_bundle_sha256=production_bundle_sha256,
        browser_binary_sha256=browser.binary_sha256,
        replays=(replays[0], replays[1]),
    )


def _load_state(replay_root: Path) -> tuple[Path, dict[str, Any]]:
    root = _resolved_existing_directory(replay_root, "replay workspace")
    state_path = root / "recovery-state.json"
    try:
        state, _ = _load_json_object(state_path, "recovery state")
    except ExtensionRecoveryError as error:
        raise ExtensionRecoveryError(f"cannot read recovery state: {state_path}: {error}") from error
    if state.get("schema") != RECOVERY_STATE_SCHEMA:
        raise ExtensionRecoveryError("unsupported recovery state schema")
    if state.get("phase") not in {
        "awaiting_missing_database_preflight",
        "ready_to_install",
        "indexeddb_installed",
    }:
        raise ExtensionRecoveryError("invalid recovery state phase")
    expected_state_fields = {
        "schema",
        "phase",
        "replay_id",
        "replay_challenge",
        "created_at",
        "source_extension_id",
        "target_extension_id",
        "database_name",
        "database_version",
        "working_copy_evidence_tree_sha256",
        "source_indexeddb_tree_sha256",
        "source_payload_sha256",
        "working_copy_evidence_manifest_sha256",
        "production_bundle_sha256",
        "browser",
        "preparation_sha256",
        "user_data_directory",
        "staged_indexeddb_directory",
        "production_extension_directory",
        "output_directory",
        "sandbox_home_directory",
        "missing_database_preflight",
        "installed_indexeddb_directory",
    }
    if state["phase"] == "indexeddb_installed":
        expected_state_fields.add("installed_at")
    if set(state) != expected_state_fields:
        raise ExtensionRecoveryError("recovery state fields do not match its phase")
    for field in ("source_extension_id", "target_extension_id"):
        if not isinstance(state.get(field), str) or not _EXTENSION_ID.fullmatch(state[field]):
            raise ExtensionRecoveryError(f"invalid {field} in recovery state")
    if state.get("database_name") != DATABASE_NAME or state.get("database_version") != DATABASE_VERSION:
        raise ExtensionRecoveryError("recovery state database contract does not match this tool")
    if state.get("replay_id") not in _REPLAY_NAMES:
        raise ExtensionRecoveryError("invalid replay identity in recovery state")
    if state["replay_id"] != root.name:
        raise ExtensionRecoveryError("recovery state replay identity does not match workspace")
    replay_challenge = state.get("replay_challenge")
    if not _is_sha256(replay_challenge):
        raise ExtensionRecoveryError("invalid replay challenge in recovery state")
    for field in (
        "working_copy_evidence_tree_sha256",
        "source_indexeddb_tree_sha256",
        "source_payload_sha256",
        "production_bundle_sha256",
        "preparation_sha256",
    ):
        if not _is_sha256(state.get(field)):
            raise ExtensionRecoveryError(f"invalid {field} in recovery state")
    evidence_manifest_sha256 = state.get("working_copy_evidence_manifest_sha256")
    if evidence_manifest_sha256 is not None and not _is_sha256(evidence_manifest_sha256):
        raise ExtensionRecoveryError(
            "invalid working_copy_evidence_manifest_sha256 in recovery state"
        )
    browser = state.get("browser")
    if not isinstance(browser, dict) or set(browser) != {
        "path",
        "flavor",
        "version",
        "binary_sha256",
    }:
        raise ExtensionRecoveryError("invalid browser identity in recovery state")
    if browser.get("flavor") not in {"chrome-for-testing", "chromium"}:
        raise ExtensionRecoveryError("invalid browser flavor in recovery state")
    if not isinstance(browser.get("version"), str) or not browser["version"]:
        raise ExtensionRecoveryError("invalid browser version in recovery state")
    if not _is_sha256(browser.get("binary_sha256")):
        raise ExtensionRecoveryError("invalid browser hash in recovery state")
    if not isinstance(browser.get("path"), str) or not os.path.isabs(browser["path"]):
        raise ExtensionRecoveryError("invalid browser path in recovery state")
    expected_paths = {
        "user_data_directory": "chrome-user-data",
        "staged_indexeddb_directory": "recovery-copy/IndexedDB",
        "production_extension_directory": "runtime/archive-extension",
        "output_directory": f"outputs/{state['replay_id']}",
        "sandbox_home_directory": "sandbox-home",
    }
    if any(state.get(field) != expected for field, expected in expected_paths.items()):
        raise ExtensionRecoveryError("recovery state path contract is not canonical")
    preflight = state.get("missing_database_preflight")
    installed = state.get("installed_indexeddb_directory")
    phase = state["phase"]
    if phase == "awaiting_missing_database_preflight":
        phase_evidence_ok = preflight is None and installed is None
    else:
        phase_evidence_ok = (
            isinstance(preflight, dict)
            and set(preflight)
            == {
                "confirmed_at",
                "chrome_exited",
                "runtime_extension_id",
                "observed_database_count",
                "target_database_absent",
            }
            and isinstance(preflight.get("confirmed_at"), str)
            and bool(preflight["confirmed_at"])
            and preflight.get("chrome_exited") is True
            and preflight.get("runtime_extension_id") == state["target_extension_id"]
            and isinstance(preflight.get("observed_database_count"), int)
            and not isinstance(preflight["observed_database_count"], bool)
            and preflight["observed_database_count"] >= 0
            and preflight.get("target_database_absent") is True
        )
        if phase == "ready_to_install":
            phase_evidence_ok = phase_evidence_ok and installed is None
        else:
            phase_evidence_ok = (
                phase_evidence_ok
                and installed == "chrome-user-data/Default/IndexedDB"
                and isinstance(state.get("installed_at"), str)
                and bool(state["installed_at"])
            )
    if not phase_evidence_ok:
        raise ExtensionRecoveryError("recovery state phase evidence is invalid")
    preparation_payload = {
        "replay_id": state["replay_id"],
        "replay_challenge": state["replay_challenge"],
        "source_extension_id": state["source_extension_id"],
        "target_extension_id": state["target_extension_id"],
        "working_copy_evidence_tree_sha256": state[
            "working_copy_evidence_tree_sha256"
        ],
        "source_indexeddb_tree_sha256": state["source_indexeddb_tree_sha256"],
        "source_payload_sha256": state["source_payload_sha256"],
        "working_copy_evidence_manifest_sha256": state[
            "working_copy_evidence_manifest_sha256"
        ],
        "production_bundle_sha256": state["production_bundle_sha256"],
        "browser": browser,
        "user_data_directory": state.get("user_data_directory"),
        "staged_indexeddb_directory": state.get("staged_indexeddb_directory"),
        "production_extension_directory": state.get(
            "production_extension_directory"
        ),
        "output_directory": state.get("output_directory"),
        "sandbox_home_directory": state.get("sandbox_home_directory"),
    }
    if _sha256_json(preparation_payload) != state["preparation_sha256"]:
        raise ExtensionRecoveryError("recovery state preparation hash does not match")
    return root, state


def _state_relative_path(root: Path, state: dict[str, Any], field: str) -> Path:
    value = state.get(field)
    if not isinstance(value, str):
        raise ExtensionRecoveryError(f"missing recovery state path: {field}")
    relative = _safe_relative_path(value, field)
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            break
        except OSError as error:
            raise ExtensionRecoveryError(
                f"cannot inspect recovery state path: {field}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionRecoveryError(
                f"recovery state path contains a symlink: {field}"
            )
    candidate = candidate.resolve(strict=False)
    if not _is_relative_to(candidate, root):
        raise ExtensionRecoveryError(f"recovery state path escapes workspace: {field}")
    return candidate


def _preparation_relative_path(root: Path, state: dict[str, Any], field: str) -> Path:
    value = state.get(field)
    if not isinstance(value, str):
        raise ExtensionRecoveryError(f"missing recovery state path: {field}")
    relative = _safe_relative_path(value, field)
    preparation_root = root.parent.parent.resolve(strict=True)
    candidate = preparation_root
    for part in relative.parts:
        candidate = candidate / part
        try:
            metadata = os.lstat(candidate)
        except OSError as error:
            raise ExtensionRecoveryError(
                f"cannot inspect preparation path: {field}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionRecoveryError(
                f"preparation path contains a symlink: {field}"
            )
    candidate = candidate.resolve(strict=True)
    if not _is_relative_to(candidate, preparation_root):
        raise ExtensionRecoveryError(f"recovery state path escapes preparation: {field}")
    return candidate


def load_replay_workspace(replay_root: Path) -> ReplayWorkspace:
    root, state = _load_state(replay_root)
    if root.parent.name != "workspaces":
        raise ExtensionRecoveryError("replay workspace is outside the prepared layout")
    preparation_root = root.parent.parent.resolve(strict=True)
    production_extension = _state_relative_path(
        root, state, "production_extension_directory"
    )
    bundle = validate_production_extension_bundle(production_extension)
    if bundle.tree_sha256 != state["production_bundle_sha256"]:
        raise ExtensionRecoveryError("production extension bundle changed after preparation")
    output_directory = _preparation_relative_path(root, state, "output_directory")
    expected_output = preparation_root / "outputs" / state["replay_id"]
    if output_directory != expected_output.resolve(strict=True):
        raise ExtensionRecoveryError("replay output directory is not the dedicated prepared path")
    if not output_directory.is_dir():
        raise ExtensionRecoveryError("replay output directory is missing or unsafe")
    if _paths_overlap(output_directory, root):
        raise ExtensionRecoveryError("replay output directory overlaps its workspace")
    browser_path = Path(_resolve_executable(state["browser"]["path"], "Chrome binary"))
    _, browser_sha256 = _hash_regular_file(browser_path)
    if browser_sha256 != state["browser"]["binary_sha256"]:
        raise ExtensionRecoveryError("Chrome binary changed after recovery preparation")
    return ReplayWorkspace(
        root=root,
        preparation_root=preparation_root,
        user_data_directory=_state_relative_path(root, state, "user_data_directory"),
        staged_indexeddb_directory=_state_relative_path(
            root, state, "staged_indexeddb_directory"
        ),
        production_extension_directory=production_extension,
        output_directory=output_directory,
        state_path=root / "recovery-state.json",
        replay_id=state["replay_id"],
        replay_challenge=state["replay_challenge"],
        source_extension_id=state["source_extension_id"],
        target_extension_id=state["target_extension_id"],
        phase=state["phase"],
        working_copy_evidence_tree_sha256=state[
            "working_copy_evidence_tree_sha256"
        ],
        source_payload_sha256=state["source_payload_sha256"],
        production_bundle_sha256=state["production_bundle_sha256"],
        preparation_sha256=state["preparation_sha256"],
        recovery_state_sha256=_sha256_json(state),
        browser_path=browser_path,
        browser_flavor=state["browser"]["flavor"],
        browser_version=state["browser"]["version"],
        browser_binary_sha256=browser_sha256,
    )


def _profile_has_live_process(user_data_directory: Path) -> bool:
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    expected = user_data_directory.resolve(strict=True)
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            arguments = [
                argument
                for argument in (process / "cmdline").read_bytes().split(b"\0")
                if argument
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        for index, argument in enumerate(arguments):
            if argument == b"--user-data-dir" and index + 1 < len(arguments):
                observed = arguments[index + 1]
            elif argument.startswith(b"--user-data-dir="):
                observed = argument.partition(b"=")[2]
            else:
                continue
            try:
                observed_path = Path(os.fsdecode(observed))
                if not observed_path.is_absolute():
                    process_cwd = (process / "cwd").resolve(strict=True)
                    observed_path = process_cwd / observed_path
                observed_path = observed_path.resolve(strict=False)
            except (OSError, UnicodeError):
                continue
            if observed_path == expected:
                return True
    return False


def _assert_profile_inactive(user_data_directory: Path) -> None:
    for marker in _ACTIVE_PROFILE_MARKERS:
        if (user_data_directory / marker).exists() or (
            user_data_directory / marker
        ).is_symlink():
            raise ExtensionRecoveryError(
                f"Chrome profile appears active ({marker}); Chrome must exit completely"
            )
    if _profile_has_live_process(user_data_directory):
        raise ExtensionRecoveryError(
            "Chrome profile has a live process; Chrome must exit completely"
        )


def _wait_for_profile_inactive(
    user_data_directory: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.25,
    stale_marker_grace_seconds: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    no_process_since: float | None = None
    while True:
        try:
            _assert_profile_inactive(user_data_directory)
            return
        except ExtensionRecoveryError as error:
            now = time.monotonic()
            if now >= deadline:
                raise ExtensionRecoveryError(
                    "timed out waiting for recovery Chrome to exit completely"
                ) from error
            if _profile_has_live_process(user_data_directory):
                no_process_since = None
            else:
                no_process_since = no_process_since or now
                if now - no_process_since >= stale_marker_grace_seconds:
                    raise ExtensionRecoveryError(
                        "recovery Chrome exited but left stale profile locks"
                    ) from error
            time.sleep(min(poll_seconds, max(0.0, deadline - now)))


def _assert_indexeddb_empty(user_data_directory: Path) -> None:
    default = user_data_directory / "Default"
    if default.exists() or default.is_symlink():
        if default.is_symlink() or not default.is_dir():
            raise ExtensionRecoveryError("fresh Chrome profile has an unsafe Default path")
        try:
            default_resolved = default.resolve(strict=True)
        except OSError as error:
            raise ExtensionRecoveryError("cannot resolve fresh Chrome Default profile") from error
        if not _is_relative_to(default_resolved, user_data_directory.resolve(strict=True)):
            raise ExtensionRecoveryError("fresh Chrome Default profile escapes replay workspace")
    indexeddb = default / "IndexedDB"
    if not indexeddb.exists():
        return
    if indexeddb.is_symlink() or not indexeddb.is_dir():
        raise ExtensionRecoveryError("fresh Chrome profile has an unsafe IndexedDB path")
    if not _directory_is_empty(indexeddb):
        raise ExtensionRecoveryError(
            "fresh Chrome profile IndexedDB is not empty; refusing to overwrite it"
        )


def confirm_missing_database_preflight(
    replay_root: Path,
    *,
    runtime_extension_id: str,
    observed_database_names: Sequence[str],
    chrome_exited: bool,
) -> ReplayWorkspace:
    """Record browser preflight proof only after the isolated Chrome has exited."""

    root, state = _load_state(replay_root)
    if state["phase"] != "awaiting_missing_database_preflight":
        raise ExtensionRecoveryError("missing-database preflight is not the current phase")
    if not chrome_exited:
        raise ExtensionRecoveryError("Chrome must exit completely before preflight is accepted")
    if runtime_extension_id != state["target_extension_id"]:
        raise ExtensionRecoveryError("preflight extension ID does not match the fixed manifest key")
    if not all(isinstance(name, str) for name in observed_database_names):
        raise ExtensionRecoveryError("observed database names must be strings")
    if DATABASE_NAME in observed_database_names:
        raise ExtensionRecoveryError("target database existed before recovery installation")
    user_data = _state_relative_path(root, state, "user_data_directory")
    _assert_profile_inactive(user_data)
    _assert_indexeddb_empty(user_data)
    state["phase"] = "ready_to_install"
    state["missing_database_preflight"] = {
        "confirmed_at": _now(),
        "chrome_exited": True,
        "runtime_extension_id": runtime_extension_id,
        "observed_database_count": len(observed_database_names),
        "target_database_absent": True,
    }
    _write_json_atomic(root / "recovery-state.json", state)
    return load_replay_workspace(root)


def install_recovery_indexeddb(
    replay_root: Path,
    *,
    chrome_exited: bool,
) -> ReplayWorkspace:
    """Install and origin-rename one independent copy after preflight and exit."""

    root, state = _load_state(replay_root)
    if state["phase"] != "ready_to_install":
        raise ExtensionRecoveryError("replay is not ready for IndexedDB installation")
    if not chrome_exited:
        raise ExtensionRecoveryError("Chrome must exit completely before IndexedDB installation")
    user_data = _state_relative_path(root, state, "user_data_directory")
    staged = _state_relative_path(root, state, "staged_indexeddb_directory")
    _assert_profile_inactive(user_data)
    _assert_indexeddb_empty(user_data)
    if not staged.is_dir() or staged.is_symlink():
        raise ExtensionRecoveryError("staged IndexedDB recovery copy is missing or unsafe")
    if _payload_fingerprint(staged, state["source_extension_id"]) != state[
        "source_payload_sha256"
    ]:
        raise ExtensionRecoveryError("staged IndexedDB recovery copy changed before install")

    default = user_data / "Default"
    default.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = default / "IndexedDB"
    if target.exists():
        target.rmdir()  # _assert_indexeddb_empty proved that this removes no evidence.
    installation_staging = default / ".pmv-indexeddb-install"
    if installation_staging.exists() or installation_staging.is_symlink():
        raise ExtensionRecoveryError("an incomplete prior IndexedDB installation exists")
    os.replace(staged, installation_staging)
    source_id = state["source_extension_id"]
    target_id = state["target_extension_id"]
    for kind in ("leveldb", "blob"):
        old = installation_staging / f"chrome-extension_{source_id}_0.indexeddb.{kind}"
        new = installation_staging / f"chrome-extension_{target_id}_0.indexeddb.{kind}"
        if not old.is_dir() or new.exists():
            raise ExtensionRecoveryError(f"cannot safely rename IndexedDB {kind} origin")
        os.replace(old, new)
    if _payload_fingerprint(installation_staging, target_id) != state[
        "source_payload_sha256"
    ]:
        raise ExtensionRecoveryError("IndexedDB payload changed during origin rename")
    os.replace(installation_staging, target)

    state["phase"] = "indexeddb_installed"
    state["installed_indexeddb_directory"] = "chrome-user-data/Default/IndexedDB"
    state["installed_at"] = _now()
    _write_json_atomic(root / "recovery-state.json", state)
    return load_replay_workspace(root)


def _resolve_executable(value: Path | str, label: str) -> str:
    text = os.fspath(value)
    candidate = shutil.which(text) if not os.path.isabs(text) else text
    if candidate is None:
        raise ExtensionRecoveryError(f"cannot locate {label}: {text}")
    resolved = Path(candidate).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ExtensionRecoveryError(f"{label} is not executable: {resolved}")
    return os.fspath(resolved)


def inspect_compatible_chrome_binary(value: Path | str) -> BrowserIdentity:
    """Identify and hash a browser that still supports unpacked recovery extensions."""

    resolved = Path(_resolve_executable(value, "Chrome binary"))
    _, binary_sha256_before = _hash_regular_file(resolved)
    try:
        completed = subprocess.run(
            [os.fspath(resolved), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="strict",
            env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ExtensionRecoveryError("cannot probe compatible Chrome binary") from error
    reported = completed.stdout.strip()
    if completed.returncode != 0 or not reported:
        raise ExtensionRecoveryError("executable is not a compatible Chrome binary")
    testing_match = re.fullmatch(
        r"Google Chrome for Testing ([0-9]+(?:\.[0-9]+){1,3})", reported
    )
    chromium_match = re.fullmatch(
        r"(?:Chromium|Chromium Browser) ([0-9]+(?:\.[0-9]+){1,3})(?: .*)?",
        reported,
    )
    if testing_match is not None:
        flavor = "chrome-for-testing"
        version = testing_match.group(1)
    elif chromium_match is not None:
        flavor = "chromium"
        version = chromium_match.group(1)
    elif reported.startswith("Google Chrome "):
        raise ExtensionRecoveryError(
            "branded Google Chrome is not accepted for unpacked recovery extensions"
        )
    else:
        raise ExtensionRecoveryError("executable is not a compatible Chrome binary")
    _, binary_sha256 = _hash_regular_file(resolved)
    if binary_sha256 != binary_sha256_before:
        raise ExtensionRecoveryError("Chrome binary changed while probing its identity")
    return BrowserIdentity(
        path=resolved,
        flavor=flavor,
        version=version,
        binary_sha256=binary_sha256,
    )


def _resolve_bubblewrap_binary(value: Path | str) -> str:
    resolved = Path(_resolve_executable(value, "bubblewrap binary"))
    _, digest_before = _hash_regular_file(resolved)
    try:
        completed = subprocess.run(
            [os.fspath(resolved), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="strict",
            env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ExtensionRecoveryError("cannot probe bubblewrap binary") from error
    if completed.returncode != 0 or re.fullmatch(
        r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", completed.stdout.strip()
    ) is None:
        raise ExtensionRecoveryError("executable is not a compatible bubblewrap binary")
    _, digest_after = _hash_regular_file(resolved)
    if digest_before != digest_after:
        raise ExtensionRecoveryError("bubblewrap binary changed while probing its identity")
    return os.fspath(resolved)


def build_isolated_chrome_argv(
    replay_root: Path,
    *,
    bwrap_binary: Path | str = "bwrap",
) -> tuple[str, ...]:
    """Construct, but never execute, an offline bubblewrap Chrome invocation."""

    workspace = load_replay_workspace(replay_root)
    _assert_profile_inactive(workspace.user_data_directory)
    bundle = validate_production_extension_bundle(
        workspace.production_extension_directory
    )
    if (
        bundle.extension_id != workspace.target_extension_id
        or bundle.tree_sha256 != workspace.production_bundle_sha256
    ):
        raise ExtensionRecoveryError("bound production extension bundle changed")
    browser = inspect_compatible_chrome_binary(workspace.browser_path)
    if (
        browser.path != workspace.browser_path
        or browser.flavor != workspace.browser_flavor
        or browser.version != workspace.browser_version
        or browser.binary_sha256 != workspace.browser_binary_sha256
    ):
        raise ExtensionRecoveryError("bound Chrome binary identity changed")

    chrome = os.fspath(browser.path)
    bwrap = _resolve_bubblewrap_binary(bwrap_binary)
    sandbox_home = workspace.root / "sandbox-home"
    config_home = sandbox_home / ".config"
    cache_home = sandbox_home / ".cache"
    data_home = sandbox_home / ".local" / "share"
    writable_binds = [workspace.user_data_directory, sandbox_home]
    if workspace.phase == "indexeddb_installed":
        if not _directory_is_empty(workspace.output_directory):
            raise ExtensionRecoveryError(
                "dedicated replay output directory is not empty before export"
            )
        if _paths_overlap(workspace.output_directory, workspace.root):
            raise ExtensionRecoveryError("replay output overlaps its workspace")
        writable_binds.append(workspace.output_directory)

    provenance = {
        "replay_id": workspace.replay_id,
        "challenge": workspace.replay_challenge,
        "working_copy_evidence_tree_sha256": workspace.working_copy_evidence_tree_sha256,
        "source_payload_sha256": workspace.source_payload_sha256,
        "production_bundle_sha256": workspace.production_bundle_sha256,
        "preparation_sha256": workspace.preparation_sha256,
        "recovery_state_sha256": workspace.recovery_state_sha256,
        "browser_flavor": workspace.browser_flavor,
        "browser_version": workspace.browser_version,
        "browser_binary_sha256": workspace.browser_binary_sha256,
    }
    export_url = (
        f"chrome-extension://{workspace.target_extension_id}/export.html?"
        f"{urlencode(provenance)}"
    )

    argv: list[str] = [
        bwrap,
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]
    for path in writable_binds:
        argv.extend(("--bind", os.fspath(path), os.fspath(path)))
    argv.extend(
        (
            "--setenv",
            "HOME",
            os.fspath(sandbox_home),
            "--setenv",
            "XDG_CONFIG_HOME",
            os.fspath(config_home),
            "--setenv",
            "XDG_CACHE_HOME",
            os.fspath(cache_home),
            "--setenv",
            "XDG_DATA_HOME",
            os.fspath(data_home),
        )
    )
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        argv.extend(("--unsetenv", variable))
    argv.extend(
        (
            "--",
            chrome,
            f"--user-data-dir={workspace.user_data_directory}",
            "--profile-directory=Default",
            f"--load-extension={workspace.production_extension_directory}",
            f"--disable-extensions-except={workspace.production_extension_directory}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-domain-reliability",
            "--ozone-platform=wayland",
            "--password-store=basic",
            export_url,
        )
    )
    forbidden = ("--no-sandbox", "--remote-debugging-port", "--remote-debugging-pipe")
    if any(any(item == token or item.startswith(f"{token}=") for token in forbidden) for item in argv):
        raise AssertionError("unsafe Chrome option entered isolated argv")
    return tuple(argv)


def run_isolated_chrome(
    replay_root: Path,
    *,
    bwrap_binary: Path | str = "bwrap",
    profile_exit_timeout_seconds: float = 12 * 60 * 60,
) -> ChromeRunResult:
    """Run the bound recovery Chrome and wait until its profile is inactive.

    The browser remains interactive because the preflight and export phases require
    a human to inspect the page and, for export, choose the dedicated output
    directory. Browser stdout/stderr are discarded so profile details cannot leak
    into automation logs.
    """

    workspace = load_replay_workspace(replay_root)
    if workspace.phase not in {
        "awaiting_missing_database_preflight",
        "indexeddb_installed",
    }:
        raise ExtensionRecoveryError(
            "isolated Chrome may run only for preflight or installed export phases"
        )
    argv = build_isolated_chrome_argv(workspace.root, bwrap_binary=bwrap_binary)
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise ExtensionRecoveryError("cannot launch isolated recovery Chrome") from error
    if completed.returncode != 0:
        raise ExtensionRecoveryError(
            f"isolated recovery Chrome exited with status {completed.returncode}"
        )
    _wait_for_profile_inactive(
        workspace.user_data_directory,
        timeout_seconds=profile_exit_timeout_seconds,
    )
    return ChromeRunResult(
        replay_id=workspace.replay_id,
        phase=workspace.phase,
        returncode=completed.returncode,
    )


def discover_export_run(replay_root: Path) -> Path:
    """Return the sole closed browser-export run for one prepared replay."""

    workspace = load_replay_workspace(replay_root)
    if workspace.phase != "indexeddb_installed":
        raise ExtensionRecoveryError(
            "replay IndexedDB must be installed before locating an export"
        )
    try:
        entries = sorted(
            os.scandir(workspace.output_directory), key=lambda item: os.fsencode(item.name)
        )
    except OSError as error:
        raise ExtensionRecoveryError("cannot inspect dedicated replay output") from error
    if len(entries) != 1:
        raise ExtensionRecoveryError(
            "dedicated replay output must contain exactly one export run"
        )
    entry = entries[0]
    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
        raise ExtensionRecoveryError("browser export run is not a safe directory")
    run = Path(entry.path).resolve(strict=True)
    if run.parent != workspace.output_directory.resolve(strict=True):
        raise ExtensionRecoveryError("browser export run escaped its dedicated output")
    return run


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ExtensionRecoveryError(f"unsafe {label} path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ExtensionRecoveryError(f"unsafe {label} path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ExtensionRecoveryError(f"unsafe {label} path: {value!r}")
    return path


def _manifest_file(root: Path, value: str, label: str, prefix: str) -> tuple[Path, str]:
    relative = _safe_relative_path(value, label)
    if relative.parts[0] != prefix:
        raise ExtensionRecoveryError(f"{label} path must be below {prefix}/")
    absolute = root.joinpath(*relative.parts)
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise ExtensionRecoveryError(f"missing {label} file: {value}") from error
    if not _is_relative_to(resolved, root) or absolute.is_symlink() or not resolved.is_file():
        raise ExtensionRecoveryError(f"unsafe {label} file: {value}")
    return resolved, relative.as_posix()


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteJSONError(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise _NonFiniteJSONError(f"non-finite JSON number: {value}")


def _load_json_object(path: Path, label: str) -> tuple[dict[str, Any], FileDigest]:
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ExtensionRecoveryError(f"{label} is not a regular file")
            chunks: list[bytes] = []
            byte_count = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                byte_count += len(chunk)
                chunks.append(chunk)
            after = os.fstat(descriptor)
            path_after = os.lstat(path)
            fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if (
                any(getattr(before, field) != getattr(after, field) for field in fields)
                or any(
                    getattr(after, field) != getattr(path_after, field)
                    for field in fields
                )
                or byte_count != after.st_size
            ):
                raise ExtensionRecoveryError(f"{label} changed while reading")
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ExtensionRecoveryError(f"{label} must not contain a UTF-8 BOM")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKeyError,
        _NonFiniteJSONError,
    ) as error:
        raise ExtensionRecoveryError(f"cannot parse {label}: {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ExtensionRecoveryError(f"{label} must be a JSON object")
    return value, FileDigest(path.name, len(raw), hashlib.sha256(raw).hexdigest())


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExtensionRecoveryError("export row is not canonical JSON data") from error


def _validate_tagged_graph(graph: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(graph, dict) or set(graph) != {"codec", "data"}:
        raise ExtensionRecoveryError(f"invalid tagged graph envelope: {label}")
    if graph.get("codec") != "tagged-structured-clone-v1":
        raise ExtensionRecoveryError(f"unsupported tagged graph codec: {label}")
    data = graph.get("data")
    if not isinstance(data, dict) or set(data) != {"root", "nodes"}:
        raise ExtensionRecoveryError(f"invalid tagged graph data: {label}")
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ExtensionRecoveryError(f"invalid tagged graph nodes: {label}")
    referenced_nodes: set[int] = set()

    def collect_references(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                if set(value) != {"$ref"}:
                    raise ExtensionRecoveryError(f"ambiguous tagged graph reference: {label}")
                reference = value["$ref"]
                if (
                    not isinstance(reference, int)
                    or isinstance(reference, bool)
                    or reference < 0
                    or reference >= len(nodes)
                ):
                    raise ExtensionRecoveryError(f"out-of-range tagged graph reference: {label}")
                referenced_nodes.add(reference)
                return
            for child in value.values():
                collect_references(child)
        elif isinstance(value, list):
            for child in value:
                collect_references(child)

    collect_references(data["root"])
    pending = list(referenced_nodes)
    visited: set[int] = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        before = set(referenced_nodes)
        collect_references(nodes[node_id])
        pending.extend(referenced_nodes - before - visited)
    if visited != set(range(len(nodes))):
        raise ExtensionRecoveryError(f"tagged graph contains unreachable nodes: {label}")

    expected_parts: list[dict[str, Any]] = []
    for node_id, node in enumerate(nodes):
        node_type = node.get("$type")
        data_field = node.get("data")
        if not (
            isinstance(data_field, dict)
            and set(data_field) == {"external"}
            and isinstance(data_field["external"], dict)
        ):
            continue
        external = data_field["external"]
        if set(external) != {"path", "byteLength"}:
            raise ExtensionRecoveryError(f"invalid external binary reference: {label}")
        path = external.get("path")
        length = external.get("byteLength")
        if (
            node_type not in {"ArrayBuffer", "Blob", "File"}
            or not isinstance(path, str)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
            or length > 2**53 - 1
        ):
            raise ExtensionRecoveryError(f"invalid external binary node: {label}")
        declared_node_size = node.get("byteLength") if node_type == "ArrayBuffer" else node.get("size")
        if declared_node_size != length:
            raise ExtensionRecoveryError(f"external binary node size mismatch: {label}")
        _safe_relative_path(path, "tagged graph binary reference")
        expected_parts.append(
            {
                "nodeId": node_id,
                "kind": node_type,
                "byteLength": length,
                "path": path,
            }
        )
    return expected_parts


def _stream_ndjson_store(
    root: Path,
    manifest: dict[str, Any],
    store: dict[str, Any],
) -> tuple[
    StoreFingerprint,
    tuple[RowFingerprint, ...],
    dict[str, tuple[int, str]],
    FileDigest,
]:
    name = store.get("name")
    if not isinstance(name, str) or not name:
        raise ExtensionRecoveryError("store manifest entry has no name")
    for field in (
        "count_before_export",
        "cursor_row_count",
        "encoded_row_count",
        "failed_row_count",
        "unencoded_cursor_row_count",
        "attempted_output_bytes",
        "external_binary_part_count",
        "external_binary_bytes",
        "external_binary_attempt_count",
        "failed_external_binary_part_count",
    ):
        if not isinstance(store.get(field), int) or isinstance(store.get(field), bool) or store[field] < 0:
            raise ExtensionRecoveryError(f"invalid {field} for store {name}")
        if store[field] > 2**53 - 1:
            raise ExtensionRecoveryError(f"unsafe integer {field} for store {name}")
    expected_count = store["count_before_export"]
    if not (
        store.get("status") == "complete"
        and store.get("count_matches") is True
        and store.get("output_committed") is True
        and store["failed_row_count"] == 0
        and store["unencoded_cursor_row_count"] == 0
        and store["failed_external_binary_part_count"] == 0
        and expected_count == store["cursor_row_count"] == store["encoded_row_count"]
    ):
        raise ExtensionRecoveryError(f"store count did not close: {name}")

    output_value = store.get("output_file")
    if not isinstance(output_value, str):
        raise ExtensionRecoveryError(f"store output path is missing: {name}")
    output, output_relative = _manifest_file(root, output_value, "store output", "stores")
    if not output_relative.endswith(".ndjson"):
        raise ExtensionRecoveryError(f"store output is not NDJSON: {name}")
    expected_binary_prefix = f"binary/{PurePosixPath(output_relative).stem}/"
    file_digest = hashlib.sha256()
    row_digest = hashlib.sha256()
    binary_references: dict[str, tuple[int, str]] = {}
    row_fingerprints: list[RowFingerprint] = []
    row_count = 0
    byte_count = 0
    try:
        descriptor = os.open(
            output,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ExtensionRecoveryError(f"store output is not a regular file: {name}")
        handle = os.fdopen(descriptor, "rb")
    except OSError as error:
        raise ExtensionRecoveryError(f"cannot open store output: {name}") from error
    with handle:
        before = os.fstat(handle.fileno())
        for line_number, raw_line in enumerate(handle, start=1):
            file_digest.update(raw_line)
            byte_count += len(raw_line)
            if not raw_line.endswith(b"\n") or raw_line == b"\n":
                raise ExtensionRecoveryError(
                    f"invalid NDJSON framing in store {name} at line {line_number}"
                )
            try:
                envelope = json.loads(
                    raw_line.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                _DuplicateKeyError,
                _NonFiniteJSONError,
            ) as error:
                raise ExtensionRecoveryError(
                    f"invalid NDJSON row in store {name} at line {line_number}: {error}"
                ) from error
            if not isinstance(envelope, dict):
                raise ExtensionRecoveryError(
                    f"NDJSON row is not an object in store {name} at line {line_number}"
                )
            expected_ordinal = line_number - 1
            required = {
                "envelope_schema": ROW_ENVELOPE_SCHEMA,
                "snapshot_id": manifest["snapshot_id"],
                "source_kind": SOURCE_KIND,
                "database_name": DATABASE_NAME,
                "database_version": DATABASE_VERSION,
                "store_name": name,
                "cursor_ordinal": expected_ordinal,
                "decode_status": "ok",
                "decoded_value_hash_status": "deferred_to_host",
            }
            if any(envelope.get(key) != value for key, value in required.items()):
                raise ExtensionRecoveryError(
                    f"row envelope contract mismatch in store {name} at line {line_number}"
                )
            if envelope.get("decoded_value_sha256") is not None or "value" not in envelope:
                raise ExtensionRecoveryError(
                    f"row host-hash state is invalid in store {name} at line {line_number}"
                )
            decoded_value_sha256 = hashlib.sha256(
                _canonical_json_bytes(envelope["value"])
            ).hexdigest()
            canonical_row_sha256 = hashlib.sha256(
                _canonical_json_bytes(envelope)
            ).hexdigest()
            row_digest.update(
                f"{expected_ordinal}\0{canonical_row_sha256}\0{decoded_value_sha256}\0".encode(
                    "ascii"
                )
            )
            key_parts = _validate_tagged_graph(
                envelope.get("source_primary_key"),
                f"{name} line {line_number} primary key",
            )
            value_parts = _validate_tagged_graph(
                envelope["value"], f"{name} line {line_number} value"
            )
            parts = envelope.get("binary_parts")
            if not isinstance(parts, list):
                raise ExtensionRecoveryError(
                    f"binary_parts is not a list in store {name} at line {line_number}"
                )
            if parts != [*key_parts, *value_parts]:
                raise ExtensionRecoveryError(
                    f"row and tagged graph binary inventories differ in store {name} at line {line_number}"
                )
            row_fingerprints.append(
                RowFingerprint(
                    store_name=name,
                    cursor_ordinal=expected_ordinal,
                    line_sha256=hashlib.sha256(raw_line).hexdigest(),
                    canonical_row_sha256=canonical_row_sha256,
                    decoded_value_graph_sha256=decoded_value_sha256,
                    binary_reference_sha256=hashlib.sha256(
                        _canonical_json_bytes(parts)
                    ).hexdigest(),
                )
            )
            for part in parts:
                if not isinstance(part, dict):
                    raise ExtensionRecoveryError(
                        f"invalid binary reference in store {name} at line {line_number}"
                    )
                path_value = part.get("path")
                length = part.get("byteLength")
                if (
                    not isinstance(path_value, str)
                    or not isinstance(length, int)
                    or isinstance(length, bool)
                    or length < 0
                    or length > 2**53 - 1
                ):
                    raise ExtensionRecoveryError(
                        f"invalid binary path or size in store {name} at line {line_number}"
                    )
                relative = _safe_relative_path(path_value, "binary reference").as_posix()
                if not relative.startswith(expected_binary_prefix):
                    raise ExtensionRecoveryError(
                        f"binary reference is outside its store directory: {name}"
                    )
                if relative in binary_references:
                    raise ExtensionRecoveryError(f"duplicate binary reference path: {relative}")
                binary_references[relative] = (length, name)
            row_count += 1
        after = os.fstat(handle.fileno())
    try:
        path_after = os.lstat(output)
    except OSError as error:
        raise ExtensionRecoveryError(
            f"cannot restat store output after reading: {name}"
        ) from error
    identity_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(getattr(before, field) != getattr(after, field) for field in identity_fields)
        or any(
            getattr(after, field) != getattr(path_after, field)
            for field in identity_fields
        )
        or byte_count != after.st_size
    ):
        raise ExtensionRecoveryError(f"store output changed while reading: {name}")
    if row_count != expected_count:
        raise ExtensionRecoveryError(
            f"actual NDJSON row count did not close for store {name}: {row_count} != {expected_count}"
        )
    if byte_count != store["attempted_output_bytes"]:
        raise ExtensionRecoveryError(f"actual NDJSON byte count did not close for store {name}")
    if len(binary_references) != store["external_binary_part_count"]:
        raise ExtensionRecoveryError(f"binary part count did not close for store {name}")
    if sum(length for length, _ in binary_references.values()) != store["external_binary_bytes"]:
        raise ExtensionRecoveryError(f"declared binary bytes did not close for store {name}")
    attempts = store.get("external_binary_parts")
    if (
        not isinstance(attempts, list)
        or not all(isinstance(attempt, dict) for attempt in attempts)
        or len(attempts) != store["external_binary_attempt_count"]
        or len(attempts) != store["external_binary_part_count"]
    ):
        raise ExtensionRecoveryError(f"binary attempt inventory did not close for store {name}")
    attempt_paths: set[str] = set()
    for attempt in attempts:
        path_value = attempt.get("path")
        byte_length = attempt.get("byte_length")
        cursor_ordinal = attempt.get("cursor_ordinal")
        node_id = attempt.get("node_id")
        if (
            not isinstance(path_value, str)
            or not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or byte_length < 0
            or not isinstance(cursor_ordinal, int)
            or isinstance(cursor_ordinal, bool)
            or cursor_ordinal < 0
            or cursor_ordinal >= row_count
            or not isinstance(node_id, int)
            or isinstance(node_id, bool)
            or node_id < 0
            or attempt.get("role") not in {"primary-key", "value"}
            or attempt.get("kind") not in {"ArrayBuffer", "Blob", "File"}
            or attempt.get("status") != "complete"
        ):
            raise ExtensionRecoveryError(f"invalid binary attempt for store {name}")
        normalized = _safe_relative_path(path_value, "binary attempt").as_posix()
        if normalized in attempt_paths:
            raise ExtensionRecoveryError(f"duplicate binary attempt path: {normalized}")
        attempt_paths.add(normalized)
        reference = binary_references.get(normalized)
        if reference is None or reference[0] != byte_length:
            raise ExtensionRecoveryError(
                f"binary attempt does not match row reference for store {name}"
            )
    if attempt_paths != set(binary_references):
        raise ExtensionRecoveryError(f"binary attempts omitted row references for store {name}")

    empty_binary_sha = hashlib.sha256(b"").hexdigest()
    fingerprint = StoreFingerprint(
        name=name,
        row_count=row_count,
        row_sha256=row_digest.hexdigest(),
        ndjson_sha256=file_digest.hexdigest(),
        ndjson_bytes=byte_count,
        binary_part_count=0,
        binary_bytes=0,
        binary_sha256=empty_binary_sha,
    )
    return (
        fingerprint,
        tuple(row_fingerprints),
        binary_references,
        FileDigest(output_relative, byte_count, file_digest.hexdigest()),
    )


def _closed_export_entries(root: Path):
    try:
        entries = scan_evidence(root)
    except EvidenceError as error:
        raise ExtensionRecoveryError(
            f"cannot safely scan browser export: {error}"
        ) from error
    unsafe = next((entry for entry in entries if entry.kind == "symlink"), None)
    if unsafe is not None:
        raise ExtensionRecoveryError(
            f"cannot safely scan browser export: symlink is forbidden: {unsafe.path}"
        )
    return entries


def _tree_inventory_from_entries(entries) -> tuple[tuple[str, ...], tuple[str, ...]]:
    files = [entry.path for entry in entries if entry.kind == "file"]
    directories_found = [
        entry.path for entry in entries if entry.kind == "directory"
    ]
    return (
        tuple(sorted(files, key=os.fsencode)),
        tuple(sorted(directories_found, key=os.fsencode)),
    )


def _tree_inventory(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _tree_inventory_from_entries(_closed_export_entries(root))


def _validate_observed_database_schema(
    observed_schema: object,
    stores: list[dict[str, Any]],
) -> None:
    if not isinstance(observed_schema, list):
        raise ExtensionRecoveryError("browser export observed schema is missing")
    observed_by_name: dict[str, dict[str, Any]] = {}
    observed_names: list[str] = []
    for entry in observed_schema:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "key_path",
            "auto_increment",
            "indexes",
        }:
            raise ExtensionRecoveryError("browser export observed schema fields are invalid")
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in observed_by_name:
            raise ExtensionRecoveryError("browser export observed schema names are invalid")
        observed_by_name[name] = entry
        observed_names.append(name)
    if set(observed_by_name) != set(EXPECTED_DATABASE_SCHEMA):
        raise ExtensionRecoveryError("browser export observed schema store set is not closed")

    for name, expected in EXPECTED_DATABASE_SCHEMA.items():
        observed = observed_by_name[name]
        if (
            observed.get("key_path") != expected["key_path"]
            or observed.get("auto_increment") is not expected["auto_increment"]
        ):
            raise ExtensionRecoveryError(
                f"browser export observed schema definition differs: {name}"
            )
        indexes = observed.get("indexes")
        if not isinstance(indexes, list):
            raise ExtensionRecoveryError(
                f"browser export observed schema indexes are invalid: {name}"
            )
        indexes_by_name: dict[str, dict[str, Any]] = {}
        for index in indexes:
            if not isinstance(index, dict) or set(index) != {
                "name",
                "key_path",
                "unique",
                "multi_entry",
            } or not isinstance(index.get("unique"), bool) or not isinstance(
                index.get("multi_entry"), bool
            ):
                raise ExtensionRecoveryError(
                    f"browser export observed index fields are invalid: {name}"
                )
            index_name = index.get("name")
            if (
                not isinstance(index_name, str)
                or not index_name
                or index_name in indexes_by_name
            ):
                raise ExtensionRecoveryError(
                    f"browser export observed index names are invalid: {name}"
                )
            indexes_by_name[index_name] = index
        expected_indexes = {
            index["name"]: index for index in expected["indexes"]
        }
        if indexes_by_name != expected_indexes:
            raise ExtensionRecoveryError(
                f"browser export observed schema indexes differ: {name}"
            )

    store_names = [store.get("name") for store in stores]
    if store_names != observed_names:
        raise ExtensionRecoveryError(
            "browser export store order does not match observed schema"
        )
    for store in stores:
        name = store["name"]
        projection = {
            "name": name,
            "key_path": store.get("key_path"),
            "auto_increment": store.get("auto_increment"),
            "indexes": store.get("indexes"),
        }
        if projection != observed_by_name[name]:
            raise ExtensionRecoveryError(
                f"browser export store schema differs from observed schema: {name}"
            )


def validate_export_run(
    run_directory: Path,
    *,
    replay_root: Path,
    chrome_exited: bool,
) -> ValidatedExport:
    """Validate one browser export and stream its rows and external binaries."""

    if not chrome_exited:
        raise ExtensionRecoveryError("Chrome must exit completely before host validation")
    workspace = load_replay_workspace(replay_root)
    if workspace.phase != "indexeddb_installed":
        raise ExtensionRecoveryError("replay IndexedDB must be installed before validation")
    _assert_profile_inactive(workspace.user_data_directory)
    root = _resolved_existing_directory(run_directory, "browser export run")
    if root.parent != workspace.output_directory.resolve(strict=True):
        raise ExtensionRecoveryError(
            "browser export run is outside its dedicated replay output directory"
        )
    initial_entries = _closed_export_entries(root)
    manifest_path = root / "manifest.json"
    manifest, manifest_file_digest = _load_json_object(
        manifest_path, "browser export manifest"
    )
    expected_manifest_fields = {
        "manifest_schema",
        "status",
        "browser_export_complete",
        "extraction_complete",
        "extraction_complete_status",
        "snapshot_id",
        "source_kind",
        "started_at",
        "completed_at",
        "output_directory_name",
        "database",
        "recovery_extension",
        "recovery_provenance",
        "browser",
        "preflight",
        "expected_store_names",
        "observed_schema",
        "schema_issues",
        "stores",
        "error",
        "excluded_operational_sources",
        "historical_versions_may_have_been_overwritten",
        "checksums",
    }
    if set(manifest) != expected_manifest_fields:
        raise ExtensionRecoveryError("browser export manifest fields are not closed")
    required_manifest = {
        "manifest_schema": EXPORT_MANIFEST_SCHEMA,
        "status": "complete",
        "browser_export_complete": True,
        "extraction_complete": False,
        "extraction_complete_status": "pending_host_hash_and_replay_validation",
        "source_kind": SOURCE_KIND,
    }
    if any(manifest.get(key) != value for key, value in required_manifest.items()):
        raise ExtensionRecoveryError("browser export manifest is not in host-validation state")
    if (
        not isinstance(manifest.get("started_at"), str)
        or not manifest["started_at"]
        or not isinstance(manifest.get("completed_at"), str)
        or not manifest["completed_at"]
    ):
        raise ExtensionRecoveryError("browser export timestamps are invalid")
    if manifest.get("historical_versions_may_have_been_overwritten") is not True:
        raise ExtensionRecoveryError(
            "browser export historical-version boundary is not explicit"
        )
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ExtensionRecoveryError("browser export manifest has no snapshot ID")
    if manifest.get("output_directory_name") != root.name:
        raise ExtensionRecoveryError("browser export directory name does not match manifest")
    database = manifest.get("database")
    if (
        not isinstance(database, dict)
        or set(database) != {"name", "expected_version", "observed_version"}
        or database.get("name") != DATABASE_NAME
        or not (
            database.get("expected_version")
            == database.get("observed_version")
            == DATABASE_VERSION
        )
    ):
        raise ExtensionRecoveryError("browser export database contract mismatch")
    extension = manifest.get("recovery_extension")
    if not isinstance(extension, dict) or set(extension) != {
        "expected_id",
        "runtime_id",
        "version",
        "manifest_version",
    }:
        raise ExtensionRecoveryError("browser export has no recovery extension identity")
    extension_id = extension.get("expected_id")
    if (
        not isinstance(extension_id, str)
        or not _EXTENSION_ID.fullmatch(extension_id)
        or extension.get("runtime_id") != extension_id
        or not isinstance(extension.get("version"), str)
        or not extension["version"]
        or extension.get("manifest_version") != 3
    ):
        raise ExtensionRecoveryError("browser export extension identity mismatch")
    if extension_id != workspace.target_extension_id:
        raise ExtensionRecoveryError("browser export does not use the bound archive extension")
    provenance = manifest.get("recovery_provenance")
    expected_provenance = {
        "replay_id": workspace.replay_id,
        "challenge": workspace.replay_challenge,
        "working_copy_evidence_tree_sha256": workspace.working_copy_evidence_tree_sha256,
        "source_payload_sha256": workspace.source_payload_sha256,
        "production_bundle_sha256": workspace.production_bundle_sha256,
        "preparation_sha256": workspace.preparation_sha256,
        "recovery_state_sha256": workspace.recovery_state_sha256,
        "browser_flavor": workspace.browser_flavor,
        "browser_version": workspace.browser_version,
        "browser_binary_sha256": workspace.browser_binary_sha256,
    }
    if not isinstance(provenance, dict) or provenance != expected_provenance:
        raise ExtensionRecoveryError("browser export replay provenance does not match")
    preflight = manifest.get("preflight")
    if (
        not isinstance(preflight, dict)
        or set(preflight) != {"database_names_observed", "passed"}
        or preflight.get("passed") is not True
    ):
        raise ExtensionRecoveryError("browser export preflight did not pass")
    names_observed = preflight.get("database_names_observed")
    if (
        not isinstance(names_observed, list)
        or not all(name is None or isinstance(name, str) for name in names_observed)
        or DATABASE_NAME not in names_observed
    ):
        raise ExtensionRecoveryError("browser export preflight did not observe the target database")
    expected_store_names = manifest.get("expected_store_names")
    if expected_store_names != list(EXPECTED_STORE_NAMES):
        raise ExtensionRecoveryError("browser export expected-store contract mismatch")
    issues = manifest.get("schema_issues")
    if issues != []:
        raise ExtensionRecoveryError("browser export schema issues are not empty")
    excluded_sources = manifest.get("excluded_operational_sources")
    if excluded_sources != ["ExtensionStorage"]:
        raise ExtensionRecoveryError("browser export does not declare ExtensionStorage exclusion")
    if manifest.get("error") is not None:
        raise ExtensionRecoveryError("complete browser export manifest contains an error")
    checksums = manifest.get("checksums")
    if checksums != {
        "algorithm": "sha256",
        "status": "deferred_to_host_after_chrome_exit",
    }:
        raise ExtensionRecoveryError("browser export checksum state is invalid")
    browser = manifest.get("browser")
    if (
        not isinstance(browser, dict)
        or set(browser) != {"user_agent"}
        or not isinstance(browser.get("user_agent"), str)
        or not browser["user_agent"]
    ):
        raise ExtensionRecoveryError("browser export browser metadata is invalid")

    stores_raw = manifest.get("stores")
    if not isinstance(stores_raw, list) or not all(isinstance(store, dict) for store in stores_raw):
        raise ExtensionRecoveryError("browser export stores must be objects")
    store_names = [store.get("name") for store in stores_raw]
    if not all(isinstance(name, str) and name for name in store_names):
        raise ExtensionRecoveryError("browser export contains an invalid store name")
    if len(set(store_names)) != len(store_names):
        raise ExtensionRecoveryError("browser export contains duplicate store entries")
    if set(store_names) != set(EXPECTED_STORE_NAMES):
        raise ExtensionRecoveryError("browser export store set is not closed")
    observed_schema = manifest.get("observed_schema")
    _validate_observed_database_schema(observed_schema, stores_raw)

    store_fingerprints: list[StoreFingerprint] = []
    row_fingerprints: list[RowFingerprint] = []
    referenced_binaries: dict[str, tuple[int, str]] = {}
    file_digests: list[FileDigest] = []
    for store in stores_raw:
        store_fingerprint, rows, references, ndjson_digest = _stream_ndjson_store(
            root, manifest, store
        )
        overlap = set(referenced_binaries) & set(references)
        if overlap:
            raise ExtensionRecoveryError(
                f"binary reference path is shared by stores: {sorted(overlap)[0]}"
            )
        referenced_binaries.update(references)
        store_fingerprints.append(store_fingerprint)
        row_fingerprints.extend(rows)
        file_digests.append(ndjson_digest)

    binary_by_store: dict[str, list[FileDigest]] = {name: [] for name in store_names}
    for relative, (declared_size, store_name) in sorted(
        referenced_binaries.items(), key=lambda item: os.fsencode(item[0])
    ):
        binary_path, normalized = _manifest_file(root, relative, "binary reference", "binary")
        actual_size, digest = _hash_regular_file(binary_path)
        if actual_size != declared_size:
            raise ExtensionRecoveryError(f"binary byte count mismatch: {normalized}")
        file_digest = FileDigest(normalized, actual_size, digest)
        binary_by_store[store_name].append(file_digest)
        file_digests.append(file_digest)

    updated_stores: list[StoreFingerprint] = []
    for store in store_fingerprints:
        binary_digest = hashlib.sha256()
        binaries = binary_by_store[store.name]
        for item in binaries:
            binary_digest.update(f"{item.path}\0{item.size}\0{item.sha256}\0".encode("utf-8"))
        updated_stores.append(
            StoreFingerprint(
                name=store.name,
                row_count=store.row_count,
                row_sha256=store.row_sha256,
                ndjson_sha256=store.ndjson_sha256,
                ndjson_bytes=store.ndjson_bytes,
                binary_part_count=len(binaries),
                binary_bytes=sum(item.size for item in binaries),
                binary_sha256=binary_digest.hexdigest(),
            )
        )
    declared_store_by_name = {store["name"]: store for store in stores_raw}
    for store in updated_stores:
        declared = declared_store_by_name[store.name]
        if store.binary_part_count != declared["external_binary_part_count"] or store.binary_bytes != declared[
            "external_binary_bytes"
        ]:
            raise ExtensionRecoveryError(f"actual binary files did not close for store {store.name}")

    manifest_digest = manifest_file_digest.sha256
    file_digests.append(manifest_file_digest)
    final_entries = _closed_export_entries(root)
    if final_entries != initial_entries:
        raise ExtensionRecoveryError("browser export tree changed during host validation")
    actual_files, actual_directories = _tree_inventory_from_entries(final_entries)
    expected_files = tuple(sorted((item.path for item in file_digests), key=os.fsencode))
    if actual_files != expected_files:
        unexpected = sorted(set(actual_files) - set(expected_files), key=os.fsencode)
        missing_files = sorted(set(expected_files) - set(actual_files), key=os.fsencode)
        detail = unexpected[0] if unexpected else missing_files[0]
        raise ExtensionRecoveryError(f"export file set did not close: {detail}")
    expected_directories = {"stores"}
    for item in file_digests:
        relative = PurePosixPath(item.path)
        for depth in range(1, len(relative.parts)):
            expected_directories.add(PurePosixPath(*relative.parts[:depth]).as_posix())
    expected_directory_tuple = tuple(sorted(expected_directories, key=os.fsencode))
    if actual_directories != expected_directory_tuple:
        unexpected = sorted(
            set(actual_directories) - set(expected_directory_tuple), key=os.fsencode
        )
        missing_directories = sorted(
            set(expected_directory_tuple) - set(actual_directories), key=os.fsencode
        )
        detail = unexpected[0] if unexpected else missing_directories[0]
        raise ExtensionRecoveryError(f"export directory set did not close: {detail}")

    updated_stores.sort(key=lambda item: os.fsencode(item.name))
    row_fingerprints.sort(
        key=lambda item: (os.fsencode(item.store_name), item.cursor_ordinal)
    )
    row_digest = hashlib.sha256()
    store_digest = hashlib.sha256()
    binary_digest = hashlib.sha256()
    for store in updated_stores:
        row_digest.update(f"{store.name}\0{store.row_count}\0{store.row_sha256}\0".encode("utf-8"))
        store_digest.update(
            (
                f"{store.name}\0{store.row_count}\0{store.row_sha256}\0"
                f"{store.ndjson_sha256}\0{store.ndjson_bytes}\0"
                f"{store.binary_part_count}\0{store.binary_bytes}\0{store.binary_sha256}\0"
            ).encode("utf-8")
        )
        binary_digest.update(
            f"{store.name}\0{store.binary_part_count}\0{store.binary_bytes}\0{store.binary_sha256}\0".encode(
                "utf-8"
            )
        )
    file_digests.sort(key=lambda item: os.fsencode(item.path))
    return ValidatedExport(
        root=root,
        replay_id=workspace.replay_id,
        replay_challenge=workspace.replay_challenge,
        working_copy_evidence_tree_sha256=workspace.working_copy_evidence_tree_sha256,
        source_payload_sha256=workspace.source_payload_sha256,
        production_bundle_sha256=workspace.production_bundle_sha256,
        preparation_sha256=workspace.preparation_sha256,
        recovery_state_sha256=workspace.recovery_state_sha256,
        browser_flavor=workspace.browser_flavor,
        browser_version=workspace.browser_version,
        browser_binary_sha256=workspace.browser_binary_sha256,
        snapshot_id=snapshot_id,
        extension_id=extension_id,
        manifest_sha256=manifest_digest,
        file_count=len(file_digests),
        byte_count=sum(item.size for item in file_digests),
        row_count=sum(store.row_count for store in updated_stores),
        row_sha256=row_digest.hexdigest(),
        store_sha256=store_digest.hexdigest(),
        binary_sha256=binary_digest.hexdigest(),
        rows=tuple(row_fingerprints),
        stores=tuple(updated_stores),
        files=tuple(file_digests),
    )


def compare_replay_exports(first: ValidatedExport, second: ValidatedExport) -> ReplayComparison:
    """Require deterministic row, store-file, and binary fingerprints across replays."""

    differences: list[str] = []
    if first.root == second.root:
        differences.append("replay export directories are not independent")
    if {first.replay_id, second.replay_id} != set(_REPLAY_NAMES):
        differences.append("replay identity pair is not independent")
    if first.replay_challenge == second.replay_challenge:
        differences.append("replay identity challenge is not independent")
    if first.preparation_sha256 == second.preparation_sha256:
        differences.append("replay identity preparation is not independent")
    if first.recovery_state_sha256 == second.recovery_state_sha256:
        differences.append("replay identity state is not independent")
    if (
        first.working_copy_evidence_tree_sha256
        != second.working_copy_evidence_tree_sha256
    ):
        differences.append("working-copy evidence tree differs")
    if first.source_payload_sha256 != second.source_payload_sha256:
        differences.append("source payload differs")
    if first.production_bundle_sha256 != second.production_bundle_sha256:
        differences.append("production extension bundle differs")
    if (
        first.browser_flavor != second.browser_flavor
        or first.browser_version != second.browser_version
        or first.browser_binary_sha256 != second.browser_binary_sha256
    ):
        differences.append("browser identity differs")
    if first.snapshot_id != second.snapshot_id:
        differences.append("snapshot ID differs")
    if first.extension_id != second.extension_id:
        differences.append("extension ID differs")
    first_rows = {
        (row.store_name, row.cursor_ordinal): row for row in first.rows
    }
    second_rows = {
        (row.store_name, row.cursor_ordinal): row for row in second.rows
    }
    if set(first_rows) != set(second_rows):
        differences.append("row locator set differs")
    for locator in sorted(
        set(first_rows) & set(second_rows), key=lambda item: (os.fsencode(item[0]), item[1])
    ):
        if first_rows[locator] != second_rows[locator]:
            differences.append(
                f"row fingerprint differs: {locator[0]} ordinal {locator[1]}"
            )
    first_stores = {store.name: store for store in first.stores}
    second_stores = {store.name: store for store in second.stores}
    if set(first_stores) != set(second_stores):
        differences.append("store set differs")
    for name in sorted(set(first_stores) & set(second_stores), key=os.fsencode):
        left = first_stores[name]
        right = second_stores[name]
        if left.row_count != right.row_count or left.row_sha256 != right.row_sha256:
            differences.append(f"row fingerprint differs for store: {name}")
        if left.ndjson_bytes != right.ndjson_bytes or left.ndjson_sha256 != right.ndjson_sha256:
            differences.append(f"store file fingerprint differs for store: {name}")
        if (
            left.binary_part_count != right.binary_part_count
            or left.binary_bytes != right.binary_bytes
            or left.binary_sha256 != right.binary_sha256
        ):
            differences.append(f"binary fingerprint differs for store: {name}")
    first_binary_files = {
        item.path: item for item in first.files if item.path.startswith("binary/")
    }
    second_binary_files = {
        item.path: item for item in second.files if item.path.startswith("binary/")
    }
    if set(first_binary_files) != set(second_binary_files):
        differences.append("binary file locator set differs")
    for path in sorted(set(first_binary_files) & set(second_binary_files), key=os.fsencode):
        if first_binary_files[path] != second_binary_files[path]:
            differences.append(f"binary file fingerprint differs: {path}")
    deterministic = not differences
    return ReplayComparison(
        deterministic=deterministic,
        extraction_complete=deterministic,
        snapshot_id=first.snapshot_id if first.snapshot_id == second.snapshot_id else None,
        first_store_sha256=first.store_sha256,
        second_store_sha256=second.store_sha256,
        first_binary_sha256=first.binary_sha256,
        second_binary_sha256=second.binary_sha256,
        differences=tuple(differences),
    )


def export_validation_report(export: ValidatedExport) -> dict[str, Any]:
    """Return metadata-only validation output; no decoded values or primary keys."""

    return {
        "replay_id": export.replay_id,
        "replay_challenge": export.replay_challenge,
        "working_copy_evidence_tree_sha256": export.working_copy_evidence_tree_sha256,
        "source_payload_sha256": export.source_payload_sha256,
        "production_bundle_sha256": export.production_bundle_sha256,
        "preparation_sha256": export.preparation_sha256,
        "recovery_state_sha256": export.recovery_state_sha256,
        "browser_flavor": export.browser_flavor,
        "browser_version": export.browser_version,
        "browser_binary_sha256": export.browser_binary_sha256,
        "snapshot_id": export.snapshot_id,
        "extension_id": export.extension_id,
        "manifest_sha256": export.manifest_sha256,
        "file_count": export.file_count,
        "byte_count": export.byte_count,
        "row_count": export.row_count,
        "row_sha256": export.row_sha256,
        "store_sha256": export.store_sha256,
        "binary_sha256": export.binary_sha256,
        "rows": [asdict(row) for row in export.rows],
        "stores": [asdict(store) for store in export.stores],
        "files": [asdict(file) for file in export.files],
    }
