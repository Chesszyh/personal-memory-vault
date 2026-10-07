"""Evidence retrieval for agents, independent of the memory review queue."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from .reader import ReaderError, connect_reader_database
from .annotations import AnnotationStore
from .pi_archive import PiArchive


# Retain absent conversations at their last observation instead of inferring deletion.
# CROSS JOIN keeps SQLite on indexed snapshot/node lookups; starting at messages
# otherwise joins every matching message against every conversation observation.
_MESSAGES = """
WITH observations AS (
 SELECT co.*, s.snapshot_key, s.captured_at, so.identity_scope,
        row_number() OVER (PARTITION BY co.conversation_identity_id
          ORDER BY s.captured_at_us DESC, s.id DESC) AS observation_rank
 FROM conversation_observations co
 JOIN snapshots s ON s.id = co.snapshot_id
 JOIN sources so ON so.id = s.source_id
), messages AS (
 SELECT o.snapshot_id, o.snapshot_key, o.captured_at, o.identity_scope,
        ci.identity_key AS conversation_id, ci.native_id, cv.title,
        mi.identity_key AS message_id, mo.id AS source_id, cb.depth, mv.author_role AS role,
        mv.create_time, sd.id AS document_id, sd.content AS text
 FROM observations o
 JOIN conversation_identities ci ON ci.id = o.conversation_identity_id
 JOIN conversation_versions cv ON cv.id = o.conversation_version_id
 CROSS JOIN current_branch_nodes cb ON cb.snapshot_id = o.snapshot_id
   AND cb.conversation_identity_id = o.conversation_identity_id
 CROSS JOIN node_observations no ON no.snapshot_id = cb.snapshot_id
   AND no.node_identity_id = cb.node_identity_id
 JOIN message_observations mo ON mo.snapshot_id = no.snapshot_id
   AND mo.message_identity_id = no.message_identity_id
 JOIN message_identities mi ON mi.id = mo.message_identity_id
 JOIN message_versions mv ON mv.id = mo.message_version_id
 JOIN search_documents sd ON sd.message_version_id = mv.id
 WHERE o.observation_rank = 1 AND mv.author_role IN ('user', 'assistant')
   AND coalesce(json_extract(mv.raw_json, '$.metadata.is_visually_hidden_from_conversation'), 0) = 0
   AND coalesce(json_extract(mv.raw_json, '$.channel'), 'final') = 'final'
   AND coalesce(mv.recipient, 'all') = 'all'
 UNION ALL
 SELECT 0, 'pi/' || ps.id, ps.updated_at, 'local-pi', 'pi/' || ps.id, ps.id, ps.title,
        'pi/' || ps.id || '/' || pm.entry_id, 'pi:' || pm.id, pm.depth, pm.role,
        pm.create_time, -pm.id, pm.text
 FROM pi_archive.messages pm JOIN pi_archive.sessions ps ON ps.id=pm.session_id
 WHERE pm.active=1
)
"""


def _bound(value: int, low: int, high: int, name: str) -> int:
    if not low <= value <= high:
        raise ReaderError(f"{name} must be between {low} and {high}")
    return value


def _clip(text: str, query: str, size: int) -> dict:
    position = text.casefold().find(query.casefold()) if query else 0
    start = max(0, position - size // 4)
    return {"text": text[start:start + size], "text_offset": start,
            "text_length": len(text), "truncated": start > 0 or len(text) > size}


def _reader_url(item: dict) -> str:
    base = os.environ.get("PERSONAL_VAULT_READER_URL", "http://127.0.0.1:8767").rstrip("/")
    return base + "/?" + urlencode({"source": item["source_id"]})


def _provenance(item: dict) -> None:
    assistant = item["role"] == "assistant"
    item["evidence_status"] = "assistant_response" if assistant else "user_message"
    item["citation_label"] = ("历史助手总结：" if assistant else "历史用户消息：") + item["title"]
    item["evidence_note"] = (
        "这是历史助手回复，只能引用为助手总结或转述；不是原始邮件、日志或用户自述。"
        if assistant else "这是用户发送的历史消息；仍需区分本人陈述与粘贴引用，不能据此认定为当前事实。")


class RecallRepository:
    def __init__(self, vault_root: Path):
        self.root = Path(vault_root).expanduser().resolve(strict=True)
        self.annotations = AnnotationStore(self.root)

    def _connect(self):
        pi = PiArchive(self.root)
        pi.connect().close()
        db = connect_reader_database(self.root / "canonical" / "archive.sqlite")
        db.execute("ATTACH DATABASE ? AS pi_archive", (f"{pi.path.resolve().as_uri()}?mode=ro",))
        return db

    def search(self, query: str, *, limit: int = 8, role: str = "user",
               budget_chars: int = 6000, per_conversation: int = 1) -> dict:
        _bound(budget_chars, 500, 40000, "budget_chars")
        _bound(per_conversation, 1, 20, "per_conversation")
        query = query.strip()
        if not query or len(query) > 512:
            raise ReaderError("query must contain 1-512 characters")
        _bound(limit, 1, 20, "limit")
        if role not in ("user", "assistant", "all"):
            raise ReaderError("role must be user, assistant or all")
        literal = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        parameters: list = []
        if len(query) >= 3:
            match = "(document_id IN (SELECT rowid FROM message_fts WHERE message_fts MATCH ?) OR (document_id < 0 AND text LIKE ? ESCAPE '\\'))"
            parameters += ['"' + query.replace('"', '""') + '"', literal]
        else:
            match = "text LIKE ? ESCAPE '\\'"
            parameters.append(literal)
        feedback = self.annotations.current()
        excluded = [key for key, value in feedback.items() if value["action"] == "excluded"]
        exclusion = " AND message_id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
        parameters += [literal, role, role, *excluded, query.casefold(), per_conversation, limit]
        sql = _MESSAGES + f"""
 , matched AS (SELECT * FROM messages WHERE ({match} OR title LIKE ? ESCAPE '\\')
 AND (? = 'all' OR role = ?){exclusion}), ranked AS (
 SELECT *, row_number() OVER (PARTITION BY conversation_id ORDER BY
   CASE WHEN instr(lower(text), ?) > 0 THEN 0 ELSE 1 END,
   CASE role WHEN 'user' THEN 0 ELSE 1 END,
   coalesce(create_time, 0) DESC, message_id) AS conversation_rank FROM matched)
 SELECT * FROM ranked WHERE conversation_rank <= ?
 ORDER BY coalesce(create_time, 0) DESC, message_id LIMIT ?
"""
        db = self._connect()
        try:
            rows = db.execute(sql, parameters).fetchall()
        finally:
            db.close()
        items = []
        remaining = budget_chars
        for row in rows:
            if remaining <= 0:
                break
            item = dict(row)
            item.pop("document_id")
            item.pop("conversation_rank", None)
            item.update(_clip(item.pop("text"), query, min(1400, remaining)))
            remaining -= len(item["text"])
            _provenance(item)
            self._annotate(item, feedback)
            item["reader_url"] = _reader_url(item)
            items.append(item)
        return {"query": query, "role": role, "items": items,
                "limit": limit, "budget_chars": budget_chars, "per_conversation": per_conversation,
                "returned_text_characters": budget_chars - remaining, "scope": "latest_observed_current_branch"}

    def context(self, conversation_id: str, *, message_id: str | None = None,
                offset: int = 0, limit: int = 8, text_offset: int = 0, budget_chars: int = 6000) -> dict:
        _bound(limit, 1, 20, "limit")
        _bound(budget_chars, 500, 40000, "budget_chars")
        _bound(offset, 0, 1000000, "offset")
        _bound(text_offset, 0, 10000000, "text_offset")
        db = self._connect()
        try:
            rows = db.execute(_MESSAGES + "SELECT * FROM messages WHERE conversation_id = ? ORDER BY depth",
                              (conversation_id,)).fetchall()
        finally:
            db.close()
        if not rows:
            raise ReaderError("conversation has no visible messages or does not exist")
        anchor = None
        if message_id:
            anchor = next((i for i, r in enumerate(rows) if r["message_id"] == message_id), None)
            if anchor is None:
                raise ReaderError("message was not found on the current branch")
            offset = max(0, anchor - min(2, limit // 2))
        items = []
        feedback = self.annotations.current()
        selected = rows[offset:offset + limit]
        anchor_size = min(4000, budget_chars) if anchor is not None else 0
        neighbor_count = len(selected) - (1 if anchor is not None else 0)
        neighbor_size = min(4000, (budget_chars - anchor_size) // max(1, neighbor_count))
        for index, row in enumerate(selected, offset):
            size = anchor_size if index == anchor else neighbor_size
            item = dict(row)
            item.pop("document_id")
            text = item.pop("text")
            item.update(text=text[text_offset:text_offset + size], text_offset=text_offset,
                        text_length=len(text), truncated=text_offset > 0 or len(text) > text_offset + size,
                        next_text_offset=text_offset + size if size and len(text) > text_offset + size else None)
            _provenance(item)
            self._annotate(item, feedback)
            item["reader_url"] = _reader_url(item)
            items.append(item)
        return {"items": items, "total": len(rows), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "source_kind": "historical_conversation", "budget_chars": budget_chars,
                "returned_text_characters": sum(len(i["text"]) for i in items)}

    def _annotate(self, item, feedback):
        correction = feedback.get(item["message_id"])
        item["stated_at"] = item.get("create_time")
        item["temporal_status"] = "historical_unverified"
        if not correction or correction["action"] == "restored":
            return
        item["feedback"] = correction
        item["valid_from"] = correction["valid_from"]
        item["valid_until"] = correction["valid_until"]
        item["assertion_kind"] = correction["assertion_kind"]
        today = date.today().isoformat()
        if correction["valid_until"] and correction["valid_until"] < today:
            item["temporal_status"] = "expired"
        elif correction["valid_from"] and correction["valid_from"] > today:
            item["temporal_status"] = "future"
        elif correction["valid_from"] or correction["valid_until"]:
            item["temporal_status"] = "within_user_stated_period"
        if correction["action"] == "outdated":
            item["temporal_status"] = "outdated"
        if correction["action"] == "excluded":
            item.update(text="", withheld=True, next_text_offset=None)
        elif correction["action"] == "quoted":
            item["evidence_status"] = "quoted_material"
            item["citation_label"] = "用户标明的引用材料：" + item["title"]
            item["evidence_note"] = "用户明确标记为引用材料，不能作为用户本人的经历。"
        elif correction["action"] in ("outdated", "corrected"):
            item["evidence_status"] = correction["action"]
            item["evidence_note"] = "用户已标记过时或纠正，只可描述当时记录；以反馈与替代消息为准。"

    def _source(self, message_id):
        with closing(self._connect()) as db:
            row = db.execute(_MESSAGES + "SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise ReaderError("message was not found in visible history")
        return dict(row)

    def feedback(self, message_id, action, **fields):
        self._source(message_id)
        replacement = fields.get("replacement_message_id")
        if replacement:
            self._source(replacement)
        return self.annotations.feedback(message_id, action, **fields)

    def remember(self, message_id, quote, kind="statement"):
        source = self._source(message_id)
        if source["role"] != "user" or not quote.strip() or quote not in source["text"]:
            raise ReaderError("memory requires an exact quote from a visible user message")
        return self.annotations.remember(message_id, quote, kind)

    def profile(self) -> dict:
        path = self.root / "memory" / "memory.sqlite"
        items = []
        if path.exists():
            db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute("""SELECT candidate_id, kind,
                  coalesce(reviewed_statement, statement) AS statement,
                  source_snapshot_key, source_conversation_identity_key AS conversation_id,
                  source_message_identity_key AS message_id, message_create_time,
                  status FROM candidates WHERE status = 'confirmed' ORDER BY kind, candidate_id""").fetchall()
                items = [dict(r) for r in rows]
            finally:
                db.close()
        for assertion in self.annotations.assertions():
            try:
                source = self._source(assertion["message_id"])
            except ReaderError:
                continue
            items.append({"statement": assertion["quote"], "kind": assertion["kind"],
                          "message_id": source["message_id"], "conversation_id": source["conversation_id"],
                          "source_snapshot_key": source["snapshot_key"], "message_create_time": source["create_time"],
                          "status": "user_requested", "reader_url": _reader_url(source)})
        feedback = self.annotations.current()
        current = []
        historical = []
        seen = set()
        for item in items:
            key = (item["message_id"], item["statement"])
            if key in seen:
                continue
            seen.add(key)
            try:
                source = self._source(item["message_id"])
            except ReaderError:
                continue
            item["reader_url"] = _reader_url(source)
            item["create_time"] = source["create_time"]
            self._annotate(item, feedback)
            correction = feedback.get(item["message_id"])
            if correction and correction["action"] != "restored":
                if correction["action"] == "excluded":
                    continue
                item["feedback"] = correction
                historical.append(item)
            else:
                current.append(item)
        from .profile import consolidate
        return {"items": current, "historical_items": historical, "observed": consolidate(self),
                "scope": "confirmed_or_user_requested_with_source", "as_of": date.today().isoformat()}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Retrieve source-linked personal history for agents.")
    parser.add_argument("vault_root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--role", choices=("user", "assistant", "all"), default="user")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--budget-chars", type=int, default=6000)
    search.add_argument("--per-conversation", type=int, default=1)
    context = commands.add_parser("context")
    context.add_argument("conversation_id")
    context.add_argument("--message-id")
    context.add_argument("--offset", type=int, default=0)
    context.add_argument("--limit", type=int, default=8)
    context.add_argument("--text-offset", type=int, default=0)
    context.add_argument("--budget-chars", type=int, default=6000)
    feedback = commands.add_parser("feedback")
    feedback.add_argument("message_id")
    feedback.add_argument("action", choices=("outdated", "quoted", "excluded", "corrected", "restored"))
    feedback.add_argument("--note", default="")
    feedback.add_argument("--valid-from")
    feedback.add_argument("--valid-until")
    feedback.add_argument("--assertion-kind", default="statement")
    feedback.add_argument("--replacement-message-id")
    remember = commands.add_parser("remember")
    remember.add_argument("message_id")
    remember.add_argument("quote")
    remember.add_argument("--kind", default="statement")
    commands.add_parser("profile")
    args = vars(parser.parse_args(argv))
    root, command = args.pop("vault_root"), args.pop("command")
    try:
        result = getattr(RecallRepository(root), command)(**args)
    except (ValueError, ReaderError, OSError, sqlite3.Error) as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
