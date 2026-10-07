"""Repeatable retrieval evaluation against source-pinned evidence."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .reader import ReaderError
from .recall import RecallRepository, _MESSAGES


def source_key(item):
    return tuple(item[k] for k in ("snapshot_key", "conversation_id", "message_id"))


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a nonempty list")
    ids = set()
    for case in cases:
        for field in ("id", "question", "category", "answer_guidance"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f"case requires nonempty {field}")
        if case["id"] in ids:
            raise ValueError("duplicate case id: " + case["id"])
        ids.add(case["id"])
        queries = case.get("queries")
        if not isinstance(queries, list) or not queries or any(
            not isinstance(q, str) or not 1 <= len(q.strip()) <= 512 for q in queries
        ):
            raise ValueError(case["id"] + ": queries must contain literal search terms")
        evidence = case.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(case["id"] + ": evidence must be a list (empty for no-record cases)")
        for item in evidence:
            for field in ("snapshot_key", "conversation_id", "message_id", "quote"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise ValueError(case["id"] + f": evidence requires {field}")
        if len({source_key(e) for e in evidence}) != len(evidence):
            raise ValueError(case["id"] + ": duplicate evidence source")


def evaluate(root: Path, cases: list, *, mode="keyword", limit=8, context_limit=4):
    validate_cases(cases)
    if mode not in ("keyword", "no-memory"):
        raise ValueError("mode must be keyword or no-memory")
    if not 1 <= limit <= 20 or not 1 <= context_limit <= 20:
        raise ValueError("limit and context_limit must be between 1 and 20")
    repo = RecallRepository(root)
    db = repo._connect()
    try:
        snapshots = [dict(row) for row in db.execute(
            "SELECT snapshot_key, captured_at FROM snapshots ORDER BY id")]
        # Check anchors independently of search so stale labels cannot look like misses.
        for case in cases:
            for evidence in case["evidence"]:
                rows = db.execute(_MESSAGES + """SELECT * FROM messages
                    WHERE snapshot_key=? AND conversation_id=? AND message_id=?""",
                    source_key(evidence)).fetchall()
                if not any(r["role"] == "user" and evidence["quote"] in r["text"] for r in rows):
                    raise ValueError(case["id"] + ": evidence is absent, changed, or not a visible user statement")
    finally:
        db.close()
    results = []
    for case in cases:
        started = time.perf_counter()
        searches, contexts, seen = [], [], set()
        if mode == "keyword":
            for query in case["queries"]:
                tick = time.perf_counter()
                result = repo.search(query, limit=limit)
                searches.append({"elapsed_seconds": time.perf_counter() - tick, **result})
                for item in result["items"]:
                    key = source_key(item)
                    if key not in seen:
                        seen.add(key)
                        contexts.append(repo.context(item["conversation_id"],
                            message_id=item["message_id"], limit=context_limit))
        retrieved = [item for s in searches for item in s["items"]]
        context_items = [item for c in contexts for item in c["items"]]
        def quote_present(evidence, items):
            return any(source_key(item) == source_key(evidence) and
                       evidence["quote"] in item["text"] for item in items)
        anchors = [{**e, "search_hit": source_key(e) in seen,
                    "search_quote_visible": quote_present(e, retrieved),
                    "context_quote_visible": quote_present(e, context_items)} for e in case["evidence"]]
        results.append({**case, "evidence": anchors,
            "source_recall": (sum(e["search_hit"] for e in anchors) / len(anchors)) if anchors else None,
            "empty_result": not retrieved,
            "search_calls": len(searches), "context_calls": len(contexts),
            "returned_text_characters": sum(len(i["text"]) for i in retrieved + context_items),
            "elapsed_seconds": time.perf_counter() - started,
            "searches": searches, "contexts": contexts,
            "answer_review": {"status": "not_run", "answer": None,
                              "correct_use_of_evidence": None, "notes": None}})
    positive = [r for r in results if r["evidence"]]
    negative = [r for r in results if not r["evidence"]]
    return {"created_at": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "scope": "latest_observed_current_branch", "snapshots": snapshots,
            "settings": {"limit": limit, "context_limit": context_limit},
            "model_usage": {"calls": 0, "tokens": 0},
            "summary": {"cases": len(results),
                "all_sources_found": sum(r["source_recall"] == 1 for r in positive),
                "positive_cases": len(positive),
                "empty_no_record_cases": sum(r["empty_result"] for r in negative),
                "no_record_cases": len(negative)}, "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate retrieval against source-pinned questions; no model calls.")
    parser.add_argument("vault_root", type=Path)
    parser.add_argument("cases", type=Path, help="JSON file containing a cases list")
    parser.add_argument("--output", type=Path, required=True, help="New report path; existing files are preserved")
    parser.add_argument("--mode", choices=("keyword", "no-memory"), default="keyword")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--context-limit", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
        report = evaluate(args.vault_root, cases, mode=args.mode, limit=args.limit,
                          context_limit=args.context_limit)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (ValueError, KeyError, TypeError, OSError, ReaderError, sqlite3.Error) as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
