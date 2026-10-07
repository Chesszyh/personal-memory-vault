from __future__ import annotations

import tempfile
import unittest
import json
import sqlite3
import contextlib
import io
from pathlib import Path

from personal_vault.chatgpt_export import (
    ChatGPTImportError,
    _verify_message_fts_integrity,
    doctor_chatgpt_export,
    import_chatgpt_export,
)
from personal_vault.database import connect_database
from personal_vault.evidence import build_manifest, write_manifest
from personal_vault.cli import main


class ChatGPTExportImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.manifest = self.root / "manifest.json"
        self.output = self.root / "vault"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fts_integrity_check_leaves_no_open_transaction(self) -> None:
        database = connect_database(self.root / "fts-integrity.sqlite")
        try:
            self.assertFalse(database.in_transaction)
            _verify_message_fts_integrity(database)
            self.assertFalse(database.in_transaction)
        finally:
            database.close()

    def write_manifest(self) -> None:
        manifest = build_manifest(
            self.evidence,
            source_id="chatgpt-official-fixture",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-10T00:00:00+08:00",
        )
        write_manifest(manifest, self.manifest, evidence_root=self.evidence)

    def write_conversations(self, conversations: list[dict[str, object]]) -> None:
        (self.evidence / "conversations-000.json").write_text(
            json.dumps(conversations, ensure_ascii=False), encoding="utf-8"
        )

    def make_snapshot(
        self,
        *,
        parent: Path,
        snapshot_key: str,
        conversations: list[dict[str, object]],
    ) -> tuple[Path, Path]:
        evidence = parent / snapshot_key
        evidence.mkdir(parents=True)
        (evidence / "conversations-000.json").write_text(
            json.dumps(conversations, ensure_ascii=False), encoding="utf-8"
        )
        manifest_path = parent / f"{snapshot_key}-manifest.json"
        manifest = build_manifest(
            evidence,
            source_id=snapshot_key,
            source_kind="chatgpt_official_export",
            captured_at=f"2026-08-{10 + len(tuple(parent.iterdir())):02d}T00:00:00Z",
        )
        write_manifest(manifest, manifest_path, evidence_root=evidence)
        return evidence, manifest_path

    @staticmethod
    def conversation() -> dict[str, object]:
        return {
            "id": "conversation-1",
            "title": "Synthetic conversation",
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
                            "parts": ["fixture searchable phrase 中文检索短语"],
                        },
                        "status": "finished_successfully",
                        "end_turn": None,
                        "weight": 1.0,
                        "metadata": {},
                        "recipient": "all",
                        "channel": None,
                        "future_message_field": {"kept": True},
                    },
                    "parent": "root",
                    "children": [],
                    "future_node_field": "kept",
                },
            },
            "future_conversation_field": ["kept"],
        }

    def test_rejects_changed_evidence_before_creating_outputs(self) -> None:
        (self.evidence / "conversations-000.json").write_text("[]\n", encoding="utf-8")
        self.write_manifest()
        (self.evidence / "conversations-000.json").write_text("[{}]\n", encoding="utf-8")

        with self.assertRaisesRegex(ChatGPTImportError, "manifest verification failed"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

        self.assertFalse(self.output.exists())

    def test_rejects_output_inside_the_verified_evidence_tree(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        unsafe_output = self.evidence / "derived-vault"

        with self.assertRaisesRegex(ChatGPTImportError, "inside the evidence root"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=unsafe_output,
                identity_scope="chatgpt-account-fixture",
            )

        self.assertFalse(unsafe_output.exists())

    def test_cli_doctor_and_import_report_counts_without_message_content(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        doctor_output = io.StringIO()
        with contextlib.redirect_stdout(doctor_output):
            doctor_status = main(
                [
                    "doctor",
                    "chatgpt-official",
                    str(self.evidence),
                    str(self.manifest),
                ]
            )
        doctor = json.loads(doctor_output.getvalue())
        self.assertEqual(doctor_status, 0)
        self.assertEqual(doctor["conversations"], 1)
        self.assertNotIn("searchable phrase", doctor_output.getvalue())

        import_output = io.StringIO()
        with contextlib.redirect_stdout(import_output):
            import_status = main(
                [
                    "import",
                    "chatgpt-official",
                    str(self.evidence),
                    str(self.manifest),
                    str(self.output),
                    "--identity-scope",
                    "chatgpt-account-fixture",
                ]
            )
        imported = json.loads(import_output.getvalue())
        self.assertEqual(import_status, 0)
        self.assertEqual(imported["status"], "imported")
        self.assertNotIn("searchable phrase", import_output.getvalue())

    def test_import_accepts_nested_files_listed_by_provider_manifest(self) -> None:
        self.write_conversations([self.conversation()])
        sites = self.evidence / "sites"
        sites.mkdir()
        nested = sites / "export_manifest.json"
        nested.write_text('{"version": 1}\n', encoding="utf-8")
        shard = self.evidence / "conversations-000.json"
        provider_manifest = {
            "version": 1,
            "manifest_file": "export_manifest.json",
            "export_files": [
                {"path": shard.name, "size_bytes": shard.stat().st_size},
                {
                    "path": "sites/export_manifest.json",
                    "size_bytes": nested.stat().st_size,
                },
            ],
            "logical_files": {
                "conversations.json": {
                    "files": [shard.name],
                    "sharded": True,
                },
                "sites/export_manifest.json": {
                    "files": ["sites/export_manifest.json"],
                    "sharded": False,
                },
            },
        }
        (self.evidence / "export_manifest.json").write_text(
            json.dumps(provider_manifest), encoding="utf-8"
        )
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        self.assertEqual(result.status, "imported")
        self.assertEqual(result.conversations, 1)

    def test_provider_manifest_still_rejects_path_traversal(self) -> None:
        self.write_conversations([self.conversation()])
        shard = self.evidence / "conversations-000.json"
        provider_manifest = {
            "version": 1,
            "manifest_file": "export_manifest.json",
            "export_files": [
                {"path": shard.name, "size_bytes": shard.stat().st_size},
                {"path": "../escape", "size_bytes": 0},
            ],
            "logical_files": {
                "conversations.json": {"files": [shard.name], "sharded": True}
            },
        }
        (self.evidence / "export_manifest.json").write_text(
            json.dumps(provider_manifest), encoding="utf-8"
        )
        self.write_manifest()

        with self.assertRaisesRegex(ChatGPTImportError, "unsafe provider export path"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_imports_source_preserving_snapshot_and_search_index(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        self.assertEqual(result.status, "imported")
        self.assertEqual(result.conversations, 1)
        self.assertEqual(result.nodes, 2)
        self.assertEqual(result.messages, 1)
        self.assertTrue(result.ndjson_path.is_file())
        staged = json.loads(result.ndjson_path.read_text(encoding="utf-8"))
        self.assertEqual(staged["provenance"]["source_file"], "conversations-000.json")
        self.assertEqual(staged["provenance"]["array_index"], 0)
        self.assertEqual(staged["raw"]["future_conversation_field"], ["kept"])

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            source = database.execute(
                "SELECT identity_scope FROM sources"
            ).fetchone()
            snapshot = database.execute(
                "SELECT snapshot_key, evidence_tree_sha256 FROM snapshots"
            ).fetchone()
            version = database.execute(
                """SELECT ci.identity_key, sr.source_file_path, sr.array_index,
                          co.current_node_identity_key, cv.unknown_json
                     FROM conversation_observations co
                     JOIN conversation_identities ci
                       ON ci.id = co.conversation_identity_id
                     JOIN conversation_versions cv
                       ON cv.id = co.conversation_version_id
                     JOIN source_records sr ON sr.id = co.source_record_id"""
            ).fetchone()
            message = database.execute(
                """SELECT mi.identity_key, mv.unknown_json
                     FROM message_versions mv
                     JOIN message_identities mi ON mi.id = mv.message_identity_id"""
            ).fetchone()
            hits = database.execute(
                "SELECT identity_key FROM message_fts WHERE message_fts MATCH ?",
                ("searchable",),
            ).fetchall()
            chinese_hits = database.execute(
                "SELECT identity_key FROM message_fts WHERE message_fts MATCH ?",
                ("检索短",),
            ).fetchall()

        self.assertEqual(source, ("chatgpt-account-fixture",))
        self.assertEqual(snapshot[0], "chatgpt-official-fixture")
        self.assertEqual(len(snapshot[1]), 64)
        self.assertEqual(
            version[:4],
            (
                "openai/chatgpt-official/chatgpt-account-fixture/conversations/conversation-1",
                "conversations-000.json",
                0,
                "openai/chatgpt-official/chatgpt-account-fixture/conversations/conversation-1/nodes/message-1",
            ),
        )
        self.assertEqual(
            json.loads(version[4]), {"future_conversation_field": ["kept"]}
        )
        self.assertEqual(
            message[0],
            "openai/chatgpt-official/chatgpt-account-fixture/conversations/conversation-1/messages/message-1",
        )
        self.assertEqual(
            json.loads(message[1]), {"future_message_field": {"kept": True}}
        )
        self.assertEqual(hits, [(message[0],)])
        self.assertEqual(chinese_hits, [(message[0],)])

    def test_search_document_updates_keep_the_external_fts_index_in_sync(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            database.execute(
                "UPDATE search_documents SET content = ?",
                ("replacement searchable token",),
            )
            database.commit()
            old_hits = database.execute(
                "SELECT count(*) FROM message_fts WHERE message_fts MATCH ?",
                ("fixture",),
            ).fetchone()[0]
            new_hits = database.execute(
                "SELECT count(*) FROM message_fts WHERE message_fts MATCH ?",
                ("replacement",),
            ).fetchone()[0]
        self.assertEqual((old_hits, new_hits), (0, 1))

    def test_rejects_broken_graphs_without_publishing_snapshot(self) -> None:
        cases: dict[str, tuple[str, object]] = {
            "current_node": (
                "current_node does not resolve",
                lambda conversation: conversation.__setitem__("current_node", "missing"),
            ),
            "parent": (
                "parent does not resolve",
                lambda conversation: conversation["mapping"]["message-1"].__setitem__(
                    "parent", "missing"
                ),
            ),
            "cycle": (
                "cycle in conversation graph",
                lambda conversation: conversation["mapping"]["root"].__setitem__(
                    "parent", "message-1"
                ),
            ),
            "node_id": (
                "node id mismatch",
                lambda conversation: conversation["mapping"]["message-1"].__setitem__(
                    "id", "different"
                ),
            ),
            "message_id": (
                "message id mismatch",
                lambda conversation: conversation["mapping"]["message-1"][
                    "message"
                ].__setitem__("id", "different"),
            ),
        }
        for case, (error_pattern, mutate) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                evidence = root / "evidence"
                evidence.mkdir()
                conversation = self.conversation()
                mutate(conversation)
                (evidence / "conversations-000.json").write_text(
                    json.dumps([conversation]), encoding="utf-8"
                )
                manifest_path = root / "manifest.json"
                manifest = build_manifest(
                    evidence,
                    source_id=f"snapshot-{case}",
                    source_kind="chatgpt_official_export",
                    captured_at="2026-08-10T00:00:00Z",
                )
                write_manifest(manifest, manifest_path, evidence_root=evidence)
                output = root / "output"

                with self.assertRaisesRegex(ChatGPTImportError, error_pattern):
                    import_chatgpt_export(
                        evidence_root=evidence,
                        manifest_path=manifest_path,
                        output_root=output,
                        identity_scope="fixture",
                    )

                self.assertFalse(tuple(output.glob("decoded/**/*.ndjson")))

    def test_preserves_multiple_roots_and_reports_anomaly(self) -> None:
        conversation = self.conversation()
        conversation["mapping"]["orphan-root"] = {
            "id": "orphan-root",
            "message": None,
            "parent": None,
            "children": [],
        }
        self.write_conversations([conversation])
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        self.assertEqual(result.warnings, 1)
        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            diagnostic = database.execute(
                "SELECT severity, code, details_json FROM diagnostics"
            ).fetchone()
        self.assertEqual(diagnostic[:2], ("warning", "root_count_anomaly"))
        self.assertEqual(json.loads(diagnostic[2]), {"root_count": 2})

    def test_missing_children_field_is_not_synthesized_in_snapshot_version(self) -> None:
        conversation = self.conversation()
        for node in conversation["mapping"].values():
            node.pop("children")
        self.write_conversations([conversation])
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            rows = database.execute(
                "SELECT declared_children_json, raw_json FROM node_versions ORDER BY id"
            ).fetchall()
        self.assertTrue(all(row[0] is None for row in rows))
        self.assertTrue(all("children" not in json.loads(row[1]) for row in rows))

    def test_same_snapshot_and_hash_is_a_strict_no_op_but_conflict_fails(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        first = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        first_mtime = first.ndjson_path.stat().st_mtime_ns

        second = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        self.assertEqual(second.status, "no_op")
        self.assertEqual(second.ndjson_path.stat().st_mtime_ns, first_mtime)
        with contextlib.closing(sqlite3.connect(second.database_path)) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT count(*) FROM import_runs").fetchone()[0], 1)

        conflicting = self.root / "conflicting"
        conflicting.mkdir()
        changed = self.conversation()
        changed["title"] = "Changed payload"
        (conflicting / "conversations-000.json").write_text(
            json.dumps([changed]), encoding="utf-8"
        )
        conflicting_manifest = self.root / "conflicting-manifest.json"
        manifest = build_manifest(
            conflicting,
            source_id="chatgpt-official-fixture",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-10T00:00:00+08:00",
        )
        write_manifest(manifest, conflicting_manifest, evidence_root=conflicting)

        with self.assertRaisesRegex(ChatGPTImportError, "different evidence hash"):
            import_chatgpt_export(
                evidence_root=conflicting,
                manifest_path=conflicting_manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_no_op_keeps_same_native_message_id_and_payload_scoped_per_conversation(self) -> None:
        first = self.conversation()
        second = self.conversation()
        second["id"] = "conversation-2"
        self.write_conversations([first, second])
        self.write_manifest()

        imported = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        repeated = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        self.assertEqual(imported.messages, 2)
        self.assertEqual(repeated.status, "no_op")
        self.assertEqual(repeated.messages, 2)

    def test_same_snapshot_and_tree_with_different_capture_metadata_fails(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        changed_manifest_path = self.root / "changed-capture-manifest.json"
        changed_manifest = build_manifest(
            self.evidence,
            source_id="chatgpt-official-fixture",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-11T00:00:00+08:00",
        )
        write_manifest(
            changed_manifest, changed_manifest_path, evidence_root=self.evidence
        )

        with self.assertRaisesRegex(ChatGPTImportError, "capture metadata"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=changed_manifest_path,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_no_op_detects_changed_database_payload(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            database.execute(
                "UPDATE conversation_versions SET raw_json = '{}' WHERE id = 1"
            )
            database.commit()

        with self.assertRaisesRegex(ChatGPTImportError, "raw JSON hash changed"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_no_op_detects_changed_current_branch_projection(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            database.execute("DELETE FROM current_branch_nodes")
            database.commit()

        with self.assertRaisesRegex(ChatGPTImportError, "snapshot (?:state|invariant)"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_snapshot_digest_details_never_embed_large_raw_payloads(self) -> None:
        conversation = self.conversation()
        large_marker = "large-raw-marker-" + ("x" * 512_000)
        conversation["mapping"]["message-1"]["message"]["content"]["parts"] = [
            large_marker
        ]
        self.write_conversations([conversation])
        self.write_manifest()
        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            details = database.execute(
                "SELECT details_json FROM snapshot_state_digests"
            ).fetchone()[0]
        self.assertLess(len(details), 10_000)
        self.assertNotIn("large-raw-marker", details)

    def test_no_op_rejects_normalized_asset_group_and_claim_tampering(self) -> None:
        mutations = {
            "normalized-column": "UPDATE conversation_versions SET title = 'tampered'",
            "asset-projection": "DELETE FROM message_asset_refs",
            "group-projection": "UPDATE group_message_versions SET text = 'tampered'",
            "claims": "DELETE FROM snapshot_claims",
        }
        for case, mutation in mutations.items():
            with self.subTest(case=case):
                case_root = self.root / case
                evidence = case_root / "evidence"
                evidence.mkdir(parents=True)
                conversation = self.conversation()
                conversation["mapping"]["message-1"]["message"]["content"] = {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://file-asset-1",
                }
                (evidence / "conversations-000.json").write_text(
                    json.dumps([conversation]), encoding="utf-8"
                )
                (evidence / "file-asset-1.dat").write_bytes(b"synthetic asset")
                (evidence / "group_chats.json").write_text(
                    json.dumps(
                        {
                            "chats": [
                                {
                                    "id": "group-1",
                                    "messages": [
                                        {
                                            "id": "group-message-1",
                                            "role": "user",
                                            "text": "group searchable",
                                            "attachments": [],
                                        }
                                    ],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                manifest_path = case_root / "manifest.json"
                manifest = build_manifest(
                    evidence,
                    source_id=f"snapshot-{case}",
                    source_kind="chatgpt_official_export",
                    captured_at="2026-08-10T00:00:00Z",
                )
                write_manifest(manifest, manifest_path, evidence_root=evidence)
                output = case_root / "vault"
                imported = import_chatgpt_export(
                    evidence_root=evidence,
                    manifest_path=manifest_path,
                    output_root=output,
                    identity_scope="fixture",
                )
                with contextlib.closing(sqlite3.connect(imported.database_path)) as database:
                    database.execute(mutation)
                    database.commit()

                with self.assertRaises(ChatGPTImportError):
                    import_chatgpt_export(
                        evidence_root=evidence,
                        manifest_path=manifest_path,
                        output_root=output,
                        identity_scope="fixture",
                    )

    def test_no_op_detects_a_missing_external_fts_index(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            database.execute("INSERT INTO message_fts(message_fts) VALUES('delete-all')")
            database.commit()

        with self.assertRaisesRegex(ChatGPTImportError, "FTS index integrity"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_no_op_rejects_a_noncanonical_stored_decoded_path_before_using_it(self) -> None:
        self.write_conversations([self.conversation()])
        self.write_manifest()
        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            database.execute(
                "UPDATE snapshots SET decoded_ndjson_path = ?",
                (str(result.ndjson_path.resolve()),),
            )
            database.commit()

        with self.assertRaisesRegex(ChatGPTImportError, "stored decoded NDJSON path changed"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_later_snapshot_records_absence_without_deleting_identity_or_history(self) -> None:
        snapshots = self.root / "snapshots"
        snapshots.mkdir()
        first_conversation = self.conversation()
        first_evidence, first_manifest = self.make_snapshot(
            parent=snapshots,
            snapshot_key="snapshot-1",
            conversations=[first_conversation],
        )
        import_chatgpt_export(
            evidence_root=first_evidence,
            manifest_path=first_manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        second_conversation = self.conversation()
        second_conversation["id"] = "conversation-2"
        second_evidence, second_manifest = self.make_snapshot(
            parent=snapshots,
            snapshot_key="snapshot-2",
            conversations=[second_conversation],
        )

        second = import_chatgpt_export(
            evidence_root=second_evidence,
            manifest_path=second_manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(second.database_path)) as database:
            identities = database.execute(
                "SELECT count(*) FROM conversation_identities"
            ).fetchone()[0]
            versions = database.execute(
                "SELECT count(*) FROM conversation_versions"
            ).fetchone()[0]
            absent = database.execute(
                """SELECT sa.entity_kind, sa.identity_key
                     FROM source_absences sa
                     JOIN snapshots s ON s.id = sa.snapshot_id
                    WHERE s.snapshot_key = 'snapshot-2' AND sa.entity_kind = 'conversation'"""
            ).fetchall()
        self.assertEqual(identities, 2)
        self.assertEqual(versions, 2)
        self.assertEqual(
            absent,
            [
                (
                    "conversation",
                    "openai/chatgpt-official/chatgpt-account-fixture/conversations/conversation-1",
                )
            ],
        )

    def test_backfilled_older_snapshot_recomputes_absence_in_time_order(self) -> None:
        snapshots = self.root / "snapshots"
        snapshots.mkdir()
        later = self.conversation()
        later["id"] = "later-only"
        later_evidence = snapshots / "later"
        later_evidence.mkdir()
        (later_evidence / "conversations-000.json").write_text(
            json.dumps([later]), encoding="utf-8"
        )
        later_manifest_path = snapshots / "later-manifest.json"
        later_manifest = build_manifest(
            later_evidence,
            source_id="later",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-12T00:00:00Z",
        )
        write_manifest(later_manifest, later_manifest_path, evidence_root=later_evidence)
        import_chatgpt_export(
            evidence_root=later_evidence,
            manifest_path=later_manifest_path,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        earlier = self.conversation()
        earlier["id"] = "earlier-only"
        earlier_evidence = snapshots / "earlier"
        earlier_evidence.mkdir()
        (earlier_evidence / "conversations-000.json").write_text(
            json.dumps([earlier]), encoding="utf-8"
        )
        earlier_manifest_path = snapshots / "earlier-manifest.json"
        earlier_manifest = build_manifest(
            earlier_evidence,
            source_id="earlier",
            source_kind="chatgpt_official_export",
            captured_at="2026-08-10T00:00:00Z",
        )
        write_manifest(
            earlier_manifest, earlier_manifest_path, evidence_root=earlier_evidence
        )
        result = import_chatgpt_export(
            evidence_root=earlier_evidence,
            manifest_path=earlier_manifest_path,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            absences = database.execute(
                """SELECT s.snapshot_key, sa.entity_kind, sa.identity_key
                     FROM source_absences sa
                     JOIN snapshots s ON s.id = sa.snapshot_id
                    WHERE sa.entity_kind = 'conversation'
                    ORDER BY s.captured_at_us, sa.identity_key"""
            ).fetchall()
        self.assertEqual(
            absences,
            [
                (
                    "later",
                    "conversation",
                    "openai/chatgpt-official/chatgpt-account-fixture/conversations/earlier-only",
                )
            ],
        )
        repeated_later = import_chatgpt_export(
            evidence_root=later_evidence,
            manifest_path=later_manifest_path,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        self.assertEqual(repeated_later.status, "no_op")

    def test_absence_remains_materialized_in_every_later_snapshot_until_reappearance(self) -> None:
        snapshots = self.root / "snapshots"
        snapshots.mkdir()
        snapshots_and_rows = (
            ("present", "2026-08-10T00:00:00Z", [self.conversation()]),
            ("missing-1", "2026-08-11T00:00:00Z", []),
            ("missing-2", "2026-08-12T00:00:00Z", []),
            ("present-again", "2026-08-13T00:00:00Z", [self.conversation()]),
        )
        for snapshot_key, captured_at, conversations in snapshots_and_rows:
            evidence = snapshots / snapshot_key
            evidence.mkdir()
            (evidence / "conversations-000.json").write_text(
                json.dumps(conversations), encoding="utf-8"
            )
            manifest_path = snapshots / f"{snapshot_key}.manifest.json"
            manifest = build_manifest(
                evidence,
                source_id=snapshot_key,
                source_kind="chatgpt_official_export",
                captured_at=captured_at,
            )
            write_manifest(manifest, manifest_path, evidence_root=evidence)
            result = import_chatgpt_export(
                evidence_root=evidence,
                manifest_path=manifest_path,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            rows = database.execute(
                """SELECT s.snapshot_key, ps.snapshot_key
                     FROM source_absences sa
                     JOIN snapshots s ON s.id = sa.snapshot_id
                     JOIN snapshots ps ON ps.id = sa.prior_snapshot_id
                    WHERE sa.entity_kind = 'conversation'
                    ORDER BY s.captured_at_us"""
            ).fetchall()
        self.assertEqual(rows, [("missing-1", "present"), ("missing-2", "present")])

    def test_group_threads_and_messages_use_the_same_persistent_absence_semantics(self) -> None:
        snapshots = self.root / "group-snapshots"
        snapshots.mkdir()
        for day, snapshot_key, chats in (
            (
                10,
                "group-present",
                [
                    {
                        "id": "group-1",
                        "messages": [
                            {
                                "id": "group-message-1",
                                "role": "user",
                                "text": "synthetic",
                                "attachments": [],
                            }
                        ],
                    }
                ],
            ),
            (11, "group-missing-1", []),
            (12, "group-missing-2", []),
        ):
            evidence = snapshots / snapshot_key
            evidence.mkdir()
            (evidence / "conversations-000.json").write_text("[]", encoding="utf-8")
            (evidence / "group_chats.json").write_text(
                json.dumps({"chats": chats}), encoding="utf-8"
            )
            manifest_path = snapshots / f"{snapshot_key}.manifest.json"
            manifest = build_manifest(
                evidence,
                source_id=snapshot_key,
                source_kind="chatgpt_official_export",
                captured_at=f"2026-08-{day:02d}T00:00:00Z",
            )
            write_manifest(manifest, manifest_path, evidence_root=evidence)
            result = import_chatgpt_export(
                evidence_root=evidence,
                manifest_path=manifest_path,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            absences = database.execute(
                """SELECT s.snapshot_key, sa.entity_kind, count(*)
                     FROM source_absences sa
                     JOIN snapshots s ON s.id = sa.snapshot_id
                    WHERE sa.entity_kind IN ('group_thread', 'group_message')
                    GROUP BY s.snapshot_key, sa.entity_kind
                    ORDER BY s.captured_at_us, sa.entity_kind"""
            ).fetchall()
        self.assertEqual(
            absences,
            [
                ("group-missing-1", "group_message", 1),
                ("group-missing-1", "group_thread", 1),
                ("group-missing-2", "group_message", 1),
                ("group-missing-2", "group_thread", 1),
            ],
        )

    def test_unchanged_payload_reuses_version_but_keeps_each_snapshot_observation(self) -> None:
        snapshots = self.root / "snapshots"
        snapshots.mkdir()
        for day, snapshot_key in ((10, "first"), (11, "second")):
            evidence = snapshots / snapshot_key
            evidence.mkdir()
            (evidence / "conversations-000.json").write_text(
                json.dumps([self.conversation()]), encoding="utf-8"
            )
            manifest_path = snapshots / f"{snapshot_key}.manifest.json"
            manifest = build_manifest(
                evidence,
                source_id=snapshot_key,
                source_kind="chatgpt_official_export",
                captured_at=f"2026-08-{day:02d}T00:00:00Z",
            )
            write_manifest(manifest, manifest_path, evidence_root=evidence)
            result = import_chatgpt_export(
                evidence_root=evidence,
                manifest_path=manifest_path,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            counts = {
                table: database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "conversation_versions",
                    "conversation_observations",
                    "node_versions",
                    "node_observations",
                    "message_versions",
                    "message_observations",
                    "search_documents",
                )
            }
        self.assertEqual(
            counts,
            {
                "conversation_versions": 1,
                "conversation_observations": 2,
                "node_versions": 2,
                "node_observations": 4,
                "message_versions": 1,
                "message_observations": 2,
                "search_documents": 1,
            },
        )

    def test_generic_source_records_cover_auxiliary_json_and_project_group_chats(self) -> None:
        self.write_conversations([self.conversation()])
        (self.evidence / "group_chats.json").write_text(
            json.dumps(
                {
                    "chats": [
                        {
                            "id": "group-1",
                            "name": "Synthetic group",
                            "assistant_name": "assistant",
                            "created_at": "2026-08-10T00:00:00Z",
                            "updated_at": "2026-08-10T00:01:00Z",
                            "last_action_at": "2026-08-10T00:01:00Z",
                            "last_read_at": "2026-08-10T00:01:00Z",
                            "members": [],
                            "should_auto_respond": False,
                            "workspace_id": "workspace-1",
                            "messages": [
                                {
                                    "id": "group-message-1",
                                    "role": "user",
                                    "text": "synthetic group message",
                                    "created_at": "2026-08-10T00:00:00Z",
                                    "updated_at": "2026-08-10T00:00:00Z",
                                    "attachments": [
                                        {
                                            "type": "file",
                                            "target_id": "file-group-missing",
                                            "title": "missing",
                                            "desc": None,
                                            "url": None,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.evidence / "shared_conversations.json").write_text(
            json.dumps([{"id": "shared-1"}, {"id": "shared-2"}]), encoding="utf-8"
        )
        (self.evidence / "user.json").write_text(
            json.dumps({"id": "user-1", "future": "preserved"}), encoding="utf-8"
        )
        (self.evidence / "ads.json").write_text("{}", encoding="utf-8")
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        self.assertEqual(result.source_records, 8)
        self.assertEqual(result.claims["source_complete"], "pass")
        self.assertEqual(result.claims["extraction_complete"], "pass")
        self.assertEqual(result.claims["reconciliation_complete"], "partial")
        self.assertTrue(result.source_records_ndjson_path.is_file())
        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            source_kinds = database.execute(
                """SELECT record_kind, count(*) FROM source_records
                     GROUP BY record_kind ORDER BY record_kind"""
            ).fetchall()
            projected = (
                database.execute("SELECT count(*) FROM group_thread_observations").fetchone()[0],
                database.execute("SELECT count(*) FROM group_message_observations").fetchone()[0],
            )
            finding = database.execute(
                "SELECT code FROM diagnostics WHERE code = 'group_asset_reference_unresolved'"
            ).fetchone()
            group_search_hits = database.execute(
                "SELECT count(*) FROM message_fts WHERE message_fts MATCH ?",
                ("group",),
            ).fetchone()[0]
        self.assertEqual(
            source_kinds,
            [
                ("conversation", 1),
                ("provider_json_file_root", 5),
                ("shared_conversation", 2),
            ],
        )
        self.assertEqual(projected, (1, 1))
        self.assertEqual(finding, ("group_asset_reference_unresolved",))
        self.assertEqual(group_search_hits, 1)

    def test_every_json_file_has_a_root_record_and_array_elements_remain_addressable(self) -> None:
        self.write_conversations([self.conversation()])
        (self.evidence / "shared_conversations.json").write_text("[]", encoding="utf-8")
        (self.evidence / "ads.json").write_text("{}", encoding="utf-8")
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            records = database.execute(
                """SELECT source_file_path, json_pointer, record_scope, record_kind
                     FROM source_records
                    ORDER BY source_file_path, json_pointer"""
            ).fetchall()
        self.assertEqual(
            records,
            [
                ("ads.json", "", "file_root", "provider_json_file_root"),
                ("conversations-000.json", "", "file_root", "provider_json_file_root"),
                ("conversations-000.json", "/0", "element", "conversation"),
                (
                    "shared_conversations.json",
                    "",
                    "file_root",
                    "provider_json_file_root",
                ),
            ],
        )
        self.assertEqual(result.source_records, 4)

    def test_doctor_rejects_group_chat_shapes_that_import_cannot_project(self) -> None:
        self.write_conversations([self.conversation()])
        (self.evidence / "group_chats.json").write_text(
            json.dumps({"chats": "not-an-array"}), encoding="utf-8"
        )
        self.write_manifest()

        with self.assertRaisesRegex(ChatGPTImportError, "must contain a chats array"):
            doctor_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
            )
        with self.assertRaisesRegex(ChatGPTImportError, "must contain a chats array"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )

    def test_file_coverage_and_claims_account_for_json_dat_and_opaque_html(self) -> None:
        self.write_conversations([self.conversation()])
        (self.evidence / "shared_conversations.json").write_text("[]", encoding="utf-8")
        (self.evidence / "file-unreferenced.dat").write_bytes(b"synthetic-binary")
        (self.evidence / "chat.html").write_text("<html>synthetic</html>", encoding="utf-8")
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            coverage = database.execute(
                """SELECT evidence_path, media_class, handling_status,
                          source_record_count, asset_observation_count
                     FROM source_file_coverage ORDER BY evidence_path"""
            ).fetchall()
            claims = {
                claim: (status, json.loads(details))
                for claim, status, details in database.execute(
                    "SELECT claim, status, details_json FROM snapshot_claims"
                )
            }
        self.assertEqual(
            coverage,
            [
                ("chat.html", "html", "preserved_opaque", 0, 0),
                ("conversations-000.json", "json", "decoded", 2, 0),
                ("file-unreferenced.dat", "dat", "cas_verified", 0, 1),
                ("shared_conversations.json", "json", "decoded", 1, 0),
            ],
        )
        self.assertEqual(claims["source_complete"][0], "pass")
        self.assertEqual(claims["extraction_complete"][0], "pass")
        self.assertEqual(claims["reconciliation_complete"][0], "partial")
        self.assertEqual(claims["source_complete"][1]["manifest_files"], 4)
        self.assertEqual(claims["extraction_complete"][1]["json_root_records"], 2)
        self.assertEqual(claims["extraction_complete"][1]["cas_verified_files"], 1)
        self.assertEqual(claims["extraction_complete"][1]["opaque_preserved_files"], 1)

    def test_preserves_and_resolves_asset_observations_and_soft_references(self) -> None:
        conversation = self.conversation()
        message = conversation["mapping"]["message-1"]["message"]
        message["content"] = {
            "content_type": "multimodal_text",
            "parts": [
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://file-asset-1",
                },
                {
                    "content_type": "audio_asset_pointer",
                    "audio_asset_pointer": "file-service://file-missing-audio",
                },
            ],
        }
        message["metadata"] = {
            "content_references": [
                {"asset_pointer_links": ["file-service://file-asset-1"]}
            ],
            "attachments": [
                {"id": "file-asset-1", "name": "upload.png", "size": 999},
                {"file_id": "file-missing-attachment", "name": "missing.txt"},
            ]
        }
        self.write_conversations([conversation])
        (self.evidence / "file-asset-1.dat").write_bytes(b"synthetic asset bytes")
        (self.evidence / "conversation_asset_file_names.json").write_text(
            json.dumps({"file-asset-1": "friendly.png"}), encoding="utf-8"
        )
        (self.evidence / "library_files.json").write_text(
            json.dumps(
                [
                    {
                        "file_id": "file-asset-1",
                        "file_name": "friendly.png",
                        "file_size_bytes": 21,
                        "mime_type": "image/png",
                        "state": "ready",
                        "origination_thread_id": "conversation-1",
                        "origination_message_id": "message-1",
                    },
                    {
                        "file_id": "file-library-missing",
                        "file_name": "gone.pdf",
                        "file_size_bytes": 100,
                        "mime_type": "application/pdf",
                        "state": "failed",
                        "origination_thread_id": "missing-conversation",
                        "origination_message_id": "missing-message",
                    },
                ]
            ),
            encoding="utf-8",
        )
        self.write_manifest()

        result = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )

        with contextlib.closing(sqlite3.connect(result.database_path)) as database:
            observation = database.execute(
                """SELECT evidence_path, declared_name, size_bytes
                     FROM asset_observations"""
            ).fetchone()
            message_refs = database.execute(
                """SELECT reference_kind, reference_value,
                          normalized_reference, asset_observation_id IS NOT NULL
                     FROM message_asset_refs ORDER BY ordinal"""
            ).fetchall()
            library_refs = database.execute(
                """SELECT evidence_path, declared_name,
                          asset_observation_id IS NOT NULL,
                          json_extract(raw_json, '$.state')
                     FROM library_asset_refs ORDER BY evidence_path"""
            ).fetchall()
            diagnostics = database.execute(
                "SELECT code, count(*) FROM diagnostics GROUP BY code ORDER BY code"
            ).fetchall()
            source_refs = database.execute(
                """SELECT library_file_reference, reference_kind,
                          resolution_status, resolved_identity_key
                     FROM library_source_refs
                    ORDER BY library_file_reference, reference_kind"""
            ).fetchall()
            asset = database.execute(
                "SELECT sha256, size_bytes, storage_path FROM assets"
            ).fetchone()

        self.assertEqual(observation, ("file-asset-1.dat", "friendly.png", 21))
        self.assertEqual(
            message_refs,
            [
                ("asset_pointer", "file-service://file-asset-1", "file-asset-1", 1),
                (
                    "audio_asset_pointer",
                    "file-service://file-missing-audio",
                    "file-missing-audio",
                    0,
                ),
                (
                    "asset_pointer_link",
                    "file-service://file-asset-1",
                    "file-asset-1",
                    1,
                ),
                ("attachment_id", "file-asset-1", "file-asset-1", 1),
                (
                    "attachment_file_id",
                    "file-missing-attachment",
                    "file-missing-attachment",
                    0,
                ),
            ],
        )
        self.assertEqual(
            library_refs,
            [
                ("file-asset-1", "friendly.png", 1, "ready"),
                ("file-library-missing", "gone.pdf", 0, "failed"),
            ],
        )
        self.assertEqual(
            diagnostics,
            [
                ("asset_reference_unresolved", 2),
                ("library_asset_unresolved", 1),
                ("library_message_reference_unresolved", 1),
                ("library_thread_reference_unresolved", 1),
            ],
        )
        self.assertEqual(
            source_refs,
            [
                (
                    "file-asset-1",
                    "origination_message_id",
                    "resolved",
                    "openai/chatgpt-official/chatgpt-account-fixture/conversations/conversation-1/messages/message-1",
                ),
                (
                    "file-asset-1",
                    "origination_thread_id",
                    "resolved",
                    "openai/chatgpt-official/chatgpt-account-fixture/conversations/conversation-1",
                ),
                (
                    "file-library-missing",
                    "origination_message_id",
                    "unresolved",
                    None,
                ),
                (
                    "file-library-missing",
                    "origination_thread_id",
                    "unresolved",
                    None,
                ),
            ],
        )
        content_asset = self.output / asset[2]
        self.assertTrue(content_asset.is_file())
        self.assertEqual(content_asset.stat().st_size, asset[1])
        self.assertNotEqual(
            content_asset.stat().st_ino,
            (self.evidence / "file-asset-1.dat").stat().st_ino,
        )
        content_asset_mtime = content_asset.stat().st_mtime_ns
        no_op = import_chatgpt_export(
            evidence_root=self.evidence,
            manifest_path=self.manifest,
            output_root=self.output,
            identity_scope="chatgpt-account-fixture",
        )
        self.assertEqual(no_op.status, "no_op")
        self.assertEqual(content_asset.stat().st_mtime_ns, content_asset_mtime)
        content_asset.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ChatGPTImportError, "CAS asset"):
            import_chatgpt_export(
                evidence_root=self.evidence,
                manifest_path=self.manifest,
                output_root=self.output,
                identity_scope="chatgpt-account-fixture",
            )


if __name__ == "__main__":
    unittest.main()
