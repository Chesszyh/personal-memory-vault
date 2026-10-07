"""Local semantic index with source-pinned chunks and cross-encoder reranking."""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .recall import RecallRepository, _MESSAGES, _provenance, _reader_url
from .reader import ReaderError

MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANKER = "BAAI/bge-reranker-large"


def chunks(text, size=600, overlap=100):
    for offset in range(0, len(text), size-overlap):
        yield offset, text[offset:offset+size]
        if offset+size >= len(text):
            break


def fuse_ranks(rows, rerank_scores):
    """Keep both rankings because the cross-encoder can demote useful source chunks."""
    order=sorted(range(len(rows)),key=lambda i:float(rerank_scores[i]),reverse=True)
    ranks={index:rank for rank,index in enumerate(order,1)}
    return sorted([(row,1/(60+index)+1/(60+ranks[index-1]))
                   for index,(row,_) in enumerate(rows,1)],key=lambda pair:pair[1],reverse=True)


class SemanticIndex:
    def __init__(self, root: Path, *, model=MODEL, device=None):
        self.root = Path(root)
        self.path = self.root / "derived" / "semantic.sqlite"
        self.model_name = model
        self.device = device
        self._encoder = None
        self._reranker = None

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS chunks(message_id TEXT, document_id INTEGER, offset INTEGER,
            title TEXT, text TEXT, vector BLOB, PRIMARY KEY(message_id, document_id, offset));""")
        saved = db.execute("SELECT value FROM metadata WHERE key='model'").fetchone()
        if saved and saved[0] != self.model_name:
            db.close()
            raise ReaderError("semantic index model differs; rebuild in a new index with the intended model")
        db.execute("INSERT OR IGNORE INTO metadata VALUES('model',?)", (self.model_name,))
        db.commit()
        return db

    def encoder(self):
        if self._encoder is None:
            import torch
            from sentence_transformers import SentenceTransformer
            device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            self._encoder = SentenceTransformer(self.model_name, device=device, local_files_only=True,
                                                model_kwargs={"dtype": torch.float16 if device == "cuda" else torch.float32},
                                                prompts={"query": "Instruct: Retrieve personal conversation passages relevant to the question.\nQuery: ", "document": ""})
            self._encoder.max_seq_length = 768
        return self._encoder

    def build(self, progress=None):
        import numpy as np
        repo = RecallRepository(self.root)
        with closing(repo._connect()) as archive:
            rows = [dict(r) for r in archive.execute(_MESSAGES + "SELECT * FROM messages WHERE role='user' AND length(trim(text))>0")]
        with closing(self.connect()) as db:
            existing = {(r["message_id"],r["document_id"],r["title"]) for r in db.execute("SELECT DISTINCT message_id,document_id,title FROM chunks")}
            live = {(r["message_id"],r["document_id"],r["title"]) for r in rows}
            with db:
                for message, document, title in existing-live:
                    db.execute("DELETE FROM chunks WHERE message_id=? AND document_id=?", (message,document))
            pending = [r for r in rows if (r["message_id"],r["document_id"],r["title"]) not in existing]
            indexed = 0
            for row in pending:
                parts = list(chunks(row["text"]))
                inputs = [row["title"][:120]+"\n"+text for _,text in parts]
                vectors = self.encoder().encode_document(inputs, batch_size=8, normalize_embeddings=True, show_progress_bar=False)
                with db:
                    db.executemany("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?)",
                        [(row["message_id"],row["document_id"],offset,row["title"],text,
                          np.asarray(vector,dtype=np.float32).tobytes()) for (offset,text),vector in zip(parts,vectors)])
                indexed += 1
                if progress and (indexed % 100 == 0 or indexed == len(pending)):
                    progress({"indexed_messages":indexed,"pending_messages":len(pending)})
            return {"indexed_messages":indexed,"total_messages":len(rows),
                    "chunks":db.execute("SELECT count(*) FROM chunks").fetchone()[0],"model":self.model_name}

    def search(self, query, *, limit=8, budget_chars=6000, rerank=True):
        import numpy as np
        if not query.strip() or len(query)>512 or not 1<=limit<=20 or not 500<=budget_chars<=40000:
            raise ReaderError("query/limit/budget is outside supported bounds")
        if not self.path.exists():
            raise ReaderError("semantic index is missing; run python -m personal_vault.semantic VAULT_ROOT build")
        repo=RecallRepository(self.root)
        with closing(repo._connect()) as archive:
            current={(r["message_id"],r["document_id"]):dict(r) for r in archive.execute(_MESSAGES+"SELECT * FROM messages WHERE role='user'")}
        feedback=repo.annotations.current()
        with closing(self.connect()) as db:
            candidates=[dict(r) for r in db.execute("SELECT * FROM chunks")
                if (r["message_id"],r["document_id"]) in current
                and feedback.get(r["message_id"],{}).get("action") != "excluded"]
        if not candidates:
            return {"query":query,"items":[],"mode":"semantic","indexed_chunks":0}
        vector=self.encoder().encode_query([query], normalize_embeddings=True, show_progress_bar=False)[0]
        matrix=np.stack([np.frombuffer(r["vector"],dtype=np.float32) for r in candidates])
        scores=matrix@vector
        best=np.argsort(scores)[::-1][:min(40,len(scores))]
        ranked=[(candidates[int(i)],float(scores[int(i)])) for i in best]
        if rerank:
            if self._reranker is None:
                from sentence_transformers import CrossEncoder
                self._reranker=CrossEncoder(RERANKER,device=self.device or self.encoder().device.type,
                                            local_files_only=True,max_length=768)
            values=self._reranker.predict([(query,r["title"]+"\n"+r["text"]) for r,_ in ranked],batch_size=4,show_progress_bar=False)
            ranked=fuse_ranks(ranked,values)
        items=[];seen=set();remaining=budget_chars
        for row,score in ranked:
            item=dict(current[(row["message_id"],row["document_id"])])
            if item["conversation_id"] in seen or not remaining:
                continue
            seen.add(item["conversation_id"])
            item.pop("document_id",None)
            full=item.pop("text")
            snippet=row["text"][:remaining]
            item.update(text=snippet,text_offset=row["offset"],text_length=len(full),
                        truncated=row["offset"]>0 or len(snippet)<len(full),
                        relevance_score=score,retrieval="semantic_fused_rerank" if rerank else "semantic")
            remaining-=len(item["text"])
            _provenance(item);repo._annotate(item,feedback)
            item["reader_url"]=_reader_url(item);items.append(item)
            if len(items)>=limit:break
        return {"query":query,"items":items,"mode":"semantic","returned_text_characters":budget_chars-remaining,
                "evidence_note":"相似度不是事实正确率；可能返回无关来源，需要核对原文。"}


def main(argv=None):
    parser=argparse.ArgumentParser(description="Build/search a local semantic archive index.")
    parser.add_argument("vault_root",type=Path)
    parser.add_argument("command",choices=("build","search"))
    parser.add_argument("query",nargs="?")
    parser.add_argument("--device")
    parser.add_argument("--no-rerank",action="store_true")
    parser.add_argument("--refresh",action="store_true",help="Index new or changed user messages before searching")
    parser.add_argument("--limit",type=int,default=8)
    parser.add_argument("--budget-chars",type=int,default=6000)
    args=parser.parse_args(argv)
    index=SemanticIndex(args.vault_root,device=args.device)
    if args.command=="search" and args.refresh:
        index.build()
    result=index.build(lambda state:print(json.dumps(state),flush=True)) if args.command=="build" else index.search(args.query or "",rerank=not args.no_rerank,limit=args.limit,budget_chars=args.budget_chars)
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
