import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_vault.evaluate_recall import evaluate, main
from personal_vault.recall import RecallRepository
from tests.test_reader import ReaderFixture


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vault"
        ReaderFixture(self.root)
        item = RecallRepository(self.root).search("短句")["items"][0]
        self.case = {"id": "literal", "category": "preference", "question": "之前说过什么？",
                     "queries": ["短句"], "answer_guidance": "引用用户原话。",
                     "evidence": [{k: item[k] for k in
                                  ("snapshot_key", "conversation_id", "message_id")} | {"quote": "短句"}]}

    def test_hit_miss_and_no_record_are_separate(self):
        miss = dict(self.case, id="miss", queries=["not-present"])
        negative = dict(miss, id="unknown", evidence=[])
        report = evaluate(self.root, [self.case, miss, negative])
        self.assertEqual([r["source_recall"] for r in report["cases"]], [1, 0, None])
        self.assertTrue(report["cases"][0]["evidence"][0]["context_quote_visible"])
        self.assertEqual(report["summary"]["empty_no_record_cases"], 1)
        self.assertEqual(report["cases"][0]["answer_review"]["status"], "not_run")

    def test_no_memory_never_calls_retrieval(self):
        with patch.object(RecallRepository, "search", side_effect=AssertionError), \
             patch.object(RecallRepository, "context", side_effect=AssertionError):
            result = evaluate(self.root, [self.case], mode="no-memory")["cases"][0]
        self.assertEqual(result["source_recall"], 0)
        self.assertEqual(result["returned_text_characters"], 0)

    def test_source_hit_does_not_imply_quote_was_returned(self):
        search = RecallRepository(self.root).search("短句")
        search["items"][0]["text"] = "unrelated clipped text"
        with patch.object(RecallRepository, "search", return_value=search):
            result = evaluate(self.root, [self.case])["cases"][0]
        anchor = result["evidence"][0]
        self.assertTrue(anchor["search_hit"])
        self.assertFalse(anchor["search_quote_visible"])
        self.assertTrue(anchor["context_quote_visible"])

    def test_stale_evidence_is_not_scored_as_a_retrieval_miss(self):
        for field, value in (("quote", "absent quote"), ("snapshot_key", "fixture-older")):
            with self.subTest(field=field):
                case = dict(self.case, evidence=[dict(self.case["evidence"][0], **{field: value})])
                with self.assertRaisesRegex(ValueError, "evidence is absent"):
                    evaluate(self.root, [case])

    def test_repeated_queries_do_not_repeat_context_reads(self):
        report = evaluate(self.root, [dict(self.case, queries=["短句", "短句"])])
        result = report["cases"][0]
        self.assertEqual(result["search_calls"], 2)
        self.assertEqual(result["context_calls"], 1)

    def test_duplicate_case_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate case"):
            evaluate(self.root, [self.case, self.case])

    def test_cli_preserves_existing_report(self):
        cases = Path(self.temp.name) / "cases.json"
        output = Path(self.temp.name) / "report.json"
        cases.write_text(json.dumps({"cases": [self.case]}))
        output.write_text("previous report")
        with self.assertRaises(SystemExit) as error:
            main([str(self.root), str(cases), "--output", str(output)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(output.read_text(), "previous report")
