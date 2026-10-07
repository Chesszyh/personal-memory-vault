from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from personal_vault.memory_store import decide_memory_candidate, scan_memory_candidates
from personal_vault.migration import MigrationError, build_chatgpt_migration_pack
from tests.test_reader import ReaderFixture


class MigrationPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = ReaderFixture(self.vault)
        canonical = sqlite3.connect(self.fixture.database_path)
        canonical.execute(
            "UPDATE search_documents SET content='我偏好简洁回答，这是合成资料。' "
            "WHERE message_version_id=1"
        )
        canonical.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
        canonical.commit()
        canonical.close()
        scan = scan_memory_candidates(vault_root=self.vault, snapshot_id=2)
        database = sqlite3.connect(scan.memory_database_path)
        self.candidate_id = database.execute("SELECT candidate_id FROM candidates").fetchone()[0]
        database.close()
        decide_memory_candidate(
            vault_root=self.vault, candidate_id=self.candidate_id, status="confirmed"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pack_contains_only_confirmed_memories_and_deterministic_manifest(self) -> None:
        output = self.root / "migration"
        result = build_chatgpt_migration_pack(vault_root=self.vault, output_directory=output)
        self.assertEqual(result.confirmed_memories, 1)
        preferences = (output / "02_INTERACTION_PREFERENCES.md").read_text(encoding="utf-8")
        self.assertIn(self.candidate_id, preferences)
        self.assertIn("source_ref", preferences)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["tree_sha256"], result.tree_sha256)
        self.assertEqual(len(manifest["files"]), result.file_count)
        with self.assertRaises(MigrationError):
            build_chatgpt_migration_pack(vault_root=self.vault, output_directory=output)


if __name__ == "__main__":
    unittest.main()
