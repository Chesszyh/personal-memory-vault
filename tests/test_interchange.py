from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from personal_vault.interchange import InterchangeError, validate_interchange_jsonl


def event(event_id: str, kind: str) -> dict[str, object]:
    return {
        "schema": "pmv.interchange-event.v1",
        "event_id": event_id,
        "provider": "fixture",
        "identity_scope": "fixture-account",
        "snapshot_id": "fixture-2026-08-11",
        "kind": kind,
        "thread_native_id": "thread-1",
        "native_id": event_id,
        "observed_at": "2026-08-11T00:00:00Z",
        "payload": {"text": "synthetic"},
        "raw_payload": {"fixture": True},
    }


class InterchangeTests(unittest.TestCase):
    def test_validates_source_neutral_events_and_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                "\n".join(json.dumps(event(f"event-{index}", kind)) for index, kind in enumerate(("thread", "message", "attachment"))) + "\n",
                encoding="utf-8",
            )
            result = validate_interchange_jsonl(path)
            self.assertEqual(result.events, 3)
            self.assertEqual(result.kinds, {"attachment": 1, "message": 1, "thread": 1})
            path.write_text(
                json.dumps(event("duplicate", "message")) + "\n" + json.dumps(event("duplicate", "message")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InterchangeError, "duplicate"):
                validate_interchange_jsonl(path)


if __name__ == "__main__":
    unittest.main()
