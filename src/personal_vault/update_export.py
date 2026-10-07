"""One-command import of a dated ChatGPT export directory or ZIP."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import shutil
import tempfile
import zipfile
from contextlib import closing

from .memory_store import scan_memory_candidates
from .workflow import update_chatgpt_official


def export_date(path: Path) -> str:
    match = re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
    if not match:
        raise ValueError("导出目录名不是 YYYY-MM-DD，请用 --date 指定导出日期。")
    return date.fromisoformat(match.group()).isoformat()


def latest_export(root: Path) -> Path:
    candidates = []
    for path in root.iterdir():
        if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name):
            export_date(path)
            if (path / "conversations.json").is_file() or any(path.glob("conversations-*.json")):
                candidates.append(path)
    if not candidates:
        raise ValueError(f"未找到日期命名的导出目录：{root}")
    return max(candidates, key=lambda p: p.name)


def extract_export(archive: Path, vault: Path, source_id: str) -> Path:
    destination = vault / "evidence" / "zip" / source_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    stat = archive.stat()
    identity = {"path": str(archive), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    marker = destination.parent / (source_id + ".zip-input.json")
    if destination.exists():
        if not marker.exists() or json.loads(marker.read_text()) != identity:
            raise ValueError("该快照已有不同的 ZIP 解压来源，请用 --source-id 指定新快照名称。")
    else:
        temporary = Path(tempfile.mkdtemp(prefix="extract-", dir=destination.parent))
        try:
            with zipfile.ZipFile(archive) as bundle:
                for info in bundle.infolist():
                    target = (temporary / info.filename).resolve()
                    if not target.is_relative_to(temporary):
                        raise ValueError("ZIP 包含越过解压目录的文件名。")
                bundle.extractall(temporary)
            roots = [temporary] + [p for p in temporary.iterdir() if p.is_dir()]
            if sum((p / "conversations.json").is_file() or any(p.glob("conversations-*.json")) for p in roots) != 1:
                raise ValueError("ZIP 应包含一个 conversations 导出目录。")
            marker.write_text(json.dumps(identity, ensure_ascii=False, indent=2))
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    roots = [destination] + [p for p in destination.iterdir() if p.is_dir()]
    matches = [p for p in roots if (p / "conversations.json").is_file() or any(p.glob("conversations-*.json"))]
    if len(matches) != 1:
        raise ValueError("ZIP 应包含一个 conversations 导出目录，请检查解压内容。")
    return matches[0]


def conversation_versions(vault, scope):
    path = vault / "canonical" / "archive.sqlite"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute("""WITH ranked AS (SELECT ci.identity_key, cv.title, co.conversation_version_id,
            row_number() OVER (PARTITION BY ci.id ORDER BY s.captured_at_us DESC,s.id DESC) AS rank
            FROM conversation_observations co JOIN conversation_identities ci ON ci.id=co.conversation_identity_id
            JOIN conversation_versions cv ON cv.id=co.conversation_version_id
            JOIN snapshots s ON s.id=co.snapshot_id JOIN sources so ON so.id=s.source_id
            WHERE so.identity_scope=?) SELECT identity_key,title,conversation_version_id FROM ranked WHERE rank=1""", (scope,)).fetchall()
    return {row[0]: {"title": row[1], "version": row[2]} for row in rows}


def main(argv=None) -> int:
    home = Path.home()
    archive_root = home / "Documents/AI对话归档/ChatGPT-chat-history-export"
    parser = argparse.ArgumentParser(description="增量导入 ChatGPT 全量导出并更新记忆候选；全程本地运行，不调用模型。")
    parser.add_argument("export", nargs="?", type=Path, help="导出目录或 ZIP；省略时选择导出根目录下最新日期目录。")
    parser.add_argument("--export-root", type=Path, default=archive_root, help="自动选择导出时扫描的根目录。")
    parser.add_argument("--vault-root", type=Path, default=archive_root.parent / "personal-memory-vault")
    parser.add_argument("--date", help="导出日期 YYYY-MM-DD；默认从目录名读取。")
    parser.add_argument("--identity-scope", default="new-chatgpt-account", help="账号身份范围；本机默认新账号。")
    parser.add_argument("--source-id", help="快照名称；默认 chatgpt-new-account-日期。同日期的另一份导出可显式指定新名称。")
    args = parser.parse_args(argv)
    try:
        evidence = (args.export or latest_export(args.export_root.expanduser())).expanduser().resolve(strict=True)
        captured = date.fromisoformat(args.date).isoformat() if args.date else export_date(evidence.with_suffix("") if evidence.is_file() else evidence)
        prefix = "chatgpt-new-account" if args.identity_scope == "new-chatgpt-account" else f"chatgpt-{args.identity_scope}"
        source_id = args.source_id or f"{prefix}-{captured}"
        vault = args.vault_root.expanduser().resolve()
        if evidence.is_file():
            if evidence.suffix.lower() != ".zip":
                raise ValueError("仅支持导出目录或 ZIP 文件。")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", source_id):
                raise ValueError("source-id 必须是安全文件名。")
            print("阶段：解压 ZIP", flush=True)
            evidence = extract_export(evidence, vault, source_id)
        before = conversation_versions(vault, args.identity_scope)
        print(f"导出：{evidence}\n档案：{vault}\n快照：{source_id}\n正在校验、增量导入并验收，大导出可能需要数分钟…", flush=True)
        result = update_chatgpt_official(evidence_root=evidence, vault_root=vault,
            identity_scope=args.identity_scope, source_id=source_id, captured_at=captured,
            progress=lambda phase: print("阶段：" + phase, flush=True))
        if not result.validation.ok:
            print(f"归档验收未通过，请查看：{result.report_path}", flush=True)
            return 1
        with closing(sqlite3.connect(f"{result.import_result.database_path.as_uri()}?mode=ro", uri=True)) as db:
            row = db.execute("""SELECT s.id FROM snapshots s JOIN sources so ON so.id=s.source_id
              WHERE s.snapshot_key=? AND so.identity_scope=?""", (source_id, args.identity_scope)).fetchone()
            observed = {r[0] for r in db.execute("""SELECT ci.identity_key FROM conversation_observations co
                JOIN conversation_identities ci ON ci.id=co.conversation_identity_id WHERE co.snapshot_id=?""", (row[0],))}
        print("归档验收通过，正在更新记忆候选…", flush=True)
        scan = scan_memory_candidates(vault_root=vault, snapshot_id=row[0])
        after = conversation_versions(vault, args.identity_scope)
        changes = {"added": [{"conversation_id": key, "title": value["title"]} for key, value in after.items() if key not in before],
                   "updated": [{"conversation_id": key, "title": value["title"]} for key, value in after.items() if key in before and before[key]["version"] != value["version"]],
                   "not_present": [{"conversation_id":key,"title":value["title"]} for key,value in before.items() if key not in observed]}
        unchanged = sum(key in observed and key in before and before[key]["version"] == value["version"] for key,value in after.items())
        changes_path = result.report_path.with_name(result.report_path.stem + "-changes.json")
        changes_path.write_text(json.dumps(changes, ensure_ascii=False, indent=2))
        summary = {"added_conversations": len(changes["added"]), "updated_conversations": len(changes["updated"]),
            "unchanged_conversations":unchanged, "not_present_conversations":len(changes["not_present"]),
            "changes_report": str(changes_path), "import_status": result.import_result.status, "snapshot_id": row[0],
            "conversations": result.import_result.conversations, "messages": result.import_result.messages,
            "warnings": result.import_result.warnings, "new_candidates": scan.inserted_candidates,
            "existing_candidates": scan.existing_candidates, "report": str(result.report_path)}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("完成。刷新阅读工作台即可查看；Pi 下次检索会直接读取新档案。无需逐条审核。")
        return 0
    except Exception as error:
        parser.exit(1, f"更新失败：{error}\n修正上述原因后，使用相同命令重试；已完成的快照会复用。\n")


if __name__ == "__main__":
    raise SystemExit(main())
