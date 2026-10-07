"""Source-backed grouping of explicit historical statements, without truth promotion."""
from __future__ import annotations

import re
from contextlib import closing
from datetime import datetime, timezone

from .memory_store import _classify


def explicit_statements(text):
    in_code=False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code=not in_code
            continue
        if in_code or line.lstrip().startswith((">", "“", '"')):
            continue
        for sentence in re.split(r"(?<=[。！？.!?])",line):
            sentence=sentence.strip()
            if not 6 <= len(sentence) <= 300 or "?" in sentence or "？" in sentence:
                continue
            if not re.match(r"^(?:我|本人|I\s)",sentence,re.I):
                continue
            kind=_classify(sentence)
            if kind:
                yield kind,sentence


def consolidate(repo, budget_chars=6000):
    from .recall import _MESSAGES, _reader_url
    feedback=repo.annotations.current()
    with closing(repo._connect()) as db:
        rows=db.execute(_MESSAGES+"SELECT * FROM messages WHERE role='user' ORDER BY coalesce(create_time,0) DESC").fetchall()
    grouped={}
    today=datetime.now(timezone.utc).date().isoformat()
    for row in rows:
        item=dict(row);correction=feedback.get(item["message_id"],{})
        if correction.get("action") in ("excluded","quoted","corrected"):
            continue
        for kind,quote in explicit_statements(item["text"]):
            key=(kind," ".join(quote.casefold().split()))
            source={"message_id":item["message_id"],"conversation_id":item["conversation_id"],
                    "title":item["title"],"stated_at":item["create_time"],"reader_url":_reader_url(item)}
            if correction and correction.get("action")!="restored":
                source["feedback"]=correction
            group=grouped.setdefault(key,{"kind":kind,"statement":quote,"sources":[],
                "status":"historical_unreviewed","time_meaning":"当时的陈述；不推定为当前状态"})
            group["sources"].append(source)
    result=[];remaining=budget_chars
    for group in grouped.values():
        if len(group["statement"])>remaining:continue
        remaining-=len(group["statement"])
        group["source_count"]=len(group["sources"])
        group["sources"]=group["sources"][:3]
        result.append(group)
        if len(result)>=30:break
    return {"as_of":today,"groups":result,"statement_characters":budget_chars-remaining,
            "interpretation":"自动合并相同原话及其来源；不同陈述保留分开，不以时间较新自动覆盖旧陈述。"}
