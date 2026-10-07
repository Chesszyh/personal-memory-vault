"""A small source-neutral event envelope for future provider adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


INTERCHANGE_SCHEMA = "pmv.interchange-event.v1"
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")
_KINDS = frozenset({"thread", "participant", "message", "attachment", "relation"})
_FIELDS = frozenset(
    {
        "schema",
        "event_id",
        "provider",
        "identity_scope",
        "snapshot_id",
        "kind",
        "thread_native_id",
        "native_id",
        "observed_at",
        "payload",
        "raw_payload",
    }
)


class InterchangeError(RuntimeError):
    """Raised when an adapter interchange stream is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class InterchangeValidationResult:
    path: Path
    events: int
    providers: dict[str, int]
    kinds: dict[str, int]
    sha256: str


def _required_text(event: dict[str, Any], key: str, line_number: int) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InterchangeError(f"line {line_number}: {key} must be non-empty text")
    return value


def validate_interchange_jsonl(path: Path) -> InterchangeValidationResult:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise InterchangeError("interchange input must be an existing regular file")
    resolved = path.resolve(strict=True)
    digest = hashlib.sha256()
    seen: set[str] = set()
    providers: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    events = 0
    with resolved.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            digest.update(raw_line)
            if len(raw_line) > 16 * 1024 * 1024:
                raise InterchangeError(f"line {line_number}: event exceeds 16 MiB")
            if not raw_line.strip():
                raise InterchangeError(f"line {line_number}: blank lines are not allowed")
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InterchangeError(f"line {line_number}: invalid UTF-8 JSON") from error
            if not isinstance(event, dict) or set(event) != _FIELDS:
                raise InterchangeError(f"line {line_number}: event fields are invalid")
            if event.get("schema") != INTERCHANGE_SCHEMA:
                raise InterchangeError(f"line {line_number}: unsupported event schema")
            event_id = _required_text(event, "event_id", line_number)
            if _EVENT_ID.fullmatch(event_id) is None:
                raise InterchangeError(f"line {line_number}: event_id is unsafe")
            if event_id in seen:
                raise InterchangeError(f"line {line_number}: duplicate event_id")
            seen.add(event_id)
            provider = _required_text(event, "provider", line_number)
            _required_text(event, "identity_scope", line_number)
            _required_text(event, "snapshot_id", line_number)
            kind = _required_text(event, "kind", line_number)
            if kind not in _KINDS:
                raise InterchangeError(f"line {line_number}: unsupported event kind")
            for optional_text in ("thread_native_id", "native_id", "observed_at"):
                value = event.get(optional_text)
                if value is not None and (not isinstance(value, str) or "\x00" in value):
                    raise InterchangeError(
                        f"line {line_number}: {optional_text} must be text or null"
                    )
            if not isinstance(event.get("payload"), dict):
                raise InterchangeError(f"line {line_number}: payload must be an object")
            if not isinstance(event.get("raw_payload"), dict):
                raise InterchangeError(f"line {line_number}: raw_payload must be an object")
            events += 1
            providers[provider] += 1
            kinds[kind] += 1
    return InterchangeValidationResult(
        path=resolved,
        events=events,
        providers=dict(sorted(providers.items())),
        kinds=dict(sorted(kinds.items())),
        sha256=digest.hexdigest(),
    )
