import tempfile
import unittest
from pathlib import Path
from personal_vault.annotations import AnnotationStore
from personal_vault.reader import ReaderError
from personal_vault.recall import RecallRepository
from tests.test_reader import ReaderFixture


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vault"
        ReaderFixture(self.root)
        self.recall = RecallRepository(self.root)
        self.hit = self.recall.search("短句")["items"][0]
        self.key = self.hit["message_id"]

    def test_exclusion_affects_search_and_context_but_is_reversible(self):
        self.recall.feedback(self.key, "excluded", note="Do not use")
        self.assertEqual(self.recall.search("短句")["items"], [])
        item = next(i for i in self.recall.context(self.hit["conversation_id"])["items"] if i["message_id"] == self.key)
        self.assertEqual(item["text"], "")
        self.assertTrue(item["withheld"])
        self.recall.feedback(self.key, "restored")
        self.assertEqual(len(self.recall.search("短句")["items"]), 1)
        self.assertEqual(len(self.recall.annotations.history(self.key)), 2)

    def test_quote_and_time_corrections_preserve_source(self):
        self.recall.feedback(self.key, "quoted", assertion_kind="plan", valid_from="2026-01-01", valid_until="2026-02-01")
        hit = self.recall.search("短句")["items"][0]
        self.assertEqual(hit["evidence_status"], "quoted_material")
        self.assertEqual(hit["text"], self.hit["text"])
        self.assertEqual(hit["valid_until"], "2026-02-01")
        self.assertEqual(hit["temporal_status"], "expired")
        self.assertEqual(hit["stated_at"], self.hit["create_time"])
        with self.assertRaises(ValueError):
            self.recall.feedback(self.key, "outdated", valid_from="2027-01-01", valid_until="2026-01-01")

    def test_future_correction_and_replacement_keep_history(self):
        replacement=self.recall.search("alpha",role="all")["items"][0]["message_id"]
        self.recall.feedback(self.key,"corrected",note="future plan",valid_from="2999-01-01",
                             assertion_kind="plan",replacement_message_id=replacement)
        hit=self.recall.search("短句")["items"][0]
        self.assertEqual(hit["temporal_status"],"future")
        self.assertEqual(hit["feedback"]["replacement_message_id"],replacement)
        self.recall.feedback(self.key,"restored")
        self.assertEqual(len(self.recall.annotations.history(self.key)),2)

    def test_saved_quote_profile_respects_later_feedback(self):
        self.recall.remember(self.key, "短句", "preference")
        self.recall.remember(self.key, "短句", "preference")
        self.assertEqual(len(self.recall.profile()["items"]), 1)
        self.recall.feedback(self.key, "outdated")
        self.assertEqual(self.recall.profile()["items"], [])
        self.assertEqual(len(self.recall.profile()["historical_items"]), 1)
        with self.assertRaises(ReaderError):
            self.recall.remember(self.key, "fabricated")

    def test_bookmarks_topics_and_position(self):
        store = self.recall.annotations
        store.bookmark(self.key, self.hit["source_id"], "title", topic="chess")
        self.assertEqual(len(store.bookmarks("chess")), 1)
        self.assertEqual(store.bookmarks("music"), [])
        store.position("snapshot", "conversation", self.key)
        self.assertEqual(store.position("snapshot", "conversation")["message_id"], self.key)
        store.bookmark(self.key, self.hit["source_id"], "title", remove=True)
        self.assertEqual(store.bookmarks(), [])
