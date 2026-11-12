"""Deterministic checks for retrieval, citations, tariffs and recovery in 0.3."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from app import EMPTY_USAGE, Store
from materials import EXTRACTION_VERSION, MaterialLibrary, profile_sections
from model_catalog import Catalog, cost, model_info, validate_effort
from publications import Http, Model, PROFILE_SCHEMA, REFERENCE_SCHEMA, ServiceError
from research import _question_answers, prepare_profile
from retrieval import BUDGETS, chunks, select


class SelectionTest(unittest.TestCase):
    def test_sentence_boundaries_overlap_page_provenance_and_stable_ids(self):
        text = " ".join(f"Sentence {i} describes an experimental measurement." for i in range(150))
        parts = [{"page": 7, "section": "Методы", "text": text}]
        fragments = chunks(parts)
        self.assertEqual(fragments, chunks(parts))
        self.assertGreater(len(fragments), 2)
        for item in fragments:
            self.assertLessEqual(len(item["text"]), 2000)
            self.assertEqual(item["page"], 7)
            self.assertIn(item["text"], text)
        for left, right in zip(fragments, fragments[1:]):
            overlap = left["offset"] + len(left["text"]) - right["offset"]
            self.assertGreaterEqual(overlap, 0)
            self.assertLessEqual(overlap, 200)

    def test_short_article_is_complete_and_abstract_counts_towards_budget(self):
        parts = [{"page": 1, "section": "Методы", "text": "An experiment with calibrated probes."},
                 {"page": 2, "section": "Библиография", "text": "This must be excluded."}]
        snippets, report = select(parts, "An abstract.", 16000)
        self.assertFalse(report["sampled"])
        self.assertEqual(report["selected_chars"], sum(len(item["text"]) for item in snippets))
        self.assertEqual(report["pages"], [1])
        self.assertEqual(len(snippets), 2)

    def test_questions_retrieve_middle_of_long_article_and_share_budget(self):
        parts = [{"page": i + 1, "section": "Основной текст", "text": "Generic background information. " * 70}
                 for i in range(80)]
        parts[37]["text"] = "Calibration uncertainty is 0.04 volts. " * 45
        parts[53]["text"] = "The sample temperature is 20 mK. " * 45
        snippets, report = select(parts, "Abstract. " * 100, 16000, questions=[
            {"text": "Какова погрешность калибровки?"}, {"text": "What is the sample temperature?"}])
        self.assertTrue(report["sampled"])
        self.assertLessEqual(report["selected_chars"], 16000)
        self.assertIn(38, report["pages"])
        self.assertIn(54, report["pages"])
        self.assertEqual(len({item["id"] for item in snippets}), len(snippets))

    def test_full_text_includes_late_pages_beyond_largest_character_budget(self):
        parts = [{"page": page, "section": "Результаты", "text": "Calibrated measurement. " * 180}
                 for page in range(1, 21)]
        parts.append({"page": 21, "section": "Библиография", "text": "Reference list."})
        snippets, report = select(parts, "Abstract.", BUDGETS["full"], questions=[{"text": "What was measured?"}])
        self.assertFalse(report["sampled"])
        self.assertIsNone(report["budget_chars"])
        self.assertGreater(report["selected_chars"], BUDGETS["deep"])
        self.assertEqual(report["pages"], list(range(1, 21)))
        self.assertEqual(report["selected_chars"], sum(len(item["text"]) for item in snippets))


class ModelTest(unittest.TestCase):
    def test_larger_model_request_respects_response_timeout_and_job_deadline(self):
        payload = json.dumps({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"reference_hints": []}'}]}]}).encode()
        for remaining, expected_timeout in ((240, 200), (120, 120)):
            with self.subTest(remaining=remaining):
                http = Http(lambda: remaining)
                model = Model(http, "fixture", {"model": "gpt-6-luna", "max_output_tokens": 1000},
                              lambda: None, lambda usage: None)
                with patch.object(http.opener, "open", return_value=io.BytesIO(payload)) as send:
                    model.ask("reference_titles", REFERENCE_SCHEMA, "instructions", {"text": "я" * 240_000})
                    self.assertEqual(send.call_args.kwargs["timeout"], expected_timeout)
                    self.assertGreater(len(send.call_args.args[0].data), 480_000)

    def test_oversized_utf8_request_is_rejected_before_api_call(self):
        http = Http(lambda: 240)
        def unexpected_call():
            self.fail("Oversized request must not count as an API call")
        model = Model(http, "fixture", {"model": "gpt-6-luna", "max_output_tokens": 1000},
                      unexpected_call, lambda usage: None)
        with patch.object(http.opener, "open") as send:
            with self.assertRaisesRegex(ServiceError, "Запрос к модели слишком велик"):
                model.ask("reference_titles", REFERENCE_SCHEMA, "instructions", {"text": "я" * 250_000})
            send.assert_not_called()

    def test_quota_error_is_distinguished_without_exposing_server_message(self):
        http = Http(lambda: 100)
        body = io.BytesIO(json.dumps({"error": {"code": "insufficient_quota", "message": "private account details"}}).encode())
        error = HTTPError("https://api.openai.com/v1/responses", 429, "", {}, body)
        with patch.object(http.opener, "open", side_effect=error):
            with self.assertRaises(ServiceError) as response:
                http.read("https://api.openai.com/v1/responses", "OpenAI")
        self.assertEqual(response.exception.code, "insufficient_quota")
        self.assertIn("биллинг", str(response.exception))
        self.assertNotIn("private", str(response.exception))

    def test_quote_verification_does_not_accept_fabricated_or_unknown_sources(self):
        snippets = [{"id": "s1", "page": 4, "section": "Методы", "text": "The calibrated probe measures temperature."}]
        questions = [{"text": "How?", "question_id": "q1"}]
        for record in ({"snippet": "s1", "evidence": "A fabricated quote here."},
                       {"snippet": "s2", "evidence": snippets[0]["text"]}):
            result = _question_answers(questions, [{"question_id": "q1", "answer": "An answer", "status": "answered", "sources": [record]}], snippets)
            self.assertEqual(result[0]["status"], "unverified")
            self.assertEqual(result[0]["sources"], [])
        result = _question_answers(questions, [{"question_id": "q1", "answer": "An answer", "status": "answered",
                                               "sources": [{"snippet": "s1", "evidence": snippets[0]["text"]}]}], snippets)
        self.assertEqual(result[0]["sources"][0]["page"], 4)

    def test_cost_counts_cached_input_once_and_missing_usage_is_unknown(self):
        price = model_info("gpt-6-luna")["pricing"]
        usage = {**EMPTY_USAGE, "calls": 1, "reported_calls": 1, "input_tokens": 1000,
                 "cached_tokens": 500, "output_tokens": 200}
        self.assertAlmostEqual(cost(usage, price), .000155)
        self.assertIsNone(cost({**usage, "reported_calls": 0}, price))
        self.assertIsNone(cost(usage, None))
        self.assertIsNone(cost({**usage, "unpriced_tokens": True}, price))
        snapshot = copy.deepcopy(price)
        price["input"] = 900
        self.assertEqual(snapshot["input"], .1)

    def test_invalid_effort_unknown_model_and_malformed_output(self):
        with self.assertRaises(ValueError):
            validate_effort("gpt-6-astra", "none")
        self.assertEqual(validate_effort("new-model", "auto"), "auto")

        class HttpStub:
            def checkpoint(self):
                return 100

            def json(self, *args, **kwargs):
                return {"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": '{"summary": []}'}]}]}

        model = Model(HttpStub(), "fixture", {"model": "gpt-6-luna", "max_output_tokens": 1000}, lambda: None, lambda usage: None)
        with self.assertRaises(ServiceError):
            model.ask("test", PROFILE_SCHEMA, "instructions", {})

    def test_empty_same_version_profile_does_not_short_circuit_refresh(self):
        class PublicationsStub:
            def latest(self, *args):
                return {"version": "v1"}

            def pdf(self, *args):
                raise LookupError("download requested")

        job = {"kind": "arxiv", "source": "arxiv:2409.10975", "scenario": "refresh"}
        profile = {"article": {"version": "v1"}, "content": {"summary": []}, "processing_error": ""}
        with self.assertRaisesRegex(LookupError, "download requested"):
            prepare_profile(job, PublicationsStub(), None, None, lambda *args: None, lambda *args: None,
                            None, profile, None)


class PersistenceTest(unittest.TestCase):
    def test_interrupted_job_with_durable_core_is_recovered_without_auto_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            job = store.create({"mode": "research", "source": "2409.10975"})
            steps = {key: {"status": "completed" if key in ("material", "core") else "pending", "message": "", "code": ""}
                     for key in ("material", "core", "references", "analogs", "manuscript")}
            profile = {"schema_version": 6, "job_id": job["id"], "revision": "durable-core", "result_status": "ready",
                       "prepared_at": "2026-11-12", "article": {"title": "Saved core", "authors": [], "version": "v1",
                       "published": "", "url": "", "doi": "", "filename": "", "author_keywords": [], "sha256": "fixture"},
                       "content": {**{key: [] for key in ("keywords", "topics", "claims", "analogs", "resources", "references")}, "summary": ["Summary"]},
                       "coverage": {"basis": "PDF", "page_count": 1, "read_pages": 1, "sections": [], "notes": []},
                       "usage": EMPTY_USAGE, "questions": "", "steps": steps}
            store.materials.publish(job["source"], b"%PDF-fixture", "Text", profile)
            with store.db:
                store.db.execute("UPDATE jobs SET status='running',steps=? WHERE id=?", (json.dumps(steps), job["id"]))
            store.db.close()
            reopened = Store(directory)
            try:
                recovered = reopened.job(job["id"])
                self.assertEqual(recovered["status"], "interrupted")
                self.assertEqual(recovered["result_revision"], "durable-core")
                self.assertTrue(recovered["retry"]["allowed"])
                self.assertEqual(reopened.paper(job["paper_id"])["profile"]["content"]["summary"], ["Summary"])
            finally:
                reopened.db.close()


if __name__ == "__main__":
    unittest.main()
