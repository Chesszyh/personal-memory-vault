import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from personal_vault.update_export import latest_export, main
from tests import test_chatgpt_export


class UpdateExportTests(unittest.TestCase):
    def test_latest_selection_and_real_repeat_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exports = root / "exports"
            exports.mkdir()
            for name in ("2026-09-01", "2026-09-24"):
                path = exports / name
                path.mkdir()
                conversation = test_chatgpt_export.ChatGPTExportImportTests.conversation()
                (path / "conversations-000.json").write_text(json.dumps([conversation]))
            (exports / "2026-09-25").mkdir()
            self.assertEqual(latest_export(exports).name, "2026-09-24")
            args = ["--export-root", str(exports), "--vault-root", str(root / "vault")]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 0)
            self.assertIn('"import_status": "no_op"', out.getvalue())
            with contextlib.closing(sqlite3.connect(root / "vault/canonical/archive.sqlite")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)

    def test_zip_import_progress_repeat_and_change_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "2026-10-02.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("export/conversations.json", json.dumps([test_chatgpt_export.ChatGPTExportImportTests.conversation()]))
            args = [str(archive), "--vault-root", str(root / "vault")]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
                self.assertIn('"added_conversations": 1', out.getvalue())
                self.assertIn("阶段：增量导入", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
                self.assertIn('"import_status": "no_op"', out.getvalue())
                self.assertIn('"added_conversations": 0', out.getvalue())
                self.assertIn('"unchanged_conversations": 1', out.getvalue())

    def test_non_dated_directory_requires_date(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main([temporary])
            self.assertEqual(result.exception.code, 1)
