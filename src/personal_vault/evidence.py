from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal


MANIFEST_SCHEMA_VERSION = 2
EntryKind = Literal["directory", "file", "symlink"]

_ENTRY_FIELDS = {"path", "kind", "size", "sha256", "link_target"}
_MANIFEST_FIELDS = {
    "schema_version",
    "source",
    "generated_at",
    "hash_algorithm",
    "tree_sha256",
    "manifest_payload_sha256",
    "totals",
    "entries",
}
_SOURCE_FIELDS = {"id", "kind", "captured_at", "captured_at_precision"}
_TOTAL_FIELDS = {"entries", "files", "directories", "symlinks", "file_bytes"}
_HEX_DIGITS = frozenset("0123456789abcdef")


class EvidenceError(RuntimeError):
    """Raised when an evidence tree cannot be captured or verified safely."""


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    path: str
    kind: EntryKind
    size: int | None = None
    sha256: str | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    expected_tree_sha256: str
    actual_tree_sha256: str
    expected_entries: int
    actual_entries: int
    differences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StatFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _fingerprint(metadata: os.stat_result) -> _StatFingerprint:
    return _StatFingerprint(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _require_safe_posix_io() -> None:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    required_dir_fd_functions = (os.open, os.stat, os.readlink, os.unlink, os.rename)
    if (
        os.name != "posix"
        or any(not hasattr(os, name) for name in required_flags)
        or any(function not in os.supports_dir_fd for function in required_dir_fd_functions)
        or os.listdir not in os.supports_fd
    ):
        raise EvidenceError(
            "safe evidence traversal requires POSIX dir_fd and O_NOFOLLOW support"
        )


def _open_flags(*, directory: bool = False, writable: bool = False) -> int:
    flags = os.O_WRONLY if writable else os.O_RDONLY
    flags |= os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _entry_sort_key(entry: EvidenceEntry) -> bytes:
    return os.fsencode(entry.path)


def _name_sort_key(name: str) -> bytes:
    return os.fsencode(name)


def _relative_path(parent: str, name: str) -> str:
    return name if not parent else f"{parent}/{name}"


def _scan_failure(action: str, relative_path: str, error: OSError) -> EvidenceError:
    location = relative_path or "."
    return EvidenceError(
        f"cannot {action} evidence entry {location!r}: "
        f"{type(error).__name__} (errno={error.errno})"
    )


def _stat_at(directory_fd: int, name: str, relative_path: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise _scan_failure("stat", relative_path, error) from error


def _hash_regular_file_at(
    directory_fd: int,
    name: str,
    relative_path: str,
    directory_entry_stat: os.stat_result,
) -> tuple[int, str, _StatFingerprint]:
    try:
        # O_NONBLOCK prevents a type-swap to a FIFO from hanging before fstat can
        # reject it. It has no effect on ordinary regular-file reads.
        descriptor = os.open(
            name, _open_flags() | os.O_NONBLOCK, dir_fd=directory_fd
        )
    except OSError as error:
        raise _scan_failure("open", relative_path, error) from error

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError(
                f"evidence entry changed type before hashing: {relative_path!r}"
            )
        if _fingerprint(before) != _fingerprint(directory_entry_stat):
            raise EvidenceError(
                f"source changed before hashing: {relative_path!r}"
            )

        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            digest.update(chunk)

        after = os.fstat(descriptor)
        if _fingerprint(before) != _fingerprint(after) or bytes_read != after.st_size:
            raise EvidenceError(f"source changed while hashing: {relative_path!r}")

        path_after = _stat_at(directory_fd, name, relative_path)
        if _fingerprint(after) != _fingerprint(path_after):
            raise EvidenceError(f"source path changed while hashing: {relative_path!r}")
        return after.st_size, digest.hexdigest(), _fingerprint(after)
    except OSError as error:
        raise _scan_failure("read", relative_path, error) from error
    finally:
        os.close(descriptor)


def _read_symlink_at(
    directory_fd: int,
    name: str,
    relative_path: str,
    before: os.stat_result,
) -> tuple[str, _StatFingerprint]:
    try:
        target = os.readlink(name, dir_fd=directory_fd)
    except OSError as error:
        raise _scan_failure("read symlink", relative_path, error) from error
    after = _stat_at(directory_fd, name, relative_path)
    if _fingerprint(before) != _fingerprint(after) or not stat.S_ISLNK(after.st_mode):
        raise EvidenceError(f"symlink changed while scanning: {relative_path!r}")
    return target, _fingerprint(after)


def _open_directory_at(
    directory_fd: int,
    name: str,
    relative_path: str,
    directory_entry_stat: os.stat_result,
) -> int:
    try:
        child_fd = os.open(name, _open_flags(directory=True), dir_fd=directory_fd)
    except OSError as error:
        raise _scan_failure("open directory", relative_path, error) from error
    try:
        child_stat = os.fstat(child_fd)
    except OSError as error:
        os.close(child_fd)
        raise _scan_failure("inspect directory", relative_path, error) from error
    if (
        not stat.S_ISDIR(child_stat.st_mode)
        or _fingerprint(child_stat) != _fingerprint(directory_entry_stat)
    ):
        os.close(child_fd)
        raise EvidenceError(f"directory changed before scanning: {relative_path!r}")
    return child_fd


def _list_directory(directory_fd: int, relative_path: str) -> list[str]:
    try:
        return sorted(os.listdir(directory_fd), key=_name_sort_key)
    except OSError as error:
        raise _scan_failure("list directory", relative_path, error) from error


def _scan_directory(
    directory_fd: int,
    relative_directory: str,
    entries: list[EvidenceEntry],
) -> None:
    directory_before = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_before.st_mode):
        raise EvidenceError(
            f"evidence directory changed type: {relative_directory or '.'!r}"
        )

    names_before = _list_directory(directory_fd, relative_directory)
    observed: dict[str, tuple[_StatFingerprint, str | None]] = {}
    for name in names_before:
        relative_path = _relative_path(relative_directory, name)
        metadata = _stat_at(directory_fd, name, relative_path)
        if stat.S_ISLNK(metadata.st_mode):
            target, fingerprint = _read_symlink_at(
                directory_fd, name, relative_path, metadata
            )
            target_bytes = os.fsencode(target)
            observed[name] = (fingerprint, target)
            entries.append(
                EvidenceEntry(
                    path=relative_path,
                    kind="symlink",
                    size=len(target_bytes),
                    sha256=hashlib.sha256(target_bytes).hexdigest(),
                    link_target=target,
                )
            )
        elif stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory_at(
                directory_fd, name, relative_path, metadata
            )
            try:
                entries.append(EvidenceEntry(path=relative_path, kind="directory"))
                _scan_directory(child_fd, relative_path, entries)
                child_after = os.fstat(child_fd)
            finally:
                os.close(child_fd)
            if _fingerprint(metadata) != _fingerprint(child_after):
                raise EvidenceError(
                    f"directory changed while scanning: {relative_path!r}"
                )
            observed[name] = (_fingerprint(child_after), None)
        elif stat.S_ISREG(metadata.st_mode):
            size, digest, fingerprint = _hash_regular_file_at(
                directory_fd, name, relative_path, metadata
            )
            observed[name] = (fingerprint, None)
            entries.append(
                EvidenceEntry(
                    path=relative_path,
                    kind="file",
                    size=size,
                    sha256=digest,
                )
            )
        else:
            raise EvidenceError(f"unsupported filesystem entry: {relative_path!r}")

    names_after = _list_directory(directory_fd, relative_directory)
    if names_after != names_before:
        raise EvidenceError(
            f"source directory members changed while scanning: "
            f"{relative_directory or '.'!r}"
        )

    for name in names_after:
        relative_path = _relative_path(relative_directory, name)
        metadata = _stat_at(directory_fd, name, relative_path)
        expected_fingerprint, expected_target = observed[name]
        if _fingerprint(metadata) != expected_fingerprint:
            raise EvidenceError(f"source changed while scanning: {relative_path!r}")
        if expected_target is not None:
            target, _ = _read_symlink_at(
                directory_fd, name, relative_path, metadata
            )
            if target != expected_target:
                raise EvidenceError(
                    f"symlink target changed while scanning: {relative_path!r}"
                )

    directory_after = os.fstat(directory_fd)
    if _fingerprint(directory_before) != _fingerprint(directory_after):
        raise EvidenceError(
            f"source directory changed while scanning: {relative_directory or '.'!r}"
        )


def scan_evidence(root: Path) -> tuple[EvidenceEntry, ...]:
    """Capture a quiescent POSIX directory without following symbolic links.

    The descriptor-based checks close normal race windows, but no userspace walker can
    establish a hostile-writer snapshot. Callers must still freeze or snapshot the source.
    """

    _require_safe_posix_io()
    root = root.expanduser().absolute()
    try:
        root_path_before = os.lstat(root)
    except OSError as error:
        raise _scan_failure("stat root", "", error) from error
    if not stat.S_ISDIR(root_path_before.st_mode):
        raise EvidenceError("evidence root must be a non-symlink directory")

    try:
        root_fd = os.open(root, _open_flags(directory=True))
    except OSError as error:
        raise _scan_failure("open root", "", error) from error
    try:
        root_fd_before = os.fstat(root_fd)
        if _fingerprint(root_path_before) != _fingerprint(root_fd_before):
            raise EvidenceError("evidence root changed before scanning")
        entries: list[EvidenceEntry] = []
        _scan_directory(root_fd, "", entries)
        root_fd_after = os.fstat(root_fd)
        try:
            root_path_after = os.lstat(root)
        except OSError as error:
            raise _scan_failure("restat root", "", error) from error
        if (
            _fingerprint(root_fd_before) != _fingerprint(root_fd_after)
            or _fingerprint(root_fd_after) != _fingerprint(root_path_after)
        ):
            raise EvidenceError("evidence root changed while scanning")
    finally:
        os.close(root_fd)

    return tuple(sorted(entries, key=_entry_sort_key))


def tree_sha256(entries: tuple[EvidenceEntry, ...]) -> str:
    """Hash entries in canonical path-byte order, independent of input order."""

    digest = hashlib.sha256()
    for entry in sorted(entries, key=_entry_sort_key):
        fields = (
            entry.kind,
            entry.path,
            "" if entry.size is None else str(entry.size),
            entry.sha256 or "",
            entry.link_target or "",
        )
        digest.update("\0".join(fields).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"manifest {field_name} must be a non-empty timestamp")
    parse_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as error:
        raise EvidenceError(f"manifest {field_name} is not valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"manifest {field_name} must include a timezone")
    return value


def _validate_capture_time(value: object, precision: object) -> tuple[str, str]:
    if precision == "day":
        if not isinstance(value, str) or len(value) != 10:
            raise EvidenceError("manifest source.captured_at must be an ISO date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise EvidenceError("manifest source.captured_at is not a valid ISO date") from error
        if parsed.isoformat() != value:
            raise EvidenceError("manifest source.captured_at is not canonical ISO date")
        return value, "day"
    if precision == "instant":
        return _validate_timestamp(value, "source.captured_at"), "instant"
    raise EvidenceError(
        "manifest source.captured_at_precision must be 'day' or 'instant'"
    )


def _validate_path(path: object) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise EvidenceError("manifest entry path must be a non-empty string without NUL")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
        raise EvidenceError(f"manifest entry path is not a safe relative path: {path!r}")
    try:
        os.fsencode(path)
    except UnicodeEncodeError as error:
        raise EvidenceError(
            f"manifest entry path cannot be represented by the filesystem: {path!r}"
        ) from error
    return path


def _validate_entry(raw_entry: object) -> EvidenceEntry:
    if not isinstance(raw_entry, dict) or set(raw_entry) != _ENTRY_FIELDS:
        raise EvidenceError("manifest entry fields are invalid")
    path = _validate_path(raw_entry["path"])
    kind = raw_entry["kind"]
    size = raw_entry["size"]
    digest = raw_entry["sha256"]
    link_target = raw_entry["link_target"]
    if not isinstance(kind, str) or kind not in {"directory", "file", "symlink"}:
        raise EvidenceError(f"manifest entry kind is invalid for {path!r}")

    if kind == "directory":
        if size is not None or digest is not None or link_target is not None:
            raise EvidenceError(f"directory entry fields are invalid for {path!r}")
    elif kind == "file":
        if not _is_nonnegative_int(size) or not _is_sha256(digest) or link_target is not None:
            raise EvidenceError(f"file entry fields are invalid for {path!r}")
    else:
        if (
            not _is_nonnegative_int(size)
            or not _is_sha256(digest)
            or not isinstance(link_target, str)
            or not link_target
            or "\0" in link_target
        ):
            raise EvidenceError(f"symlink entry fields are invalid for {path!r}")
        try:
            target_bytes = os.fsencode(link_target)
        except UnicodeEncodeError as error:
            raise EvidenceError(
                f"symlink target cannot be represented for {path!r}"
            ) from error
        if size != len(target_bytes) or digest != hashlib.sha256(target_bytes).hexdigest():
            raise EvidenceError(f"symlink target metadata is invalid for {path!r}")

    return EvidenceEntry(
        path=path,
        kind=kind,
        size=size,
        sha256=digest,
        link_target=link_target,
    )


def _expected_totals(entries: tuple[EvidenceEntry, ...]) -> dict[str, int]:
    return {
        "entries": len(entries),
        "files": sum(entry.kind == "file" for entry in entries),
        "directories": sum(entry.kind == "directory" for entry in entries),
        "symlinks": sum(entry.kind == "symlink" for entry in entries),
        "file_bytes": sum(
            entry.size or 0 for entry in entries if entry.kind == "file"
        ),
    }


def _validate_entries(raw_entries: object) -> tuple[EvidenceEntry, ...]:
    if not isinstance(raw_entries, list):
        raise EvidenceError("manifest entries must be a list")
    entries = tuple(_validate_entry(raw_entry) for raw_entry in raw_entries)
    if len({entry.path for entry in entries}) != len(entries):
        raise EvidenceError("manifest contains duplicate paths")
    kinds_by_path = {entry.path: entry.kind for entry in entries}
    for entry in entries:
        parent = PurePosixPath(entry.path).parent
        if parent != PurePosixPath(".") and kinds_by_path.get(parent.as_posix()) != "directory":
            raise EvidenceError(
                f"manifest entry parent directory is missing for {entry.path!r}"
            )
    return tuple(sorted(entries, key=_entry_sort_key))


def _canonical_payload(manifest: dict[str, Any], entries: tuple[EvidenceEntry, ...]) -> bytes:
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"generated_at", "manifest_payload_sha256", "entries"}
    }
    payload["entries"] = [asdict(entry) for entry in entries]
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def manifest_payload_sha256(
    manifest: dict[str, Any], entries: tuple[EvidenceEntry, ...] | None = None
) -> str:
    """Return an accidental-corruption digest, not an authenticity signature.

    Authenticity requires this digest to be signed or anchored outside the mutable
    manifest. ``generated_at`` is deliberately excluded from the canonical payload.
    """

    canonical_entries = _validate_entries(manifest.get("entries")) if entries is None else entries
    return hashlib.sha256(_canonical_payload(manifest, canonical_entries)).hexdigest()


def _validate_manifest(manifest: object) -> tuple[EvidenceEntry, ...]:
    if not isinstance(manifest, dict):
        raise EvidenceError("evidence manifest must be a JSON object")
    schema_version = manifest.get("schema_version")
    if schema_version == 1:
        raise EvidenceError(
            "evidence manifest schema 1 is legacy and does not bind provenance; "
            "regenerate it as schema 2"
        )
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise EvidenceError(f"unsupported evidence manifest schema: {schema_version!r}")
    if set(manifest) != _MANIFEST_FIELDS:
        raise EvidenceError("evidence manifest top-level fields are invalid")
    if manifest.get("hash_algorithm") != "sha256":
        raise EvidenceError("unsupported evidence hash algorithm")

    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise EvidenceError("manifest source fields are invalid")
    if not isinstance(source["id"], str) or not source["id"]:
        raise EvidenceError("manifest source id must be a non-empty string")
    if not isinstance(source["kind"], str) or not source["kind"]:
        raise EvidenceError("manifest source kind must be a non-empty string")
    _validate_capture_time(source["captured_at"], source["captured_at_precision"])
    _validate_timestamp(manifest.get("generated_at"), "generated_at")

    entries = _validate_entries(manifest.get("entries"))
    totals = manifest.get("totals")
    if (
        not isinstance(totals, dict)
        or set(totals) != _TOTAL_FIELDS
        or any(not _is_nonnegative_int(value) for value in totals.values())
        or totals != _expected_totals(entries)
    ):
        raise EvidenceError("manifest totals do not match its entries")

    expected_tree_hash = tree_sha256(entries)
    if not _is_sha256(manifest.get("tree_sha256")):
        raise EvidenceError("manifest tree_sha256 is invalid")
    if manifest["tree_sha256"] != expected_tree_hash:
        raise EvidenceError("manifest tree hash does not match its entries")

    declared_payload_hash = manifest.get("manifest_payload_sha256")
    if not _is_sha256(declared_payload_hash):
        raise EvidenceError("manifest payload digest is invalid")
    expected_payload_hash = manifest_payload_sha256(manifest, entries)
    if declared_payload_hash != expected_payload_hash:
        raise EvidenceError("manifest payload digest does not match its canonical payload")
    return entries


def build_manifest(
    root: Path,
    *,
    source_id: str,
    source_kind: str,
    captured_at: str,
    captured_at_precision: str | None = None,
) -> dict[str, Any]:
    if not isinstance(source_id, str) or not source_id:
        raise EvidenceError("source_id must be a non-empty string")
    if not isinstance(source_kind, str) or not source_kind:
        raise EvidenceError("source_kind must be a non-empty string")
    if captured_at_precision is None:
        captured_at_precision = (
            "day" if isinstance(captured_at, str) and len(captured_at) == 10 else "instant"
        )
    _validate_capture_time(captured_at, captured_at_precision)
    entries = scan_evidence(root)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": {
            "id": source_id,
            "kind": source_kind,
            "captured_at": captured_at,
            "captured_at_precision": captured_at_precision,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "hash_algorithm": "sha256",
        "tree_sha256": tree_sha256(entries),
        "manifest_payload_sha256": "",
        "totals": _expected_totals(entries),
        "entries": [asdict(entry) for entry in entries],
    }
    manifest["manifest_payload_sha256"] = manifest_payload_sha256(manifest, entries)
    _validate_manifest(manifest)
    return manifest


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _open_output_directory(path: Path) -> int:
    try:
        descriptor = os.open(path, _open_flags(directory=True))
    except OSError as error:
        raise EvidenceError(
            f"cannot open manifest output directory: {type(error).__name__} "
            f"(errno={error.errno})"
        ) from error
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise EvidenceError(
            f"cannot inspect manifest output directory: {type(error).__name__} "
            f"(errno={error.errno})"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise EvidenceError("manifest output parent is not a directory")
    return descriptor


def write_manifest(manifest: dict[str, Any], output: Path, *, evidence_root: Path) -> None:
    _require_safe_posix_io()
    _validate_manifest(manifest)
    evidence_root = evidence_root.expanduser().resolve(strict=True)
    requested_output = output.expanduser().absolute()
    if _is_relative_to(requested_output, evidence_root) or _is_relative_to(
        requested_output.resolve(strict=False), evidence_root
    ):
        raise EvidenceError("manifest output must be outside the immutable evidence root")
    requested_output.parent.mkdir(parents=True, exist_ok=True)
    output_parent = requested_output.parent.resolve(strict=True)
    output = output_parent / requested_output.name
    if _is_relative_to(output, evidence_root) or _is_relative_to(
        output.resolve(strict=False), evidence_root
    ):
        raise EvidenceError("manifest output must be outside the immutable evidence root")

    parent_fd = _open_output_directory(output_parent)
    temporary_name = f".{output.name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                temporary_name,
                _open_flags(writable=True) | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise EvidenceError(
                f"cannot create temporary manifest: {type(error).__name__} "
                f"(errno={error.errno})"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(
                manifest,
                handle,
                ensure_ascii=True,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(
                temporary_name,
                output.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except OSError as error:
            raise EvidenceError(
                f"cannot publish evidence manifest: {type(error).__name__} "
                f"(errno={error.errno})"
            ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"evidence manifest contains duplicate key: {key!r}")
        result[key] = value
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except EvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(
            f"cannot read evidence manifest: {type(error).__name__}"
        ) from error
    _validate_manifest(manifest)
    return manifest


def _entries_from_manifest(manifest: dict[str, Any]) -> tuple[EvidenceEntry, ...]:
    return _validate_manifest(manifest)


def verify_manifest(root: Path, manifest: dict[str, Any]) -> VerificationResult:
    expected_entries = _entries_from_manifest(manifest)
    expected_hash = tree_sha256(expected_entries)
    actual_entries = scan_evidence(root)
    actual_hash = tree_sha256(actual_entries)
    expected_by_path = {entry.path: entry for entry in expected_entries}
    actual_by_path = {entry.path: entry for entry in actual_entries}
    differences: list[str] = []
    for path in sorted(expected_by_path.keys() | actual_by_path.keys(), key=os.fsencode):
        expected = expected_by_path.get(path)
        actual = actual_by_path.get(path)
        if expected is None:
            differences.append(f"unexpected: {path}")
        elif actual is None:
            differences.append(f"missing: {path}")
        elif actual != expected:
            differences.append(f"changed: {path}")
    return VerificationResult(
        ok=not differences and actual_hash == expected_hash,
        expected_tree_sha256=expected_hash,
        actual_tree_sha256=actual_hash,
        expected_entries=len(expected_entries),
        actual_entries=len(actual_entries),
        differences=tuple(differences),
    )
