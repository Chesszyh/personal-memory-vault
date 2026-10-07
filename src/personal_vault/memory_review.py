"""Loopback-only interactive review workbench for personal memory candidates."""

from __future__ import annotations

import difflib
import json
import math
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

from .memory_store import (
    MemoryStoreError,
    _connect_memory,
    decide_memory_candidates,
    edit_memory_candidate,
    export_confirmed_profile,
)
from .migration import MigrationError, build_chatgpt_migration_pack
from .reader import ReaderError, connect_reader_database


STATIC_ROOT = Path(__file__).with_name("memory_review_static")
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_CANDIDATE_ID = re.compile(r"memc_[0-9a-f]{32}\Z")
_KINDS = {"identity", "preference", "plan", "boundary"}
_STATUSES = {"pending", "confirmed", "rejected"}
_NEGATION = re.compile(r"(?:不要|不喜欢|不需要|不希望|别再|讨厌|\bnot\b|\bnever\b|don't)", re.I)
_NEGATION_STRIP = re.compile(r"(?:不要|不喜欢|不需要|不希望|别再|讨厌|\bnot\b|\bnever\b|don't)", re.I)


class MemoryReviewError(RuntimeError):
    """Safe error returned by the local review workbench."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "memory_review_error",
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _effective_statement(row: Mapping[str, Any]) -> str:
    return str(row["reviewed_statement"] or row["statement"])


def _candidate_payload(row: Mapping[str, Any], title: str | None = None) -> dict[str, Any]:
    return {
        "candidate_id": str(row["candidate_id"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "statement": _effective_statement(row),
        "original_statement": str(row["statement"]),
        "edited": row["reviewed_statement"] is not None,
        "confidence": float(row["confidence"]),
        "source_snapshot_id": int(row["source_snapshot_id"]),
        "source_snapshot_key": str(row["source_snapshot_key"]),
        "source_conversation_identity_key": str(row["source_conversation_identity_key"]),
        "source_message_identity_key": str(row["source_message_identity_key"]),
        "message_create_time": row["message_create_time"],
        "conversation_title": title,
        "reviewed_at": row["reviewed_at"],
    }


class MemoryReviewRepository:
    """Small write surface over memory.sqlite plus read-only canonical context."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root).expanduser().resolve(strict=True)
        self.memory_path = self.vault_root / "memory" / "memory.sqlite"
        self.canonical_path = self.vault_root / "canonical" / "archive.sqlite"
        database = _connect_memory(self.memory_path)
        database.close()
        canonical = connect_reader_database(self.canonical_path)
        canonical.close()

    def _memory(self) -> sqlite3.Connection:
        return _connect_memory(self.memory_path)

    def _canonical(self) -> sqlite3.Connection:
        return connect_reader_database(self.canonical_path)

    def list_batches(self) -> dict[str, Any]:
        memory = self._memory()
        try:
            rows = memory.execute(
                """SELECT source_snapshot_id, source_snapshot_key, status, kind,
                          count(*) AS count
                     FROM candidates
                    GROUP BY source_snapshot_id, source_snapshot_key, status, kind
                    ORDER BY source_snapshot_id, source_snapshot_key"""
            ).fetchall()
        finally:
            memory.close()
        batches: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            key = (int(row["source_snapshot_id"]), str(row["source_snapshot_key"]))
            batch = batches.setdefault(
                key,
                {
                    "snapshot_id": key[0],
                    "snapshot_key": key[1],
                    "total": 0,
                    "by_status": {status: 0 for status in sorted(_STATUSES)},
                    "by_kind": {kind: 0 for kind in sorted(_KINDS)},
                },
            )
            count = int(row["count"])
            batch["total"] += count
            batch["by_status"][str(row["status"])] += count
            batch["by_kind"][str(row["kind"])] += count
        return {"items": list(batches.values())}

    def list_candidates(
        self,
        *,
        snapshot_keys: Sequence[str],
        statuses: Sequence[str],
        kinds: Sequence[str],
        query: str | None,
        sort: str,
        min_confidence: float | None,
        after_time: float | None,
        before_time: float | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if any(status not in _STATUSES for status in statuses):
            raise MemoryReviewError("unknown candidate status", code="invalid_status")
        if any(kind not in _KINDS for kind in kinds):
            raise MemoryReviewError("unknown candidate kind", code="invalid_kind")
        if sort not in {"newest", "oldest", "kind", "status"}:
            raise MemoryReviewError("unknown candidate sort", code="invalid_sort")
        if min_confidence is not None and not 0 <= min_confidence <= 1:
            raise MemoryReviewError("minimum confidence is outside 0..1", code="invalid_confidence")
        if after_time is not None and before_time is not None and after_time >= before_time:
            raise MemoryReviewError("candidate date range is invalid", code="invalid_date_range")
        if not 1 <= limit <= 1000 or offset < 0:
            raise MemoryReviewError("candidate pagination is invalid", code="invalid_pagination")

        scope_clauses: list[str] = []
        scope_params: list[Any] = []
        if snapshot_keys:
            scope_clauses.append(
                "source_snapshot_key IN (%s)" % ",".join("?" for _ in snapshot_keys)
            )
            scope_params.extend(snapshot_keys)
        clauses = list(scope_clauses)
        params = list(scope_params)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        if query:
            normalized = " ".join(query.split()).casefold()
            clauses.append(
                "(normalized_statement LIKE ? ESCAPE '\\' OR candidate_id LIKE ? ESCAPE '\\')"
            )
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend((f"%{escaped}%", f"%{escaped}%"))
        if min_confidence is not None:
            clauses.append("confidence>=?")
            params.append(min_confidence)
        if after_time is not None:
            clauses.append("message_create_time>=?")
            params.append(after_time)
        if before_time is not None:
            clauses.append("message_create_time<?")
            params.append(before_time)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        scope_where = " WHERE " + " AND ".join(scope_clauses) if scope_clauses else ""
        order = {
            "newest": "message_create_time DESC, candidate_id",
            "oldest": "message_create_time, candidate_id",
            "kind": "kind, candidate_id",
            "status": "status, kind, candidate_id",
        }[sort]

        memory = self._memory()
        try:
            total = int(memory.execute(f"SELECT count(*) FROM candidates{where}", params).fetchone()[0])
            rows = memory.execute(
                f"SELECT * FROM candidates{where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            status_counts = {
                str(row["status"]): int(row["count"])
                for row in memory.execute(
                    f"SELECT status, count(*) AS count FROM candidates{scope_where} GROUP BY status",
                    scope_params,
                )
            }
            kind_counts = {
                str(row["kind"]): int(row["count"])
                for row in memory.execute(
                    f"SELECT kind, count(*) AS count FROM candidates{scope_where} GROUP BY kind",
                    scope_params,
                )
            }
            if snapshot_keys:
                decisions = int(
                    memory.execute(
                        """SELECT count(*) FROM decisions d
                             JOIN candidates c ON c.candidate_id=d.candidate_id
                            WHERE c.source_snapshot_key IN (%s)"""
                        % ",".join("?" for _ in snapshot_keys),
                        snapshot_keys,
                    ).fetchone()[0]
                )
            else:
                decisions = int(memory.execute("SELECT count(*) FROM decisions").fetchone()[0])
        finally:
            memory.close()

        titles = self._conversation_titles(rows)
        return {
            "items": [
                _candidate_payload(
                    row,
                    titles.get(
                        (int(row["source_snapshot_id"]), str(row["source_conversation_identity_key"]))
                    ),
                )
                for row in rows
            ],
            "pagination": {"total": total, "limit": limit, "offset": offset},
            "counts": {
                "total": sum(status_counts.values()),
                "by_status": {status: status_counts.get(status, 0) for status in sorted(_STATUSES)},
                "by_kind": {kind: kind_counts.get(kind, 0) for kind in sorted(_KINDS)},
                "decision_events": decisions,
            },
        }

    def _conversation_titles(self, rows: Sequence[Mapping[str, Any]]) -> dict[tuple[int, str], str | None]:
        requested = {
            (int(row["source_snapshot_id"]), str(row["source_conversation_identity_key"]))
            for row in rows
        }
        if not requested:
            return {}
        canonical = self._canonical()
        try:
            titles: dict[tuple[int, str], str | None] = {}
            for snapshot_id, identity_key in requested:
                row = canonical.execute(
                    """SELECT cv.title FROM conversation_identities ci
                         JOIN conversation_observations co ON co.conversation_identity_id=ci.id
                         JOIN conversation_versions cv ON cv.id=co.conversation_version_id
                        WHERE co.snapshot_id=? AND ci.identity_key=?""",
                    (snapshot_id, identity_key),
                ).fetchone()
                titles[(snapshot_id, identity_key)] = None if row is None else row["title"]
            return titles
        finally:
            canonical.close()

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        if not _CANDIDATE_ID.fullmatch(candidate_id):
            raise MemoryReviewError("candidate ID is invalid", code="invalid_candidate")
        memory = self._memory()
        try:
            row = memory.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise MemoryReviewError(
                    "memory candidate was not found",
                    code="candidate_not_found",
                    status=HTTPStatus.NOT_FOUND,
                )
            decisions = [dict(item) for item in memory.execute(
                """SELECT prior_status, status, note, decided_at FROM decisions
                     WHERE candidate_id=? ORDER BY id DESC LIMIT 12""",
                (candidate_id,),
            )]
            related_rows = memory.execute(
                "SELECT * FROM candidates WHERE kind=? AND candidate_id<>?",
                (row["kind"], candidate_id),
            ).fetchall()
        finally:
            memory.close()
        context = self._source_context(row)
        related = self._related_candidates(row, related_rows)
        return {
            "candidate": _candidate_payload(row, context.get("conversation_title")),
            "context": context,
            "related": related,
            "decisions": decisions,
            "guidance": self._guidance(str(row["kind"])),
        }

    def _source_context(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        canonical = self._canonical()
        try:
            source = canonical.execute(
                """SELECT cb.depth, ci.id AS conversation_identity_id, cv.title
                     FROM message_identities mi
                     JOIN message_observations mo ON mo.message_identity_id=mi.id
                     JOIN node_observations no ON no.id=mo.node_observation_id
                     JOIN current_branch_nodes cb
                       ON cb.snapshot_id=mo.snapshot_id AND cb.node_identity_id=no.node_identity_id
                     JOIN conversation_identities ci ON ci.id=cb.conversation_identity_id
                     JOIN conversation_observations co
                       ON co.snapshot_id=cb.snapshot_id AND co.conversation_identity_id=ci.id
                     JOIN conversation_versions cv ON cv.id=co.conversation_version_id
                    WHERE mo.snapshot_id=? AND mi.identity_key=?""",
                (candidate["source_snapshot_id"], candidate["source_message_identity_key"]),
            ).fetchone()
            if source is None:
                return {"conversation_title": None, "messages": [], "source_available": False}
            depth = int(source["depth"])
            messages = canonical.execute(
                """SELECT cb.depth, mi.identity_key, mv.author_role, mv.create_time, sd.content
                     FROM current_branch_nodes cb
                     JOIN node_observations no
                       ON no.snapshot_id=cb.snapshot_id AND no.node_identity_id=cb.node_identity_id
                     JOIN message_observations mo ON mo.node_observation_id=no.id
                     JOIN message_identities mi ON mi.id=mo.message_identity_id
                     JOIN message_versions mv ON mv.id=mo.message_version_id
                     JOIN search_documents sd ON sd.message_version_id=mv.id
                    WHERE cb.snapshot_id=? AND cb.conversation_identity_id=?
                      AND cb.depth BETWEEN ? AND ?
                    ORDER BY cb.depth""",
                (
                    candidate["source_snapshot_id"],
                    source["conversation_identity_id"],
                    max(0, depth - 2),
                    depth + 2,
                ),
            ).fetchall()
            return {
                "conversation_title": source["title"],
                "source_available": True,
                "messages": [
                    {
                        "depth": int(message["depth"]),
                        "role": message["author_role"],
                        "content": message["content"],
                        "create_time": message["create_time"],
                        "is_source": message["identity_key"] == candidate["source_message_identity_key"],
                    }
                    for message in messages
                ],
            }
        finally:
            canonical.close()

    def _related_candidates(
        self, candidate: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        statement = _effective_statement(candidate)
        normalized = " ".join(statement.casefold().split())
        stripped = _NEGATION_STRIP.sub("", normalized)
        negative = bool(_NEGATION.search(normalized))
        related: list[tuple[float, str, Mapping[str, Any]]] = []
        for row in rows:
            other = " ".join(_effective_statement(row).casefold().split())
            ratio = difflib.SequenceMatcher(None, normalized, other).ratio()
            other_stripped = _NEGATION_STRIP.sub("", other)
            core_ratio = difflib.SequenceMatcher(None, stripped, other_stripped).ratio()
            same_conversation = (
                row["source_conversation_identity_key"]
                == candidate["source_conversation_identity_key"]
            )
            if normalized == other:
                clue = "duplicate"
                score = 1.0
            elif negative != bool(_NEGATION.search(other)) and core_ratio >= 0.52:
                clue = "possible_conflict"
                score = core_ratio + 0.2
            elif ratio >= 0.58 or (same_conversation and ratio >= 0.42):
                clue = "related"
                score = ratio + (0.08 if same_conversation else 0)
            else:
                continue
            related.append((score, clue, row))
        related.sort(key=lambda item: (-item[0], str(item[2]["candidate_id"])))
        return [
            {
                **_candidate_payload(row),
                "clue": clue,
                "similarity": round(min(score, 1.0), 3),
            }
            for score, clue, row in related[:8]
        ]

    @staticmethod
    def _guidance(kind: str) -> dict[str, list[str]]:
        accept = [
            "确实是你本人直接表达的内容，而不是引用、示例或转述。",
            "改写后能脱离原会话独立理解，没有把几个不同事实揉成一条。",
            "未来回答仍可能用得上；若是计划，至少目前仍在进行。",
        ]
        reject = [
            "只是对当时那一次回答的临时要求、提示词片段或正则误命中。",
            "内容已经不成立、不是关于你，或需要猜测上下文才能解释。",
            "整条消息太杂且无法编辑成一条准确、可追溯的记忆。",
        ]
        if kind == "plan":
            accept.append("计划或当前状态仍未结束；已完成/放弃的计划应拒绝或继续待定。")
        if kind == "boundary":
            accept.append("这是一条可跨会话复用的协作边界，而非单次任务约束。")
        return {"accept": accept, "reject": reject}

    def decide(
        self, candidate_ids: Sequence[str], status: str, note: str | None
    ) -> dict[str, Any]:
        try:
            result = decide_memory_candidates(
                vault_root=self.vault_root,
                candidate_ids=tuple(candidate_ids),
                status=status,
                note=note,
            )
        except MemoryStoreError as error:
            raise MemoryReviewError(str(error), code="decision_failed") from error
        return {
            "candidate_ids": list(result.candidate_ids),
            "status": result.status,
            "updated_count": result.updated_count,
            "decided_at": result.decided_at,
        }

    def edit(self, candidate_id: str, statement: str) -> dict[str, Any]:
        try:
            result = edit_memory_candidate(
                vault_root=self.vault_root,
                candidate_id=candidate_id,
                statement=statement,
            )
        except MemoryStoreError as error:
            raise MemoryReviewError(str(error), code="edit_failed") from error
        return {
            "candidate_id": result.candidate_id,
            "statement": result.statement,
            "edited": result.edited,
            "reviewed_at": result.reviewed_at,
        }

    def rebuild(self) -> dict[str, Any]:
        try:
            profile = export_confirmed_profile(vault_root=self.vault_root)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            base = self.vault_root / "migration" / "chatgpt" / f"reviewed-{stamp}"
            output = base
            suffix = 2
            while output.exists():
                output = base.with_name(f"{base.name}-{suffix}")
                suffix += 1
            migration = build_chatgpt_migration_pack(
                vault_root=self.vault_root, output_directory=output
            )
        except (MemoryStoreError, MigrationError) as error:
            raise MemoryReviewError(str(error), code="rebuild_failed") from error
        return {
            "profile_path": str(profile.path),
            "confirmed_count": profile.confirmed_count,
            "migration_path": str(migration.root),
            "migration_files": migration.file_count,
        }


class _MemoryReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_memory_review_server(
    vault_root: Path, *, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise MemoryReviewError("memory review may only bind to 127.0.0.1", code="unsafe_bind")
    try:
        numeric_port = int(port)
    except (TypeError, ValueError) as error:
        raise MemoryReviewError("review port must be an integer", code="invalid_port") from error
    if not 0 <= numeric_port <= 65535:
        raise MemoryReviewError("review port is outside the valid range", code="invalid_port")
    try:
        repository = MemoryReviewRepository(vault_root)
    except (MemoryStoreError, ReaderError, OSError) as error:
        raise MemoryReviewError(str(error), code="repository_failed") from error

    class Handler(_MemoryReviewRequestHandler):
        review_repository = repository

    try:
        return _MemoryReviewHTTPServer((host, numeric_port), Handler)
    except OSError as error:
        raise MemoryReviewError("the local review port could not be opened", code="bind_failed") from error


class _MemoryReviewRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    review_repository: MemoryReviewRepository

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path in _STATIC_FILES:
                self._serve_static(parsed.path)
                return
            if parsed.path == "/api/candidates":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                snapshot_keys = _query_csv(query, "snapshot", ())
                statuses = _query_csv(query, "status", ("pending",))
                kinds = _query_csv(query, "kind", ())
                self._send_json(
                    self.review_repository.list_candidates(
                        snapshot_keys=snapshot_keys,
                        statuses=statuses,
                        kinds=kinds,
                        query=_query_text(query, "q"),
                        sort=_query_text(query, "sort") or "newest",
                        min_confidence=_query_float(query, "min_confidence"),
                        after_time=_query_float(query, "after_time"),
                        before_time=_query_float(query, "before_time"),
                        limit=_query_int(query, "limit", 1000),
                        offset=_query_int(query, "offset", 0),
                    )
                )
                return
            if parsed.path == "/api/batches":
                self._send_json(self.review_repository.list_batches())
                return
            prefix = "/api/candidates/"
            if parsed.path.startswith(prefix):
                candidate_id = urllib.parse.unquote(parsed.path[len(prefix) :])
                self._send_json(self.review_repository.get_candidate(candidate_id))
                return
            raise MemoryReviewError("local review route was not found", code="not_found", status=HTTPStatus.NOT_FOUND)
        except MemoryReviewError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send_error(MemoryReviewError("the local review encountered an unexpected error", code="internal_error", status=HTTPStatus.INTERNAL_SERVER_ERROR))

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            payload = self._read_json()
            if parsed.path == "/api/decisions":
                candidate_ids = payload.get("candidate_ids")
                if not isinstance(candidate_ids, list) or not all(isinstance(value, str) for value in candidate_ids):
                    raise MemoryReviewError("candidate_ids must be a string array", code="invalid_request")
                status = payload.get("status")
                note = payload.get("note")
                if not isinstance(status, str) or (note is not None and not isinstance(note, str)):
                    raise MemoryReviewError("decision payload is invalid", code="invalid_request")
                self._send_json(self.review_repository.decide(candidate_ids, status, note))
                return
            edit_prefix = "/api/candidates/"
            edit_suffix = "/edit"
            if parsed.path.startswith(edit_prefix) and parsed.path.endswith(edit_suffix):
                candidate_id = urllib.parse.unquote(parsed.path[len(edit_prefix) : -len(edit_suffix)])
                statement = payload.get("statement")
                if not isinstance(statement, str):
                    raise MemoryReviewError("statement must be a string", code="invalid_request")
                self._send_json(self.review_repository.edit(candidate_id, statement))
                return
            if parsed.path == "/api/rebuild":
                self._send_json(self.review_repository.rebuild())
                return
            raise MemoryReviewError("local review route was not found", code="not_found", status=HTTPStatus.NOT_FOUND)
        except MemoryReviewError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send_error(MemoryReviewError("the local review encountered an unexpected error", code="internal_error", status=HTTPStatus.INTERNAL_SERVER_ERROR))

    def do_HEAD(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _method_not_allowed(self) -> None:
        self._send_error(MemoryReviewError("the review route does not accept this method", code="method_not_allowed", status=HTTPStatus.METHOD_NOT_ALLOWED), extra_headers={"Allow": "GET, POST"})

    def _serve_static(self, path: str) -> None:
        filename, content_type = _STATIC_FILES[path]
        target = STATIC_ROOT / filename
        if target.is_symlink() or not target.is_file():
            raise MemoryReviewError("review static asset is unavailable", code="static_missing", status=HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send_bytes(target.read_bytes(), content_type=content_type)

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise MemoryReviewError("request body must be application/json", code="invalid_content_type", status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise MemoryReviewError("request content length is invalid", code="invalid_request") from error
        if not 0 < length <= 2 * 1024 * 1024:
            raise MemoryReviewError("request body size is invalid", code="invalid_request")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MemoryReviewError("request body is not valid JSON", code="invalid_json") from error
        if not isinstance(payload, dict):
            raise MemoryReviewError("request body must be a JSON object", code="invalid_request")
        return payload

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send_json(self, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send_bytes(encoded, content_type="application/json; charset=utf-8")

    def _send_error(self, error: MemoryReviewError, *, extra_headers: Mapping[str, str] | None = None) -> None:
        encoded = json.dumps({"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False).encode("utf-8")
        self._send_bytes(encoded, content_type="application/json; charset=utf-8", status=error.status, extra_headers=extra_headers)

    def _send_bytes(
        self,
        payload: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


def _query_text(query: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def _query_csv(query: Mapping[str, Sequence[str]], name: str, default: Sequence[str]) -> tuple[str, ...]:
    if name not in query:
        return tuple(default)
    value = _query_text(query, name)
    if value is None:
        return ()
    return tuple(dict.fromkeys(part for part in value.split(",") if part))


def _query_int(query: Mapping[str, Sequence[str]], name: str, default: int) -> int:
    value = _query_text(query, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise MemoryReviewError(f"query parameter {name} must be an integer", code="invalid_query") from error


def _query_float(query: Mapping[str, Sequence[str]], name: str) -> float | None:
    value = _query_text(query, name)
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError as error:
        raise MemoryReviewError(
            f"query parameter {name} must be a number", code="invalid_query"
        ) from error
    if not math.isfinite(parsed):
        raise MemoryReviewError(
            f"query parameter {name} must be finite", code="invalid_query"
        )
    return parsed
