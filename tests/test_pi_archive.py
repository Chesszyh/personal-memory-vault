import json
import tempfile
import unittest
from pathlib import Path
from personal_vault.pi_archive import PiArchive
from personal_vault.recall import RecallRepository
from personal_vault.reader import ReaderRepository
from tests.test_reader import ReaderFixture


class PiArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);ReaderFixture(self.root)
        self.archive=PiArchive(self.root);self.path=self.root/"session.jsonl"
        self.records=[{"type":"session","id":"session","timestamp":"2026-10-02T00:00:00Z"}]
    def append(self,id,parent,text,role="user"):
        self.records.append({"type":"message","id":id,"parentId":parent,"timestamp":"2026-10-02T01:00:00Z",
                             "message":{"role":role,"content":[{"type":"text","text":text}]}})
        self.path.write_text("".join(json.dumps(r)+"\n" for r in self.records))
    def test_incremental_ingest_selected_branch_and_shared_recall(self):
        self.append("a",None,"我喜欢合成蓝色。")
        self.append("b","a","abandoned branch")
        self.append("c","a","active branch","assistant")
        first=self.archive.ingest(self.path)
        self.assertEqual(first["inserted_messages"],3)
        self.assertEqual(self.archive.ingest(self.path)["inserted_messages"],0)
        recall=RecallRepository(self.root)
        self.assertEqual(recall.search("abandoned")["items"],[])
        hit=recall.search("合成蓝色")["items"][0]
        self.assertTrue(hit["reader_url"].endswith("source=pi%3A1"))
        self.assertEqual(recall.context(hit["conversation_id"])["total"],2)
        reader=ReaderRepository(self.root/"canonical/archive.sqlite",self.root)
        self.assertEqual(reader.citation(hit["source_id"])["message_id"],hit["message_id"])
        self.archive.ingest(self.path,leaf_id="b")
        self.assertEqual(len(recall.search("abandoned")["items"]),1)
    def test_profile_groups_explicit_quotes_and_keeps_opposites_separate(self):
        self.append("a",None,"我喜欢合成蓝色。\n我不喜欢合成红色。\n> 我喜欢引用内容。")
        self.archive.ingest(self.path)
        profile=RecallRepository(self.root).profile()
        groups=profile["observed"]["groups"]
        self.assertEqual(len(groups),2)
        self.assertTrue(all(g["status"]=="historical_unreviewed" for g in groups))
        self.assertTrue(all(g["sources"][0]["reader_url"] for g in groups))

    def test_partial_line_is_deferred_and_changed_entry_rejected(self):
        self.append("a",None,"first")
        with self.path.open("a") as stream:stream.write('{"type":')
        self.archive.ingest(self.path)
        self.records[1]["message"]["content"][0]["text"]="changed"
        self.path.write_text("".join(json.dumps(r)+"\n" for r in self.records))
        with self.assertRaisesRegex(ValueError,"changed"):
            self.archive.ingest(self.path)
