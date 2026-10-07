from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from personal_vault.recall import RecallRepository
from personal_vault.reader import ReaderError, ReaderRepository
from personal_vault.memory_store import scan_memory_candidates, decide_memory_candidate, edit_memory_candidate
from tests.test_reader import ReaderFixture


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vault"
        self.fixture = ReaderFixture(self.root)
        self.recall = RecallRepository(self.root)

    def change(self, sql, parameters=()):
        with closing(sqlite3.connect(self.fixture.database_path)) as db, db:
            db.execute(sql, parameters)

    def test_reader_links_pin_snapshot_conversation_and_message(self):
        with patch.dict("os.environ", {"PERSONAL_VAULT_READER_URL": "http://localhost:9000/"}):
            hit = self.recall.search("短句")["items"][0]
            context = self.recall.context(hit["conversation_id"], message_id=hit["message_id"], limit=1)
        self.assertEqual(context["items"][0]["reader_url"], hit["reader_url"])
        url = urlparse(hit["reader_url"])
        self.assertEqual(url.netloc, "localhost:9000")
        self.assertEqual(parse_qs(url.query), {"source": [str(hit["source_id"])]})
        reader = ReaderRepository(self.fixture.database_path, self.root)
        self.assertEqual(reader.citation(hit["source_id"]), {
            "snapshot_key": hit["snapshot_key"], "snapshot_id": hit["snapshot_id"],
            "conversation_id": hit["conversation_id"], "message_id": hit["message_id"]})
        with self.assertRaises(ReaderError):
            reader.citation(-1)

    def test_current_branch_latest_version_roles_and_literal_search(self):
        self.assertEqual(self.recall.search("alpha")["items"], [])
        hits = self.recall.search("alpha", role="all")["items"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["snapshot_id"], 2)
        self.assertIn("已更新", hits[0]["text"])
        self.assertEqual(self.recall.search("替代回答", role="all")["items"], [])
        self.assertEqual(len(self.recall.search("%")["items"]), 1)
        self.assertEqual(len(self.recall.search("短句")["items"]), 1)
        self.assertEqual(self.recall.search("not-in-history")["items"], [])

    def test_search_deduplicates_and_budget_preserves_anchor(self):
        hits = self.recall.search("中文归档", role="all")["items"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["role"], "user")
        expanded = self.recall.search("中文归档", role="all", per_conversation=20)["items"]
        self.assertGreater(len(expanded), 1)
        hit = self.recall.search("alpha", role="all")["items"][0]
        self.change("UPDATE search_documents SET content=? WHERE message_version_id=4", ("长" * 8000,))
        context = self.recall.context(hit["conversation_id"], message_id=hit["message_id"], budget_chars=500)
        self.assertLessEqual(context["returned_text_characters"], 500)
        anchor = next(i for i in context["items"] if i["message_id"] == hit["message_id"])
        self.assertEqual(len(anchor["text"]), 500)
        self.assertEqual(anchor["next_text_offset"], 500)
        self.assertEqual(anchor["evidence_status"], "assistant_response")
        self.assertTrue(anchor["citation_label"].startswith("历史助手总结"))
        result = self.recall.search("长", role="all", budget_chars=500)
        self.assertLessEqual(result["returned_text_characters"], 500)

    def test_hidden_analysis_and_tool_directed_messages_are_excluded(self):
        for patch, recipient in (({"channel": "analysis"}, "all"),
                                 ({"metadata": {"is_visually_hidden_from_conversation": True}}, "all"),
                                 ({}, "python")):
            with self.subTest(patch=patch, recipient=recipient):
                self.change("UPDATE message_versions SET raw_json=?,recipient=? WHERE id=4",
                            (json.dumps(patch), recipient))
                self.assertEqual(self.recall.search("alpha", role="all")["items"], [])
                context = self.recall.context(self.fixture.c1_key)
                self.assertTrue(all(m["role"] == "user" for m in context["items"]))

    def test_absent_conversation_remains_retrievable_from_prior_snapshot(self):
        self.change("DELETE FROM current_branch_nodes WHERE snapshot_id=2 AND conversation_identity_id=1")
        self.change("DELETE FROM conversation_observations WHERE snapshot_id=2 AND conversation_identity_id=1")
        hit = self.recall.search("alpha", role="all")["items"][0]
        self.assertEqual(hit["snapshot_id"], 1)
        self.assertNotIn("已更新", hit["text"])

    def test_context_anchor_pagination_and_long_message(self):
        hit = self.recall.search("alpha", role="all")["items"][0]
        context = self.recall.context(hit["conversation_id"], message_id=hit["message_id"], limit=1)
        self.assertEqual(context["items"][0]["message_id"], hit["message_id"])
        first = self.recall.context(hit["conversation_id"], limit=1)
        second = self.recall.context(hit["conversation_id"], offset=first["next_offset"], limit=1)
        self.assertNotEqual(first["items"][0]["message_id"], second["items"][0]["message_id"])
        self.change("UPDATE search_documents SET content=? WHERE message_version_id=4", ("长" * 5000 + "结尾",))
        tail = self.recall.context(hit["conversation_id"], message_id=hit["message_id"], limit=1, text_offset=4000)
        self.assertTrue(tail["items"][0]["text"].endswith("结尾"))
        self.assertIsNone(tail["items"][0]["next_text_offset"])
        with self.assertRaises(ReaderError):
            self.recall.context(hit["conversation_id"], message_id="missing")

    def test_review_is_not_required_for_retrieval_and_profile_uses_edits(self):
        self.change("UPDATE search_documents SET content=? WHERE message_version_id=1", ("我希望回答直接一些，这是合成偏好。",))
        scan_memory_candidates(vault_root=self.root, snapshot_id=2)
        self.assertEqual(self.recall.profile()["items"], [])
        self.assertEqual(len(self.recall.search("合成偏好")["items"]), 1)
        with closing(sqlite3.connect(self.root / "memory/memory.sqlite")) as db:
            candidate_id = db.execute("SELECT candidate_id FROM candidates").fetchone()[0]
        decide_memory_candidate(vault_root=self.root, candidate_id=candidate_id, status="confirmed")
        edit_memory_candidate(vault_root=self.root, candidate_id=candidate_id, statement="回答直接。")
        self.assertEqual(self.recall.profile()["items"][0]["statement"], "回答直接。")


if __name__ == "__main__":
    unittest.main()
