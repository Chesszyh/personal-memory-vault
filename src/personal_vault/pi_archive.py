"""Append-only Pi session evidence with an explicit active branch projection."""
from __future__ import annotations
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


class PiArchive:
    def __init__(self, root):
        self.root=Path(root)
        self.path=self.root/"canonical"/"pi.sqlite"

    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row
        db.executescript("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, filename TEXT, title TEXT, leaf_id TEXT, updated_at TEXT, header_json TEXT);
          CREATE TABLE IF NOT EXISTS entries (
            session_id TEXT, id TEXT, parent_id TEXT, raw_json TEXT,
            PRIMARY KEY(session_id,id));
          CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, session_id TEXT, entry_id TEXT, role TEXT, text TEXT,
            create_time REAL, depth INTEGER, active INTEGER DEFAULT 0,
            UNIQUE(session_id,entry_id));
          CREATE INDEX IF NOT EXISTS pi_active ON messages(active,session_id);""")
        return db

    def ingest(self, filename, leaf_id=None):
        path=Path(filename).expanduser().resolve(strict=True)
        data=path.read_bytes()
        # Pi appends JSONL; the last incomplete line belongs to the next import.
        complete=data[:data.rfind(b"\n")+1]
        records=[json.loads(line) for line in complete.splitlines() if line.strip()]
        if not records or records[0].get("type")!="session":
            raise ValueError("input must be a Pi session JSONL")
        header=records[0];session=header["id"]
        entries={r["id"]:r for r in records[1:] if "id" in r}
        leaf_id=leaf_id or (next(reversed(entries)) if entries else None)
        branch=[];seen=set();cursor=leaf_id
        while cursor:
            if cursor in seen or cursor not in entries:
                raise ValueError("Pi session branch is incomplete or cyclic")
            seen.add(cursor);branch.append(cursor);cursor=entries[cursor].get("parentId")
        branch.reverse();depths={entry:i for i,entry in enumerate(branch)}
        inserted=0
        with closing(self.connect()) as db,db:
            for key,entry in entries.items():
                raw=json.dumps(entry,ensure_ascii=False,separators=(",",":"))
                old=db.execute("SELECT raw_json FROM entries WHERE session_id=? AND id=?",(session,key)).fetchone()
                if old and old[0]!=raw:
                    raise ValueError("an archived Pi entry changed; retain the original and import a separate session")
                db.execute("INSERT OR IGNORE INTO entries VALUES(?,?,?,?)",(session,key,entry.get("parentId"),raw))
                message=entry.get("message",{})
                if entry.get("type")!="message" or message.get("role") not in ("user","assistant"):
                    continue
                content=message.get("content",[])
                text=content if isinstance(content,str) else "\n".join(c.get("text","") for c in content if c.get("type")=="text")
                if not text.strip():continue
                time=message.get("timestamp")
                created=float(time)/1000 if isinstance(time,(int,float)) else datetime.fromisoformat(entry["timestamp"].replace("Z","+00:00")).timestamp()
                cur=db.execute("INSERT OR IGNORE INTO messages(session_id,entry_id,role,text,create_time,depth,active) VALUES(?,?,?,?,?,0,0)",
                               (session,key,message["role"],text,created))
                inserted+=cur.rowcount
            db.execute("UPDATE messages SET active=0 WHERE session_id=?",(session,))
            db.executemany("UPDATE messages SET active=1,depth=? WHERE session_id=? AND entry_id=?",[(depth,session,key) for key,depth in depths.items()])
            first=db.execute("SELECT text FROM messages WHERE session_id=? AND active=1 AND role='user' ORDER BY depth LIMIT 1",(session,)).fetchone()
            title=next((e.get("name") for e in reversed(list(entries.values())) if e.get("type")=="session_info" and e.get("name")),None) or (first[0][:80] if first else "Pi 会话")
            updated=entries.get(leaf_id,header).get("timestamp",header["timestamp"])
            db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",(session,str(path),title,leaf_id,updated,json.dumps(header,ensure_ascii=False)))
        return {"session_id":session,"inserted_messages":inserted,"active_entries":len(branch),"leaf_id":leaf_id}

    def citation(self, source_id):
        with closing(self.connect()) as db:
            row=db.execute("SELECT m.*,s.title,s.updated_at FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=?",(source_id,)).fetchone()
        if row is None:raise ValueError("Pi source was not found")
        return {"snapshot_key":"pi/"+row["session_id"],"snapshot_id":"pi/"+row["session_id"],
                "conversation_id":"pi/"+row["session_id"],"message_id":"pi/"+row["session_id"]+"/"+row["entry_id"],
                "title":row["title"],"captured_at":row["updated_at"],"source_kind":"pi_session"}

    def sessions(self):
        with closing(self.connect()) as db:
            return {"items":[dict(r) for r in db.execute("""SELECT s.id,s.title,s.updated_at,
                (SELECT 'pi:' || m.id FROM messages m WHERE m.session_id=s.id AND m.active=1 ORDER BY m.depth LIMIT 1) AS source_id
                FROM sessions s ORDER BY s.updated_at DESC""")]}

    def conversation(self, session):
        with closing(self.connect()) as db:
            head=db.execute("SELECT * FROM sessions WHERE id=?",(session,)).fetchone()
            rows=db.execute("SELECT * FROM messages WHERE session_id=? AND active=1 ORDER BY depth",(session,)).fetchall()
        if head is None:raise ValueError("Pi session was not found")
        nodes=[]
        for row in rows:
            key="pi/"+session+"/"+row["entry_id"]
            nodes.append({"node_identity_key":key,"depth":row["depth"],"child_options":[],
                "message":{"identity_key":key,"source_id":"pi:"+str(row["id"]),"role":row["role"],
                           "text":row["text"],"create_time":row["create_time"],"attachments":[]}})
        return {"snapshot":{"id":"pi/"+session,"snapshot_key":"pi/"+session,"captured_at":head["updated_at"],
                            "source":{"kind":"pi_session","identity_scope":"local-pi"}},
                "conversation":{"identity_key":"pi/"+session,"native_id":session,"title":head["title"],
                                "current_branch":nodes,"alternative_nodes":[],"stats":{"messages":len(nodes)}}}


def main(argv=None):
    parser=argparse.ArgumentParser(description="Archive Pi session source and project its selected branch.")
    parser.add_argument("vault_root",type=Path);parser.add_argument("session",type=Path)
    parser.add_argument("--leaf")
    args=parser.parse_args(argv)
    print(json.dumps(PiArchive(args.vault_root).ingest(args.session,args.leaf),ensure_ascii=False))


if __name__=="__main__":main()
