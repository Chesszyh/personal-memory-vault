import tempfile
import unittest
from pathlib import Path

from personal_vault.reader import ReaderRepository
from tests.test_reader import ReaderFixture


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vault"
        fixture = ReaderFixture(self.root)
        self.reader = ReaderRepository(fixture.database_path, self.root)

    def test_latest_versions_absent_conversations_and_pagination(self):
        data = self.reader.library()
        self.assertEqual(data["pagination"]["total"], 3)
        by_id = {item["native_id"]: item for item in data["items"]}
        self.assertEqual(by_id["c1"]["snapshot_id"], 2)
        self.assertEqual(by_id["c3"]["snapshot_id"], 1)
        page = self.reader.library(limit=1)
        following = self.reader.library(limit=1, offset=page["pagination"]["next_offset"])
        self.assertNotEqual(page["items"][0]["identity_key"], following["items"][0]["identity_key"])

    def test_search_matches_title_or_body_once_per_conversation(self):
        self.assertEqual(len(self.reader.library(query="alpha")["items"]), 1)
        self.assertEqual(len(self.reader.library(query="中文")["items"]), 1)
        self.assertEqual(len(self.reader.library(query="%")["items"]), 1)
        self.assertEqual(self.reader.library(query="no-such-record")["items"], [])
