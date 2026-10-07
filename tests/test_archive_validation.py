from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from personal_vault.archive_validation import validate_archive
from personal_vault.chatgpt_export import import_chatgpt_export
from personal_vault.evidence import build_manifest, write_manifest


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = Path(self.temporary.name) / "vault"
        self.evidence = self.vault / "raw" / "fixture"
        self.evidence.mkdir(parents=True)
        self.manifest = self.vault / "manifests" / "fixture" / "manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self._write_fixture()
        manifest = build_manifest(
            self.evidence,
            source_id="snapshot-fixture",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-11T00:00:00+08:00",
        )
        write_manifest(manifest, self.manifest, evidence_root=self.evidence)
        self.imported = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.vault,
            identity_scope="validation-fixture",
        )
        self.database = self.imported.database_path

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_fixture(self) -> None:
        conversation = {
            "id": "private-conversation-id",
            "title": "private-title-marker",
            "create_time": 1.0,
            "update_time": 2.0,
            "current_node": "message-1",
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["message-1"],
                },
                "message-1": {
                    "id": "message-1",
                    "message": {
                        "id": "message-1",
                        "author": {"role": "user", "name": None, "metadata": {}},
                        "create_time": 1.5,
                        "update_time": None,
                        "content": {
                            "content_type": "text",
                            "parts": ["private-message-marker searchable phrase"],
                        },
                        "status": "finished_successfully",
                        "end_turn": None,
                        "weight": 1.0,
                        "metadata": {},
                        "recipient": "all",
                        "channel": None,
                    },
                    "parent": "root",
                    "children": [],
                },
            },
        }
        (self.evidence / "conversations-000.json").write_text(
            json.dumps([conversation]), encoding="utf-8"
        )
        (self.evidence / "group_chats.json").write_text(
            json.dumps(
                {
                    "chats": [
                        {
                            "id": "private-group-id",
                            "name": "private-group-name",
                            "messages": [
                                {
                                    "id": "private-group-message-id",
                                    "role": "user",
                                    "text": "private-group-message-marker",
                                    "created_at": "2026-08-11T00:00:00Z",
                                    "attachments": [],
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.evidence / "file-unreferenced.dat").write_bytes(b"synthetic asset")
        (self.evidence / "chat.html").write_text(
            "<html>private-rendered-marker</html>", encoding="utf-8"
        )

    @staticmethod
    def _check(result: object, name: str):
        return next(check for check in result.checks if check.name == name)

    def test_valid_archive_passes_without_returning_private_content_or_paths(self) -> None:
        before_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        before_mtime = self.database.stat().st_mtime_ns

        result = validate_archive(self.vault)

        self.assertTrue(result.ok)
        serialized = result.as_dict()
        self.assertTrue(
            all(
                set(check) == {"name", "status", "count", "digest"}
                for check in serialized["checks"]
            )
        )
        payload = json.dumps(serialized, sort_keys=True)
        for secret in (
            "private-conversation-id",
            "private-title-marker",
            "private-message-marker",
            "private-group-id",
            "private-group-message-marker",
            str(self.vault),
        ):
            self.assertNotIn(secret, payload)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(self.database.stat().st_mtime_ns, before_mtime)
        self.assertEqual(
            self._check(result, "fts.external_content_integrity.violations").status,
            "pass",
        )
        self.assertEqual(
            self._check(result, "decoded_ndjson.closure.violations").count, 0
        )

    def test_optional_expectations_are_exact_and_missing_metrics_fail(self) -> None:
        result = validate_archive(
            self.vault,
            expectations={
                "global.assets.count": 999,
                "missing.metric": 1,
            },
        )

        self.assertFalse(result.ok)
        assets = self._check(result, "global.assets.count")
        missing = self._check(result, "missing.metric")
        self.assertEqual(assets.status, "fail")
        self.assertEqual(assets.count, 1)
        self.assertEqual(missing.status, "fail")

    def test_external_content_fts_damage_is_detected_on_a_temporary_backup(self) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute("INSERT INTO message_fts(message_fts) VALUES('delete-all')")
            database.commit()

        result = validate_archive(self.vault)

        self.assertFalse(result.ok)
        check = self._check(result, "fts.external_content_integrity.violations")
        self.assertEqual(check.status, "fail")
        self.assertEqual(check.count, 1)

    def test_semantic_link_and_snapshot_digest_tampering_are_detected(self) -> None:
        with closing(sqlite3.connect(self.database)) as database:
            database.execute(
                "UPDATE conversation_observations "
                "SET current_node_identity_key='private-invalid-node-key'"
            )
            database.commit()

        result = validate_archive(self.vault)

        self.assertFalse(result.ok)
        self.assertEqual(
            self._check(
                result,
                "snapshot.snapshot-fixture.invariant.conversation_links.violations",
            ).status,
            "fail",
        )
        self.assertEqual(
            self._check(result, "snapshot.snapshot-fixture.state_digest").status,
            "fail",
        )

    def test_persistent_cross_snapshot_absences_pass_semantic_validation(self) -> None:
        second_evidence = self.vault / "raw" / "fixture-2"
        second_evidence.mkdir()
        conversations = json.loads(
            (self.evidence / "conversations-000.json").read_text(encoding="utf-8")
        )
        conversations[0]["id"] = "private-second-conversation-id"
        conversations[0]["title"] = "private-second-title"
        (second_evidence / "conversations-000.json").write_text(
            json.dumps(conversations), encoding="utf-8"
        )
        second_manifest = self.vault / "manifests" / "fixture-2" / "manifest.json"
        second_manifest.parent.mkdir()
        manifest = build_manifest(
            second_evidence,
            source_id="snapshot-fixture-2",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-12T00:00:00+08:00",
        )
        write_manifest(manifest, second_manifest, evidence_root=second_evidence)
        import_chatgpt_export(
            evidence_root=second_evidence,
            manifest_path=second_manifest,
            output_root=self.vault,
            identity_scope="validation-fixture",
        )

        result = validate_archive(self.vault)

        self.assertTrue(result.ok)
        self.assertEqual(
            self._check(result, "source_absences.semantic.violations").count, 0
        )
        self.assertGreater(
            self._check(
                result,
                "snapshot.snapshot-fixture-2.absence.conversation.count",
            ).count
            or 0,
            0,
        )

    def test_changed_ndjson_and_cas_bytes_are_detected(self) -> None:
        with self.imported.source_records_ndjson_path.open("ab") as handle:
            handle.write(b"{}\n")
        asset = next((self.vault / "assets" / "sha256").glob("*/*"))
        asset.write_bytes(b"changed synthetic asset")

        result = validate_archive(self.vault)

        self.assertFalse(result.ok)
        self.assertGreater(
            self._check(result, "snapshot_artifacts.hash.violations").count or 0, 0
        )
        self.assertGreater(
            self._check(result, "decoded_ndjson.closure.violations").count or 0, 0
        )
        self.assertGreater(self._check(result, "cas.closure.violations").count or 0, 0)

    def test_staging_residue_and_cas_symlink_are_rejected_without_names(self) -> None:
        staging = self.vault / ".staging"
        (staging / "residual").write_bytes(b"residual")
        asset = next((self.vault / "assets" / "sha256").glob("*/*"))
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"outside")
        asset.unlink()
        asset.symlink_to(outside)

        result = validate_archive(self.vault)

        self.assertFalse(result.ok)
        self.assertEqual(self._check(result, "staging.entries.violations").status, "fail")
        self.assertEqual(self._check(result, "cas.closure.violations").status, "fail")
        payload = json.dumps(result.as_dict(), sort_keys=True)
        self.assertNotIn("residual", payload)
        self.assertNotIn(str(outside), payload)


if __name__ == "__main__":
    unittest.main()
