from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from personal_vault.analysis_report import AnalysisReportError, build_snapshot_analysis_report
from tests.test_reader import ReaderFixture


class AnalysisReportTests(unittest.TestCase):
    def test_report_is_deterministic_sql_and_keeps_unknown_deletion_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            ReaderFixture(vault)
            output = root / "analysis"
            result = build_snapshot_analysis_report(
                vault_root=vault, snapshot_id=2, output_directory=output
            )
            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["generated_from"], "deterministic_sql")
            self.assertEqual(payload["llm_inference"], "disabled")
            self.assertIsNone(payload["snapshot_quality"]["source_deletions"])
            self.assertIn("source_deletions: unknown", result.markdown_path.read_text(encoding="utf-8"))
            with self.assertRaises(AnalysisReportError):
                build_snapshot_analysis_report(
                    vault_root=vault, snapshot_id=2, output_directory=output
                )


if __name__ == "__main__":
    unittest.main()
