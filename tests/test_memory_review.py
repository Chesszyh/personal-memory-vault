from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from personal_vault.memory_review import MemoryReviewError, create_memory_review_server
from personal_vault.memory_store import scan_memory_candidates
from tests.test_reader import ReaderFixture


class MemoryReviewHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.fixture = ReaderFixture(self.vault)
        canonical = sqlite3.connect(self.fixture.database_path)
        canonical.execute(
            "UPDATE search_documents SET content='我希望回答直接一些，这是合成审核候选。' "
            "WHERE message_version_id=1"
        )
        canonical.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
        canonical.commit()
        canonical.close()
        scan_memory_candidates(vault_root=self.vault, snapshot_id=2)
        self.server = create_memory_review_server(self.vault, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _get(self, path: str) -> tuple[dict[str, object], urllib.response.addinfourl]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read()), response

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_interactive_review_filters_context_edits_and_batch_decides(self) -> None:
        candidates, response = self._get("/api/candidates?status=pending&limit=50")
        self.assertEqual(candidates["pagination"]["total"], 1)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        candidate = candidates["items"][0]
        candidate_id = candidate["candidate_id"]

        detail, _response = self._get(
            f"/api/candidates/{urllib.parse.quote(candidate_id, safe='')}"
        )
        self.assertTrue(detail["context"]["source_available"])
        self.assertTrue(any(item["is_source"] for item in detail["context"]["messages"]))

        edited = self._post(
            f"/api/candidates/{urllib.parse.quote(candidate_id, safe='')}/edit",
            {"statement": "我希望回答更直接。"},
        )
        self.assertTrue(edited["edited"])
        decision = self._post(
            "/api/decisions",
            {"candidate_ids": [candidate_id], "status": "confirmed", "note": "fixture"},
        )
        self.assertEqual(decision["updated_count"], 1)
        confirmed, _response = self._get("/api/candidates?status=confirmed&limit=50")
        self.assertEqual(confirmed["items"][0]["statement"], "我希望回答更直接。")

        confidence, _response = self._get(
            "/api/candidates?status=confirmed&min_confidence=0.5&after_time=0&before_time=4102444800"
        )
        self.assertEqual(confidence["pagination"]["total"], 1)
        filtered, _response = self._get(
            "/api/candidates?status=confirmed&min_confidence=0.7"
        )
        self.assertEqual(filtered["pagination"]["total"], 0)
        with self.assertRaises(urllib.error.HTTPError) as response_error:
            self._get("/api/candidates?min_confidence=1.1")
        self.assertEqual(response_error.exception.code, 400)
        response_error.exception.close()

    def test_static_assets_are_local_and_non_loopback_is_rejected(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/", timeout=5) as response:
            html = response.read()
        self.assertIn(b"memory review", html.lower())
        self.assertNotIn(b"https://", html)
        with urllib.request.urlopen(f"{self.base_url}/static/app.js", timeout=5) as response:
            app = response.read()
        self.assertNotIn(b"innerHTML", app)
        with self.assertRaises(MemoryReviewError):
            create_memory_review_server(self.vault, host="0.0.0.0", port=0)

    def test_candidate_batches_are_listed_and_filter_progress_counts(self) -> None:
        memory = sqlite3.connect(self.vault / "memory" / "memory.sqlite")
        memory.execute(
            """INSERT INTO candidates
                 SELECT 'memc_11111111111111111111111111111111', extractor_version,
                        kind, '我希望保留旧批次。', '我希望保留旧批次。', status,
                        confidence, sensitivity, 1, 'fixture-older',
                        source_conversation_identity_key, source_message_identity_key,
                        source_message_raw_sha256, message_create_time,
                        first_seen_at, last_seen_at, reviewed_statement, reviewed_at
                   FROM candidates LIMIT 1"""
        )
        memory.commit()
        memory.close()

        batches, _response = self._get("/api/batches")
        self.assertEqual(
            [(item["snapshot_key"], item["total"]) for item in batches["items"]],
            [("fixture-older", 1), ("fixture-newer", 1)],
        )

        older, _response = self._get(
            "/api/candidates?status=pending&snapshot=fixture-older&limit=50"
        )
        self.assertEqual(older["pagination"]["total"], 1)
        self.assertEqual(older["counts"]["total"], 1)
        self.assertEqual(older["items"][0]["source_snapshot_key"], "fixture-older")

        newer, _response = self._get(
            "/api/candidates?status=pending&snapshot=fixture-newer&limit=50"
        )
        self.assertEqual(newer["pagination"]["total"], 1)
        self.assertEqual(newer["counts"]["total"], 1)
        self.assertEqual(newer["items"][0]["source_snapshot_key"], "fixture-newer")


if __name__ == "__main__":
    unittest.main()
