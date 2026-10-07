from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from personal_vault.database import connect_database
from personal_vault.reader import (
    ReaderError,
    ReaderRepository,
    connect_reader_database,
    create_reader_server,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ReaderFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.database_path = root / "canonical" / "archive.sqlite"
        self.asset_bytes = b"synthetic attachment\n"
        self.asset_sha256 = hashlib.sha256(self.asset_bytes).hexdigest()
        self.asset_path = root / "assets" / "sha256" / self.asset_sha256[:2] / self.asset_sha256
        self.asset_path.parent.mkdir(parents=True)
        self.asset_path.write_bytes(self.asset_bytes)
        self.c1_key = "openai/chatgpt-official/fixture/conversations/c1"
        self.c2_key = "openai/chatgpt-official/fixture/conversations/c2"
        self.group_key = "openai/chatgpt-official/fixture/group_threads/g1"
        self._build()

    def _build(self) -> None:
        database = connect_database(self.database_path)
        try:
            database.execute(
                "INSERT INTO sources(id, source_key, provider, kind, identity_scope) "
                "VALUES(1, 'fixture-source', 'openai', 'chatgpt_official_export', 'fixture')"
            )
            for snapshot_id, key, captured_at, captured_at_us in (
                (1, "fixture-older", "2026-07-01T00:00:00Z", 1_751_328_000_000_000),
                (2, "fixture-newer", "2026-08-01T00:00:00Z", 1_753_987_200_000_000),
            ):
                database.execute(
                    """INSERT INTO snapshots(
                           id, source_id, snapshot_key, captured_at, captured_at_us,
                           raw_source_json, evidence_tree_sha256, evidence_manifest_sha256,
                           evidence_manifest_path, decoded_ndjson_path, decoded_ndjson_sha256,
                           source_records_ndjson_path, source_records_ndjson_sha256, imported_at)
                         VALUES (?, 1, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        key,
                        captured_at,
                        captured_at_us,
                        f"tree-{snapshot_id}",
                        f"manifest-{snapshot_id}",
                        f"manifests/{key}.json",
                        f"decoded/{key}.ndjson",
                        f"decoded-{snapshot_id}",
                        f"decoded/{key}-source.ndjson",
                        f"source-{snapshot_id}",
                        captured_at,
                    ),
                )
                database.execute(
                    """INSERT INTO import_runs(
                           id, source_id, snapshot_id, started_at, completed_at, status,
                           importer_version, schema_version, config_sha256,
                           source_records_count, conversations_count, nodes_count,
                           messages_count, group_threads_count, group_messages_count,
                           warnings_count)
                         VALUES (?, 1, ?, ?, ?, 'completed', 2, 2, ?, 2, 2, 7, 4, 0, 0, ?)""",
                    (
                        f"run-{snapshot_id}",
                        snapshot_id,
                        captured_at,
                        captured_at,
                        f"config-{snapshot_id}",
                        0 if snapshot_id == 1 else 1,
                    ),
                )
                for claim, status in (
                    ("source_complete", "pass"),
                    ("extraction_complete", "pass"),
                    ("reconciliation_complete", "partial"),
                    ("account_complete", "not_applicable"),
                ):
                    database.execute(
                        "INSERT INTO snapshot_claims VALUES (?, ?, ?, '{}')",
                        (snapshot_id, claim, status),
                    )

            source_record_id = 0

            def source_record(snapshot_id: int, native_id: str, index: int) -> int:
                nonlocal source_record_id
                source_record_id += 1
                raw = {"id": native_id, "mapping": {}}
                database.execute(
                    """INSERT INTO source_records(
                           id, snapshot_id, record_kind, provider_record_kind, record_scope,
                           native_id, source_file_path, array_index, json_pointer, raw_sha256,
                           raw_json, ndjson_line_number)
                         VALUES (?, ?, 'conversation', 'conversation', 'element', ?,
                                 'conversations-fixture.json', ?, ?, ?, ?, ?)""",
                    (
                        source_record_id,
                        snapshot_id,
                        native_id,
                        index,
                        f"/{index}",
                        hashlib.sha256(_json(raw).encode()).hexdigest(),
                        _json(raw),
                        source_record_id,
                    ),
                )
                return source_record_id

            records = {
                (1, "c1"): source_record(1, "c1", 0),
                (1, "c3"): source_record(1, "c3", 1),
                (2, "c1"): source_record(2, "c1", 0),
                (2, "c2"): source_record(2, "c2", 1),
            }

            identities = {
                "c1": (1, self.c1_key),
                "c2": (2, self.c2_key),
                "c3": (3, "openai/chatgpt-official/fixture/conversations/c3"),
            }
            for native_id, (identity_id, identity_key) in identities.items():
                database.execute(
                    "INSERT INTO conversation_identities VALUES (?, 1, ?, ?)",
                    (identity_id, identity_key, native_id),
                )

            conversation_versions = [
                (1, 1, "c1-v1", "中文归档", 1_719_792_000.0, 1_719_792_060.0),
                (2, 1, "c1-v2", "中文归档（更新）", 1_719_792_000.0, 1_722_470_460.0),
                (3, 2, "c2-v1", "新会话", 1_722_470_500.0, 1_722_470_500.0),
                (4, 3, "c3-v1", "后续源缺席", 1_719_792_500.0, 1_719_792_500.0),
            ]
            for version_id, identity_id, marker, title, created, updated in conversation_versions:
                raw = {"id": marker, "title": title}
                database.execute(
                    """INSERT INTO conversation_versions(
                           id, conversation_identity_id, raw_sha256, raw_json, unknown_json,
                           title, create_time, update_time)
                         VALUES (?, ?, ?, ?, '{}', ?, ?, ?)""",
                    (
                        version_id,
                        identity_id,
                        hashlib.sha256(marker.encode()).hexdigest(),
                        _json(raw),
                        title,
                        created,
                        updated,
                    ),
                )

            node_specs = {
                "c1-root": (1, "n-root"),
                "c1-user": (1, "n-user"),
                "c1-main": (1, "n-main"),
                "c1-alt": (1, "n-alt"),
                "c2-root": (2, "n2-root"),
                "c2-user": (2, "n2-user"),
                "c3-root": (3, "n3-root"),
            }
            node_identity_ids: dict[str, int] = {}
            for offset, (name, (identity_id, native_id)) in enumerate(node_specs.items(), 1):
                identity_key = f"{identities[f'c{identity_id}'][1]}/nodes/{native_id}"
                node_identity_ids[name] = offset
                database.execute(
                    "INSERT INTO node_identities VALUES (?, ?, ?, ?)",
                    (offset, identity_id, identity_key, native_id),
                )

            message_specs = {
                "c1-user": (1, 1, "m-user"),
                "c1-main": (2, 1, "m-main"),
                "c1-alt": (3, 1, "m-alt"),
                "c2-user": (4, 2, "m2-user"),
            }
            message_identity_ids: dict[str, int] = {}
            for name, (message_id, identity_id, native_id) in message_specs.items():
                message_identity_ids[name] = message_id
                database.execute(
                    "INSERT INTO message_identities VALUES (?, ?, ?, ?)",
                    (
                        message_id,
                        identity_id,
                        f"{identities[f'c{identity_id}'][1]}/messages/{native_id}",
                        native_id,
                    ),
                )

            message_versions = {
                "user-v1": (1, "c1-user", "user", "甲短句，百分比%仅作合成测试。", "fixture-user", 1_719_792_010.0),
                "main-v1": (2, "c1-main", "assistant", "稳定全文检索词 alpha。", "fixture-model-a", 1_719_792_020.0),
                "alt-v1": (3, "c1-alt", "assistant", "替代回答，仅用于分支测试。", "fixture-model-b", 1_719_792_021.0),
                "main-v2": (4, "c1-main", "assistant", "稳定全文检索词 alpha，已更新。", "fixture-model-a", 1_722_470_420.0),
                "c2-v1": (5, "c2-user", "user", "第二条合成会话。", "fixture-user", 1_722_470_510.0),
            }
            for _name, (version_id, identity_name, role, text, model, created) in message_versions.items():
                raw = {
                    "id": f"message-version-{version_id}",
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": [text]},
                    "create_time": created,
                    "metadata": {"model_slug": model},
                }
                identity_id = message_identity_ids[identity_name]
                database.execute(
                    """INSERT INTO message_versions(
                           id, message_identity_id, raw_sha256, raw_json, unknown_json,
                           author_role, author_name, content_type, create_time, update_time,
                           status, recipient)
                         VALUES (?, ?, ?, ?, '{}', ?, NULL, 'text', ?, NULL, 'finished_successfully', 'all')""",
                    (
                        version_id,
                        identity_id,
                        hashlib.sha256(_json(raw).encode()).hexdigest(),
                        _json(raw),
                        role,
                        created,
                    ),
                )
                database.execute(
                    """INSERT INTO search_documents(
                           document_kind, message_version_id, group_message_version_id,
                           identity_key, author_role, content_type, content)
                         VALUES ('conversation_message', ?, NULL, ?, ?, 'text', ?)""",
                    (
                        version_id,
                        f"message-identity-{identity_id}",
                        role,
                        text,
                    ),
                )

            node_version_id = 0
            node_observation_id = 0
            message_observation_id = 0

            def observe_conversation(
                snapshot_id: int,
                conversation: str,
                conversation_version_id: int,
                nodes: list[tuple[str, str | None, str | None, int | None]],
                current: list[str],
            ) -> dict[str, int]:
                nonlocal node_version_id, node_observation_id, message_observation_id
                conversation_identity_id = identities[conversation][0]
                source_id = records[(snapshot_id, conversation)]
                tip_key = database.execute(
                    "SELECT identity_key FROM node_identities WHERE id = ?",
                    (node_identity_ids[current[-1]],),
                ).fetchone()[0]
                cursor = database.execute(
                    """INSERT INTO conversation_observations(
                           snapshot_id, conversation_identity_id, conversation_version_id,
                           source_record_id, current_node_identity_key)
                         VALUES (?, ?, ?, ?, ?)""",
                    (snapshot_id, conversation_identity_id, conversation_version_id, source_id, tip_key),
                )
                conversation_observation_id = int(cursor.lastrowid)
                observed: dict[str, int] = {}
                for pointer_index, (node_name, parent_name, message_name, message_version_id) in enumerate(nodes):
                    node_version_id += 1
                    raw = {"id": node_name, "parent": parent_name, "children": []}
                    database.execute(
                        """INSERT INTO node_versions(
                               id, node_identity_id, raw_sha256, raw_json, unknown_json,
                               declared_children_json)
                             VALUES (?, ?, ?, ?, '{}', '[]')""",
                        (
                            node_version_id,
                            node_identity_ids[node_name],
                            hashlib.sha256(f"{snapshot_id}:{node_name}".encode()).hexdigest(),
                            _json(raw),
                        ),
                    )
                    node_observation_id += 1
                    database.execute(
                        """INSERT INTO node_observations(
                               id, snapshot_id, node_identity_id, node_version_id,
                               source_record_id, json_pointer, parent_node_identity_id,
                               message_identity_id)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            node_observation_id,
                            snapshot_id,
                            node_identity_ids[node_name],
                            node_version_id,
                            source_id,
                            f"/{pointer_index}/mapping/{node_name}",
                            node_identity_ids[parent_name] if parent_name else None,
                            message_identity_ids[message_name] if message_name else None,
                        ),
                    )
                    observed[node_name] = node_observation_id
                    if message_name and message_version_id:
                        message_observation_id += 1
                        database.execute(
                            """INSERT INTO message_observations(
                                   id, snapshot_id, message_identity_id, message_version_id,
                                   node_observation_id, source_record_id, json_pointer)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                message_observation_id,
                                snapshot_id,
                                message_identity_ids[message_name],
                                message_version_id,
                                node_observation_id,
                                source_id,
                                f"/{pointer_index}/mapping/{node_name}/message",
                            ),
                        )
                for depth, node_name in enumerate(current):
                    database.execute(
                        "INSERT INTO current_branch_nodes VALUES (?, ?, ?, ?, ?)",
                        (
                            snapshot_id,
                            conversation_identity_id,
                            conversation_observation_id,
                            node_identity_ids[node_name],
                            depth,
                        ),
                    )
                return observed

            c1_nodes_s1 = observe_conversation(
                1,
                "c1",
                1,
                [
                    ("c1-root", None, None, None),
                    ("c1-user", "c1-root", "c1-user", 1),
                    ("c1-main", "c1-user", "c1-main", 2),
                    ("c1-alt", "c1-user", "c1-alt", 3),
                ],
                ["c1-root", "c1-user", "c1-main"],
            )
            observe_conversation(
                1,
                "c3",
                4,
                [("c3-root", None, None, None)],
                ["c3-root"],
            )
            c1_nodes_s2 = observe_conversation(
                2,
                "c1",
                2,
                [
                    ("c1-root", None, None, None),
                    ("c1-user", "c1-root", "c1-user", 1),
                    ("c1-main", "c1-user", "c1-main", 4),
                    ("c1-alt", "c1-user", "c1-alt", 3),
                ],
                ["c1-root", "c1-user", "c1-main"],
            )
            observe_conversation(
                2,
                "c2",
                3,
                [
                    ("c2-root", None, None, None),
                    ("c2-user", "c2-root", "c2-user", 5),
                ],
                ["c2-root", "c2-user"],
            )

            database.execute(
                "INSERT INTO assets VALUES (1, ?, ?, ?)",
                (
                    self.asset_sha256,
                    len(self.asset_bytes),
                    f"assets/sha256/{self.asset_sha256[:2]}/{self.asset_sha256}",
                ),
            )
            asset_observation_ids = {}
            for snapshot_id in (1, 2):
                cursor = database.execute(
                    """INSERT INTO asset_observations(
                           snapshot_id, asset_id, evidence_path, sha256, size_bytes,
                           declared_name, observation_kind, raw_json)
                         VALUES (?, 1, 'file-demo.dat', ?, ?, 'synthetic-note.txt',
                                 'physical_export_file', '{}')""",
                    (snapshot_id, self.asset_sha256, len(self.asset_bytes)),
                )
                asset_observation_ids[snapshot_id] = int(cursor.lastrowid)

            message_observation_by_snapshot = {}
            for snapshot_id, node_observation in ((1, c1_nodes_s1["c1-user"]), (2, c1_nodes_s2["c1-user"])):
                row = database.execute(
                    "SELECT id FROM message_observations WHERE snapshot_id = ? AND node_observation_id = ?",
                    (snapshot_id, node_observation),
                ).fetchone()
                message_observation_by_snapshot[snapshot_id] = int(row[0])
                database.execute(
                    """INSERT INTO message_asset_refs(
                           snapshot_id, message_observation_id, ordinal, reference_kind,
                           reference_value, normalized_reference, asset_observation_id,
                           resolution_status, resolution_method, candidate_count,
                           candidates_json, raw_json)
                         VALUES (?, ?, 0, 'asset_pointer', 'file-demo', 'file-demo.dat', ?,
                                 'resolved', 'exact', 1, '[]', '{}')""",
                    (snapshot_id, int(row[0]), asset_observation_ids[snapshot_id]),
                )

            database.execute(
                """INSERT INTO diagnostics(
                       import_run_id, snapshot_id, conversation_identity_id,
                       severity, code, details_json)
                     VALUES ('run-2', 2, 1, 'warning', 'fixture_warning', '{"fixture":true}')"""
            )
            database.execute(
                """INSERT INTO source_absences(
                       source_id, snapshot_id, prior_snapshot_id, entity_kind, identity_key)
                     VALUES (1, 2, 1, 'conversation', ?)""",
                (identities["c3"][1],),
            )
            group_raw = {"id": "g1", "name": "合成群聊"}
            database.execute(
                """INSERT INTO source_records(
                       id, snapshot_id, record_kind, provider_record_kind, record_scope,
                       native_id, source_file_path, array_index, json_pointer, raw_sha256,
                       raw_json, ndjson_line_number)
                     VALUES (5, 2, 'group_thread', 'group_thread', 'element', 'g1',
                             'group_chats.json', 0, '/0', ?, ?, 5)""",
                (hashlib.sha256(_json(group_raw).encode()).hexdigest(), _json(group_raw)),
            )
            database.execute(
                "INSERT INTO group_thread_identities VALUES (1, 1, ?, 'g1')",
                (self.group_key,),
            )
            database.execute(
                """INSERT INTO group_thread_versions(
                       id, group_thread_identity_id, raw_sha256, raw_json, unknown_json, name)
                     VALUES (1, 1, ?, ?, '{}', '合成群聊')""",
                (hashlib.sha256(b"group-v1").hexdigest(), _json(group_raw)),
            )
            database.execute(
                "INSERT INTO group_thread_observations VALUES (1, 2, 1, 1, 5, '/0')"
            )
            for message_id, role, text, created in (
                (1, "user", "群聊中的合成用户消息。", "2026-08-01T01:00:00Z"),
                (2, "assistant", "群聊中的合成助手消息。", "2026-08-01T01:01:00Z"),
            ):
                identity_key = f"{self.group_key}/messages/gm{message_id}"
                raw = {"id": f"gm{message_id}", "role": role, "text": text}
                database.execute(
                    "INSERT INTO group_message_identities VALUES (?, 1, ?, ?)",
                    (message_id, identity_key, f"gm{message_id}"),
                )
                database.execute(
                    """INSERT INTO group_message_versions(
                           id, group_message_identity_id, raw_sha256, raw_json,
                           unknown_json, role, text, created_at)
                         VALUES (?, ?, ?, ?, '{}', ?, ?, ?)""",
                    (
                        message_id,
                        message_id,
                        hashlib.sha256(_json(raw).encode()).hexdigest(),
                        _json(raw),
                        role,
                        text,
                        created,
                    ),
                )
                database.execute(
                    """INSERT INTO group_message_observations(
                           id, snapshot_id, group_message_identity_id,
                           group_message_version_id, group_thread_observation_id,
                           source_record_id, json_pointer)
                         VALUES (?, 2, ?, ?, 1, 5, ?)""",
                    (message_id, message_id, message_id, f"/0/messages/{message_id - 1}"),
                )
                database.execute(
                    """INSERT INTO search_documents(
                           document_kind, message_version_id, group_message_version_id,
                           identity_key, author_role, content_type, content)
                         VALUES ('group_message', NULL, ?, ?, ?, 'text', ?)""",
                    (message_id, identity_key, role, text),
                )
            database.execute(
                """INSERT INTO group_message_asset_refs(
                       snapshot_id, group_message_observation_id, ordinal,
                       reference_value, resolution_status, asset_observation_id,
                       candidates_json, raw_json)
                     VALUES (2, 1, 0, 'file-demo.dat', 'resolved', ?, '[]', '{}')""",
                (asset_observation_ids[2],),
            )
            database.execute(
                "UPDATE import_runs SET group_threads_count=1, group_messages_count=2 WHERE snapshot_id=2"
            )
            database.commit()
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            database.close()


class ReaderRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = ReaderFixture(self.root)
        self.repository = ReaderRepository(self.fixture.database_path, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_database_connection_is_read_only_query_only_and_schema_checked(self) -> None:
        database = connect_reader_database(self.fixture.database_path)
        try:
            self.assertEqual(database.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)
            with self.assertRaises(sqlite3.OperationalError):
                database.execute("INSERT INTO sources(source_key, provider, kind, identity_scope) VALUES('x','x','x','x')")
        finally:
            database.close()

    def test_snapshot_listing_and_conversation_pagination_are_deterministic(self) -> None:
        snapshots = self.repository.list_snapshots(limit=20, offset=0)
        self.assertEqual(snapshots["default_snapshot_id"], 2)
        self.assertEqual([item["snapshot_key"] for item in snapshots["items"]], ["fixture-newer", "fixture-older"])

        conversations = self.repository.list_conversations(snapshot_id=2, limit=1, offset=0)
        self.assertEqual(conversations["pagination"], {"limit": 1, "offset": 0, "total": 2, "next_offset": 1})
        self.assertEqual(conversations["items"][0]["identity_key"], self.fixture.c2_key)

    def test_search_uses_literal_like_for_short_queries_and_fts_for_long_queries(self) -> None:
        short = self.repository.search(snapshot_id=2, query="甲", limit=20, offset=0)
        self.assertEqual(short["mode"], "literal_like")
        self.assertEqual(short["items"][0]["conversation_identity_key"], self.fixture.c1_key)

        literal_percent = self.repository.search(snapshot_id=2, query="%", limit=20, offset=0)
        self.assertEqual(literal_percent["pagination"]["total"], 1)

        full_text = self.repository.search(snapshot_id=2, query="全文检索", limit=20, offset=0)
        self.assertEqual(full_text["mode"], "fts_trigram")
        self.assertEqual(full_text["items"][0]["conversation_identity_key"], self.fixture.c1_key)
        self.assertIn("全文检索", full_text["items"][0]["text"])

    def test_detail_restores_current_branch_alternatives_assets_sources_and_diagnostics(self) -> None:
        payload = self.repository.get_conversation(2, self.fixture.c1_key)
        conversation = payload["conversation"]
        self.assertEqual(
            [node["native_id"] for node in conversation["current_branch"]],
            ["n-root", "n-user", "n-main"],
        )
        self.assertEqual([node["native_id"] for node in conversation["alternative_nodes"]], ["n-alt"])
        user_message = conversation["current_branch"][1]["message"]
        self.assertEqual(user_message["attachments"][0]["sha256"], self.fixture.asset_sha256)
        self.assertEqual(user_message["attachments"][0]["status"], "resolved")
        self.assertTrue(user_message["attachments"][0]["download_url"].startswith("/api/assets/"))
        self.assertEqual(conversation["source"]["source_file_path"], "conversations-fixture.json")
        self.assertEqual(conversation["diagnostics"][0]["code"], "fixture_warning")
        self.assertEqual(conversation["current_branch"][2]["message"]["model"], "fixture-model-a")

    def test_group_threads_are_listed_searched_and_rendered_with_assets(self) -> None:
        listed = self.repository.list_group_threads(snapshot_id=2)
        self.assertEqual(listed["pagination"]["total"], 1)
        self.assertEqual(listed["items"][0]["identity_key"], self.fixture.group_key)
        searched = self.repository.list_group_threads(snapshot_id=2, query="合成助手")
        self.assertEqual(searched["pagination"]["total"], 1)
        missing = self.repository.list_group_threads(snapshot_id=2, query="不存在词")
        self.assertEqual(missing["pagination"]["total"], 0)

        payload = self.repository.get_group_thread(2, self.fixture.group_key)
        thread = payload["conversation"]
        self.assertEqual(thread["kind"], "group_thread")
        self.assertEqual(len(thread["current_branch"]), 2)
        self.assertEqual(thread["alternative_nodes"], [])
        attachment = thread["current_branch"][0]["message"]["attachments"][0]
        self.assertEqual(attachment["sha256"], self.fixture.asset_sha256)
        self.assertEqual(attachment["status"], "resolved")

    def test_analytics_are_deterministic_and_keep_absence_separate_from_deletion(self) -> None:
        analytics = self.repository.analytics(2)
        self.assertEqual(analytics["generated_from"], "deterministic_sql")
        self.assertEqual(analytics["llm_inference"], "disabled")
        self.assertEqual(analytics["branches"]["branch_points"], 1)
        self.assertEqual(analytics["branches"]["alternative_nodes"], 1)
        self.assertEqual(analytics["attachments"]["resolved"], 1)
        self.assertEqual(analytics["snapshot_quality"]["source_absences"], 1)
        self.assertEqual(analytics["snapshot_quality"]["source_deletions"], None)
        self.assertEqual(analytics["snapshot_deltas"][-1]["conversations"], {"added": 1, "changed": 1, "absent": 1})
        self.assertTrue(any(item["key"] == "fixture-model-a" for item in analytics["models"]))
        self.assertTrue(any(item["month"] == "2024-08" for item in analytics["activity"]))

    def test_cas_open_is_content_verified_and_fails_closed(self) -> None:
        with self.repository.open_asset(self.fixture.asset_sha256) as asset:
            self.assertEqual(asset.file.read(), self.fixture.asset_bytes)
            self.assertEqual(asset.download_name, "synthetic-note.txt")

        for invalid in ("../archive.sqlite", "a" * 63, "A" * 64, "g" * 64):
            with self.subTest(invalid=invalid), self.assertRaises(ReaderError):
                self.repository.open_asset(invalid)

        self.fixture.asset_path.write_bytes(b"changed attachment!\n")
        with self.assertRaises(ReaderError):
            self.repository.open_asset(self.fixture.asset_sha256)

        self.fixture.asset_path.unlink()
        self.fixture.asset_path.symlink_to(self.fixture.database_path)
        with self.assertRaises(ReaderError):
            self.repository.open_asset(self.fixture.asset_sha256)


class ReaderHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = ReaderFixture(self.root)
        self.server = create_reader_server(self.fixture.database_path, self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _get(self, path: str) -> tuple[bytes, urllib.response.addinfourl]:
        response = urllib.request.urlopen(f"{self.base_url}{path}", timeout=5)
        return response.read(), response

    def test_server_is_loopback_only_static_is_offline_and_apis_are_read_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        html, response = self._get("/")
        self.assertIn(b"Archive Reader", html)
        self.assertNotIn(b"https://", html)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

        app, _response = self._get("/static/app.js")
        self.assertNotIn(b"innerHTML", app)
        self.assertNotIn(b"https://", app)

        payload, _response = self._get("/api/snapshots")
        self.assertEqual(json.loads(payload)["default_snapshot_id"], 2)
        detail, _response = self._get(
            f"/api/conversations/{urllib.parse.quote(self.fixture.c1_key, safe='')}?snapshot_id=2"
        )
        self.assertEqual(json.loads(detail)["conversation"]["native_id"], "c1")
        groups, _response = self._get("/api/group-threads?snapshot_id=2")
        self.assertEqual(json.loads(groups)["pagination"]["total"], 1)
        group_detail, _response = self._get(
            f"/api/group-threads/{urllib.parse.quote(self.fixture.group_key, safe='')}?snapshot_id=2"
        )
        self.assertEqual(json.loads(group_detail)["conversation"]["kind"], "group_thread")

        asset, response = self._get(f"/api/assets/{self.fixture.asset_sha256}")
        self.assertEqual(asset, self.fixture.asset_bytes)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

        request = urllib.request.Request(f"{self.base_url}/api/snapshots", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 405)
        error.exception.close()

    def test_non_loopback_binding_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReaderError, "127.0.0.1"):
            create_reader_server(self.fixture.database_path, self.root, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
