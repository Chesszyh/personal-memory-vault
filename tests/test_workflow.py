from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from personal_vault.archive_validation import ArchiveValidationResult, ValidationCheck
from personal_vault.chatgpt_export import ChatGPTDoctorResult, ChatGPTImportResult
from personal_vault.workflow import WorkflowError, update_chatgpt_official


class UpdateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.vault = self.root / "vault"
        self.evidence.mkdir()
        (self.evidence / "conversations-000.json").write_text("[]", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self):
        doctor = ChatGPTDoctorResult(True, True, "snapshot-1", "a" * 64, 1, 0, 0, 0, 0, 0, 0, 0)
        imported = ChatGPTImportResult(
            "imported", "source", "snapshot-1",
            self.vault / "canonical/archive.sqlite",
            self.vault / "decoded/messages.ndjson",
            self.vault / "decoded/source.ndjson",
            1, 0, 0, 0, 0, 0, 0, {"source_complete": "pass"},
        )
        validation = ArchiveValidationResult(
            True, (ValidationCheck("sqlite.integrity.violations", "pass", count=0),)
        )
        with mock.patch(
            "personal_vault.workflow.doctor_chatgpt_export", return_value=doctor
        ), mock.patch(
            "personal_vault.workflow.import_chatgpt_export", return_value=imported
        ), mock.patch(
            "personal_vault.workflow.validate_archive", return_value=validation
        ):
            return update_chatgpt_official(
                evidence_root=self.evidence,
                vault_root=self.vault,
                identity_scope="fixture-account",
                source_id="chatgpt-official-2026-08-11",
                captured_at="2026-08-11",
            )

    def test_creates_then_reuses_bound_manifest_and_writes_metadata_report(self) -> None:
        first = self._run()
        self.assertEqual(first.status, "pass")
        self.assertEqual(first.manifest_status, "created")
        self.assertEqual(first.report_path.stat().st_mode & 0o777, 0o600)
        report = json.loads(first.report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["metadata_only"])
        self.assertNotIn("synthetic private body", json.dumps(report))

        second = self._run()
        self.assertEqual(second.manifest_status, "reused")
        self.assertEqual(second.evidence_tree_sha256, first.evidence_tree_sha256)

    def test_rejects_unsafe_identity_and_manifest_drift_before_import(self) -> None:
        with self.assertRaises(WorkflowError):
            update_chatgpt_official(
                evidence_root=self.evidence,
                vault_root=self.vault,
                identity_scope="fixture",
                source_id="../escape",
                captured_at="2026-08-11",
            )
        first = self._run()
        (self.evidence / "conversations-000.json").write_text("[1]", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "no longer matches"):
            self._run()
        self.assertTrue(first.manifest_path.is_file())


if __name__ == "__main__":
    unittest.main()
