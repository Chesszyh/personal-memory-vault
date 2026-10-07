from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from personal_vault.memory_store import (
    MemoryStoreError,
    decide_memory_candidate,
    decide_memory_candidates,
    edit_memory_candidate,
    export_confirmed_profile,
    export_memory_review_html,
    scan_memory_candidates,
)
from tests.test_reader import ReaderFixture


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name) / "vault"
        self.fixture = ReaderFixture(self.vault)
        canonical = sqlite3.connect(self.fixture.database_path)
        canonical.execute(
            "UPDATE search_documents SET content = '我希望以后回答直接一些，这是合成偏好。' "
            "WHERE message_version_id = 1"
        )
        canonical.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
        canonical.commit()
        canonical.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scan_is_pending_idempotent_and_profile_requires_confirmation(self) -> None:
        first = scan_memory_candidates(vault_root=self.vault, snapshot_id=2)
        self.assertEqual(first.inserted_candidates, 1)
        self.assertEqual(first.confirmed_candidates, 0)
        second = scan_memory_candidates(vault_root=self.vault, snapshot_id=2)
        self.assertEqual(second.inserted_candidates, 0)
        self.assertEqual(second.existing_candidates, 1)

        review = export_memory_review_html(vault_root=self.vault)
        review_html = review.path.read_text(encoding="utf-8")
        self.assertEqual(review.pending_count, 1)
        self.assertIn("不是已经确认的事实", review_html)
        self.assertIn("Content-Security-Policy", review_html)
        self.assertIn("http://127.0.0.1:8766/", review_html)

        database = sqlite3.connect(first.memory_database_path)
        candidate_id, role_source = database.execute(
            "SELECT candidate_id, source_message_identity_key FROM candidates"
        ).fetchone()
        database.close()
        self.assertIn("messages/", role_source)
        decision = decide_memory_candidate(
            vault_root=self.vault, candidate_id=candidate_id, status="confirmed"
        )
        self.assertEqual(decision.prior_status, "pending")
        profile = export_confirmed_profile(vault_root=self.vault)
        rendered = profile.path.read_text(encoding="utf-8")
        self.assertEqual(profile.confirmed_count, 1)
        self.assertIn(candidate_id, rendered)
        self.assertIn("source_ref", rendered)
        self.assertIn("仅包含已确认的候选", rendered)
        self.assertNotIn("人工确认", rendered)

    def test_rejects_invalid_decisions_and_never_scans_assistant_only_text(self) -> None:
        result = scan_memory_candidates(vault_root=self.vault, snapshot_id=1)
        self.assertEqual(result.inserted_candidates, 1)
        database = sqlite3.connect(result.memory_database_path)
        sources = [row[0] for row in database.execute("SELECT source_message_identity_key FROM candidates")]
        database.close()
        self.assertTrue(all("m-main" not in source for source in sources))
        with self.assertRaises(MemoryStoreError):
            decide_memory_candidate(
                vault_root=self.vault, candidate_id="../bad", status="confirmed"
            )

    def test_edits_and_batch_decisions_preserve_original_evidence(self) -> None:
        result = scan_memory_candidates(vault_root=self.vault, snapshot_id=2)
        database = sqlite3.connect(result.memory_database_path)
        candidate_id, original = database.execute(
            "SELECT candidate_id, statement FROM candidates"
        ).fetchone()
        database.close()

        edit = edit_memory_candidate(
            vault_root=self.vault,
            candidate_id=candidate_id,
            statement="我希望回答更直接。",
        )
        self.assertTrue(edit.edited)
        batch = decide_memory_candidates(
            vault_root=self.vault,
            candidate_ids=[candidate_id, candidate_id],
            status="confirmed",
            note="reviewed in workbench",
        )
        self.assertEqual(batch.updated_count, 1)
        with self.assertRaisesRegex(MemoryStoreError, "at most 1000"):
            decide_memory_candidates(
                vault_root=self.vault,
                candidate_ids=[f"memc_{index:032x}" for index in range(1001)],
                status="confirmed",
            )

        database = sqlite3.connect(result.memory_database_path)
        stored_original, reviewed, version = database.execute(
            "SELECT statement, reviewed_statement, (SELECT user_version FROM pragma_user_version) FROM candidates"
        ).fetchone()
        database.close()
        self.assertEqual(stored_original, original)
        self.assertEqual(reviewed, "我希望回答更直接。")
        self.assertEqual(version, 2)
        profile = export_confirmed_profile(vault_root=self.vault)
        rendered = profile.path.read_text(encoding="utf-8")
        self.assertIn("我希望回答更直接。", rendered)
        self.assertNotIn(original, rendered)


if __name__ == "__main__":
    unittest.main()
