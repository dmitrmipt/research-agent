"""Exercise the real queue, PDF parsing and profile storage with fixture services."""
import base64
import io
import json
import shutil
import sqlite3
import tarfile
import threading
import time
import unittest
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import EMPTY_USAGE, Handler, LocalServer, Store, Worker
from manuscripts import select_manuscript
from materials import MANAGED_FILES, PROFILE_VERSION, MaterialLibrary, _reference_item, document_text, split_sections
from publications import Http, Publications, ServiceError
from research import BIBLIOGRAPHY_MISSING, _candidate_snippets, bibliography, reference_url
from pipeline import Pipeline, StepExpired

ROOT = Path(__file__).resolve().parents[1]
FOCAL = "2409.10975"
NEIGHBOR = "2301.00002"
QUOTE = "We measured a 4 dB difference at 20 mK."
NEIGHBOR_QUOTE = "The calibrated probe measured spectra at 30 mK."
MOLLOW = "B. R. Mollow, Physical Review 188, 1969 (1969)."
PUBLISHER = "https://journals.aps.org/pr/abstract/10.1103/PhysRev.188.1969"
ABSTRACT = "We report calibrated measurements of microwave spectra."
TEX = b"\\documentclass{article}\n\\begin{document}\n% <script>fixture</script>\nMeasured $x$.\n\\end{document}\n"


def pdf_bytes(title, result=QUOTE, *, blank=False):
    writer = PdfWriter()
    writer.add_metadata({"/Title": title, "/Author": "A. Physicist; B. Researcher", "/Keywords": "microwave; calibration"})
    page = writer.add_blank_page(612, 792)
    if not blank:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        lines = [title, "Abstract: " + ABSTRACT, "Introduction", "Prior experiments are described in [1-3].",
                 "Methods", "We used a calibrated superconducting probe.", "Results", result,
                 "Code availability", "https://github.com/example/fixture", "References",
                 "[1] Related apparatus arXiv:2301.00002", "[2] Microwave test doi:10.1234/older", "[3] " + MOLLOW]
        operations = " 0 -20 Td ".join(f"({line}) Tj" for line in lines)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 11 Tf 40 740 Td {operations} ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def atom(*records):
    entries = []
    for base, version, title in records:
        entries.append(f"""<entry><id>http://arxiv.org/abs/{base}{version}</id>
          <title>{escape(title)}</title><published>2024-01-10T00:00:00Z</published>
          <updated>2025-02-11T00:00:00Z</updated><summary>A study of microwave probe measurements.</summary>
          <author><name>A. Physicist</name></author></entry>""")
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>").encode()


def responses(content):
    return json.dumps({"status": "completed", "usage": {"input_tokens": 120, "output_tokens": 42},
                       "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(content)}]}]}).encode()


def citation(doi, title, abstract):
    return {"DOI": doi, "title": [title], "type": "proceedings-article", "abstract": abstract, "published": {"date-parts": [[2023]]},
            "author": [{"given": "R.", "family": "Researcher"}]}


def source_archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return buffer.getvalue()


def word_source():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


class TempTest(unittest.TestCase):
    def setUp(self):
        parent = (ROOT / ".test-data").resolve()
        self.temp = parent / uuid.uuid4().hex
        self.temp.mkdir(parents=True)
        self.addCleanup(self.remove_temp, parent)

    def remove_temp(self, parent):
        if self.temp.resolve().parent == parent:
            shutil.rmtree(self.temp)


class ResearchTest(TempTest):
    def setUp(self):
        super().setUp()
        self.version = "v3"
        self.downloads = []
        self.source_downloads = []
        self.fail_source = False
        self.docx_source = None
        self.resolutions = []
        self.requests = []
        self.crossref_queries = []
        self.fail_pdf = False
        self.fail_atom = False
        self.arxiv_pages = []
        self.fail_model = False
        self.fail_catalog = False
        self.empty_analogs = False
        self.model_arrived = threading.Event()
        self.resume_model = threading.Event()
        self.resume_model.set()
        self.patchers = [patch("publications.Http.read", autospec=True, side_effect=self.fixture_http),
                         patch("publications.ARXIV_QUERY_INTERVAL", 0)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = LocalServer(("127.0.0.1", 0), Handler)
        self.server.store = Store(self.temp)
        self.server.worker = Worker(self.server.store, "fixture-key")
        self.server.worker.thread.start()
        self.http_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05})
        self.http_thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.resume_model.set()
        self.server.worker.stop.set()
        self.server.worker.thread.join(10)
        self.server.shutdown()
        self.http_thread.join(5)
        self.server.server_close()
        self.server.store.db.close()

    def fixture_http(self, http, url, service, **kwargs):
        http.checkpoint()
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.hostname == "export.arxiv.org":
            if self.fail_atom:
                raise ServiceError("arXiv: HTTP 429.", status=429)
            if "search_query" in query:
                return atom((FOCAL, self.version, "Fixture focal article"),
                            (NEIGHBOR, "v2", "Calibrated microwave probe"))
            identifiers = query["id_list"][0]
            if FOCAL in identifiers:
                self.resolutions.append(identifiers)
                return atom((FOCAL, self.version, "Fixture focal article"))
            return atom((NEIGHBOR, "v2", "Calibrated microwave probe"))
        if parts.hostname == "arxiv.org" and parts.path in {f"/abs/{FOCAL}", f"/abs/{NEIGHBOR}"}:
            self.arxiv_pages.append(parts.path)
            base = parts.path.removeprefix("/abs/")
            version, title = (self.version, "Fixture focal article") if base == FOCAL else ("v2", "Calibrated microwave probe")
            return f'''<meta property="og:url" content="https://arxiv.org/abs/{base}{version}">
                       <meta name="citation_title" content="{title}">
                       <meta name="citation_author" content="A. Physicist">
                       <meta name="citation_date" content="2024/01/10">'''.encode()
        if parts.hostname == "arxiv.org" and parts.path.startswith("/pdf/"):
            self.downloads.append(parts.path)
            if parts.path.startswith(f"/pdf/{FOCAL}"):
                if self.fail_pdf:
                    raise ServiceError("PDF arXiv: HTTP 503.")
                return pdf_bytes("Fixture focal article " + self.version)
            return pdf_bytes("Calibrated microwave probe", NEIGHBOR_QUOTE)
        if parts.hostname == "arxiv.org" and parts.path.startswith("/src/"):
            self.assertEqual(parts.path, f"/src/{FOCAL}{self.version}")
            self.source_downloads.append(parts.path)
            if self.fail_source:
                raise ServiceError("Исходник arXiv: HTTP 404.", status=404)
            if self.docx_source is not None:
                return source_archive({"submission.docx": self.docx_source})
            return source_archive({"nested/main.tex": TEX + self.version.encode(), "appendix.tex": TEX,
                                   "../../paper.tex": TEX, "section.tex": b"\\section{Introduction}"})
        if parts.hostname == "api.crossref.org":
            journal = citation("10.1234/journal", "Journal comparison", "The experiment compares two microwave probe spectra.")
            if parts.path == "/works":
                self.crossref_queries.append(query["query.bibliographic"][0])
                if query["query.bibliographic"][0] == MOLLOW:
                    match = {**citation("10.1103/physrev.188.1969", "Power spectrum of light scattered by two-level systems",
                                        "The spectrum of light scattered by a driven two-level system is calculated."),
                             "author": [{"family": "Mollow"}], "container-title": ["Physical Review"],
                             "volume": "188", "page": "1969-1975", "published": {"date-parts": [[1969]]},
                             "resource": {"primary": {"URL": PUBLISHER}}}
                    other = {**match, "DOI": "10.1234/other", "page": "76"}
                    return json.dumps({"message": {"items": [other, match]}}).encode()
                return json.dumps({"message": {"items": [journal, citation("10.1234/no-text", "Metadata only", "")]}}).encode()
            return json.dumps({"message": citation("10.1234/older", "Cited apparatus", "The authors report a microwave calibration.")}).encode()
        if parts.hostname == "api.openai.com":
            if parts.path == "/v1/models":
                if self.fail_catalog:
                    raise ServiceError("OpenAI: HTTP 401.", status=401)
                return json.dumps({"data": [{"id": "gpt-6-luna"}, {"id": "gpt-6.1-sol"}, {"id": "text-embedding-3-small"}]}).encode()
            self.assertEqual(parts.path, "/v1/responses")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fixture-key")
            request = json.loads(kwargs["data"])
            self.requests.append(request)
            if self.fail_model:
                raise ServiceError("OpenAI: HTTP 401. Проверьте ключ API.")
            self.assertFalse(request["store"])
            self.assertTrue(request["text"]["format"]["strict"])
            inputs = json.loads(request["input"])
            if request["text"]["format"]["name"] == "reference_titles":
                return responses({"reference_hints": [{"reference_id": entry["reference_id"], "title": "", "evidence": "",
                                                        "search_query": entry["text"]} for entry in inputs["references"]]})
            if request["text"]["format"]["name"] == "article_profile":
                self.model_arrived.set()
                self.resume_model.wait(8)
                snippet = next(part for part in inputs["snippets"] if QUOTE in part["text"])
                claim = {"statement": "Измерена разница 4 дБ.", "kind": "measurement", "snippet": snippet["id"],
                         "evidence": QUOTE, "locator": "Несуществующий рисунок 9", "check": "Проверить калибровку.",
                         "explanation": "Разница относится к измеренным спектрам. Температура образца составила 20 мК."}
                answers = [{"question_id": question["question_id"], "answer": reply, "status": status,
                            "sources": [{"snippet": snippet["id"], "evidence": QUOTE}] if status == "answered" else []}
                           for question, (reply, status) in zip(inputs["questions"], (
                               ("Измерена разница 4 дБ.", "answered"), ("Не указан предел шума.", "insufficient"),
                           ))]
                return responses({"summary": ["Изучены микроволновые спектры калиброванным зондом.",
                                               "Авторы сообщают разницу 4 дБ при 20 мК."],
                                  "answers": answers[::-1],
                                  "glossary": [{"term": term, "explanation": "Величина или метод измерения.",
                                                "role": "Используется при сравнении спектров."}
                                               for term in ("Зонд", "Калибровка", "Спектр", "Децибел",
                                                            "Температура", "Шум", "Микроволновое излучение")],
                                  "keywords": [{"term": term, "importance": weight, "role": "метод"}
                                               for term, weight in [("microwave", 5), ("probe", 4), ("calibration", 3), ("spectra", 2), ("temperature", 1)]],
                                  "topics": ["Микроволновые измерения"],
                                  "claims": [claim, {**claim, "statement": "Выдуманное утверждение.", "evidence": "A fabricated sentence."}],
                                  "search_terms": ["microwave probe", "superconducting spectra"], "reference_hints": []})
            if self.empty_analogs:
                return responses({"analogs": []})
            self.assertNotIn("evidence", request["text"]["format"]["schema"]["properties"]["analogs"]["items"]["properties"])
            analogs = [{"candidate_id": "fabricated", "snippet": "missing",
                        "similarity": "Выдумано", "difference": "Выдумано"}]
            for candidate in inputs["candidates"]:
                self.assertNotEqual(candidate["title"], "Metadata only")
                main = next((part for part in candidate["snippets"] if NEIGHBOR_QUOTE in part["text"]), None)
                part = main or candidate["snippets"][0]
                if main:
                    analogs.append({"candidate_id": candidate["id"], "snippet": "missing",
                                    "similarity": "Близкий метод измерения.", "difference": "Иные условия эксперимента."})
                analogs.append({"candidate_id": candidate["id"], "snippet": part["id"],
                                "similarity": "Близкий метод измерения.", "difference": "Иные условия эксперимента."})
            return responses({"analogs": analogs})
        self.fail(f"Unexpected fixture request: {parts.hostname}")

    def call(self, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=6) as response:
            return response.read() if raw else json.load(response)

    def settled(self, job):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = self.call("/api/jobs/" + job["id"])
            if result["status"] not in {"queued", "running"}:
                return result
            time.sleep(.05)
        self.fail(f'Job still running: {result["stage"]}')

    def uploaded(self, raw, **settings):
        return self.call("/api/jobs", {"mode": "research", "pdf": {"name": "input.pdf", "data": base64.b64encode(raw).decode()},
                                       **settings})

    def test_latest_version_refresh_replaces_bundle_and_preserves_previous_on_failure(self):
        job = self.call("/api/jobs", {"source": f"https://arxiv.org/abs/{FOCAL}v1", "mode": "research"})
        result = self.settled(job)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(self.resolutions, [FOCAL])
        self.assertEqual(self.downloads[0], f"/pdf/{FOCAL}v3")
        self.assertEqual(self.source_downloads, [f"/src/{FOCAL}v3"])
        self.assertEqual(result["resolved_version"], "v3")
        self.assertEqual(result["api_usage"]["input_tokens"], 240)
        path = "/api/papers/" + job["paper_id"]
        initial = self.call(path)
        self.assertEqual(initial["profile"]["article"]["version"], "v3")
        self.assertEqual(initial["profile"]["article"]["abstract"], "A study of microwave probe measurements.")
        self.assertEqual(initial["profile"]["article"]["manuscript"], {"name": "nested/main.tex", "file": "manuscript.tex"})
        folder = Path(initial["directory"])
        self.assertEqual(folder.name, "arxiv-" + FOCAL)
        names = set(MANAGED_FILES) - {"manuscript.docx"}
        initial_files = {name: (folder / name).read_bytes() for name in names}
        self.assertEqual(initial_files["manuscript.tex"], TEX + b"v3")
        self.assertEqual(self.call(path + "/manuscript.tex", raw=True), TEX + b"v3")
        revision = initial["profile"]["revision"]
        source = self.call(path + "/manuscript.html?snapshot=" + revision, raw=True).decode()
        self.assertIn('<span class="tex-command">\\documentclass</span>', source)
        self.assertIn('<span class="tex-math">$</span>', source)
        self.assertIn("&lt;script&gt;fixture&lt;/script&gt;", source)
        self.assertNotIn("<script>", source)
        self.assertIn(f'manuscript.tex?snapshot={revision}', source)
        (folder / "notes.txt").write_text("Reader notes", encoding="utf-8")

        same = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(same["stage"], "Сохранённая версия актуальна")
        self.assertEqual(same["api_usage"]["calls"], 0)
        self.assertEqual(len(self.source_downloads), 1)
        self.assertEqual(len(self.requests), 2)

        self.version = "v2"
        stale = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(stale["status"], "failed")
        self.assertEqual(self.call(path)["profile"]["article"]["version"], "v3")
        self.assertEqual(len(self.requests), 2)

        self.version, self.fail_pdf = "v4", True
        failed = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(failed["status"], "failed")
        paper = self.call(path)
        self.assertEqual(paper["profile"]["article"]["version"], "v3")
        self.assertEqual(paper["latest_job"]["resolved_version"], "v4")
        self.assertEqual({name: (folder / name).read_bytes() for name in names}, initial_files)

        self.fail_pdf = False
        self.docx_source = word_source()
        updated = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(updated["status"], "completed", updated["error"])
        self.assertEqual(self.call(path)["profile"]["article"]["version"], "v4")
        self.assertEqual(self.call(path)["profile"]["article"]["manuscript"]["file"], "manuscript.docx")
        self.assertEqual((folder / "manuscript.docx").read_bytes(), self.docx_source)
        self.assertFalse((folder / "manuscript.tex").exists())
        with self.assertRaises(HTTPError) as no_preview:
            self.call(path + "/manuscript.html", raw=True)
        self.assertEqual(no_preview.exception.code, 404)
        self.assertNotEqual((folder / "article.pdf").read_bytes(), initial_files["article.pdf"])
        self.assertEqual(len(self.call("/api/papers")), 1)
        self.assertEqual((folder / "notes.txt").read_text(encoding="utf-8"), "Reader notes")
        self.assertEqual({p.name for p in folder.iterdir()}, (set(MANAGED_FILES) - {"manuscript.tex"}) | {"notes.txt"})
        with self.assertRaises(HTTPError) as error:
            self.call(path + "/article.pdf?snapshot=" + job["id"], raw=True)
        self.assertEqual(error.exception.code, 409)

    def test_same_version_refresh_can_add_missing_source_without_model(self):
        self.fail_source = True
        job = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
        self.assertEqual(self.settled(job)["status"], "completed")
        path = "/api/papers/" + job["paper_id"]
        original = self.call(path)["profile"]
        self.assertNotIn("manuscript", original["article"])
        self.fail_source = False
        refreshed = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(refreshed["status"], "completed")
        self.assertEqual(refreshed["api_usage"]["calls"], 0)
        saved = self.call(path)["profile"]
        self.assertEqual(saved["content"], original["content"])
        self.assertFalse(any("HTTP 404" in note for note in saved["coverage"]["notes"]))
        self.assertEqual(self.call(path + "/manuscript.tex", raw=True), TEX + b"v3")

    def test_uploaded_pdf_extraction_sources_and_matching_markdown(self):
        raw = pdf_bytes("Uploaded paper")
        job = self.uploaded(raw, questions="Сосредоточься на калибровке.\nЧто измерено? Известен ли предел шума?\n3. Опиши образец")
        result = self.settled(job)
        self.assertEqual(result["status"], "completed", result["error"])
        path = "/api/papers/" + job["paper_id"]
        paper = self.call(path)
        profile, folder = paper["profile"], Path(paper["directory"])
        self.assertEqual(profile["article"]["title"], "Uploaded paper")
        self.assertEqual(profile["article"]["authors"], ["A. Physicist", "B. Researcher"])
        self.assertEqual(profile["article"]["abstract"], ABSTRACT)
        library_row = self.call("/api/papers")[0]
        self.assertEqual(library_row["authors"], profile["article"]["authors"])
        self.assertTrue(library_row["has_abstract"])
        self.assertEqual(library_row["profile_job_id"], job["id"])
        self.assertEqual(profile["article"]["author_keywords"], ["microwave", "calibration"])
        self.assertEqual(profile["coverage"]["read_pages"], 1)
        self.assertIn("Библиография", profile["coverage"]["sections"])
        content = profile["content"]
        self.assertEqual(sum(word["weight"] for word in content["keywords"]), 100)
        self.assertEqual(len(content["claims"]), 1)
        self.assertEqual(content["claims"][0]["evidence"], QUOTE)
        self.assertEqual(content["claims"][0]["location"], "Страница 1 PDF · Результаты")
        self.assertTrue(content["claims"][0]["explanation"].startswith("Разница относится"))
        request = json.loads(self.requests[0]["input"])
        self.assertEqual(request["focus"], "Сосредоточься на калибровке.")
        self.assertEqual([question["question_id"] for question in request["questions"]], ["q1", "q2", "q3"])
        self.assertEqual([answer["question"] for answer in content["answers"]],
                         ["Что измерено?", "Известен ли предел шума?", "Опиши образец"])
        self.assertEqual([answer["status"] for answer in content["answers"]], ["answered", "insufficient", "missing"])
        self.assertEqual(len(content["glossary"]), 7)
        overview = next(section for section in paper["sections"] if section["key"] == "overview")
        self.assertEqual(len(overview["paragraphs"]), 2)
        self.assertEqual([sub["key"] for sub in overview["subsections"]], ["answers", "glossary"])
        self.assertTrue(overview["subsections"][0]["items"][1]["text"].startswith("Недостаточно данных в выбранных фрагментах."))
        self.assertEqual(content["resources"][0]["kind"], "Код")
        self.assertIn(MOLLOW, self.crossref_queries)
        self.assertEqual([entry["number"] for entry in content["references"]], ["1", "2", "3"])
        self.assertEqual(content["references"][2]["text"], MOLLOW)
        self.assertEqual(content["references"][1]["url"], "https://doi.org/10.1234/older")
        self.assertEqual(content["references"][2]["url"], PUBLISHER)
        references = next(section["items"] for section in paper["sections"] if section["key"] == "references")
        self.assertEqual(references[2]["label"], "Power spectrum of light scattered by two-level systems")
        self.assertEqual(references[2]["authors"], "B. R. Mollow")
        self.assertEqual(references[2]["text"], "Physical Review 188, 1969-1975 (1969)")
        self.assertEqual(references[2]["citations"], [{"page": 1, "label": "с. 1 · Введение"}])
        matches = {entry["title"]: entry for entry in content["analogs"]}
        self.assertIn("Cited apparatus", matches)
        self.assertIn("предварительно", matches["Journal comparison"]["basis"])
        self.assertIn("основного текста", matches["Calibrated microwave probe"]["basis"])
        self.assertIn("Power spectrum of light scattered by two-level systems", matches)
        search = profile["analog_search"]
        self.assertEqual(search["records"], 6)
        self.assertEqual(search["candidates"], 5)
        self.assertEqual(search["accepted"], 5)
        self.assertEqual(search["rejected"], [{"title": "fabricated", "reason": "Неизвестный идентификатор кандидата."},
                                              {"title": "Calibrated microwave probe", "reason": "Неизвестный фрагмент кандидата."}])
        self.assertEqual(self.call(path + "/article.pdf", raw=True), raw)
        text = self.call(path + "/text.txt", raw=True).decode()
        self.assertIn("--- Страница 1 ---", text)
        self.assertIn(QUOTE, text)
        markdown = self.call(path + "/profile.md", raw=True)
        self.assertEqual(markdown, (folder / "profile.md").read_bytes())
        self.assertEqual(json.loads((folder / "profile.json").read_bytes()), profile)
        headings = [line[3:] for line in markdown.decode().splitlines() if line.startswith("## ")]
        self.assertEqual(headings, [section["title"] for section in paper["sections"]])
        self.assertIn("Список литературы", headings)
        self.assertIn("\n  B. R. Mollow  \n  Physical Review 188, 1969-1975 (1969)", markdown.decode().replace("\r\n", "\n"))
        self.assertIn("### Ответы на вопросы", markdown.decode())
        self.assertIn("### Словарь терминов", markdown.decode())
        self.assertIn("(article.pdf#page=1)", markdown.decode())
        revision = profile["revision"]
        preview_path = path + "/profile.html?snapshot=" + revision
        preview = self.call(preview_path, raw=True).decode()
        self.assertIn("<h1>Uploaded paper</h1>", preview)
        self.assertIn("B. R. Mollow<br>", preview)
        self.assertNotIn("<strong>B. R. Mollow</strong>", preview)
        self.assertIn(f'href="article.pdf?snapshot={revision}#page=1"', preview)
        self.assertIn(f'href="https://arxiv.org/pdf/{NEIGHBOR}v2#page=1"', preview)

        with (folder / "profile.md").open("a", encoding="utf-8") as saved:
            saved.write("\n## Примечание из сохранённого Markdown\n\n<script>alert(1)</script>\n")
        preview = self.call(preview_path, raw=True).decode()
        self.assertIn("<h2>Примечание из сохранённого Markdown</h2>", preview)
        self.assertIn("&lt;script&gt;", preview)
        self.assertNotIn("<script>", preview)
        with self.assertRaises(HTTPError) as stale:
            self.call(path + "/profile.html?snapshot=previous", raw=True)
        self.assertEqual(stale.exception.code, 409)

    def test_fallback_and_single_call_budget(self):
        self.fail_atom = True
        self.call("/api/settings", {"max_api_calls": 1, "max_output_tokens": 1500})
        job = self.call("/api/jobs", {"source": FOCAL + "v1", "mode": "research"})
        result = self.settled(job)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(self.downloads, [f"/pdf/{FOCAL}v3"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["max_output_tokens"], 1500)
        profile = self.call("/api/papers/" + job["paper_id"])["profile"]
        self.assertEqual(profile["content"]["analogs"], [])
        self.assertEqual(len(profile["content"]["references"]), 3)
        self.assertEqual(profile["content"]["references"][2]["url"], PUBLISHER)
        self.assertEqual(profile["steps"]["analogs"]["code"], "call_limit")

    def test_blank_pdf_saves_partial_profile_without_calling_model(self):
        job = self.uploaded(pdf_bytes("Image-only fixture", blank=True))
        result = self.settled(job)
        self.assertEqual(result["status"], "completed", result["error"])
        profile = self.call("/api/papers/" + job["paper_id"])["profile"]
        self.assertEqual(profile["coverage"]["read_pages"], 0)
        self.assertEqual(profile["content"]["summary"], [])
        self.assertEqual(profile["usage"]["calls"], 0)
        self.assertEqual(self.requests, [])

    def test_cited_arxiv_fulltext_is_compared_when_atom_is_rate_limited(self):
        self.fail_atom = True
        job = self.call("/api/jobs", {"source": FOCAL + "v1", "mode": "research"})
        result = self.settled(job)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(self.arxiv_pages, [f"/abs/{FOCAL}", f"/abs/{NEIGHBOR}"])
        profile = self.call("/api/papers/" + job["paper_id"])["profile"]
        analog = next(item for item in profile["content"]["analogs"] if item["title"] == "Calibrated microwave probe")
        self.assertEqual(analog["evidence"], NEIGHBOR_QUOTE)
        self.assertIn("основного текста", analog["basis"])
        self.assertTrue(any(source["status"] == "deferred" and source["http_status"] == 429
                            for source in profile["analog_search"]["sources"]))

    def test_repeat_analogs_uses_saved_article_and_preserves_matches_on_empty_result(self):
        self.call("/api/settings", {"max_api_calls": 1})
        job = self.call("/api/jobs", {"source": FOCAL, "mode": "research", "depth": "deep"})
        self.assertEqual(self.settled(job)["status"], "completed")
        path = "/api/papers/" + job["paper_id"]
        original = self.call(path)["profile"]
        pdf = self.call(path + "/article.pdf", raw=True)
        text = self.call(path + "/text.txt", raw=True)
        self.assertEqual(original["content"]["analogs"], [])

        retry = self.settled(self.call(path + "/analogs", {}))
        self.assertEqual(retry["status"], "completed", retry["error"])
        self.assertEqual(retry["api_usage"]["calls"], 1)
        refreshed = self.call(path)["profile"]
        self.assertEqual(refreshed["job_id"], retry["id"])
        self.assertEqual(refreshed["content"]["summary"], original["content"]["summary"])
        self.assertEqual(refreshed["usage"], original["usage"])
        self.assertGreater(len(refreshed["content"]["analogs"]), 0)

        self.empty_analogs = True
        self.assertEqual(self.settled(self.call(path + "/analogs", {}))["status"], "completed")
        latest = self.call(path)["profile"]
        self.assertEqual(latest["content"]["analogs"], refreshed["content"]["analogs"])
        self.assertEqual(latest["analog_search"]["proposed"], 0)
        self.assertEqual(latest["analog_search"]["preserved"], len(refreshed["content"]["analogs"]))
        self.assertEqual(self.resolutions, [FOCAL])
        self.assertEqual(self.downloads.count(f"/pdf/{FOCAL}v3"), 1)
        self.assertEqual(self.call(path + "/article.pdf", raw=True), pdf)
        self.assertEqual(self.call(path + "/text.txt", raw=True), text)
        self.assertEqual([request["text"]["format"]["name"] for request in self.requests],
                         ["article_profile", "nearest_publications", "nearest_publications"])

    def test_first_model_failure_leaves_downloaded_pdf_and_allows_retry(self):
        self.fail_model = True
        job = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
        result = self.settled(job)
        self.assertEqual(result["status"], "failed")
        self.assertIn("HTTP 401", result["error"])
        path = "/api/papers/" + job["paper_id"]
        self.assertTrue(self.call(path + "/article.pdf", raw=True).startswith(b"%PDF-"))
        self.assertEqual(self.call(path)["profile"]["content"]["summary"], [])
        self.fail_model = False
        retry = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(retry["status"], "completed", retry["error"])
        self.assertTrue(self.call(path)["profile"]["content"]["summary"])

    def test_stop_during_api_prevents_followup_requests_and_publication(self):
        self.resume_model.clear()
        job = self.uploaded(pdf_bytes("Cancelled fixture"))
        self.assertTrue(self.model_arrived.wait(8))
        result = self.call("/api/jobs/" + job["id"] + "/cancel", {})
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["api_usage"]["calls"], 1)
        self.resume_model.set()
        next_job = self.uploaded(pdf_bytes("Queue recovery fixture", blank=True))
        self.assertEqual(self.settled(next_job)["status"], "completed")
        self.assertIsNone(self.call("/api/papers/" + job["paper_id"])["profile"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.crossref_queries, [])

    def test_timeout_after_core_preserves_profile_and_continuation_skips_core(self):
        def expire(pipeline):
            pipeline.deadline = 0
            pipeline.optional_checkpoint()

        with patch.object(Pipeline, "analogs", expire):
            first = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
            result = self.settled(first)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result_status"], "ready")
        self.assertEqual(result["steps"]["analogs"]["code"], "time_limit")
        self.assertTrue(result["retry"]["allowed"])
        path = "/api/papers/" + first["paper_id"]
        saved = self.call(path)["profile"]
        self.assertTrue(saved["content"]["summary"])
        self.assertEqual(len(self.requests), 1)
        resumed = self.settled(self.call("/api/jobs/" + first["id"] + "/retry", {}))
        self.assertEqual(resumed["status"], "completed", resumed["error"])
        self.assertEqual(resumed["parent_job_id"], first["id"])
        self.assertEqual(resumed["api_usage"]["calls"], 1)
        self.assertEqual(self.resolutions, [FOCAL])
        self.assertEqual(self.downloads.count(f"/pdf/{FOCAL}v3"), 1)
        final = self.call(path)["profile"]
        self.assertEqual(final["content"]["summary"], saved["content"]["summary"])
        self.assertNotEqual(final["revision"], saved["revision"])
        with self.assertRaises(HTTPError) as stale:
            self.call(path + "/article.pdf?snapshot=" + saved["revision"], raw=True)
        self.assertEqual(stale.exception.code, 409)

    def test_core_visible_before_optional_work_and_cancel_keeps_it(self):
        arrived, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def wait(pipeline):
            arrived.set()
            release.wait(6)
            pipeline.alive()

        with patch.object(Pipeline, "analogs", wait):
            first = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
            self.assertTrue(arrived.wait(6))
            path = "/api/papers/" + first["paper_id"]
            core = self.call(path)["profile"]
            self.assertTrue(core["content"]["summary"])
            self.call("/api/jobs/" + first["id"] + "/cancel", {})
            release.set()
            time.sleep(.1)
        stopped = self.call("/api/jobs/" + first["id"])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertTrue(stopped["retry"]["allowed"])
        self.assertEqual(self.call(path)["profile"]["revision"], core["revision"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.source_downloads, [])

    def test_reanalysis_reuses_pdf_and_document_with_isolated_settings(self):
        first = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
        self.assertEqual(self.settled(first)["status"], "completed")
        path = "/api/papers/" + first["paper_id"]
        original = self.call(path)["profile"]
        downloads, resolutions = list(self.downloads), list(self.resolutions)
        with patch("research.extract_pdf", side_effect=AssertionError("must use extraction cache")), \
                patch("pipeline.extract_pdf", side_effect=AssertionError("must use extraction cache")):
            repeated = self.settled(self.call(path + "/reanalyze", {"questions": "Что измерено?", "depth": "deep",
                "settings": {"model": "gpt-6.1-sol", "reasoning_effort": "high", "max_api_calls": 1}}))
        self.assertEqual(repeated["status"], "completed", repeated["error"])
        self.assertEqual(repeated["operation"], "reanalyze")
        self.assertEqual(repeated["api_usage"]["calls"], 1)
        self.assertEqual(self.downloads, downloads)
        self.assertEqual(self.resolutions, resolutions)
        self.assertEqual(self.requests[-1]["reasoning"], {"effort": "high"})
        self.assertEqual(self.call("/api/settings")["model"], "gpt-6-luna")
        final = self.call(path)["profile"]
        self.assertNotEqual(final["revision"], original["revision"])
        self.assertEqual(final["article"]["sha256"], original["article"]["sha256"])
        self.assertEqual(final["content"]["answers"][0]["sources"][0]["evidence"], QUOTE)
        self.assertEqual(final["coverage"]["selection"]["budget_chars"], 48000)

    def test_failed_reanalysis_keeps_old_profile_and_retry_repeats_model(self):
        first = self.uploaded(pdf_bytes("Stable profile"))
        self.assertEqual(self.settled(first)["status"], "completed")
        path = "/api/papers/" + first["paper_id"]
        before = self.call(path)["profile"]
        self.fail_model = True
        failed = self.settled(self.call(path + "/reanalyze", {"questions": "Что измерено?"}))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.call(path)["profile"], before)
        self.fail_model = False
        count = len(self.requests)
        repeated = self.settled(self.call("/api/jobs/" + failed["id"] + "/retry", {}))
        self.assertEqual(repeated["status"], "completed", repeated["error"])
        self.assertEqual(self.requests[count]["text"]["format"]["name"], "article_profile")
        self.assertEqual(self.call(path)["profile"]["questions"], "Что измерено?")

    def test_retry_first_failure_uses_downloaded_materials_and_blank_is_blocked(self):
        self.fail_model = True
        first = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
        self.assertEqual(self.settled(first)["status"], "failed")
        before = list(self.downloads)
        self.fail_model = False
        repeated = self.settled(self.call("/api/jobs/" + first["id"] + "/retry", {}))
        self.assertEqual(repeated["status"], "completed", repeated["error"])
        self.assertEqual(self.downloads[:len(before)], before)
        self.assertEqual(self.downloads.count(f"/pdf/{FOCAL}v3"), 1)
        blank = self.settled(self.uploaded(pdf_bytes("No text", blank=True)))
        self.assertEqual(blank["result_status"], "partial")
        self.assertFalse(blank["retry"]["allowed"])
        with self.assertRaises(HTTPError) as denied:
            self.call("/api/jobs/" + blank["id"] + "/retry", {})
        self.assertEqual(denied.exception.code, 400)

    def test_catalog_and_effort_validation_do_not_call_inference(self):
        catalog = self.call("/api/models/refresh", {})
        known = {item["id"]: item for item in catalog["models"]}
        self.assertTrue(known["gpt-6-luna"]["available"])
        self.assertFalse(known["gpt-6-astra"]["available"])
        self.assertIn("text-embedding-3-small", catalog["other_models"])
        self.assertEqual(self.requests, [])
        with self.assertRaises(HTTPError) as invalid:
            self.call("/api/settings", {"model": "gpt-6-astra", "reasoning_effort": "none"})
        self.assertEqual(invalid.exception.code, 400)
        self.call("/api/settings", {"model": "custom-model", "reasoning_effort": "auto"})
        self.fail_catalog = True
        self.server.worker.catalog.last_attempt = 0
        failed = self.call("/api/models/refresh", {})
        self.assertIsNotNone(failed["error"])
        self.assertFalse(failed["availability_checked"])
        self.assertEqual(self.requests, [])

    def test_stale_extraction_cache_is_rebuilt_and_tampered_pdf_is_rejected(self):
        first = self.uploaded(pdf_bytes("Cache fixture"))
        self.assertEqual(self.settled(first)["status"], "completed")
        path = "/api/papers/" + first["paper_id"]
        folder = Path(self.call(path)["directory"])
        cache = json.loads((folder / "document.json").read_text(encoding="utf-8"))
        cache["parser"] = "old-parser"
        (folder / "document.json").write_text(json.dumps(cache), encoding="utf-8")
        from research import extract_pdf
        with patch("research.extract_pdf", wraps=extract_pdf) as extractor:
            repeated = self.settled(self.call(path + "/reanalyze", {"settings": {"max_api_calls": 1}}))
            self.assertEqual(repeated["status"], "completed", repeated["error"])
            self.assertEqual(extractor.call_count, 1)
        (folder / "article.pdf").write_bytes(pdf_bytes("Changed content"))
        with self.assertRaises(HTTPError) as tampered:
            self.call(path + "/reanalyze", {})
        self.assertEqual(tampered.exception.code, 400)

    def test_same_version_refresh_keeps_continuation_of_missing_analogs(self):
        self.call("/api/settings", {"max_api_calls": 1})
        first = self.call("/api/jobs", {"source": FOCAL, "mode": "research"})
        self.assertEqual(self.settled(first)["status"], "completed")
        path = "/api/papers/" + first["paper_id"]
        refreshed = self.settled(self.call(path + "/refresh", {}))
        self.assertEqual(refreshed["api_usage"]["calls"], 0)
        paper = self.call(path)
        self.assertEqual(paper["continuation_job"]["id"], first["id"])
        resumed = self.settled(self.call("/api/jobs/" + first["id"] + "/retry", {}))
        self.assertEqual(resumed["api_usage"]["calls"], 1)
        self.assertFalse(resumed["retry"]["allowed"])

    def test_cancelled_queued_reanalysis_can_be_continued(self):
        first = self.uploaded(pdf_bytes("Queued continuation"))
        self.assertEqual(self.settled(first)["status"], "completed")
        self.server.worker.stop.set()
        self.server.worker.thread.join(5)
        path = "/api/papers/" + first["paper_id"]
        queued = self.call(path + "/reanalyze", {})
        self.assertEqual(queued["status"], "queued")
        stopped = self.call("/api/jobs/" + queued["id"] + "/cancel", {})
        self.assertTrue(stopped["retry"]["allowed"])
        resumed = self.call("/api/jobs/" + queued["id"] + "/retry", {})
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["input_revision"], queued["input_revision"])

    def test_new_job_can_override_model_and_effort_without_changing_defaults(self):
        first = self.uploaded(pdf_bytes("Per article model"), settings={"model": "gpt-6.1-sol", "reasoning_effort": "high", "max_api_calls": 1})
        result = self.settled(first)
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["settings"]["model"], "gpt-6.1-sol")
        self.assertEqual(self.requests[0]["reasoning"], {"effort": "high"})
        self.assertEqual(self.call("/api/settings")["model"], "gpt-6-luna")
        self.assertEqual(self.call("/api/settings")["reasoning_effort"], "low")
        count = len(self.requests)
        second = self.uploaded(pdf_bytes("Default article model"))
        self.assertEqual(self.settled(second)["status"], "completed")
        self.assertEqual(self.requests[count]["model"], "gpt-6-luna")
        self.assertEqual(self.requests[count]["reasoning"], {"effort": "low"})

    def test_reference_title_lookup_is_separate_and_preserves_overview(self):
        first = self.uploaded(pdf_bytes("Reference lookup"))
        self.assertEqual(self.settled(first)["status"], "completed")
        path = "/api/papers/" + first["paper_id"]
        paper = self.call(path)
        profile = paper["profile"]
        original_summary = profile["content"]["summary"][:]
        entry = profile["content"]["references"][2]
        for key in ("title", "resolved_record", "publisher_url", "publication"):
            entry.pop(key, None)
        entry["dois"], entry["url"] = [], ""
        store = self.server.store
        with store.lock:
            store.materials.publish(first["source"], store.materials.read(first["source"], "article.pdf"),
                                    store.materials.read_text(first["source"]), profile)
        count, downloads = len(self.requests), list(self.downloads)
        repeated = self.settled(self.call(path + "/references", {}))
        self.assertEqual(repeated["status"], "completed", repeated["error"])
        self.assertEqual(repeated["operation"], "references")
        self.assertEqual(repeated["api_usage"]["calls"], 1)
        self.assertEqual(self.requests[count]["text"]["format"]["name"], "reference_titles")
        final = self.call(path)["profile"]
        self.assertEqual(final["content"]["summary"], original_summary)
        self.assertEqual(final["content"]["references"][2]["title"], "Power spectrum of light scattered by two-level systems")
        self.assertEqual(final["content"]["references"][2]["url"], PUBLISHER)
        self.assertEqual(self.downloads, downloads)

    def test_title_resolution_survives_timeout_after_partial_progress(self):
        def resolve(publications, http, references, on_update):
            references[0]["title"] = "Confirmed title from metadata"
            references[0]["url"] = "https://doi.org/10.1234/confirmed"
            on_update()
            raise StepExpired()

        with patch("pipeline.resolve_reference_titles", side_effect=resolve):
            job = self.uploaded(pdf_bytes("Partial references"), settings={"max_api_calls": 1})
            result = self.settled(job)
        self.assertEqual(result["status"], "completed")
        final = self.call("/api/papers/" + job["paper_id"])["profile"]
        self.assertEqual(final["content"]["references"][0]["title"], "Confirmed title from metadata")
        self.assertEqual(final["steps"]["references"]["code"], "time_limit")


class StorageTest(TempTest):
    def test_legacy_numbered_references_repair_wraps_without_losing_metadata(self):
        old = {"number": "1", "page": 1, "text": "A. Reader, Superconduc- tivity, Journal of Tests, 2020.",
               "title": "Superconductivity", "arxiv_ids": [], "dois": ["10.1234/saved"],
               "url": "https://doi.org/10.1234/saved", "citations": []}
        _, _, paper_id, _ = self._save_old_profile(
            ["Introduction\nSuperconductivity was observed [1].\nReferences\n[1] A. Reader, Superconduc-\ntivity, Journal of Tests, 2020."],
            schema_version=3, references=[old])
        store = Store(self.temp)
        self.addCleanup(store.db.close)
        recovered = store.paper(paper_id)["profile"]["content"]["references"][0]
        self.assertIn("Superconductivity", recovered["text"])
        self.assertNotIn("Superconduc-", recovered["text"])
        self.assertEqual(recovered["title"], "Superconductivity")
        self.assertEqual(recovered["url"], old["url"])

    def test_bibliography_upgrade_keeps_previously_verified_answers(self):
        library, source, paper_id, pdf = self._save_old_profile(["References\n[1] " + MOLLOW], schema_version=6)
        profile = library.profile(source)
        support = {"evidence": QUOTE, "page": 1, "section": "Результаты", "location": "Страница 1"}
        profile["content"]["answers"] = [{"question": "Что измерено?", "answer": "4 дБ", "status": "answered", "sources": [support]},
                                           {"question": "Старый ответ?", "answer": "Ответ", "status": "answered"}]
        library.publish(source, pdf, library.read_text(source), profile)
        store = Store(self.temp)
        self.addCleanup(store.db.close)
        answers = store.paper(paper_id)["profile"]["content"]["answers"]
        self.assertEqual(answers[0]["status"], "answered")
        self.assertEqual(answers[0]["sources"], [support])
        self.assertEqual(answers[1]["status"], "unverified")

    def _save_old_profile(self, pages, *, schema_version, references=None):
        with closing(Store(self.temp).db) as db:
            store_source = "arxiv:" + FOCAL
            paper_id = "a" * 32
            with db:
                db.execute("INSERT INTO papers VALUES (?,?,?,?,?)", (paper_id, store_source, "Saved paper", "arxiv", "2026-01-01"))
        library = MaterialLibrary(self.temp / "Materials")
        profile = {"schema_version": schema_version, "job_id": "original", "prepared_at": "2026-01-01",
                   "article": {"title": "Saved paper", "authors": [], "version": "v1", "published": "",
                               "url": "", "doi": "", "filename": "", "author_keywords": []},
                   "content": {key: [] for key in ("summary", "keywords", "topics", "claims", "analogs", "resources")},
                   "coverage": {"basis": "PDF", "page_count": len(pages), "read_pages": len(pages), "sections": [],
                                "notes": [BIBLIOGRAPHY_MISSING]}, "usage": EMPTY_USAGE, "questions": ""}
        if references is not None:
            profile["content"]["references"] = references
        pdf = pdf_bytes("Saved paper")
        library.publish(store_source, pdf, document_text({"pages": pages}), profile)
        return library, store_source, paper_id, pdf

    def test_upgrade_fills_bibliography_from_saved_text(self):
        library, store_source, paper_id, pdf = self._save_old_profile(["References\n[1] " + MOLLOW], schema_version=1)
        store = Store(self.temp)
        with closing(store.db):
            upgraded = store.paper(paper_id)
            self.assertEqual(upgraded["profile"]["job_id"], "original")
            self.assertEqual(upgraded["profile"]["content"]["references"][0]["text"], MOLLOW)
            self.assertIn("Библиография", upgraded["profile"]["coverage"]["sections"])
            self.assertEqual(upgraded["profile"]["coverage"]["notes"], [])
            self.assertEqual(store.jobs(), [])
            self.assertIn("**Название не установлено**  \n  B. R. Mollow",
                          (library.folder(store_source) / "profile.md").read_text(encoding="utf-8"))
            self.assertEqual(library.read(store_source, "article.pdf"), pdf)

    def test_upgrade_splits_saved_unnumbered_entries_and_keeps_resolved_metadata(self):
        first = "Laura Bender and Martin Klein. A sample paper. Journal of Tests, 2021."
        second = "Maya Ortiz. A different paper. Journal of Tests, 2022."
        third = "Morgan Reed. A final paper. Journal of Tests, 2023."
        base = {"number": "", "page": 1, "arxiv_ids": [], "dois": [], "url": "", "citations": []}
        merged = {**base, "text": first + " " + second}
        resolved = {**base, "text": third, "title": "Verified title", "dois": ["10.1234/final"],
                    "url": "https://doi.org/10.1234/final"}
        pages = ["References\n" + first + "\n" + second + "\n\n" + third]
        library, source, paper_id, pdf = self._save_old_profile(pages, schema_version=3, references=[merged, resolved])
        store = Store(self.temp)
        with closing(store.db):
            paper = store.paper(paper_id)
            profile = paper["profile"]
            entries = profile["content"]["references"]
            self.assertEqual(profile["schema_version"], PROFILE_VERSION)
            self.assertEqual(profile["job_id"], "original")
            self.assertEqual([entry["text"] for entry in entries], [first, second, third])
            self.assertEqual(entries[2], resolved)
            items = next(section["items"] for section in paper["sections"] if section["key"] == "references")
            self.assertEqual([item["label"] for item in items], ["A sample paper", "A different paper", "Verified title"])
            self.assertIn("A different paper", library.read(source, "profile.md").decode())
            self.assertEqual(library.read(source, "article.pdf"), pdf)
            self.assertEqual(store.jobs(), [])

    def test_recover_interrupted_file_replacement(self):
        library = MaterialLibrary(self.temp / "Materials")
        source = "arxiv:" + FOCAL
        folder = library.folder(source)
        previous = {name: f"Previous {name}".encode() for name in MANAGED_FILES}
        old = folder / ".rollback" / "old"
        old.mkdir(parents=True)
        for name, content in previous.items():
            (old / name).write_bytes(content)
            (folder / name).write_bytes(b"Uncommitted replacement")
        (folder / "user-notes.txt").write_bytes(b"Reader notes")
        MaterialLibrary(library.root)
        self.assertEqual({name: (folder / name).read_bytes() for name in MANAGED_FILES}, previous)
        self.assertEqual((folder / "user-notes.txt").read_bytes(), b"Reader notes")
        self.assertFalse((folder / ".rollback").exists())

    def test_publication_retries_transient_windows_directory_errors(self):
        rename = Path.rename
        interrupted = set()

        def rename_with_temporary_lock(path, destination):
            if path.name in {".prepare", ".rollback"} and path.name not in interrupted:
                interrupted.add(path.name)
                error = PermissionError("Temporary directory lock")
                error.winerror = 5
                raise error
            return rename(path, destination)

        with patch.object(Path, "rename", rename_with_temporary_lock), patch("materials.time.sleep"):
            library, source, _, pdf = self._save_old_profile(["Article text"], schema_version=PROFILE_VERSION)
        self.assertEqual(interrupted, {".prepare", ".rollback"})
        self.assertEqual(library.read(source, "article.pdf"), pdf)
        self.assertEqual(library.profile(source)["job_id"], "original")
        self.assertFalse((library.folder(source) / ".rollback").exists())

    def test_upgrade_preserves_demo_history_and_merges_arxiv_versions(self):
        with closing(sqlite3.connect(self.temp / "research.sqlite3")) as db, db:
            db.executescript("""
                CREATE TABLE papers (id TEXT PRIMARY KEY, source TEXT UNIQUE, title TEXT, kind TEXT, created TEXT);
                CREATE TABLE jobs (id TEXT PRIMARY KEY, paper_id TEXT REFERENCES papers(id), created TEXT,
                    updated TEXT, status TEXT, stage TEXT, progress INTEGER, questions TEXT, depth TEXT,
                    scenario TEXT, settings TEXT, events TEXT, report TEXT, error TEXT);
                INSERT INTO papers VALUES ('a','arxiv:2409.10975v1','Article v1','arxiv','2024-01-01');
                INSERT INTO papers VALUES ('b','arxiv:2409.10975v2','Article v2','arxiv','2024-01-02');
                INSERT INTO jobs VALUES ('one','a','2024-01-01','2024-01-01','completed','Demo',100,'','brief','success','{}','[]','Old report 1',NULL);
                INSERT INTO jobs VALUES ('two','b','2024-01-02','2024-01-02','completed','Demo',100,'','brief','success','{}','[]','Old report 2',NULL);
            """)
        store = Store(self.temp)
        self.addCleanup(store.db.close)
        self.assertEqual(len(store.papers()), 1)
        self.assertEqual(store.papers()[0]["source"], "arxiv:" + FOCAL)
        self.assertEqual(store.papers()[0]["job_count"], 2)
        self.assertEqual(store.job("one")["input_source"], "arxiv:" + FOCAL + "v1")
        self.assertEqual(store.job("two")["input_source"], "arxiv:" + FOCAL + "v2")
        self.assertEqual(store.job("two")["mode"], "demo")
        self.assertEqual(store.job("two")["report"], "Old report 2")


class ParsingTest(unittest.TestCase):
    def test_reference_link_priority_and_year_without_metadata(self):
        entry = {"number": "1", "text": "A. Reader, Sample article, Journal of Tests, 2022.",
                 "arxiv_ids": [], "dois": ["10.1234/example"], "url": ""}
        self.assertEqual(reference_url(entry), "https://doi.org/10.1234/example")
        self.assertEqual(_reference_item(entry)["text"], "Journal of Tests (2022)")
        entry["publisher_url"] = "https://journals.example/one"
        self.assertEqual(reference_url(entry), entry["publisher_url"])
        entry["arxiv_ids"] = [FOCAL]
        self.assertEqual(reference_url(entry), "https://arxiv.org/abs/" + FOCAL)
        entry["text"] += " https://journals.example/one.pdf"
        self.assertEqual(reference_url(entry), "https://journals.example/one.pdf")

    def test_select_manuscript_handles_plain_tex_and_ambiguous_archive(self):
        self.assertEqual(select_manuscript(TEX), ("manuscript.tex", TEX))
        self.assertIsNone(select_manuscript(source_archive({"first.tex": TEX, "second.tex": TEX})))
        self.assertIsNone(select_manuscript(source_archive({"../../main.tex": TEX})))
        self.assertIsNone(select_manuscript(b"\x1f\x8b"))

    def test_unnumbered_references_cross_pages_and_end_at_lettered_appendix(self):
        pages = [
            "References\nLaura Bender and Martin Klein. A sample paper.Journal of Tests, 8:20, 2021.\n"
            "Maya Ortiz, Noah Kim, and Lina Costa. A study of\n1",
            "2\nvariational circuits.arXiv preprint arXiv:2411.12345, 2024.\n"
            "Morgan Reed. Test notes. November 2026.\nURLhttps://example.org/notes/.\n"
            "Alex Lee. An empirical study.\nJournal of Tests, 9:3, 2025.\n"
            "A Additional results\nThe appendix discusses another result.\nReferences\n[1] Appendix bibliography.",
        ]
        parts = split_sections(pages)
        entries = bibliography(parts)
        self.assertEqual([entry["page"] for entry in entries], [1, 1, 2, 2])
        self.assertEqual([entry["number"] for entry in entries], ["", "", "", ""])
        self.assertEqual(entries[1]["text"], "Maya Ortiz, Noah Kim, and Lina Costa. A study of "
                         "variational circuits.arXiv preprint arXiv:2411.12345, 2024.")
        self.assertEqual(entries[1]["url"], "https://arxiv.org/abs/2411.12345")
        self.assertEqual(entries[2]["url"], "https://example.org/notes/")
        self.assertEqual(entries[3]["text"], "Alex Lee. An empirical study. Journal of Tests, 9:3, 2025.")
        self.assertEqual(_reference_item(entries[0])["authors"], "Laura Bender and Martin Klein")
        self.assertEqual(_reference_item(entries[0])["label"], "A sample paper")
        self.assertEqual(parts[-1]["section"], "Приложение")

    def test_unnumbered_author_year_entries_split_at_author_line(self):
        entries = bibliography(split_sections([
            "References\nBender, L.,\nKlein, M. (2021).\nQuantum Circuit Benchmarks.\nJournal of Tests, 8, 20–23.\n"
            "Ortiz, M. (2022). A follow-up study.\nJournal of Tests, 9, 14–19.",
        ]))
        self.assertEqual([entry["text"] for entry in entries], [
            "Bender, L., Klein, M. (2021). Quantum Circuit Benchmarks. Journal of Tests, 8, 20–23.",
            "Ortiz, M. (2022). A follow-up study. Journal of Tests, 9, 14–19.",
        ])

    def test_citations_track_main_text_pages_and_expand_groups(self):
        pages = [
            "Introduction\nEarlier work [1, 3–4]. Another mention of [4].",
            "Methods\nWe follow [2; 4].",
            "References\n[1] Reference one.\n[2] Reference two.\n[3] Reference three.\n[4] Reference four.",
            "Supplemental Material\nWe also use [1].",
        ]
        entries = bibliography(split_sections(pages))
        introduction = {"page": 1, "section": "Введение"}
        methods = {"page": 2, "section": "Методы"}
        self.assertEqual([entry["citations"] for entry in entries],
                         [[introduction], [methods], [introduction], [introduction, methods]])

    def test_unnamed_bibliography_can_start_early_and_ends_before_supplement(self):
        pages = ["[1] Step one\n[2] Step two\n[3] Step three",
                 "Methods\nWe measured the radiation from the probe.\nResults\nThe spectra had a clear peak.",
                 "[1] " + MOLLOW + "\n[2] Other author, 2023.\n[3] Last author,",
                 "a paper published in 2024.", "1\nSupplemental Material\nMethods\nA separate apparatus.",
                 "References\n[1] A supplement reference."]
        parts = split_sections(pages)
        entries = bibliography(parts)
        self.assertEqual([entry["number"] for entry in entries], ["1", "2", "3"])
        self.assertEqual(entries[0]["page"], 3)
        self.assertEqual(entries[-1]["text"], "Last author, a paper published in 2024.")
        snippets = _candidate_snippets(parts)
        self.assertEqual({snippet["section"] for snippet in snippets} & {"Методы", "Результаты"}, {"Методы", "Результаты"})
        self.assertTrue(all(snippet["page"] < 3 for snippet in snippets))

    def test_arxiv_uses_retry_after_and_skips_requests_during_pause(self):
        http = Http(lambda: 100)
        provider = Publications()
        response = HTTPError("https://export.arxiv.org/api/query", 429, "", {"Retry-After": "17"}, None)
        with patch("publications.time.monotonic", return_value=100) as clock, \
                patch.object(http.opener, "open", side_effect=[response, io.BytesIO(atom())]) as request:
            with self.assertRaises(ServiceError) as rejected:
                provider.atom(http, id_list=FOCAL)
            self.assertFalse(rejected.exception.deferred)
            self.assertEqual(rejected.exception.retry_after, 17)
            clock.return_value = 110
            with self.assertRaises(ServiceError) as paused:
                provider.atom(http, id_list=FOCAL)
            self.assertTrue(paused.exception.deferred)
            self.assertEqual(paused.exception.retry_after, 7)
            self.assertEqual(request.call_count, 1)
            clock.return_value = 117
            self.assertEqual(provider.atom(http, id_list=FOCAL), [])
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
