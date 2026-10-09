import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from personal_vault.evaluate_pi import run_suite, summarize_events


def assistant(content, usage=None, stop="stop"):
    return {"type": "message_end", "message": {"role": "assistant", "content": content,
            "provider": "test", "model": "fixture", "usage": usage or {}, "stopReason": stop}}


class PiEvaluationTests(unittest.TestCase):
    def test_usage_counts_completed_messages_only_and_preserves_missing_values(self):
        message = assistant([{"type": "text", "text": "final"}], {"input": 3, "output": 4, "totalTokens": 7})
        result = summarize_events([dict(message, type="message_update"), message])
        self.assertEqual(result["usage"]["totalTokens"], 7)
        self.assertIsNone(result["usage"]["cacheRead"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["answer"], "final")

    def test_final_answer_and_tool_errors_are_separate(self):
        events = [assistant([{"type": "toolCall", "name": "memory_search"}], stop="toolUse"),
                  {"type": "tool_execution_end", "isError": True},
                  {"type": "message_end", "message": {"role": "toolResult", "isError": True,
                   "content": [{"type": "text", "text": "failure"}]}},
                  assistant([{"type": "text", "text": "cannot retrieve"}])]
        result = summarize_events(events)
        self.assertEqual(len(result["tool_errors"]), 1)
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_result_characters"], 7)
        self.assertEqual(result["answer"], "cannot retrieve")

    def test_runs_isolated_pairs_without_passing_expected_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                kwargs["stdout"].write(json.dumps(assistant([{"type": "text", "text": "answer"}])) + "\n")
                return SimpleNamespace(returncode=0)
            case = {"id": "one", "question": "question", "evidence": [{"quote": "SECRET_EXPECTATION"}]}
            with patch("personal_vault.evaluate_pi.evaluate", return_value={"snapshots": []}), \
                 patch("personal_vault.evaluate_pi.subprocess.run", side_effect=run):
                result = run_suite(root, [case], root / "out", root,
                                   provider="test", model="fixture", thinking="low")
            self.assertEqual(len(result["runs"]), 2)
            self.assertNotIn("--extension", commands[0])
            self.assertIn("--extension", commands[1])
            self.assertTrue(all("--no-session" in c for c in commands))
            self.assertTrue(all("SECRET_EXPECTATION" not in str(c) for c in commands))
            self.assertTrue((root / "out/01-with-memory.jsonl").exists())

    def test_unicode_separators_preserve_jsonl_events_and_answers(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            for newline in ("\n", "\r\n"):
                with self.subTest(separator=repr(separator), newline=repr(newline)), \
                     tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    answer = f"第一段{separator}第二段"
                    events = [{"type": "agent_start"},
                              assistant([{"type": "text", "text": answer}])]
                    # Keep literal Unicode in a multi-record stream, without a final newline.
                    raw = newline.join(json.dumps(event, ensure_ascii=False) for event in events)

                    def run(args, **kwargs):
                        kwargs["stdout"].buffer.write(raw.encode("utf-8"))
                        return SimpleNamespace(returncode=0)

                    with patch("personal_vault.evaluate_pi.evaluate", return_value={"snapshots": []}), \
                         patch("personal_vault.evaluate_pi.subprocess.run", side_effect=run) as process:
                        report = run_suite(root, [{"id": "unicode", "question": "q"}], root / "out", root,
                                           provider="test", model="fixture", thinking="low")
                    self.assertEqual(process.call_count, 2)
                    saved = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
                    self.assertEqual([run["answer"] for run in saved["runs"]], [answer, answer])
                    self.assertEqual([run["mode"] for run in report["runs"]], ["no-memory", "with-memory"])
                    for result in report["runs"]:
                        self.assertEqual(result["status"], "completed")
                        self.assertEqual(result["answer"], answer)
                        self.assertEqual(result["model_calls"], 1)
                        self.assertEqual((root / "out" / result["raw_events"]).read_bytes(), raw.encode("utf-8"))

    def test_invalid_json_still_saves_evidence_and_stops_suite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def run(args, **kwargs):
                kwargs["stdout"].write("not json\n")
                return SimpleNamespace(returncode=0)

            with patch("personal_vault.evaluate_pi.evaluate", return_value={"snapshots": []}), \
                 patch("personal_vault.evaluate_pi.subprocess.run", side_effect=run) as process:
                with self.assertRaisesRegex(RuntimeError, "invalid_json"):
                    run_suite(root, [{"id": "one", "question": "q"}], root / "out", root,
                              provider="test", model="fixture", thinking="low")
            self.assertEqual(process.call_count, 1)
            report = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["runs"][0]["status"], "invalid_json")
            self.assertEqual((root / "out/01-no-memory.jsonl").read_text(encoding="utf-8"), "not json\n")

    def test_failure_saves_evidence_and_stops_suite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("personal_vault.evaluate_pi.evaluate", return_value={"snapshots": []}), \
                 patch("personal_vault.evaluate_pi.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaisesRegex(RuntimeError, "process_error"):
                    run_suite(root, [{"id": "one", "question": "q"}], root / "out", root,
                              provider="test", model="fixture", thinking="low")
            self.assertEqual(run.call_count, 1)
            report = json.loads((root / "out/report.json").read_text())
            self.assertEqual(report["runs"][0]["status"], "process_error")
