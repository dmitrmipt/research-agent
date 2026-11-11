"""Explicit, optional live quality check on local PDFs. Not discovered by unittest.

Usage: python tests/live_check.py --live paper-one.pdf paper-two.pdf
Uses OPENAI_API_KEY, GPT-6 Luna/low and at most one 5000-output-token call per PDF.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import DEFAULTS, EMPTY_USAGE
from materials import _pdf_data
from model_catalog import Catalog, cost, model_info
from publications import Http, Model, Publications
from research import prepare_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("pdf", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()) / "research-agent-live-results.json")
    args = parser.parse_args()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENAI_API_KEY is not configured")
    catalog = Catalog(key)
    catalog.refresh(Http(lambda: 10))
    listing = catalog.snapshot()
    if not any(item["id"] == "gpt-6-luna" and item["available"] for item in listing["models"]):
        raise SystemExit(listing["error"] or "GPT-6 Luna is not listed for this key")
    settings = {**DEFAULTS, "max_api_calls": 1, "reasoning_effort": "low",
                "pricing": model_info("gpt-6-luna")["pricing"]}
    results = []
    for path in args.pdf:
        raw = path.read_bytes()
        document = _pdf_data(raw)
        usage = dict(EMPTY_USAGE)
        deadline = time.monotonic() + 120

        def checkpoint():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Live check timed out")
            return remaining

        def start():
            if usage["calls"]:
                raise AssertionError("Only one model call is allowed per PDF")
            usage.update(model=settings["model"], calls=1)

        def record(value):
            if isinstance(value, dict):
                usage.update(reported_calls=1, input_tokens=value["input_tokens"], output_tokens=value["output_tokens"],
                             cached_tokens=(value.get("input_tokens_details") or {}).get("cached_tokens", 0))

        http = Http(checkpoint)
        model = Model(http, key, settings, start, record)
        job = {"id": "live-check", "kind": "pdf", "source": "upload:" + hashlib.sha256(raw).hexdigest(),
               "title": path.name, "depth": "standard", "scenario": "success", "settings": settings,
               "questions": "Какой основной результат заявлен авторами?\nКаким методом он получен?\n"
                            "Указана ли в выбранных фрагментах проверка экспериментом?"}
        _, _, profile = prepare_profile(job, Publications(), http, model, lambda *args: None, lambda *args: None,
                                       lambda: raw, None, lambda: usage,
                                       cached={"pdf": raw, "document": document, "article": {
                                           "title": path.name, "filename": path.name, "authors": [], "source": "PDF",
                                           "arxiv_id": "", "version": "", "doi": "", "published": "", "updated": "",
                                           "abstract": "", "url": ""}})
        if profile["processing_error"]:
            raise SystemExit("Live check could not finish: " + profile["processing_error"])
        assert profile["content"]["summary"]
        assert profile["content"]["claims"], "No source-verified claims"
        for answer in profile["content"]["answers"]:
            assert answer["status"] in ("answered", "insufficient"), answer
            if answer["status"] == "answered":
                assert answer["sources"], answer
        print(json.dumps({"paper": profile["article"]["title"], "summary": profile["content"]["summary"],
                          "answers": profile["content"]["answers"], "usage": usage,
                          "estimated_standard_usd": cost(usage, settings["pricing"])}, ensure_ascii=False, indent=2))
        results.append(profile)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Live checks passed for {len(results)} PDFs; report: {args.output}")


if __name__ == "__main__":
    main()
