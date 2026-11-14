"""Local Research Agent server, job queue, and material library."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, urlencode, urlparse

from manuscripts import render_tex_preview
from materials import (PDF_LIMIT, PROFILE_VERSION, EXTRACTION_VERSION, RMP_REFERENCE_START, MaterialError, MaterialLibrary,
                       abstract_from_parts, arxiv_id, citation_location, clean_text, extraction_signature, main_text_pages, profile_sections,
                       render_markdown_preview, saved_pages, split_sections)
from publications import Http, Publications, ServiceError, metadata_title
from research import (BIBLIOGRAPHY_MISSING, LEGACY_SEARCH_NOTES, bibliography, locate_citations,
                      reference_url)
from retrieval import BUDGETS
from version import VERSION
from model_catalog import Catalog, cost, model_info, validate_effort
from pipeline import JobExpired, JobStopped, Pipeline, STEP_NAMES
from pdf_citations import highlighted_pdf

ROOT = Path(__file__).resolve().parent
ACTIVE = ("queued", "running")
ANALYSIS_DEPTHS = tuple(BUDGETS)
STAGES = ["Получение материалов", "Выделение утверждений", "Поиск предшественников",
          "Проверки", "Подготовка отчёта"]
DEFAULTS = {
    "interests": "", "exclusions": "",
    "rules": "Различать утверждения авторов и независимые проверки. Для замечаний указывать место в статье, обоснование и статус проверки. Новизну, надёжность и значимость оценивать отдельно. Явно описывать границы анализа.",
    "language": "Русский", "stage_seconds": 3, "time_limit_seconds": 240,
    "model": "gpt-6-luna", "reasoning_effort": "low", "max_api_calls": 2, "max_output_tokens": 5000,
}
EMPTY_USAGE = {"model": "", "calls": 0, "reported_calls": 0, "input_tokens": 0, "output_tokens": 0,
               "cached_tokens": 0, "unpriced_tokens": False}
MANUSCRIPT_NOTE = "Исходник рукописи · "


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def exclusive_data(directory):
    """Prevent two server processes from using the same queue and materials."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "server.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("This data directory is already in use by another Research Agent server.") from None
        yield


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory, materials_dir=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "uploads").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.materials = MaterialLibrary(materials_dir or self.directory / "Materials")
        self.db = sqlite3.connect(self.directory / "research.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                    kind TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
                    created TEXT NOT NULL, updated TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    questions TEXT NOT NULL, depth TEXT NOT NULL, scenario TEXT NOT NULL,
                    settings TEXT NOT NULL, events TEXT NOT NULL, report TEXT, error TEXT,
                    mode TEXT NOT NULL DEFAULT 'demo', input_source TEXT NOT NULL DEFAULT '',
                    resolved_version TEXT NOT NULL DEFAULT '', api_usage TEXT NOT NULL DEFAULT '{}');
            """)
            self._migrate()
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULTS, ensure_ascii=False),))
            for row in self.db.execute("""SELECT j.id,j.events,j.mode,p.source,p.id AS paper_id FROM jobs j
                JOIN papers p ON p.id=j.paper_id WHERE j.status IN ('queued', 'running')""").fetchall():
                profile = self.materials.profile(row["source"]) if row["mode"] == "research" else None
                if profile and profile["job_id"] == row["id"]:
                    self.db.execute("UPDATE jobs SET result_revision=?,result_status=?,source_sha256=? WHERE id=?",
                                    (profile.get("revision", profile["job_id"]), profile.get("result_status", "ready"),
                                     profile["article"].get("sha256", ""), row["id"]))
                if profile and profile["job_id"] == row["id"] and (not profile.get("steps") or all(
                        value["status"] in ("completed", "not_applicable") for value in profile["steps"].values())):
                    self.db.execute("UPDATE papers SET title=? WHERE id=?", (profile["article"]["title"], row["paper_id"]))
                    error = profile.get("processing_error") or None
                    self.change(row["id"], "failed" if error else "completed",
                                "Профиль восстановлен после перезапуска", 100, error=error)
                    continue
                events = json.loads(row["events"]) + [{"time": now(), "message": "Приложение перезапущено; автоматический повтор отключён."}]
                self.db.execute("UPDATE jobs SET status='interrupted', stage='Прервано перезапуском', updated=?, events=? WHERE id=?",
                                (now(), json.dumps(events, ensure_ascii=False), row["id"]))
        self._upgrade_profiles()

    def _migrate(self):
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, default in (("mode", "'demo'"), ("input_source", "''"),
                              ("resolved_version", "''"), ("api_usage", "'{}'"),
                              ("operation", "'profile'"), ("parent_job_id", "''"), ("steps", "'{}'"),
                              ("result_revision", "''"), ("result_status", "'none'"),
                              ("input_revision", "''"), ("source_sha256", "''")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}")
        self.db.execute("""UPDATE jobs SET input_source=(SELECT source FROM papers WHERE id=jobs.paper_id)
                           WHERE input_source=''""")
        self.db.execute("UPDATE jobs SET operation=scenario WHERE operation='profile' AND scenario IN ('refresh','analogs')")
        papers = self.db.execute("SELECT id,source FROM papers WHERE kind='arxiv' ORDER BY created,id").fetchall()
        for paper in papers:
            base, _ = arxiv_id(paper["source"])
            canonical = "arxiv:" + base
            if canonical == paper["source"]:
                continue
            existing = self.db.execute("SELECT id FROM papers WHERE source=?", (canonical,)).fetchone()
            if existing:
                self.db.execute("UPDATE jobs SET paper_id=? WHERE paper_id=?", (existing["id"], paper["id"]))
                self.db.execute("DELETE FROM papers WHERE id=?", (paper["id"],))
            else:
                self.db.execute("UPDATE papers SET source=?,title=? WHERE id=?", (canonical, "arXiv " + base, paper["id"]))

    def _upgrade_profiles(self):
        for row in self.db.execute("SELECT source FROM papers").fetchall():
            source = row["source"]
            try:
                profile = self.materials.profile(source)
                if not profile or profile["schema_version"] >= PROFILE_VERSION:
                    continue
                text = self.materials.read_text(source)
                pages = saved_pages(text)
                main_pages, _ = main_text_pages(pages)
                parts = split_sections(main_pages)
                content = profile["content"]
                content["analogs"] = [analog for analog in content.get("analogs", []) if analog.get("page")]
                entries = content.get("references") or []
                if parts and (profile["schema_version"] < 9 or not entries or any(
                        not entry["number"] and RMP_REFERENCE_START.match(entry["text"]) for entry in entries)):
                    entries = bibliography(parts, entries) or entries
                if parts:
                    locate_citations(entries, split_sections(pages), max_page=len(main_pages))
                for entry in entries:
                    entry["url"] = reference_url(entry)
                    if entry.get("title"):
                        entry["title"] = metadata_title(entry["title"])
                content["references"] = entries
                if profile["schema_version"] < 5 and content["summary"]:
                    content["summary"] = [" ".join(content["summary"])]
                profile.setdefault("revision", profile["job_id"])
                profile.setdefault("result_status", "ready" if content["summary"] else "partial")
                for answer in content.get("answers", []):
                    answer.setdefault("sources", [])
                    if answer["status"] == "answered" and not answer.get("sources"):
                        answer["status"] = "unverified"
                article = profile["article"]
                if not article.get("abstract"):
                    article["abstract"] = abstract_from_parts(parts)
                profile["schema_version"] = PROFILE_VERSION
                if entries:
                    coverage = profile["coverage"]
                    coverage["notes"] = [note for note in coverage["notes"] if note != BIBLIOGRAPHY_MISSING]
                    if "Библиография" not in coverage["sections"]:
                        coverage["sections"].append("Библиография")
                self.materials.publish(source, self.materials.read(source, "article.pdf"), text, profile)
            except (OSError, ValueError, MaterialError, KeyError, TypeError) as error:
                print(f"Profile upgrade skipped for {source}: {type(error).__name__}", file=sys.stderr, flush=True)

    def settings(self):
        with self.lock:
            return self.setting_defaults(json.loads(self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]))

    @staticmethod
    def setting_defaults(settings):
        result = {**DEFAULTS, **settings}
        if "reasoning_effort" not in settings and result["model"] != "gpt-6-luna":
            result["reasoning_effort"] = "auto"
        return result

    def validate_settings(self, data, current=None):
        if not isinstance(data, dict):
            raise ValueError("Параметры должны быть объектом JSON.")
        current, result = current or self.settings(), {}
        for key in ("interests", "exclusions", "rules", "language"):
            value = data.get(key, current[key])
            if not isinstance(value, str) or len(value) > 12000:
                raise ValueError("Некорректное поле настроек: " + key)
            result[key] = clean_text(value)
        for key, low, high in (("stage_seconds", 1, 10), ("time_limit_seconds", 5, 3600),
                               ("max_api_calls", 1, 6), ("max_output_tokens", 1000, 12000)):
            value = data.get(key, current[key])
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key}: допустимо целое число от {low} до {high}.")
            result[key] = value
        model = data.get("model", current["model"])
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}", model):
            raise ValueError("Укажите идентификатор модели OpenAI, например gpt-6-luna.")
        result["model"] = model
        # Changing a model never carries an incompatible effort over silently.
        effort = data.get("reasoning_effort", current.get("reasoning_effort", "auto") if model == current["model"] else "auto")
        result["reasoning_effort"] = validate_effort(model, effort)
        review = data.get("review_bibliography", current.get("review_bibliography", False))
        if type(review) is not bool:
            raise ValueError("Некорректный параметр проверки библиографии.")
        if review:
            result["review_bibliography"] = True
        return result

    def save_settings(self, data):
        result = self.validate_settings(data)
        with self.lock, self.db:
            self.db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(result, ensure_ascii=False),))
        return result

    def create(self, data, *, refresh=False):
        mode = data.get("mode", "demo")
        depth = data.get("depth", "standard")
        scenario = data.get("scenario", "success")
        questions = data.get("questions", "")
        job_settings = self.validate_settings(data.get("settings", {}))
        if mode not in ("demo", "research") or depth not in ANALYSIS_DEPTHS or scenario not in ("success", "error"):
            raise ValueError("Неизвестная глубина или сценарий.")
        if not isinstance(questions, str) or len(questions) > 12000:
            raise ValueError("Вопросы должны быть текстом длиной не более 12000 символов.")
        pdf = data.get("pdf")
        raw = None
        if pdf is not None:
            if not isinstance(pdf, dict):
                raise ValueError("Некорректная загрузка PDF.")
            try:
                raw = base64.b64decode(pdf.get("data", ""), validate=True)
            except (ValueError, TypeError):
                raise ValueError("Некорректные данные PDF.") from None
            if not raw.startswith(b"%PDF-"):
                raise ValueError("Нужен файл PDF.")
            if depth != "vision" and len(raw) > PDF_LIMIT:
                raise ValueError("Для PDF больше 15 МБ выберите «Полный текст + картинки».")
            digest = hashlib.sha256(raw).hexdigest()
            source, kind = "upload:" + digest, "pdf"
            name = pdf.get("name", "document.pdf")
            if not isinstance(name, str) or len(name) > 255:
                raise ValueError("Некорректное имя файла.")
            title = clean_text(name.replace("\\", "/").split("/")[-1]) or "document.pdf"
            input_source = source
        else:
            base, version = arxiv_id(data.get("source", ""))
            title, source, kind = "arXiv " + base, "arxiv:" + base, "arxiv"
            input_source = source + version
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO papers VALUES (?, ?, ?, ?, ?)",
                            (uuid.uuid4().hex, source, title, kind, now()))
            paper_id = self.db.execute("SELECT id FROM papers WHERE source=?", (source,)).fetchone()[0]
            job_id = self._enqueue(paper_id, mode, depth, questions, "refresh" if refresh else scenario, input_source,
                                   settings=job_settings)
            if raw is not None:
                upload = self.directory / "uploads" / (digest + ".pdf")
                pending = upload.with_suffix("." + job_id + ".tmp")
                try:
                    pending.write_bytes(raw)
                    pending.replace(upload)
                finally:
                    pending.unlink(missing_ok=True)
        return self.job(job_id)

    def _enqueue(self, paper_id, mode, depth, questions, scenario, input_source, *, settings=None,
                 operation=None, parent_job_id="", input_revision="", source_sha256=""):
        if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 30:
            raise ValueError("Очередь заполнена: максимум 30 активных заданий.")
        if mode == "research" and self.db.execute("""SELECT 1 FROM jobs WHERE paper_id=? AND mode='research'
                                                     AND status IN ('queued','running')""", (paper_id,)).fetchone():
            raise ValueError("Эта статья уже стоит в очереди или обрабатывается.")
        if mode == "research" and not input_revision:
            source = self.db.execute("SELECT source FROM papers WHERE id=?", (paper_id,)).fetchone()[0]
            current_profile = self.materials.profile(source)
            if current_profile:
                input_revision = current_profile.get("revision", current_profile["job_id"])
        settings = dict(settings or self.settings())
        if mode == "research" and depth == "vision":
            settings.update(max_api_calls=6, time_limit_seconds=max(600, settings["time_limit_seconds"]))
        elif mode == "research" and settings.get("review_bibliography") and not parent_job_id:
            settings["max_api_calls"] = min(6, settings["max_api_calls"] + 1)
        job_id, timestamp = uuid.uuid4().hex, now()
        if scenario == "analogs":
            message = "Задание на повторный поиск аналогов сохранено."
        else:
            message = ("Задание сохранено. Режим демонстрации; API не используется."
                       if mode == "demo" else "Задание на первичную обработку сохранено.")
        self.db.execute("""INSERT INTO jobs
            (id,paper_id,created,updated,status,stage,questions,depth,scenario,settings,events,mode,input_source)
            VALUES (?, ?, ?, ?, 'queued', 'В очереди', ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, paper_id, timestamp, timestamp, clean_text(questions), depth, scenario,
             json.dumps({**settings, "pricing": model_info(settings["model"])["pricing"]}, ensure_ascii=False),
             json.dumps([{"time": timestamp, "message": message}], ensure_ascii=False), mode, input_source))
        self.db.execute("""UPDATE jobs SET operation=?,parent_job_id=?,input_revision=?,source_sha256=? WHERE id=?""",
                        (operation or (scenario if scenario in ("refresh", "analogs") else "profile"),
                         parent_job_id, input_revision, source_sha256, job_id))
        if mode == "research":
            steps = {key: {"status": "pending", "message": "", "code": ""} for key in STEP_NAMES}
            self.db.execute("UPDATE jobs SET steps=? WHERE id=?", (json.dumps(steps), job_id))
        return job_id

    def reanalyze(self, paper_id, data):
        with self.lock, self.db:
            paper = self.paper(paper_id)
            if not paper:
                return None
            profile = paper["profile"]
            if not profile:
                raise ValueError("Сначала получите и сохраните PDF статьи.")
            previous = self.job(profile["job_id"])
            defaults = profile.get("analysis", {}).get("settings") or (previous or {}).get("settings", self.settings())
            params = self.validate_settings(data.get("settings", {}), defaults)
            depth = data.get("depth", profile.get("analysis", {}).get("depth", (previous or {}).get("depth", "standard")))
            questions = data.get("questions", profile.get("questions", ""))
            self.validate_analysis(depth, questions)
            raw = self.materials.read(paper["source"], "article.pdf")
            if profile["article"].get("sha256") and hashlib.sha256(raw).hexdigest() != profile["article"]["sha256"]:
                raise ValueError("Сохранённый PDF изменился. Загрузите файл заново.")
            job_id = self._enqueue(paper_id, "research", depth, questions, "success", paper["source"],
                                   settings=params, operation="reanalyze", input_revision=profile.get("revision", profile["job_id"]),
                                   source_sha256=hashlib.sha256(raw).hexdigest())
            return self.job(job_id)

    @staticmethod
    def validate_analysis(depth, questions):
        if depth not in ANALYSIS_DEPTHS or not isinstance(questions, str) or len(questions) > 12000:
            raise ValueError("Проверьте охват текста и вопросы (не более 12000 символов).")

    def retry(self, job_id, data):
        with self.lock, self.db:
            previous = self.job(job_id)
            if not previous:
                return None
            if previous["mode"] != "research" or previous["status"] in ACTIVE:
                raise ValueError("Продолжение доступно для завершившегося научного задания.")
            if not self.retry_info(previous)["allowed"]:
                raise ValueError(self.retry_info(previous)["message"])
            profile = self.materials.profile(previous["source"])
            revision = profile.get("revision", profile["job_id"]) if profile else ""
            expected = previous["result_revision"] or previous["input_revision"]
            if revision != expected:
                raise ValueError("Профиль уже изменился. Запустите новый разбор из актуального профиля.")
            params = self.validate_settings(data.get("settings", {}), previous["settings"])
            new_id = self._enqueue(previous["paper_id"], "research", previous["depth"], previous["questions"],
                                   previous["scenario"], previous["input_source"], settings=params,
                                   operation=previous["operation"], parent_job_id=job_id, input_revision=revision,
                                   source_sha256=previous["source_sha256"] or (profile or {}).get("article", {}).get("sha256", ""))
            return self.job(new_id)

    def retry_info(self, job):
        pending = [key for key, value in job["steps"].items()
                   if value["status"] not in ("completed", "not_applicable")
                   and (job["operation"] not in ("analogs", "references") or key in ("material", job["operation"]))]
        blocked = any(value.get("code") == "no_text" for value in job["steps"].values())
        allowed = job["mode"] == "research" and job["status"] not in ACTIVE and bool(pending) and not blocked
        model_needed = ("core" in pending or "analogs" in pending or "references" in pending
                        and (job["operation"] == "references" or job["settings"].get("review_bibliography")))
        return {"allowed": allowed, "steps": [STEP_NAMES[key] for key in pending], "model_needed": model_needed,
                "message": "Нужен PDF с текстовым слоем. Загрузите подходящий файл." if blocked else
                "Выполним только недостающие этапы. Вызовы модели учитываются в новом задании." if pending else
                "Все этапы завершены."}

    def step(self, job_id, key, status, message="", code=""):
        with self.lock, self.db:
            job = self.job(job_id)
            if not job or job["status"] not in ACTIVE:
                raise JobStopped()
            steps = job["steps"]
            steps[key] = {"status": status, "message": message, "code": code}
            self.db.execute("UPDATE jobs SET steps=?,updated=? WHERE id=?", (json.dumps(steps, ensure_ascii=False), now(), job_id))

    def save_checkpoint(self, job, raw, article, document):
        with self.lock:
            if not self.active(job["id"]):
                raise JobStopped()
            root = MaterialLibrary._direct(self.directory.resolve() / "checkpoints")
            folder = MaterialLibrary._direct(root / job["id"])
            folder.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(raw).hexdigest()
            for name, data in (("article.pdf", raw), ("checkpoint.json", json.dumps({
                    "version": EXTRACTION_VERSION, "parser": extraction_signature(), "source": job["source"], "sha256": digest,
                    "article": article, "document": document}, ensure_ascii=False).encode("utf-8"))):
                target = MaterialLibrary._direct(folder / name)
                pending = MaterialLibrary._direct(folder / (name + ".tmp"))
                pending.write_bytes(data)
                pending.replace(target)
            with self.db:
                self.db.execute("UPDATE jobs SET source_sha256=? WHERE id=?", (digest, job["id"]))

    def checkpoint(self, job_id, source):
        folder = self.directory / "checkpoints" / job_id
        if not folder.exists():
            return None
        folder = MaterialLibrary._direct(folder.absolute())
        try:
            raw = MaterialLibrary._direct(folder / "article.pdf").read_bytes()
            meta = json.loads(MaterialLibrary._direct(folder / "checkpoint.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if meta["source"] != source or meta["sha256"] != hashlib.sha256(raw).hexdigest():
            raise MaterialError("Сохранённые материалы задания изменились.")
        current_parser = meta.get("version") == EXTRACTION_VERSION and meta.get("parser") == extraction_signature()
        visual = None
        if current_parser:
            try:
                visual = json.loads(MaterialLibrary._direct(folder / "visual.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                pass
        return {"pdf": raw, "article": meta["article"],
                "document": meta.get("document") if current_parser else None, "visual": visual}

    def save_visual_checkpoint(self, job_id, state):
        with self.lock:
            if not self.active(job_id):
                raise JobStopped()
            folder = MaterialLibrary._direct(self.directory.resolve() / "checkpoints" / job_id)
            pending = MaterialLibrary._direct(folder / "visual.json.tmp")
            pending.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            MaterialLibrary._move_with_retry(pending, MaterialLibrary._direct(folder / "visual.json"), replace=True)

    def discard_checkpoint(self, job_id):
        """Remove only our temporary files after all stages have been saved."""
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return
        folder = MaterialLibrary._direct(self.directory.resolve() / "checkpoints" / job_id)
        if not folder.exists():
            return
        try:
            for name in ("article.pdf", "checkpoint.json", "visual.json", "article.pdf.tmp", "checkpoint.json.tmp", "visual.json.tmp"):
                MaterialLibrary._direct(folder / name).unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            pass  # The durable result is already committed; cleanup can be retried later.

    def refresh(self, paper_id):
        with self.lock:
            paper = self.db.execute("SELECT source,kind FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                return None
            if paper["kind"] != "arxiv":
                raise ValueError("Проверить новую версию можно для материалов arXiv.")
            previous = self.db.execute("""SELECT questions,depth FROM jobs WHERE paper_id=? AND mode='research' AND scenario!='analogs'
                                         ORDER BY created DESC LIMIT 1""", (paper_id,)).fetchone()
            return self.create({"source": paper["source"], "mode": "research",
                                **(dict(previous) if previous else {})}, refresh=True)

    def repeat_analogs(self, paper_id):
        with self.lock, self.db:
            paper = self.db.execute("SELECT source FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                return None
            profile = self.materials.profile(paper["source"])
            if not profile or not profile["content"]["summary"]:
                raise ValueError("Сначала подготовьте профиль статьи с обзором.")
            job_id = self._enqueue(paper_id, "research", "standard", profile["questions"], "analogs", paper["source"])
            return self.job(job_id)

    def repeat_references(self, paper_id):
        with self.lock, self.db:
            paper = self.db.execute("SELECT source FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                return None
            profile = self.materials.profile(paper["source"])
            if not profile or not profile["content"].get("references"):
                raise ValueError("Сначала получите статью со списком литературы.")
            job_id = self._enqueue(paper_id, "research", "standard", profile["questions"], "success", paper["source"], operation="references")
            return self.job(job_id)

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT j.*, p.title, p.source, p.kind FROM jobs j JOIN papers p ON j.paper_id=p.id WHERE j.id=?", (job_id,)).fetchone()
            if not row:
                return None
            value = dict(row)
            for key in ("settings", "events", "api_usage", "steps"):
                value[key] = json.loads(value[key])
            value["settings"] = self.setting_defaults(value["settings"])
            value["api_usage"] = {**EMPTY_USAGE, **value["api_usage"]}
            value["estimated_cost"] = cost(value["api_usage"], value["settings"].get("pricing"))
            value["retry"] = self.retry_info(value)
            return value

    def jobs(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("""SELECT j.id,j.paper_id,j.created,j.updated,j.status,j.stage,j.progress,j.mode,j.result_status,p.title
                FROM jobs j JOIN papers p ON j.paper_id=p.id ORDER BY j.created DESC""")]

    def delete_job(self, job_id):
        with self.lock, self.db:
            job = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return False
            if job["status"] in ACTIVE or self.db.execute(
                "SELECT 1 FROM jobs WHERE parent_job_id=? AND status IN ('queued','running')", (job_id,)
            ).fetchone():
                raise ValueError("Дождитесь окончания задания или его продолжения.")
            self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self.discard_checkpoint(job_id)
        return True

    def active(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            return row is not None and row["status"] in ACTIVE

    def papers(self):
        with self.lock:
            papers = [dict(r) for r in self.db.execute("""SELECT p.*,count(j.id) AS job_count,
                sum(CASE WHEN j.status='completed' THEN 1 ELSE 0 END) AS report_count
                FROM papers p LEFT JOIN jobs j ON j.paper_id=p.id GROUP BY p.id ORDER BY p.created DESC""")]
            for paper in papers:
                try:
                    profile = self.materials.profile(paper["source"])
                    article = profile["article"] if profile else {}
                except (OSError, ValueError, MaterialError, KeyError, TypeError):
                    profile, article = None, {}
                paper.update(has_profile=bool(profile), authors=article.get("authors", []),
                             has_abstract=bool(article.get("abstract")), profile_job_id=profile["job_id"] if profile else "",
                             result_status=profile.get("result_status", "ready" if profile["content"]["summary"] else "partial") if profile else "none")
            return papers

    def paper(self, paper_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not row:
                return None
            paper = dict(row)
            latest = self.db.execute("""SELECT id,status,stage,error,resolved_version,updated FROM jobs
                                      WHERE paper_id=? AND mode='research' ORDER BY created DESC LIMIT 1""", (paper_id,)).fetchone()
            profile = self.materials.profile(paper["source"])
            paper["latest_job"] = dict(latest) if latest else None
            if latest:
                full_job = self.job(latest["id"])
                paper["latest_job"].update(retry=full_job["retry"], steps=full_job["steps"], result_status=full_job["result_status"])
            continuation = self.job(profile["job_id"]) if profile else None
            paper["continuation_job"] = ({key: continuation[key] for key in ("id", "retry")}
                                         if continuation and continuation["retry"]["allowed"] else None)
            paper["profile"] = profile
            paper["sections"] = profile_sections(profile) if profile else []
            paper["directory"] = ""
            if profile:
                folder = self.materials.folder(paper["source"])
                windows_materials = os.environ.get("RESEARCH_AGENT_HOST_MATERIALS")
                paper["directory"] = str(PureWindowsPath(windows_materials) / folder.name if windows_materials else folder)
            return paper

    def paper_file(self, paper_id, filename, snapshot):
        with self.lock:
            row = self.db.execute("SELECT source FROM papers WHERE id=?", (paper_id,)).fetchone()
            profile = self.materials.profile(row["source"]) if row else None
            if not profile:
                return None
            if snapshot and profile.get("revision", profile["job_id"]) != snapshot:
                raise ValueError("Профиль обновился. Откройте актуальную версию из библиотеки.")
            if filename in {"manuscript.tex", "manuscript.html"}:
                manuscript = profile["article"].get("manuscript", {})
                if manuscript.get("file") != "manuscript.tex":
                    return None
                data = self.materials.read(row["source"], "manuscript.tex")
                return render_tex_preview(data, manuscript["name"], profile.get("revision", profile["job_id"])) if filename.endswith(".html") else data
            return self.materials.read(row["source"], filename)

    def set_version(self, job_id, version):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET resolved_version=?,updated=? WHERE id=?", (version, now(), job_id))

    def uploaded_pdf(self, source):
        try:
            raw = (self.directory / "uploads" / (source.removeprefix("upload:") + ".pdf")).read_bytes()
        except FileNotFoundError:
            raise MaterialError("Локальный PDF недоступен. Загрузите файл заново.") from None
        if hashlib.sha256(raw).hexdigest() != source.removeprefix("upload:"):
            raise MaterialError("Сохранённый PDF изменился. Загрузите файл заново.")
        return raw

    def record_api(self, job_id, *, attempt_model=None, usage=None):
        with self.lock, self.db:
            job = self.job(job_id)
            if job is None:
                raise JobStopped()
            record = job["api_usage"]
            if attempt_model is not None:
                if job["status"] not in ACTIVE:
                    raise JobStopped()
                if record["calls"] >= job["settings"]["max_api_calls"]:
                    raise ServiceError("Исчерпан лимит запросов к модели для этого задания.")
                record["model"] = attempt_model
                record["calls"] += 1
            elif isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                                                   for key in ("input_tokens", "output_tokens")):
                record["reported_calls"] += 1
                record["input_tokens"] += usage["input_tokens"]
                record["output_tokens"] += usage["output_tokens"]
                details = usage.get("input_tokens_details") or {}
                cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
                if type(cached) is int and 0 <= cached <= usage["input_tokens"]:
                    record["cached_tokens"] += cached
                else:
                    record["unpriced_tokens"] = True
                if isinstance(details, dict) and any(details.get(key) for key in ("cache_creation_tokens", "cache_write_tokens")):
                    record["unpriced_tokens"] = True
            self.db.execute("UPDATE jobs SET api_usage=?,updated=? WHERE id=?",
                            (json.dumps(record), now(), job_id))

    def change(self, job_id, status, stage, progress=None, error=None, report=None):
        with self.lock, self.db:
            job = self.job(job_id)
            if not job or job["status"] not in ACTIVE:
                return False
            events = job["events"] + [{"time": now(), "message": stage}]
            self.db.execute("UPDATE jobs SET status=?,stage=?,progress=?,updated=?,events=?,error=?,report=? WHERE id=?",
                (status, stage, progress if progress is not None else job["progress"], now(),
                 json.dumps(events, ensure_ascii=False), error, report, job_id))
            return True


class Worker:
    def __init__(self, store, api_key):
        self.store = store
        self.api_key = api_key
        self.publications = Publications()
        self.catalog = Catalog(api_key)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="research-worker")

    def run(self):
        while not self.stop.is_set():
            with self.store.lock:
                row = self.store.db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                self.stop.wait(.2)
                continue
            job_id = row[0]
            try:
                self.process(job_id)
            except JobStopped:
                pass
            except JobExpired:
                if not self.stop.is_set():
                    self.store.change(job_id, "failed", "Превышен лимит времени",
                                      error="Обработка остановлена по лимиту времени из настроек задания.")
            except (ServiceError, MaterialError) as error:
                if not self.stop.is_set():
                    self.fail(job_id, str(error))
            except Exception as error:
                if not self.stop.is_set():
                    print(f"Job {job_id[:8]}: {type(error).__name__}", file=sys.stderr, flush=True)
                    self.fail(job_id, "Внутренняя ошибка обработки. Проверьте настройки и доступ к папке данных.")

    def fail(self, job_id, message):
        job = self.store.job(job_id)
        if job is None or job["status"] not in ACTIVE:
            return
        for key, value in job["steps"].items():
            if value["status"] == "running":
                self.store.step(job_id, key, "failed", message, "processing_error")
        self.store.change(job_id, "failed", "Ошибка · " + job["stage"], error=message)

    def process(self, job_id):
        job = self.store.job(job_id)
        if job["mode"] == "research":
            return self.research(job)
        self.demo(job)

    def research(self, job):
        return Pipeline(self, job).run()

    def demo(self, job):
        job_id = job["id"]
        started = time.monotonic()
        for index, stage in enumerate(STAGES):
            if self.stop.is_set() or not self.store.change(job_id, "running", "Демонстрация: " + stage, index * 20):
                return
            deadline = time.monotonic() + job["settings"]["stage_seconds"]
            while time.monotonic() < deadline:
                if self.stop.wait(.1) or self.store.job(job_id)["status"] != "running":
                    return
                if time.monotonic() - started >= job["settings"]["time_limit_seconds"]:
                    self.store.change(job_id, "failed", "Превышен лимит времени", error="Обработка остановлена по лимиту времени из настроек задания.")
                    return
            if job["scenario"] == "error" and index == 2:
                self.store.change(job_id, "failed", "Демонстрационная ошибка", error="Имитирована недоступность поиска предшественников. Это выбранный тестовый сценарий; сетевых запросов не было.")
                return
        material_note = ("Загруженный PDF сохранён локально, но его текст не извлекался." if job["kind"] == "pdf"
                         else "Сохранён только введённый идентификатор arXiv: существование статьи и версия не проверены. Текст статьи не загружался.")
        report = f"""# Демонстрационный отчёт

Материал: {job['title']}
Источник: {job['input_source']}
Задание: {job_id}

Это шаблон результата для проверки приложения. Научный анализ не выполнялся.
{material_note}

## Краткий итог
Оценка работы отсутствует: модель не подключена.

## Основные результаты
Утверждения авторов и численные результаты не извлечены.

## Новизна
Поиск предшественников не выполнялся. Новизна не оценена.

## Научная строгость
Формулы, экспериментальные контроли и вычисления не проверялись.
Независимо проверенных научных выводов нет. Несоответствий не установлено, поскольку анализ не проводился.

## Масштаб продвижения и дальнейшие исследования
Значимость и перспективы не оценены.

## Вопросы пользователя
{job['questions'] or 'Не указаны.'}

## Границы разбора
Все пять этапов были имитированы. Глубина «{job['depth']}» и научные правила сохранены, но пока не влияют на анализ.
Вызовы API: 0. Расходы на API: 0. Внешних сетевых запросов: 0.
"""
        self.store.change(job_id, "completed", "Демонстрация завершена", 100, report=report)


class Handler(BaseHTTPRequestHandler):
    server_version = f"ResearchAgent/{VERSION}"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        pass  # Do not log uploaded content, questions, or credentials.

    def respond(self, status, body, content_type="application/json; charset=utf-8", attachment=None):
        if content_type.startswith("application/json"):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            pass

    def allowed(self):
        host = self.headers.get("Host", "")
        valid = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in valid:
            self.respond(403, {"error": "Недопустимый адрес сервера."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in valid}:
            self.respond(403, {"error": "Запрос с другого сайта запрещён."})
            return False
        return True

    def citation_pdf(self, paper_id, snapshot, anchor, redirect=False):
        with self.store.lock:
            text = self.store.paper_file(paper_id, "text.txt", snapshot)
            raw = None if redirect else self.store.paper_file(paper_id, "article.pdf", snapshot)
        location = citation_location(text, anchor) if text is not None else None
        if location is None or (not redirect and raw is None):
            return self.respond(404, {"error": "Место цитирования не найдено."})
        if redirect:
            query = urlencode({"snapshot": snapshot, "citation": anchor})
            self.send_response(302)
            self.send_header("Location", f"article.pdf?{query}#page={location[0]}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.respond(200, highlighted_pdf(raw, *location), "application/pdf")

    def do_GET(self):
        if not self.allowed():
            return
        request = urlparse(self.path)
        path = request.path
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        static.update({"/" + name: (name, "text/javascript; charset=utf-8")
                       for name in ("math.js", "vendor/katex/katex.min.js")})
        static.update({"/fonts/" + name: ("fonts/" + name, mime) for name, mime in (
            ("InterVariable.woff2", "font/woff2"), ("PTSerif-Regular.ttf", "font/ttf"), ("PTSerif-Bold.ttf", "font/ttf"))})
        if path in static:
            name, mime = static[path]
            return self.respond(200, (ROOT / "static" / name).read_bytes(), mime)
        if path == "/api/health":
            return self.respond(200, {"app": "research-agent", "version": VERSION, "mode": "research",
                                      "instance": os.environ.get("RESEARCH_AGENT_INSTANCE", ""),
                                      "api_configured": bool(self.server.worker.api_key),
                                      "pdf_configured": find_spec("pypdf") is not None,
                                      "worker_alive": self.server.worker.thread.is_alive()})
        if path == "/api/settings":
            return self.respond(200, self.store.settings())
        if path == "/api/models":
            return self.respond(200, self.server.worker.catalog.snapshot())
        if path == "/api/jobs":
            return self.respond(200, self.store.jobs())
        if path == "/api/papers":
            return self.respond(200, self.store.papers())
        match = re.fullmatch(r"/api/papers/([a-f0-9]{32})(?:/(article\.pdf|profile\.md|profile\.html|citation\.html|text\.txt|manuscript\.tex|manuscript\.html))?", path)
        if match:
            try:
                if not match[2]:
                    paper = self.store.paper(match[1])
                    if paper:
                        return self.respond(200, paper)
                else:
                    filename = match[2]
                    query = parse_qs(request.query)
                    snapshot = query.get("snapshot", [""])[0]
                    if filename == "citation.html":
                        return self.citation_pdf(match[1], snapshot, query.get("at", [""])[0], redirect=True)
                    if filename == "article.pdf" and "citation" in query:
                        return self.citation_pdf(match[1], snapshot, query["citation"][0])
                    stored = "profile.md" if filename == "profile.html" else filename
                    data = self.store.paper_file(match[1], stored, snapshot)
                    if data is not None:
                        if filename == "profile.html":
                            return self.respond(200, render_markdown_preview(data, snapshot), "text/html; charset=utf-8")
                        mime = {"article.pdf": "application/pdf", "profile.md": "text/markdown; charset=utf-8",
                                "text.txt": "text/plain; charset=utf-8", "manuscript.html": "text/html; charset=utf-8",
                                "manuscript.tex": "application/x-tex"}[filename]
                        return self.respond(200, data, mime, filename if filename in {"profile.md", "manuscript.tex"} else None)
            except FileNotFoundError:
                return self.respond(404, {"error": "Файл материала отсутствует."})
            except ValueError as error:
                return self.respond(409, {"error": str(error)})
            except (OSError, MaterialError, KeyError, TypeError):
                return self.respond(500, {"error": "Не удалось прочитать сохранённый профиль."})
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(/report)?", path)
        if match:
            job = self.store.job(match[1])
            if job and not match[2]:
                return self.respond(200, job)
            if job and job["report"]:
                return self.respond(200, job["report"], "text/markdown; charset=utf-8", "review-" + job["id"][:8] + ".md")
        self.respond(404, {"error": "Не найдено."})

    def do_POST(self):
        if not self.allowed():
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(415, {"error": "Ожидается JSON."})
        try:
            path = urlparse(self.path).path
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or (path != "/api/jobs" and size > 22 * 1024 * 1024):
                return self.respond(413, {"error": "Размер запроса превышен или запрос пуст."})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект JSON.")
            if path == "/api/settings":
                return self.respond(200, self.store.save_settings(data))
            if path == "/api/jobs":
                return self.respond(201, self.store.create(data))
            if path == "/api/models/refresh":
                deadline = time.monotonic() + 9
                def catalog_checkpoint():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ServiceError("Истекло время получения списка моделей.")
                    return remaining
                self.server.worker.catalog.refresh(Http(catalog_checkpoint))
                return self.respond(200, self.server.worker.catalog.snapshot())
            match = re.fullmatch(r"/api/papers/([a-f0-9]{32})/(refresh|analogs|reanalyze|references)", path)
            if match:
                job = (self.store.refresh(match[1]) if match[2] == "refresh" else
                       self.store.reanalyze(match[1], data) if match[2] == "reanalyze" else
                       self.store.repeat_references(match[1]) if match[2] == "references" else self.store.repeat_analogs(match[1]))
                if job:
                    return self.respond(201, job)
                return self.respond(404, {"error": "Статья не найдена."})
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/delete", path)
            if match:
                if self.store.delete_job(match[1]):
                    return self.respond(200, {"deleted": True})
                return self.respond(404, {"error": "Задание не найдено."})
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", path)
            if match:
                if not self.store.job(match[1]):
                    return self.respond(404, {"error": "Задание не найдено."})
                self.store.change(match[1], "cancelled", "Остановлено пользователем")
                return self.respond(200, self.store.job(match[1]))
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/retry", path)
            if match:
                job = self.store.retry(match[1], data)
                return self.respond(201, job) if job else self.respond(404, {"error": "Задание не найдено."})
            self.respond(404, {"error": "Не найдено."})
        except (ValueError, UnicodeError) as error:
            self.respond(400, {"error": str(error)})
        except Exception:
            self.respond(500, {"error": "Не удалось сохранить данные. Проверьте доступ к папке данных и свободное место."})


class LocalServer(ThreadingHTTPServer):
    # Unix can reopen a listener while its closed connections are in TIME_WAIT.
    allow_reuse_address = os.name == "posix"
    allow_reuse_port = False
    block_on_close = True
    daemon_threads = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--materials-dir", type=Path)
    args = parser.parse_args()
    # Bind before recovery: a second instance must not interrupt the first one's jobs.
    try:
        server = LocalServer((args.bind, args.port), Handler)
    except OSError:
        raise SystemExit(f"Port {args.port} is unavailable. Is Research Agent already running?")
    try:
        with exclusive_data(args.data_dir):
            materials_dir = args.materials_dir
            if materials_dir is None:
                materials_dir = ROOT / "Materials" if args.data_dir.resolve() == ROOT / "data" else args.data_dir / "Materials"
            serve(server, args.data_dir, materials_dir)
    except AlreadyRunning as error:
        server.server_close()
        raise SystemExit(str(error)) from None


def serve(server, data_dir, materials_dir):
    server.store = Store(data_dir, materials_dir)
    server.worker = Worker(server.store, os.environ.get("OPENAI_API_KEY", "").strip())
    server.worker.thread.start()
    print(f"Research Agent {VERSION}: http://127.0.0.1:{server.server_port}\nData: {data_dir}\n"
          f"Materials: {materials_dir}\nPress Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.worker.stop.set()
        server.worker.thread.join(timeout=2)
        server.server_close()
        with server.store.lock, server.store.db:
            pending = server.store.db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
            for item in pending:
                server.store.change(item["id"], "interrupted", "Сервер остановлен; автоматический повтор отключён")
        server.store.db.close()


if __name__ == "__main__":
    main()
