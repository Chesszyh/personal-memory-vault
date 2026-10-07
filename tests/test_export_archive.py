from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from personal_vault.export_archive import (
    ArchiveExportError,
    export_conversation_html,
    export_conversation_pdf,
    export_snapshot_html_archive,
    verify_snapshot_html_archive,
)
from personal_vault.extension_recovery import BrowserIdentity
from tests.test_reader import ReaderFixture


class ArchiveExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = ReaderFixture(self.vault)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_static_export_preserves_current_branch_alternatives_and_assets(self) -> None:
        output = self.root / "conversation"
        result = export_conversation_html(
            vault_root=self.vault,
            snapshot_id=2,
            conversation_identity_key=self.fixture.c1_key,
            output_directory=output,
        )
        rendered = result.html_path.read_text(encoding="utf-8")
        self.assertIn("中文归档（更新）", rendered)
        self.assertIn("替代分支节点 1", rendered)
        self.assertIn("稳定全文检索词 alpha", rendered)
        self.assertNotIn("innerHTML", rendered)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "pmv.static-conversation-export.v1")
        self.assertEqual(result.asset_count, 1)
        asset = output / manifest["assets"][0]["path"]
        self.assertEqual(asset.read_bytes(), self.fixture.asset_bytes)
        with self.assertRaisesRegex(ArchiveExportError, "already exists"):
            export_conversation_html(
                vault_root=self.vault,
                snapshot_id=2,
                conversation_identity_key=self.fixture.c1_key,
                output_directory=output,
            )

    def test_pdf_export_binds_browser_and_rejects_non_pdf_output(self) -> None:
        html_path = self.root / "index.html"
        html_path.write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
        pdf_path = self.root / "out.pdf"
        browser = BrowserIdentity(
            Path("/synthetic/chrome"), "chrome-for-testing", "151.0", "a" * 64
        )

        page = mock.Mock()
        launched = mock.Mock()
        launched.new_page.return_value = page
        runtime = mock.Mock()
        runtime.chromium.launch.return_value = launched
        manager = mock.MagicMock()
        manager.__enter__.return_value = runtime

        def successful_pdf(**kwargs):
            Path(kwargs["path"]).write_bytes(b"%PDF-1.7\nfixture\n")

        page.pdf.side_effect = successful_pdf

        with mock.patch(
            "personal_vault.export_archive.inspect_compatible_chrome_binary",
            return_value=browser,
        ), mock.patch(
            "personal_vault.export_archive._load_playwright",
            return_value=(lambda: manager, RuntimeError),
        ):
            result = export_conversation_pdf(
                html_path=html_path, pdf_path=pdf_path, chrome_binary=Path("chrome")
            )
        self.assertEqual(result.browser_binary_sha256, "a" * 64)
        self.assertGreater(result.pdf_bytes, 8)

        bad_path = self.root / "bad.pdf"
        page.pdf.side_effect = lambda **kwargs: Path(kwargs["path"]).write_bytes(b"not pdf")
        with mock.patch(
            "personal_vault.export_archive.inspect_compatible_chrome_binary",
            return_value=browser,
        ), mock.patch(
            "personal_vault.export_archive._load_playwright",
            return_value=(lambda: manager, RuntimeError),
        ):
            with self.assertRaisesRegex(ArchiveExportError, "not a PDF"):
                export_conversation_pdf(
                    html_path=html_path, pdf_path=bad_path, chrome_binary=Path("chrome")
                )
        self.assertFalse(bad_path.exists())

    def test_snapshot_export_builds_archive_index_and_conversation_manifests(self) -> None:
        output = self.root / "snapshot-archive"
        result = export_snapshot_html_archive(
            vault_root=self.vault, snapshot_id=2, output_directory=output
        )
        self.assertEqual(result.conversation_count, 2)
        index = result.index_path.read_text(encoding="utf-8")
        self.assertIn("2 个会话", index)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["tree_sha256"], result.tree_sha256)
        for conversation in manifest["conversations"]:
            self.assertTrue((output / conversation["path"]).is_file())

        verified = verify_snapshot_html_archive(output)
        self.assertTrue(verified.ok)
        self.assertEqual(verified.conversation_count, 2)
        self.assertEqual(verified.asset_file_count, 1)
        self.assertEqual(verified.asset_bytes, len(self.fixture.asset_bytes))

        first = output / manifest["conversations"][0]["path"]
        first.write_text(first.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
        with self.assertRaisesRegex(ArchiveExportError, "conversation index hash"):
            verify_snapshot_html_archive(output)

    def test_snapshot_verifier_rejects_unsafe_manifest_paths_and_active_html(self) -> None:
        output = self.root / "snapshot-archive"
        export_snapshot_html_archive(
            vault_root=self.vault, snapshot_id=2, output_directory=output
        )
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["conversations"][0]["path"] = "../escape/index.html"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ArchiveExportError, "unsafe archive path"):
            verify_snapshot_html_archive(output)

        output2 = self.root / "snapshot-active"
        export_snapshot_html_archive(
            vault_root=self.vault, snapshot_id=2, output_directory=output2
        )
        manifest2 = json.loads((output2 / "manifest.json").read_text(encoding="utf-8"))
        page = output2 / manifest2["conversations"][0]["path"]
        payload = page.read_text(encoding="utf-8").replace("</body>", "<script></script></body>")
        page.write_text(payload, encoding="utf-8")
        manifest2["conversations"][0]["index_sha256"] = hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()
        inner = json.loads((page.parent / "manifest.json").read_text(encoding="utf-8"))
        inner["index_sha256"] = manifest2["conversations"][0]["index_sha256"]
        (page.parent / "manifest.json").write_text(json.dumps(inner), encoding="utf-8")
        base = {k: v for k, v in manifest2.items() if k != "tree_sha256"}
        manifest2["tree_sha256"] = hashlib.sha256(
            json.dumps(
                base,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        (output2 / "manifest.json").write_text(json.dumps(manifest2), encoding="utf-8")
        with self.assertRaisesRegex(ArchiveExportError, "active HTML tag"):
            verify_snapshot_html_archive(output2)

        link = self.root / "snapshot-link"
        link.symlink_to(output2, target_is_directory=True)
        with self.assertRaisesRegex(ArchiveExportError, "must not be a symlink"):
            verify_snapshot_html_archive(link)


if __name__ == "__main__":
    unittest.main()
