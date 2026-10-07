"""Local archive reader with separate user annotations and reading state."""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import stat
import sys
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, Sequence

from .database import SCHEMA_VERSION
from .annotations import AnnotationStore
from .pi_archive import PiArchive


STATIC_ROOT = Path(__file__).with_name("reader_static")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_STATIC_FILES = {
    "/static/annotations.js": ("annotations.js", "text/javascript; charset=utf-8"),
    "/static/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
    "/static/vendor/katex.js": ("vendor/katex.js", "text/javascript; charset=utf-8"),
    "/static/vendor/marked.js": ("vendor/marked.js", "text/javascript; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class ReaderError(RuntimeError):
    """A safe, user-facing reader error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "reader_error",
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def connect_reader_database(path: Path) -> sqlite3.Connection:
    """Open an existing archive database without any write capability."""

    database_path = Path(path)
    if database_path.is_symlink() or not database_path.is_file():
        raise ReaderError(
            "archive database must be an existing regular file",
            code="database_not_found",
            status=HTTPStatus.NOT_FOUND,
        )
    try:
        resolved = database_path.resolve(strict=True)
        database = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only = ON")
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA busy_timeout = 5000")
        version = int(database.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            database.close()
            raise ReaderError(
                f"archive schema version {version} is not supported; expected {SCHEMA_VERSION}",
                code="schema_mismatch",
                status=HTTPStatus.CONFLICT,
            )
        required = {
            "snapshots",
            "conversation_observations",
            "current_branch_nodes",
            "search_documents",
            "message_fts",
            "assets",
        }
        present = {
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE name IN (?, ?, ?, ?, ?, ?)",
                tuple(sorted(required)),
            )
        }
        if present != required:
            database.close()
            raise ReaderError(
                "archive database is missing required reader tables",
                code="schema_incomplete",
                status=HTTPStatus.CONFLICT,
            )
        return database
    except ReaderError:
        raise
    except sqlite3.Error as error:
        raise ReaderError(
            "archive database could not be opened read-only",
            code="database_open_failed",
            status=HTTPStatus.CONFLICT,
        ) from error


@dataclass(slots=True)
class OpenedAsset:
    file: BinaryIO
    size_bytes: int
    sha256: str
    download_name: str
    content_type: str

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> "OpenedAsset":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ReaderRepository:
    """Deterministic, read-only projections used by the local reader UI."""

    def __init__(self, database_path: Path, vault_root: Path) -> None:
        self.database_path = Path(database_path).resolve(strict=True)
        self.vault_root = Path(vault_root).resolve(strict=True)
        if not self.vault_root.is_dir() or not self.database_path.is_relative_to(self.vault_root):
            raise ReaderError("archive database must be inside the configured vault root")
        database = connect_reader_database(self.database_path)
        database.close()

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        database = connect_reader_database(self.database_path)
        try:
            yield database
        finally:
            database.close()

    def list_snapshots(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset, maximum=200)
        with self._connection() as database:
            total = int(database.execute("SELECT count(*) FROM snapshots").fetchone()[0])
            rows = database.execute(
                """SELECT s.id, s.snapshot_key, s.captured_at, s.captured_at_us,
                          s.imported_at, s.evidence_tree_sha256,
                          so.provider, so.kind, so.source_key, so.identity_scope,
                          ir.conversations_count, ir.nodes_count, ir.messages_count,
                          ir.group_threads_count, ir.group_messages_count,
                          ir.warnings_count,
                          (SELECT count(*) FROM source_absences sa
                            WHERE sa.snapshot_id = s.id) AS source_absences,
                          (SELECT count(*) FROM diagnostics d
                            WHERE d.snapshot_id = s.id AND d.severity = 'error') AS errors
                     FROM snapshots s
                     JOIN sources so ON so.id = s.source_id
                     LEFT JOIN import_runs ir ON ir.snapshot_id = s.id
                    ORDER BY s.captured_at_us DESC, s.id DESC
                    LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            items = [_snapshot_payload(row) for row in rows]
            default_row = database.execute(
                "SELECT id FROM snapshots ORDER BY captured_at_us DESC, id DESC LIMIT 1"
            ).fetchone()
        return {
            "items": items,
            "default_snapshot_id": int(default_row[0]) if default_row else None,
            "pagination": _pagination_payload(limit, offset, total),
        }

    def citation(self, source_id: int | str) -> dict[str, Any]:
        if str(source_id).startswith("pi:"):
            try:
                return PiArchive(self.vault_root).citation(int(str(source_id)[3:]))
            except ValueError as error:
                raise ReaderError(str(error)) from error
        with self._connection() as database:
            row = database.execute("""SELECT s.snapshot_key, s.id AS snapshot_id,
                ci.identity_key AS conversation_id, mi.identity_key AS message_id
                FROM message_observations mo
                JOIN snapshots s ON s.id = mo.snapshot_id
                JOIN node_observations no ON no.id = mo.node_observation_id
                JOIN node_identities ni ON ni.id = no.node_identity_id
                JOIN conversation_identities ci ON ci.id = ni.conversation_identity_id
                JOIN message_identities mi ON mi.id = mo.message_identity_id
                WHERE mo.id = ?""", (source_id,)).fetchone()
        if row is None:
            raise ReaderError("source message was not found", status=HTTPStatus.NOT_FOUND)
        return dict(row)

    def library(self, *, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List the last observation of every conversation across accounts."""
        limit, offset = _pagination(limit, offset, maximum=200)
        query = query.strip()
        if len(query) > 512:
            raise ReaderError("search query is too long", code="query_too_long")
        parameters: list[Any] = []
        where = ""
        if query:
            literal = f"%{_escape_like(query)}%"
            parameters.append(literal)
            if len(query) <= 2:
                match = "sd.content LIKE ? ESCAPE '\\'"
                parameters.append(literal)
            else:
                match = "sd.id IN (SELECT rowid FROM message_fts WHERE message_fts MATCH ?)"
                parameters.append('"' + query.replace('"', '""') + '"')
            where = f"""AND (cv.title LIKE ? ESCAPE '\\' OR EXISTS (
              SELECT 1 FROM node_identities ni
              CROSS JOIN node_observations no ON no.node_identity_id=ni.id AND no.snapshot_id=o.snapshot_id
              CROSS JOIN message_observations mo ON mo.snapshot_id=no.snapshot_id AND mo.message_identity_id=no.message_identity_id
              CROSS JOIN search_documents sd ON sd.message_version_id=mo.message_version_id
              WHERE ni.conversation_identity_id=ci.id AND {match}))"""
        sql = f"""WITH observed AS (
          SELECT co.*, row_number() OVER (PARTITION BY conversation_identity_id
            ORDER BY s.captured_at_us DESC,s.id DESC) AS rank
          FROM conversation_observations co JOIN snapshots s ON s.id=co.snapshot_id
        ), selected AS (
          SELECT ci.identity_key,ci.native_id,cv.title,cv.create_time,cv.update_time,o.snapshot_id
          FROM observed o JOIN conversation_identities ci ON ci.id=o.conversation_identity_id
          JOIN conversation_versions cv ON cv.id=o.conversation_version_id
          WHERE o.rank=1 {where}
        ) SELECT *,count(*) OVER () AS total FROM selected
          ORDER BY coalesce(update_time,create_time,0) DESC,identity_key LIMIT ? OFFSET ?"""
        with self._connection() as database:
            rows = database.execute(sql, (*parameters, limit, offset)).fetchall()
        items = [{key: row[key] for key in row.keys() if key != "total"} for row in rows]
        return {"items": items, "pagination": _pagination_payload(limit, offset, int(rows[0]["total"]) if rows else 0)}

    def list_conversations(
        self,
        *,
        snapshot_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset, maximum=200)
        with self._connection() as database:
            snapshot = self._snapshot(database, snapshot_id)
            total = int(
                database.execute(
                    "SELECT count(*) FROM conversation_observations WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()[0]
            )
            rows = database.execute(
                """WITH
                    node_counts AS (
                      SELECT ni.conversation_identity_id,
                             count(*) AS nodes,
                             count(no.message_identity_id) AS messages
                        FROM node_observations no
                        JOIN node_identities ni ON ni.id = no.node_identity_id
                       WHERE no.snapshot_id = ?
                       GROUP BY ni.conversation_identity_id
                    ),
                    current_counts AS (
                      SELECT conversation_identity_id, count(*) AS current_nodes
                        FROM current_branch_nodes
                       WHERE snapshot_id = ?
                       GROUP BY conversation_identity_id
                    ),
                    branch_counts AS (
                      SELECT ni.conversation_identity_id, count(*) AS branch_points
                        FROM (
                          SELECT parent_node_identity_id
                            FROM node_observations
                           WHERE snapshot_id = ? AND parent_node_identity_id IS NOT NULL
                           GROUP BY parent_node_identity_id
                          HAVING count(*) > 1
                        ) branch
                        JOIN node_identities ni ON ni.id = branch.parent_node_identity_id
                       GROUP BY ni.conversation_identity_id
                    ),
                    asset_counts AS (
                      SELECT ni.conversation_identity_id,
                             count(r.id) AS attachments,
                             sum(CASE WHEN r.resolution_status <> 'resolved' THEN 1 ELSE 0 END)
                               AS unresolved_assets
                        FROM message_asset_refs r
                        JOIN message_observations mo ON mo.id = r.message_observation_id
                        JOIN node_observations no ON no.id = mo.node_observation_id
                        JOIN node_identities ni ON ni.id = no.node_identity_id
                       WHERE r.snapshot_id = ?
                       GROUP BY ni.conversation_identity_id
                    )
                  SELECT ci.identity_key, ci.native_id, cv.title, cv.create_time, cv.update_time,
                         sr.source_file_path, sr.array_index, sr.json_pointer,
                         co.current_node_identity_key,
                         coalesce(nc.nodes, 0) AS nodes,
                         coalesce(nc.messages, 0) AS messages,
                         coalesce(bc.branch_points, 0) AS branch_points,
                         max(coalesce(nc.nodes, 0) - coalesce(cc.current_nodes, 0), 0)
                           AS alternative_nodes,
                         coalesce(ac.attachments, 0) AS attachments,
                         coalesce(ac.unresolved_assets, 0) AS unresolved_assets,
                         (SELECT sd.content
                            FROM current_branch_nodes cb
                            JOIN node_observations no
                              ON no.snapshot_id = cb.snapshot_id
                             AND no.node_identity_id = cb.node_identity_id
                            JOIN message_observations mo ON mo.node_observation_id = no.id
                            JOIN search_documents sd ON sd.message_version_id = mo.message_version_id
                           WHERE cb.snapshot_id = co.snapshot_id
                             AND cb.conversation_identity_id = co.conversation_identity_id
                             AND length(trim(sd.content)) > 0
                           ORDER BY cb.depth DESC LIMIT 1) AS preview,
                         EXISTS(
                           SELECT 1 FROM source_absences sa
                            JOIN snapshots later ON later.id = sa.snapshot_id
                           WHERE sa.source_id = ? AND sa.entity_kind = 'conversation'
                             AND sa.identity_key = ci.identity_key
                             AND later.captured_at_us > ?
                         ) AS absent_in_later_snapshot
                    FROM conversation_observations co
                    JOIN conversation_identities ci ON ci.id = co.conversation_identity_id
                    JOIN conversation_versions cv ON cv.id = co.conversation_version_id
                    JOIN source_records sr ON sr.id = co.source_record_id
                    LEFT JOIN node_counts nc ON nc.conversation_identity_id = ci.id
                    LEFT JOIN current_counts cc ON cc.conversation_identity_id = ci.id
                    LEFT JOIN branch_counts bc ON bc.conversation_identity_id = ci.id
                    LEFT JOIN asset_counts ac ON ac.conversation_identity_id = ci.id
                   WHERE co.snapshot_id = ?
                   ORDER BY coalesce(cv.update_time, cv.create_time, 0) DESC,
                            ci.identity_key ASC
                   LIMIT ? OFFSET ?""",
                (
                    snapshot_id,
                    snapshot_id,
                    snapshot_id,
                    snapshot_id,
                    snapshot["source_id"],
                    snapshot["captured_at_us"],
                    snapshot_id,
                    limit,
                    offset,
                ),
            ).fetchall()
        return {
            "snapshot": _snapshot_payload(snapshot),
            "items": [_conversation_list_payload(row) for row in rows],
            "pagination": _pagination_payload(limit, offset, total),
        }

    def search(
        self,
        *,
        snapshot_id: int,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset, maximum=100)
        normalized = str(query).strip()
        if not normalized:
            raise ReaderError("search query must not be empty", code="empty_query")
        if len(normalized) > 512:
            raise ReaderError("search query is too long", code="query_too_long")
        literal = f"%{_escape_like(normalized)}%"
        mode = "literal_like" if len(normalized) <= 2 else "fts_trigram"
        with self._connection() as database:
            snapshot = self._snapshot(database, snapshot_id)
            if mode == "literal_like":
                sql = """WITH base AS (
                           SELECT ci.identity_key AS conversation_identity_key,
                                  cv.title AS conversation_title,
                                  mi.identity_key AS message_identity_key,
                                  mv.author_role AS role,
                                  mv.content_type,
                                  sd.content AS text,
                                  mv.create_time,
                                  CASE WHEN sd.content LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END AS score
                             FROM conversation_observations co
                             JOIN conversation_identities ci ON ci.id = co.conversation_identity_id
                             JOIN conversation_versions cv ON cv.id = co.conversation_version_id
                             JOIN node_identities ni
                               ON ni.conversation_identity_id = ci.id
                             JOIN node_observations no
                               ON no.snapshot_id = co.snapshot_id AND no.node_identity_id = ni.id
                             JOIN message_observations mo ON mo.node_observation_id = no.id
                             JOIN message_identities mi ON mi.id = mo.message_identity_id
                             JOIN message_versions mv ON mv.id = mo.message_version_id
                             JOIN search_documents sd ON sd.message_version_id = mv.id
                            WHERE co.snapshot_id = ?
                              AND (sd.content LIKE ? ESCAPE '\\'
                                   OR cv.title LIKE ? ESCAPE '\\')
                         ), ranked AS (
                           SELECT *, row_number() OVER (
                             PARTITION BY conversation_identity_key
                             ORDER BY score, coalesce(create_time, 0) DESC, message_identity_key
                           ) AS hit_rank
                             FROM base
                         ), final AS (
                           SELECT *, count(*) OVER () AS total
                             FROM ranked WHERE hit_rank = 1
                         )
                         SELECT * FROM final
                          ORDER BY score, coalesce(create_time, 0) DESC,
                                   conversation_identity_key
                          LIMIT ? OFFSET ?"""
                parameters: Sequence[Any] = (
                    literal,
                    snapshot_id,
                    literal,
                    literal,
                    limit,
                    offset,
                )
            else:
                phrase = f'"{normalized.replace(chr(34), chr(34) * 2)}"'
                sql = """WITH fts_hits AS (
                           SELECT rowid AS document_id, bm25(message_fts) AS fts_score
                             FROM message_fts WHERE message_fts MATCH ?
                         ), base AS (
                           SELECT ci.identity_key AS conversation_identity_key,
                                  cv.title AS conversation_title,
                                  mi.identity_key AS message_identity_key,
                                  mv.author_role AS role,
                                  mv.content_type,
                                  sd.content AS text,
                                  mv.create_time,
                                  CASE WHEN fh.document_id IS NOT NULL THEN fh.fts_score ELSE 1000000 END AS score
                             FROM conversation_observations co
                             JOIN conversation_identities ci ON ci.id = co.conversation_identity_id
                             JOIN conversation_versions cv ON cv.id = co.conversation_version_id
                             JOIN node_identities ni
                               ON ni.conversation_identity_id = ci.id
                             JOIN node_observations no
                               ON no.snapshot_id = co.snapshot_id AND no.node_identity_id = ni.id
                             JOIN message_observations mo ON mo.node_observation_id = no.id
                             JOIN message_identities mi ON mi.id = mo.message_identity_id
                             JOIN message_versions mv ON mv.id = mo.message_version_id
                             JOIN search_documents sd ON sd.message_version_id = mv.id
                             LEFT JOIN fts_hits fh ON fh.document_id = sd.id
                            WHERE co.snapshot_id = ?
                              AND (fh.document_id IS NOT NULL
                                   OR cv.title LIKE ? ESCAPE '\\')
                         ), ranked AS (
                           SELECT *, row_number() OVER (
                             PARTITION BY conversation_identity_key
                             ORDER BY score, coalesce(create_time, 0) DESC, message_identity_key
                           ) AS hit_rank
                             FROM base
                         ), final AS (
                           SELECT *, count(*) OVER () AS total
                             FROM ranked WHERE hit_rank = 1
                         )
                         SELECT * FROM final
                          ORDER BY score, coalesce(create_time, 0) DESC,
                                   conversation_identity_key
                          LIMIT ? OFFSET ?"""
                parameters = (phrase, snapshot_id, literal, limit, offset)
            try:
                rows = database.execute(sql, parameters).fetchall()
            except sqlite3.DatabaseError as error:
                raise ReaderError(
                    "local full-text index could not execute this query",
                    code="search_failed",
                    status=HTTPStatus.CONFLICT,
                ) from error
        total = int(rows[0]["total"]) if rows else 0
        return {
            "snapshot": _snapshot_payload(snapshot),
            "query": normalized,
            "mode": mode,
            "guidance": (
                "短关键词使用转义后的逐字匹配，不会把 % 或 _ 当作通配符"
                if mode == "literal_like"
                else "消息正文使用本地 trigram 全文索引；标题保留逐字匹配"
            ),
            "items": [
                {
                    "document_kind": "conversation_message",
                    "conversation_identity_key": row["conversation_identity_key"],
                    "conversation_title": row["conversation_title"],
                    "message_identity_key": row["message_identity_key"],
                    "role": row["role"],
                    "content_type": row["content_type"],
                    "text": row["text"],
                    "create_time": row["create_time"],
                }
                for row in rows
            ],
            "pagination": _pagination_payload(limit, offset, total),
        }

    def list_group_threads(
        self,
        *,
        snapshot_id: int,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset, maximum=200)
        normalized = str(query).strip()
        if len(normalized) > 512:
            raise ReaderError("search query is too long", code="query_too_long")
        literal = f"%{_escape_like(normalized)}%"
        with self._connection() as database:
            snapshot = self._snapshot(database, snapshot_id)
            parameters: tuple[Any, ...] = (snapshot_id,)
            where = "gto.snapshot_id = ?"
            if normalized:
                where += """ AND (
                    gtv.name LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM group_message_observations search_gmo
                        JOIN group_message_versions search_gmv
                          ON search_gmv.id = search_gmo.group_message_version_id
                        WHERE search_gmo.group_thread_observation_id = gto.id
                          AND coalesce(search_gmv.text, '') LIKE ? ESCAPE '\\'
                    )
                )"""
                parameters += (literal, literal)
            rows = database.execute(
                f"""WITH base AS (
                      SELECT gti.identity_key, gti.native_id,
                             coalesce(gtv.name, '未命名群聊') AS title,
                             min(gmv.created_at) AS create_time,
                             max(gmv.created_at) AS update_time,
                             count(gmo.id) AS messages,
                             (SELECT search_gmv.text
                                FROM group_message_observations search_gmo
                                JOIN group_message_versions search_gmv
                                  ON search_gmv.id = search_gmo.group_message_version_id
                               WHERE search_gmo.group_thread_observation_id = gto.id
                                 AND length(trim(coalesce(search_gmv.text, ''))) > 0
                               ORDER BY search_gmv.created_at DESC,
                                        search_gmo.json_pointer DESC LIMIT 1) AS preview,
                             (SELECT count(*) FROM group_message_asset_refs gar
                                JOIN group_message_observations asset_gmo
                                  ON asset_gmo.id = gar.group_message_observation_id
                               WHERE asset_gmo.group_thread_observation_id = gto.id)
                               AS attachments,
                             (SELECT count(*) FROM group_message_asset_refs gar
                                JOIN group_message_observations asset_gmo
                                  ON asset_gmo.id = gar.group_message_observation_id
                               WHERE asset_gmo.group_thread_observation_id = gto.id
                                 AND gar.resolution_status NOT IN ('resolved', 'external'))
                               AS unresolved_assets
                        FROM group_thread_observations gto
                        JOIN group_thread_identities gti
                          ON gti.id = gto.group_thread_identity_id
                        JOIN group_thread_versions gtv
                          ON gtv.id = gto.group_thread_version_id
                        LEFT JOIN group_message_observations gmo
                          ON gmo.group_thread_observation_id = gto.id
                        LEFT JOIN group_message_versions gmv
                          ON gmv.id = gmo.group_message_version_id
                       WHERE {where}
                       GROUP BY gto.id
                    ), final AS (
                      SELECT *, count(*) OVER () AS total FROM base
                    )
                    SELECT * FROM final
                     ORDER BY coalesce(update_time, create_time, '') DESC, identity_key
                     LIMIT ? OFFSET ?""",
                (*parameters, limit, offset),
            ).fetchall()
        total = int(rows[0]["total"]) if rows else 0
        return {
            "snapshot": _snapshot_payload(snapshot),
            "query": normalized,
            "mode": "literal_like",
            "guidance": "群聊名称和消息正文使用转义后的逐字匹配",
            "items": [
                {
                    "document_kind": "group_thread",
                    "identity_key": row["identity_key"],
                    "native_id": row["native_id"],
                    "title": row["title"],
                    "preview": row["preview"] or "",
                    "create_time": row["create_time"],
                    "update_time": row["update_time"],
                    "stats": {
                        "messages": int(row["messages"] or 0),
                        "attachments": int(row["attachments"] or 0),
                        "unresolved_assets": int(row["unresolved_assets"] or 0),
                        "branch_points": 0,
                    },
                }
                for row in rows
            ],
            "pagination": _pagination_payload(limit, offset, total),
        }

    def get_group_thread(self, snapshot_id: int, identity_key: str) -> dict[str, Any]:
        if not identity_key or len(identity_key) > 2048:
            raise ReaderError("group thread identity is invalid", code="invalid_identity")
        with self._connection() as database:
            snapshot = self._snapshot(database, snapshot_id)
            thread = database.execute(
                """SELECT gto.id AS observation_id, gto.group_thread_identity_id,
                          gti.identity_key, gti.native_id,
                          coalesce(gtv.name, '未命名群聊') AS title,
                          sr.source_file_path, sr.array_index, sr.json_pointer,
                          sr.raw_sha256 AS source_record_sha256
                     FROM group_thread_observations gto
                     JOIN group_thread_identities gti
                       ON gti.id = gto.group_thread_identity_id
                     JOIN group_thread_versions gtv ON gtv.id = gto.group_thread_version_id
                     JOIN source_records sr ON sr.id = gto.source_record_id
                    WHERE gto.snapshot_id = ? AND gti.identity_key = ?""",
                (snapshot_id, identity_key),
            ).fetchone()
            if thread is None:
                raise ReaderError(
                    "group thread was not observed in this snapshot",
                    code="group_thread_not_found",
                    status=HTTPStatus.NOT_FOUND,
                )
            rows = database.execute(
                """SELECT gmo.id AS message_observation_id, gmo.json_pointer,
                          gmi.identity_key AS message_identity_key, gmi.native_id,
                          gmv.role, gmv.text, gmv.created_at
                     FROM group_message_observations gmo
                     JOIN group_message_identities gmi
                       ON gmi.id = gmo.group_message_identity_id
                     JOIN group_message_versions gmv ON gmv.id = gmo.group_message_version_id
                    WHERE gmo.snapshot_id = ? AND gmo.group_thread_observation_id = ?
                    ORDER BY coalesce(gmv.created_at, ''), gmo.json_pointer,
                             gmi.identity_key""",
                (snapshot_id, thread["observation_id"]),
            ).fetchall()
            attachments = self._group_thread_attachments(
                database,
                snapshot_id=snapshot_id,
                group_thread_observation_id=int(thread["observation_id"]),
            )
            observations = self._group_thread_snapshot_observations(
                database,
                source_id=int(snapshot["source_id"]),
                identity_key=identity_key,
            )
        nodes: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            node_key = str(row["message_identity_key"])
            nodes.append(
                {
                    "node_identity_key": node_key,
                    "native_id": row["native_id"],
                    "parent_node_identity_key": (
                        str(rows[index - 1]["message_identity_key"]) if index else None
                    ),
                    "child_options": (
                        [str(rows[index + 1]["message_identity_key"])]
                        if index + 1 < len(rows)
                        else []
                    ),
                    "depth": index,
                    "json_pointer": row["json_pointer"],
                    "message": {
                        "source_id": row["message_observation_id"],
            "identity_key": row["message_identity_key"],
                        "native_id": row["native_id"],
                        "role": row["role"] or "unknown",
                        "name": None,
                        "content_type": "text",
                        "create_time": row["created_at"],
                        "update_time": None,
                        "status": None,
                        "recipient": None,
                        "text": row["text"] or "",
                        "model": None,
                        "attachments": attachments.get(
                            int(row["message_observation_id"]), []
                        ),
                    },
                }
            )
        all_attachments = [
            attachment
            for node in nodes
            for attachment in node["message"]["attachments"]
        ]
        return {
            "snapshot": _snapshot_payload(snapshot),
            "conversation": {
                "kind": "group_thread",
                "identity_key": thread["identity_key"],
                "native_id": thread["native_id"],
                "title": thread["title"],
                "create_time": rows[0]["created_at"] if rows else None,
                "update_time": rows[-1]["created_at"] if rows else None,
                "preview": next(
                    (str(row["text"]) for row in reversed(rows) if row["text"]), ""
                ),
                "current_node_identity_key": (
                    str(rows[-1]["message_identity_key"]) if rows else None
                ),
                "current_branch": nodes,
                "alternative_nodes": [],
                "stats": {
                    "nodes": len(nodes),
                    "messages": len(nodes),
                    "current_nodes": len(nodes),
                    "alternative_nodes": 0,
                    "branch_points": 0,
                    "attachments": len(all_attachments),
                    "unresolved_assets": sum(
                        1
                        for attachment in all_attachments
                        if attachment["status"] not in {"resolved", "external"}
                    ),
                },
                "source": {
                    "source_file_path": thread["source_file_path"],
                    "array_index": thread["array_index"],
                    "json_pointer": thread["json_pointer"],
                    "source_record_sha256": thread["source_record_sha256"],
                },
                "diagnostics": [],
                "snapshot_observations": observations,
            },
        }

    def get_conversation(self, snapshot_id: int, identity_key: str) -> dict[str, Any]:
        if not identity_key or len(identity_key) > 2048:
            raise ReaderError("conversation identity is invalid", code="invalid_identity")
        with self._connection() as database:
            snapshot = self._snapshot(database, snapshot_id)
            conversation = database.execute(
                """SELECT co.id AS observation_id, co.conversation_identity_id,
                          co.current_node_identity_key,
                          ci.identity_key, ci.native_id,
                          cv.title, cv.create_time, cv.update_time,
                          sr.source_file_path, sr.array_index, sr.json_pointer,
                          sr.raw_sha256 AS source_record_sha256
                     FROM conversation_observations co
                     JOIN conversation_identities ci ON ci.id = co.conversation_identity_id
                     JOIN conversation_versions cv ON cv.id = co.conversation_version_id
                     JOIN source_records sr ON sr.id = co.source_record_id
                    WHERE co.snapshot_id = ? AND ci.identity_key = ?""",
                (snapshot_id, identity_key),
            ).fetchone()
            if conversation is None:
                raise ReaderError(
                    "conversation was not observed in this snapshot",
                    code="conversation_not_found",
                    status=HTTPStatus.NOT_FOUND,
                )
            node_rows = database.execute(
                """SELECT no.id AS node_observation_id, no.json_pointer AS node_json_pointer,
                          ni.identity_key AS node_identity_key, ni.native_id,
                          nv.declared_children_json,
                          pi.identity_key AS parent_node_identity_key,
                          cb.depth,
                          mo.id AS message_observation_id,
                          mi.identity_key AS message_identity_key,
                          mi.native_id AS message_native_id,
                          mv.raw_json AS message_raw_json,
                          mv.author_role, mv.author_name, mv.content_type,
                          mv.create_time AS message_create_time,
                          mv.update_time AS message_update_time,
                          mv.status AS message_status, mv.recipient,
                          sd.content AS message_text
                     FROM node_observations no
                     JOIN node_identities ni ON ni.id = no.node_identity_id
                     JOIN node_versions nv ON nv.id = no.node_version_id
                     LEFT JOIN node_identities pi ON pi.id = no.parent_node_identity_id
                     LEFT JOIN current_branch_nodes cb
                       ON cb.snapshot_id = no.snapshot_id
                      AND cb.conversation_identity_id = ni.conversation_identity_id
                      AND cb.node_identity_id = ni.id
                     LEFT JOIN message_observations mo ON mo.node_observation_id = no.id
                     LEFT JOIN message_identities mi ON mi.id = mo.message_identity_id
                     LEFT JOIN message_versions mv ON mv.id = mo.message_version_id
                     LEFT JOIN search_documents sd ON sd.message_version_id = mv.id
                    WHERE no.snapshot_id = ? AND ni.conversation_identity_id = ?
                    ORDER BY CASE WHEN cb.depth IS NULL THEN 1 ELSE 0 END,
                             cb.depth, no.json_pointer, ni.identity_key""",
                (snapshot_id, conversation["conversation_identity_id"]),
            ).fetchall()
            attachments = self._conversation_attachments(
                database,
                snapshot_id=snapshot_id,
                conversation_identity_id=int(conversation["conversation_identity_id"]),
            )
            child_options: dict[str | None, list[str]] = {}
            for row in node_rows:
                child_options.setdefault(row["parent_node_identity_key"], []).append(
                    row["node_identity_key"]
                )
            identity_by_native_id = {
                str(row["native_id"]): str(row["node_identity_key"]) for row in node_rows
            }
            for row in node_rows:
                actual = child_options.get(row["node_identity_key"], [])
                declared = _parse_json(row["declared_children_json"], [])
                ordered = [
                    identity_by_native_id[native_id]
                    for native_id in declared
                    if isinstance(native_id, str)
                    and identity_by_native_id.get(native_id) in actual
                ] if isinstance(declared, list) else []
                ordered.extend(sorted(set(actual) - set(ordered)))
                child_options[row["node_identity_key"]] = ordered
            nodes = [
                _node_payload(row, child_options.get(row["node_identity_key"], []), attachments)
                for row in node_rows
            ]
            current = [node for node in nodes if node["depth"] is not None]
            alternatives = [node for node in nodes if node["depth"] is None]
            diagnostics = [
                {
                    "severity": row["severity"],
                    "code": row["code"],
                    "details": _parse_json(row["details_json"], {}),
                }
                for row in database.execute(
                    """SELECT severity, code, details_json FROM diagnostics
                        WHERE snapshot_id = ? AND conversation_identity_id = ?
                        ORDER BY severity DESC, code, id""",
                    (snapshot_id, conversation["conversation_identity_id"]),
                )
            ]
            observations = self._conversation_snapshot_observations(
                database,
                source_id=int(snapshot["source_id"]),
                identity_key=identity_key,
            )
        stats = {
            "nodes": len(nodes),
            "messages": sum(1 for node in nodes if node["message"]),
            "current_nodes": len(current),
            "alternative_nodes": len(alternatives),
            "branch_points": sum(
                1
                for parent, children in child_options.items()
                if parent is not None and len(children) > 1
            ),
            "attachments": sum(
                len(node["message"]["attachments"])
                for node in nodes
                if node["message"]
            ),
            "unresolved_assets": sum(
                1
                for node in nodes
                if node["message"]
                for attachment in node["message"]["attachments"]
                if attachment["status"] != "resolved"
            ),
        }
        return {
            "snapshot": _snapshot_payload(snapshot),
            "conversation": {
                "identity_key": conversation["identity_key"],
                "native_id": conversation["native_id"],
                "title": conversation["title"],
                "create_time": conversation["create_time"],
                "update_time": conversation["update_time"],
                "preview": next(
                    (
                        node["message"]["text"]
                        for node in reversed(current)
                        if node["message"] and node["message"]["text"]
                    ),
                    "",
                ),
                "current_node_identity_key": conversation["current_node_identity_key"],
                "current_branch": current,
                "alternative_nodes": alternatives,
                "stats": stats,
                "source": {
                    "source_file_path": conversation["source_file_path"],
                    "array_index": conversation["array_index"],
                    "json_pointer": conversation["json_pointer"],
                    "source_record_sha256": conversation["source_record_sha256"],
                },
                "diagnostics": diagnostics,
                "snapshot_observations": observations,
            },
        }

    def analytics(self, snapshot_id: int) -> dict[str, Any]:
        with self._connection() as database:
            snapshot = self._snapshot(database, snapshot_id)
            activity = [
                dict(row)
                for row in database.execute(
                    """SELECT strftime('%Y-%m', mv.create_time, 'unixepoch') AS month,
                              count(*) AS messages,
                              count(DISTINCT ni.conversation_identity_id) AS conversations
                         FROM message_observations mo
                         JOIN message_versions mv ON mv.id = mo.message_version_id
                         JOIN node_observations no ON no.id = mo.node_observation_id
                         JOIN node_identities ni ON ni.id = no.node_identity_id
                        WHERE mo.snapshot_id = ? AND mv.create_time IS NOT NULL
                          AND mv.create_time > 0
                        GROUP BY month HAVING month IS NOT NULL
                        ORDER BY month""",
                    (snapshot_id,),
                )
            ]
            roles = _count_rows(
                database,
                """SELECT coalesce(mv.author_role, 'unlabeled') AS key, count(*) AS count
                     FROM message_observations mo
                     JOIN message_versions mv ON mv.id = mo.message_version_id
                    WHERE mo.snapshot_id = ? GROUP BY key ORDER BY count DESC, key""",
                snapshot_id,
            )
            content_types = _count_rows(
                database,
                """SELECT coalesce(mv.content_type, 'unlabeled') AS key, count(*) AS count
                     FROM message_observations mo
                     JOIN message_versions mv ON mv.id = mo.message_version_id
                    WHERE mo.snapshot_id = ? GROUP BY key ORDER BY count DESC, key""",
                snapshot_id,
            )
            model_counts: Counter[str] = Counter()
            for row in database.execute(
                """SELECT mv.raw_json FROM message_observations mo
                    JOIN message_versions mv ON mv.id = mo.message_version_id
                    WHERE mo.snapshot_id = ? AND mv.author_role = 'assistant'""",
                (snapshot_id,),
            ):
                model_counts[_message_model(row["raw_json"]) or "unlabeled"] += 1
            models = [
                {"key": key, "count": count}
                for key, count in sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))
            ]
            branches = self._branch_analytics(database, snapshot_id)
            attachments = self._attachment_analytics(database, snapshot_id)
            snapshot_quality = self._snapshot_quality(database, snapshot_id)
            snapshot_deltas = self._snapshot_deltas(database, snapshot)
        return {
            "snapshot": _snapshot_payload(snapshot),
            "generated_from": "deterministic_sql",
            "llm_inference": "disabled",
            "profile_writeback": "disabled",
            "activity": activity,
            "activity_years": _year_activity(activity),
            "roles": roles,
            "content_types": content_types,
            "models": models,
            "branches": branches,
            "attachments": attachments,
            "snapshot_quality": snapshot_quality,
            "snapshot_deltas": snapshot_deltas,
        }

    def open_asset(self, sha256: str) -> OpenedAsset:
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ReaderError("asset identity must be a lowercase SHA-256 digest", code="invalid_asset")
        with self._connection() as database:
            row = database.execute(
                """SELECT a.sha256, a.size_bytes, a.storage_path,
                          (SELECT ao.declared_name FROM asset_observations ao
                            WHERE ao.asset_id = a.id AND ao.declared_name IS NOT NULL
                            ORDER BY ao.snapshot_id DESC, ao.id DESC LIMIT 1) AS declared_name
                     FROM assets a WHERE a.sha256 = ?""",
                (sha256,),
            ).fetchone()
        if row is None:
            raise ReaderError(
                "asset is not present in the content store index",
                code="asset_not_found",
                status=HTTPStatus.NOT_FOUND,
            )
        expected = f"assets/sha256/{sha256[:2]}/{sha256}"
        if row["storage_path"] != expected:
            raise ReaderError("asset storage path is not canonical", code="asset_path_invalid")
        file = _open_regular_beneath(self.vault_root, expected.split("/"))
        try:
            file_stat = os.fstat(file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != int(row["size_bytes"]):
                raise ReaderError("asset file metadata does not match the index", code="asset_changed")
            digest = hashlib.sha256()
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != sha256:
                raise ReaderError("asset content hash does not match the index", code="asset_changed")
            file.seek(0)
            download_name = _safe_download_name(row["declared_name"], sha256)
            content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
            return OpenedAsset(file, file_stat.st_size, sha256, download_name, content_type)
        except BaseException:
            file.close()
            raise

    def _snapshot(self, database: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row:
        try:
            numeric_id = int(snapshot_id)
        except (TypeError, ValueError) as error:
            raise ReaderError("snapshot_id must be an integer", code="invalid_snapshot") from error
        row = database.execute(
            """SELECT s.id, s.source_id, s.snapshot_key, s.captured_at, s.captured_at_us,
                      s.imported_at, s.evidence_tree_sha256,
                      so.provider, so.kind, so.source_key, so.identity_scope,
                      ir.conversations_count, ir.nodes_count, ir.messages_count,
                      ir.group_threads_count, ir.group_messages_count, ir.warnings_count,
                      (SELECT count(*) FROM source_absences sa
                        WHERE sa.snapshot_id = s.id) AS source_absences,
                      (SELECT count(*) FROM diagnostics d
                        WHERE d.snapshot_id = s.id AND d.severity = 'error') AS errors
                 FROM snapshots s
                 JOIN sources so ON so.id = s.source_id
                 LEFT JOIN import_runs ir ON ir.snapshot_id = s.id
                WHERE s.id = ?""",
            (numeric_id,),
        ).fetchone()
        if row is None:
            raise ReaderError(
                "snapshot was not found",
                code="snapshot_not_found",
                status=HTTPStatus.NOT_FOUND,
            )
        return row

    def _conversation_attachments(
        self,
        database: sqlite3.Connection,
        *,
        snapshot_id: int,
        conversation_identity_id: int,
    ) -> dict[int, list[dict[str, Any]]]:
        result: dict[int, list[dict[str, Any]]] = {}
        rows = database.execute(
            """SELECT mo.id AS message_observation_id, r.ordinal,
                      r.reference_kind, r.reference_value, r.normalized_reference,
                      r.resolution_status, r.resolution_method, r.candidate_count,
                      r.candidates_json,
                      ao.declared_name, a.sha256, a.size_bytes
                 FROM message_asset_refs r
                 JOIN message_observations mo ON mo.id = r.message_observation_id
                 JOIN node_observations no ON no.id = mo.node_observation_id
                 JOIN node_identities ni ON ni.id = no.node_identity_id
                 LEFT JOIN asset_observations ao ON ao.id = r.asset_observation_id
                 LEFT JOIN assets a ON a.id = ao.asset_id
                WHERE r.snapshot_id = ? AND ni.conversation_identity_id = ?
                ORDER BY mo.id, r.ordinal""",
            (snapshot_id, conversation_identity_id),
        )
        for row in rows:
            name = row["declared_name"] or row["normalized_reference"] or row["reference_value"]
            sha256 = row["sha256"] if _valid_sha256(row["sha256"]) else None
            result.setdefault(int(row["message_observation_id"]), []).append(
                {
                    "ordinal": int(row["ordinal"]),
                    "name": name or "未命名附件",
                    "mime": mimetypes.guess_type(str(name or ""))[0] or "application/octet-stream",
                    "size_bytes": row["size_bytes"],
                    "sha256": sha256,
                    "status": row["resolution_status"],
                    "resolution_method": row["resolution_method"],
                    "candidate_count": int(row["candidate_count"]),
                    "candidates": _parse_json(row["candidates_json"], []),
                    "download_url": f"/api/assets/{sha256}" if row["resolution_status"] == "resolved" and sha256 else None,
                }
            )
        return result

    def _conversation_snapshot_observations(
        self,
        database: sqlite3.Connection,
        *,
        source_id: int,
        identity_key: str,
    ) -> list[dict[str, Any]]:
        rows = database.execute(
            """SELECT s.id, s.snapshot_key, s.captured_at,
                      CASE WHEN co.id IS NOT NULL THEN 'present'
                           WHEN sa.id IS NOT NULL THEN 'absent_in_snapshot'
                           ELSE 'unknown' END AS state
                 FROM snapshots s
                 JOIN conversation_identities ci
                   ON ci.source_id = s.source_id AND ci.identity_key = ?
                 LEFT JOIN conversation_observations co
                   ON co.snapshot_id = s.id AND co.conversation_identity_id = ci.id
                 LEFT JOIN source_absences sa
                   ON sa.snapshot_id = s.id AND sa.entity_kind = 'conversation'
                  AND sa.identity_key = ci.identity_key
                WHERE s.source_id = ?
                ORDER BY s.captured_at_us DESC, s.id DESC""",
            (identity_key, source_id),
        )
        return [dict(row) for row in rows]

    def _group_thread_attachments(
        self,
        database: sqlite3.Connection,
        *,
        snapshot_id: int,
        group_thread_observation_id: int,
    ) -> dict[int, list[dict[str, Any]]]:
        result: dict[int, list[dict[str, Any]]] = {}
        rows = database.execute(
            """SELECT gmo.id AS message_observation_id, gar.ordinal,
                      gar.reference_value, gar.resolution_status, gar.candidates_json,
                      ao.declared_name, a.sha256, a.size_bytes
                 FROM group_message_asset_refs gar
                 JOIN group_message_observations gmo
                   ON gmo.id = gar.group_message_observation_id
                 LEFT JOIN asset_observations ao ON ao.id = gar.asset_observation_id
                 LEFT JOIN assets a ON a.id = ao.asset_id
                WHERE gar.snapshot_id = ? AND gmo.group_thread_observation_id = ?
                ORDER BY gmo.id, gar.ordinal""",
            (snapshot_id, group_thread_observation_id),
        )
        for row in rows:
            name = row["declared_name"] or row["reference_value"] or "未命名附件"
            sha256 = row["sha256"] if _valid_sha256(row["sha256"]) else None
            status = str(row["resolution_status"])
            result.setdefault(int(row["message_observation_id"]), []).append(
                {
                    "ordinal": int(row["ordinal"]),
                    "name": name,
                    "mime": mimetypes.guess_type(str(name))[0]
                    or "application/octet-stream",
                    "size_bytes": row["size_bytes"],
                    "sha256": sha256,
                    "status": status,
                    "resolution_method": None,
                    "candidate_count": len(_parse_json(row["candidates_json"], [])),
                    "candidates": _parse_json(row["candidates_json"], []),
                    "download_url": (
                        f"/api/assets/{sha256}"
                        if status == "resolved" and sha256
                        else None
                    ),
                }
            )
        return result

    def _group_thread_snapshot_observations(
        self,
        database: sqlite3.Connection,
        *,
        source_id: int,
        identity_key: str,
    ) -> list[dict[str, Any]]:
        rows = database.execute(
            """SELECT s.id, s.snapshot_key, s.captured_at,
                      CASE WHEN gto.id IS NOT NULL THEN 'present'
                           WHEN sa.id IS NOT NULL THEN 'absent_in_snapshot'
                           ELSE 'unknown' END AS state
                 FROM snapshots s
                 JOIN group_thread_identities gti
                   ON gti.source_id = s.source_id AND gti.identity_key = ?
                 LEFT JOIN group_thread_observations gto
                   ON gto.snapshot_id = s.id AND gto.group_thread_identity_id = gti.id
                 LEFT JOIN source_absences sa
                   ON sa.snapshot_id = s.id AND sa.entity_kind = 'group_thread'
                  AND sa.identity_key = gti.identity_key
                WHERE s.source_id = ?
                ORDER BY s.captured_at_us DESC, s.id DESC""",
            (identity_key, source_id),
        )
        return [dict(row) for row in rows]

    def _branch_analytics(self, database: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
        nodes = int(
            database.execute(
                "SELECT count(*) FROM node_observations WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()[0]
        )
        current_nodes = int(
            database.execute(
                "SELECT count(*) FROM current_branch_nodes WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()[0]
        )
        row = database.execute(
            """WITH sibling_groups AS (
                 SELECT parent_node_identity_id, count(*) AS children
                   FROM node_observations
                  WHERE snapshot_id = ? AND parent_node_identity_id IS NOT NULL
                  GROUP BY parent_node_identity_id HAVING count(*) > 1
               )
               SELECT count(*) AS branch_points,
                      count(DISTINCT ni.conversation_identity_id) AS conversations_with_branches
                 FROM sibling_groups sg
                 JOIN node_identities ni ON ni.id = sg.parent_node_identity_id""",
            (snapshot_id,),
        ).fetchone()
        role_alternatives = {
            str(role_row["role"]): int(role_row["alternatives"])
            for role_row in database.execute(
                """WITH roles AS (
                     SELECT no.parent_node_identity_id,
                            coalesce(mv.author_role, 'unlabeled') AS role,
                            count(*) AS role_count
                       FROM node_observations no
                       JOIN message_observations mo ON mo.node_observation_id = no.id
                       JOIN message_versions mv ON mv.id = mo.message_version_id
                      WHERE no.snapshot_id = ? AND no.parent_node_identity_id IS NOT NULL
                      GROUP BY no.parent_node_identity_id, role
                     HAVING count(*) > 1
                   )
                   SELECT role, sum(role_count - 1) AS alternatives
                     FROM roles GROUP BY role""",
                (snapshot_id,),
            )
        }
        conversations = int(
            database.execute(
                "SELECT count(*) FROM conversation_observations WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()[0]
        )
        branch_points = int(row["branch_points"])
        conversations_with_branches = int(row["conversations_with_branches"])
        return {
            "nodes": nodes,
            "current_nodes": current_nodes,
            "alternative_nodes": max(nodes - current_nodes, 0),
            "branch_points": branch_points,
            "conversations_with_branches": conversations_with_branches,
            "conversation_branch_rate": _ratio(conversations_with_branches, conversations),
            "regenerated_assistant_nodes": role_alternatives.get("assistant", 0),
            "edited_user_nodes": role_alternatives.get("user", 0),
        }

    def _attachment_analytics(self, database: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
        row = database.execute(
            """SELECT count(*) AS refs,
                      sum(CASE WHEN resolution_status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
                      sum(CASE WHEN resolution_status = 'unresolved' THEN 1 ELSE 0 END) AS unresolved,
                      sum(CASE WHEN resolution_status = 'ambiguous' THEN 1 ELSE 0 END) AS ambiguous
                 FROM message_asset_refs WHERE snapshot_id = ?""",
            (snapshot_id,),
        ).fetchone()
        assets = database.execute(
            """SELECT count(*) AS unique_assets, coalesce(sum(size_bytes), 0) AS total_bytes
                 FROM (
                   SELECT DISTINCT a.id, a.size_bytes
                     FROM asset_observations ao JOIN assets a ON a.id = ao.asset_id
                    WHERE ao.snapshot_id = ?
                 )""",
            (snapshot_id,),
        ).fetchone()
        return {
            "references": int(row["refs"] or 0),
            "resolved": int(row["resolved"] or 0),
            "unresolved": int(row["unresolved"] or 0),
            "ambiguous": int(row["ambiguous"] or 0),
            "unique_assets": int(assets["unique_assets"] or 0),
            "total_bytes": int(assets["total_bytes"] or 0),
        }

    def _snapshot_quality(self, database: sqlite3.Connection, snapshot_id: int) -> dict[str, Any]:
        diagnostics = {
            str(row["severity"]): int(row["count"])
            for row in database.execute(
                "SELECT severity, count(*) AS count FROM diagnostics WHERE snapshot_id = ? GROUP BY severity",
                (snapshot_id,),
            )
        }
        absences = {
            str(row["entity_kind"]): int(row["count"])
            for row in database.execute(
                "SELECT entity_kind, count(*) AS count FROM source_absences WHERE snapshot_id = ? GROUP BY entity_kind",
                (snapshot_id,),
            )
        }
        claims = [
            {"claim": row["claim"], "status": row["status"], "details": _parse_json(row["details_json"], {})}
            for row in database.execute(
                "SELECT claim, status, details_json FROM snapshot_claims WHERE snapshot_id = ? ORDER BY claim",
                (snapshot_id,),
            )
        ]
        coverage = [
            dict(row)
            for row in database.execute(
                """SELECT media_class AS key, handling_status, count(*) AS files,
                          coalesce(sum(size_bytes), 0) AS size_bytes
                     FROM source_file_coverage WHERE snapshot_id = ?
                    GROUP BY media_class, handling_status
                    ORDER BY media_class, handling_status""",
                (snapshot_id,),
            )
        ]
        return {
            "diagnostics": {
                "warnings": diagnostics.get("warning", 0),
                "errors": diagnostics.get("error", 0),
            },
            "source_absences": sum(absences.values()),
            "source_absences_by_kind": absences,
            "source_deletions": None,
            "source_deletions_note": "the canonical schema contains no explicit provider-deletion event",
            "claims": claims,
            "source_file_coverage": coverage,
        }

    def _snapshot_deltas(
        self, database: sqlite3.Connection, selected_snapshot: sqlite3.Row
    ) -> list[dict[str, Any]]:
        rows = database.execute(
            """SELECT id, snapshot_key, captured_at FROM snapshots
                WHERE source_id = ? AND captured_at_us <= ?
                ORDER BY captured_at_us, id""",
            (selected_snapshot["source_id"], selected_snapshot["captured_at_us"]),
        ).fetchall()
        result = []
        previous_id: int | None = None
        for row in rows:
            current_id = int(row["id"])
            result.append(
                {
                    "snapshot_id": current_id,
                    "snapshot_key": row["snapshot_key"],
                    "captured_at": row["captured_at"],
                    "prior_snapshot_id": previous_id,
                    "conversations": self._versioned_delta(
                        database,
                        table="conversation_observations",
                        identity_column="conversation_identity_id",
                        version_column="conversation_version_id",
                        entity_kind="conversation",
                        current_id=current_id,
                        previous_id=previous_id,
                    ),
                    "nodes": self._versioned_delta(
                        database,
                        table="node_observations",
                        identity_column="node_identity_id",
                        version_column="node_version_id",
                        entity_kind="node",
                        current_id=current_id,
                        previous_id=previous_id,
                    ),
                    "messages": self._versioned_delta(
                        database,
                        table="message_observations",
                        identity_column="message_identity_id",
                        version_column="message_version_id",
                        entity_kind="message",
                        current_id=current_id,
                        previous_id=previous_id,
                    ),
                    "assets": self._asset_delta(database, current_id, previous_id),
                }
            )
            previous_id = current_id
        return result

    @staticmethod
    def _versioned_delta(
        database: sqlite3.Connection,
        *,
        table: str,
        identity_column: str,
        version_column: str,
        entity_kind: str,
        current_id: int,
        previous_id: int | None,
    ) -> dict[str, int]:
        allowed = {
            ("conversation_observations", "conversation_identity_id", "conversation_version_id"),
            ("node_observations", "node_identity_id", "node_version_id"),
            ("message_observations", "message_identity_id", "message_version_id"),
        }
        if (table, identity_column, version_column) not in allowed:
            raise AssertionError("untrusted delta table")
        if previous_id is None:
            added = int(
                database.execute(
                    f"SELECT count(*) FROM {table} WHERE snapshot_id = ?", (current_id,)
                ).fetchone()[0]
            )
            changed = 0
        else:
            added = int(
                database.execute(
                    f"""SELECT count(*) FROM {table} current
                        LEFT JOIN {table} prior
                          ON prior.snapshot_id = ?
                         AND prior.{identity_column} = current.{identity_column}
                        WHERE current.snapshot_id = ? AND prior.id IS NULL""",
                    (previous_id, current_id),
                ).fetchone()[0]
            )
            changed = int(
                database.execute(
                    f"""SELECT count(*) FROM {table} current
                        JOIN {table} prior
                          ON prior.snapshot_id = ?
                         AND prior.{identity_column} = current.{identity_column}
                        WHERE current.snapshot_id = ?
                          AND prior.{version_column} <> current.{version_column}""",
                    (previous_id, current_id),
                ).fetchone()[0]
            )
        absent = int(
            database.execute(
                "SELECT count(*) FROM source_absences WHERE snapshot_id = ? AND entity_kind = ?",
                (current_id, entity_kind),
            ).fetchone()[0]
        )
        return {"added": added, "changed": changed, "absent": absent}

    @staticmethod
    def _asset_delta(
        database: sqlite3.Connection, current_id: int, previous_id: int | None
    ) -> dict[str, int]:
        if previous_id is None:
            added = int(
                database.execute(
                    "SELECT count(DISTINCT asset_id) FROM asset_observations WHERE snapshot_id = ?",
                    (current_id,),
                ).fetchone()[0]
            )
        else:
            added = int(
                database.execute(
                    """SELECT count(*) FROM (
                         SELECT DISTINCT current.asset_id FROM asset_observations current
                         LEFT JOIN asset_observations prior
                           ON prior.snapshot_id = ? AND prior.asset_id = current.asset_id
                         WHERE current.snapshot_id = ? AND prior.id IS NULL
                       )""",
                    (previous_id, current_id),
                ).fetchone()[0]
            )
        absent = int(
            database.execute(
                "SELECT count(*) FROM source_absences WHERE snapshot_id = ? AND entity_kind = 'asset'",
                (current_id,),
            ).fetchone()[0]
        )
        return {"added": added, "changed": 0, "absent": absent}


class _ReaderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_reader_server(
    database_path: Path,
    vault_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Create a loopback-only reader server; the caller owns its lifecycle."""

    if host != "127.0.0.1":
        raise ReaderError("the archive reader may only bind to 127.0.0.1", code="unsafe_bind")
    try:
        numeric_port = int(port)
    except (TypeError, ValueError) as error:
        raise ReaderError("reader port must be an integer", code="invalid_port") from error
    if not 0 <= numeric_port <= 65535:
        raise ReaderError("reader port is outside the valid range", code="invalid_port")
    repository = ReaderRepository(database_path, vault_root)

    class Handler(_ReaderRequestHandler):
        reader_repository = repository

    try:
        return _ReaderHTTPServer((host, numeric_port), Handler)
    except OSError as error:
        raise ReaderError("the local reader port could not be opened", code="bind_failed") from error


class _ReaderRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    reader_repository: ReaderRepository

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path in _STATIC_FILES:
                self._serve_static(parsed.path)
                return
            if parsed.path == "/api/snapshots":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(
                    self.reader_repository.list_snapshots(
                        limit=_query_int(query, "limit", 100),
                        offset=_query_int(query, "offset", 0),
                    )
                )
                return
            if parsed.path in ("/api/feedback", "/api/bookmarks", "/api/position"):
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                store = AnnotationStore(self.reader_repository.vault_root)
                if parsed.path == "/api/feedback":
                    self._send_json({"items": store.history(_optional_query_text(query, "message_id"))})
                elif parsed.path == "/api/bookmarks":
                    self._send_json({"items": store.bookmarks(_optional_query_text(query, "topic") or None)})
                else:
                    self._send_json({"position": store.position(_optional_query_text(query, "snapshot"), _optional_query_text(query, "conversation"))})
                return
            if parsed.path == "/api/citation":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(self.reader_repository.citation(_optional_query_text(query, "source")))
                return
            if parsed.path == "/api/pi/sessions":
                self._send_json(PiArchive(self.reader_repository.vault_root).sessions())
                return
            if parsed.path.startswith("/api/pi/sessions/"):
                session = urllib.parse.unquote(parsed.path[len("/api/pi/sessions/"):])
                self._send_json(PiArchive(self.reader_repository.vault_root).conversation(session))
                return
            if parsed.path == "/api/library":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(self.reader_repository.library(
                    query=_optional_query_text(query, "q"), limit=_query_int(query, "limit", 50),
                    offset=_query_int(query, "offset", 0)))
                return
            if parsed.path == "/api/conversations":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(
                    self.reader_repository.list_conversations(
                        snapshot_id=_required_query_int(query, "snapshot_id"),
                        limit=_query_int(query, "limit", 50),
                        offset=_query_int(query, "offset", 0),
                    )
                )
                return
            if parsed.path == "/api/group-threads":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(
                    self.reader_repository.list_group_threads(
                        snapshot_id=_required_query_int(query, "snapshot_id"),
                        query=_optional_query_text(query, "q"),
                        limit=_query_int(query, "limit", 50),
                        offset=_query_int(query, "offset", 0),
                    )
                )
                return
            group_prefix = "/api/group-threads/"
            if parsed.path.startswith(group_prefix):
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                identity = urllib.parse.unquote(parsed.path[len(group_prefix) :])
                self._send_json(
                    self.reader_repository.get_group_thread(
                        _required_query_int(query, "snapshot_id"), identity
                    )
                )
                return
            prefix = "/api/conversations/"
            if parsed.path.startswith(prefix):
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                identity = urllib.parse.unquote(parsed.path[len(prefix) :])
                self._send_json(
                    self.reader_repository.get_conversation(
                        _required_query_int(query, "snapshot_id"), identity
                    )
                )
                return
            if parsed.path == "/api/search":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(
                    self.reader_repository.search(
                        snapshot_id=_required_query_int(query, "snapshot_id"),
                        query=_required_query_text(query, "q"),
                        limit=_query_int(query, "limit", 50),
                        offset=_query_int(query, "offset", 0),
                    )
                )
                return
            if parsed.path == "/api/analytics":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(
                    self.reader_repository.analytics(
                        _required_query_int(query, "snapshot_id")
                    )
                )
                return
            asset_prefix = "/api/assets/"
            if parsed.path.startswith(asset_prefix):
                sha256 = urllib.parse.unquote(parsed.path[len(asset_prefix) :])
                self._serve_asset(sha256)
                return
            raise ReaderError(
                "local reader route was not found",
                code="not_found",
                status=HTTPStatus.NOT_FOUND,
            )
        except ReaderError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send_error(
                ReaderError(
                    "the local reader encountered an unexpected error",
                    code="internal_error",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )

    def do_HEAD(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/feedback", "/api/bookmarks", "/api/position"):
            self._method_not_allowed()
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("request body must be application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError("request body must contain 1-1048576 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            store = AnnotationStore(self.reader_repository.vault_root)
            if path == "/api/feedback":
                from .recall import RecallRepository
                result = RecallRepository(self.reader_repository.vault_root).feedback(**payload)
            elif path == "/api/bookmarks":
                source = self.reader_repository.citation(payload["source_id"])
                result = store.bookmark(source["message_id"], payload["source_id"],
                    payload.get("title", ""), topic=payload.get("topic", ""),
                    note=payload.get("note", ""), remove=bool(payload.get("remove")))
            else:
                source = self.reader_repository.citation(payload["source_id"])
                result = store.position(source["snapshot_key"], source["conversation_id"], source["message_id"])
            self._send_json(result)
        except ReaderError as error:
            self._send_error(error)
        except (ValueError, TypeError, KeyError) as error:
            self._send_error(ReaderError(str(error)))

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def log_message(self, _format: str, *_args: object) -> None:
        # Paths can contain search terms and identity keys.  The local server is
        # intentionally silent instead of copying private values to terminal logs.
        return

    def _method_not_allowed(self) -> None:
        self._send_error(
            ReaderError(
                "the archive reader is GET-only",
                code="method_not_allowed",
                status=HTTPStatus.METHOD_NOT_ALLOWED,
            ),
            extra_headers={"Allow": "GET"},
        )

    def _serve_static(self, path: str) -> None:
        filename, content_type = _STATIC_FILES[path]
        target = STATIC_ROOT / filename
        if target.is_symlink() or not target.is_file():
            raise ReaderError(
                "reader static asset is unavailable",
                code="static_missing",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        payload = target.read_bytes()
        self._send_bytes(payload, content_type=content_type, cache_control="no-store")

    def _serve_asset(self, sha256: str) -> None:
        with self.reader_repository.open_asset(sha256) as asset:
            encoded = urllib.parse.quote(asset.download_name, safe="")
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", asset.content_type)
            self.send_header("Content-Length", str(asset.size_bytes))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"attachment\"; filename*=UTF-8''{encoded}",
            )
            self.end_headers()
            for chunk in iter(lambda: asset.file.read(1024 * 1024), b""):
                self.wfile.write(chunk)

    def _send_json(self, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._send_bytes(body, content_type="application/json; charset=utf-8", cache_control="no-store")

    def _send_error(
        self,
        error: ReaderError,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            {"error": {"code": error.code, "message": str(error)}},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(error.status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, payload: bytes, *, content_type: str, cache_control: str) -> None:
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(payload)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")


def _snapshot_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": int(row["id"]),
        "snapshot_key": row["snapshot_key"],
        "captured_at": row["captured_at"],
        "captured_at_us": int(row["captured_at_us"]),
        "imported_at": row["imported_at"],
        "evidence_tree_sha256": row["evidence_tree_sha256"],
        "source": {
            "provider": row["provider"],
            "kind": row["kind"],
            "source_key": row["source_key"],
            "identity_scope": row["identity_scope"],
        },
        "counts": {
            "conversations": int(row["conversations_count"] or 0),
            "nodes": int(row["nodes_count"] or 0),
            "messages": int(row["messages_count"] or 0),
            "group_threads": int(row["group_threads_count"] or 0),
            "group_messages": int(row["group_messages_count"] or 0),
            "warnings": int(row["warnings_count"] or 0),
        },
        "quality": {
            "source_absences": int(row["source_absences"] or 0) if "source_absences" in keys else 0,
            "errors": int(row["errors"] or 0) if "errors" in keys else 0,
        },
    }


def _conversation_list_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "identity_key": row["identity_key"],
        "native_id": row["native_id"],
        "title": row["title"],
        "create_time": row["create_time"],
        "update_time": row["update_time"],
        "preview": row["preview"] or "",
        "current_node_identity_key": row["current_node_identity_key"],
        "absent_in_later_snapshot": bool(row["absent_in_later_snapshot"]),
        "source": {
            "source_file_path": row["source_file_path"],
            "array_index": row["array_index"],
            "json_pointer": row["json_pointer"],
        },
        "stats": {
            "nodes": int(row["nodes"]),
            "messages": int(row["messages"]),
            "branch_points": int(row["branch_points"]),
            "alternative_nodes": int(row["alternative_nodes"]),
            "attachments": int(row["attachments"]),
            "unresolved_assets": int(row["unresolved_assets"]),
        },
    }


def _node_payload(
    row: sqlite3.Row,
    children: Sequence[str],
    attachments: Mapping[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    message = None
    if row["message_observation_id"] is not None:
        message = {
            "source_id": row["message_observation_id"],
            "identity_key": row["message_identity_key"],
            "native_id": row["message_native_id"],
            "role": row["author_role"],
            "name": row["author_name"],
            "content_type": row["content_type"],
            "create_time": row["message_create_time"],
            "update_time": row["message_update_time"],
            "status": row["message_status"],
            "recipient": row["recipient"],
            "text": row["message_text"] or "",
            "model": _message_model(row["message_raw_json"]),
            "attachments": attachments.get(int(row["message_observation_id"]), []),
        }
    return {
        "node_identity_key": row["node_identity_key"],
        "native_id": row["native_id"],
        "parent_node_identity_key": row["parent_node_identity_key"],
        "child_options": sorted(children),
        "depth": row["depth"],
        "json_pointer": row["node_json_pointer"],
        "message": message,
    }


def _message_model(raw_json: Any) -> str | None:
    raw = _parse_json(raw_json, {})
    if not isinstance(raw, dict):
        return None
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    for value in (
        metadata.get("model_slug"),
        metadata.get("model_name"),
        raw.get("model_slug"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _count_rows(database: sqlite3.Connection, sql: str, snapshot_id: int) -> list[dict[str, Any]]:
    return [{"key": row["key"], "count": int(row["count"])} for row in database.execute(sql, (snapshot_id,))]


def _year_activity(activity: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    years: dict[str, dict[str, int]] = {}
    for row in activity:
        month = str(row.get("month") or "")
        if len(month) < 4:
            continue
        bucket = years.setdefault(month[:4], {"messages": 0, "conversations_month_sum": 0})
        bucket["messages"] += int(row.get("messages") or 0)
        bucket["conversations_month_sum"] += int(row.get("conversations") or 0)
    return [{"year": year, **values} for year, values in sorted(years.items())]


def _pagination(limit: int, offset: int, *, maximum: int) -> tuple[int, int]:
    try:
        numeric_limit = int(limit)
        numeric_offset = int(offset)
    except (TypeError, ValueError) as error:
        raise ReaderError("pagination values must be integers", code="invalid_pagination") from error
    if numeric_limit < 1 or numeric_limit > maximum or numeric_offset < 0:
        raise ReaderError(
            f"pagination requires 1 <= limit <= {maximum} and offset >= 0",
            code="invalid_pagination",
        )
    return numeric_limit, numeric_offset


def _pagination_payload(limit: int, offset: int, total: int) -> dict[str, int | None]:
    next_offset = offset + limit if offset + limit < total else None
    return {"limit": limit, "offset": offset, "total": total, "next_offset": next_offset}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _safe_download_name(value: Any, sha256: str) -> str:
    if not isinstance(value, str):
        return sha256
    normalized = value.replace("\\", "/").split("/")[-1]
    normalized = "".join(character for character in normalized if character >= " " and character != "\x7f")
    return normalized[:240] or sha256


def _open_regular_beneath(root: Path, parts: Sequence[str]) -> BinaryIO:
    if not parts or any(part in {"", ".", ".."} or "/" in part for part in parts):
        raise ReaderError("asset storage path is invalid", code="asset_path_invalid")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags | nofollow, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | nofollow, dir_fd=current_fd)
    except (OSError, ValueError) as error:
        raise ReaderError(
            "asset path is missing or crosses an unsafe filesystem boundary",
            code="asset_unavailable",
            status=HTTPStatus.NOT_FOUND,
        ) from error
    finally:
        os.close(current_fd)
    return os.fdopen(file_fd, "rb", closefd=True)


def _query_int(query: Mapping[str, list[str]], key: str, default: int) -> int:
    values = query.get(key)
    if not values:
        return default
    if len(values) != 1:
        raise ReaderError(f"query parameter {key} must appear once", code="invalid_query")
    try:
        return int(values[0])
    except ValueError as error:
        raise ReaderError(f"query parameter {key} must be an integer", code="invalid_query") from error


def _required_query_int(query: Mapping[str, list[str]], key: str) -> int:
    if key not in query:
        raise ReaderError(f"query parameter {key} is required", code="missing_query")
    return _query_int(query, key, 0)


def _required_query_text(query: Mapping[str, list[str]], key: str) -> str:
    values = query.get(key)
    if values is None or len(values) != 1:
        raise ReaderError(f"query parameter {key} is required once", code="missing_query")
    return values[0]


def _optional_query_text(query: Mapping[str, list[str]], key: str) -> str:
    values = query.get(key)
    if values is None:
        return ""
    if len(values) != 1:
        raise ReaderError(f"query parameter {key} must appear once", code="invalid_query")
    return values[0]
