"""Durable stages of one paper analysis; optional work cannot discard a published core."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from manuscripts import select_manuscript
from materials import EXTRACTION_VERSION, MaterialError, _reference_item, document_text, extract_pdf, extraction_signature, split_sections
from publications import Http, Model, REFERENCE_SCHEMA, ServiceError
from research import (ANALOGS_SKIPPED, LEGACY_SEARCH_NOTES, _bibliography_records, _enrich_references,
                      REFERENCE_INSTRUCTIONS, apply_reference_hints, reference_inputs, resolve_reference_titles,
                      _ranked_references, prepare_profile, search_analogs)

STEP_NAMES = {"material": "Получение и извлечение текста", "core": "Основной профиль",
              "references": "Уточнение библиографии", "analogs": "Поиск аналогов", "manuscript": "Исходник рукописи"}


class JobStopped(RuntimeError):
    pass


class JobExpired(RuntimeError):
    pass


class StepExpired(RuntimeError):
    pass


class Pipeline:
    def __init__(self, worker, job):
        self.worker, self.store, self.job = worker, worker.store, job
        self.job_id = job["id"]
        self.deadline = time.monotonic() + job["settings"]["time_limit_seconds"]
        self.reserve = min(3, job["settings"]["time_limit_seconds"] * .05)
        self.http = Http(self.checkpoint)
        self.optional_http = Http(self.optional_checkpoint)
        self.profile = None
        self.new_core = False
        self.resume_steps = {}
        self.raw = self.document = None
        self.model = Model(self.http, worker.api_key, job["settings"],
                           lambda: self.store.record_api(self.job_id, attempt_model=job["settings"]["model"]),
                           lambda usage: self.store.record_api(self.job_id, usage=usage) if not worker.stop.is_set() else None)

    def alive(self):
        if self.worker.stop.is_set() or not self.store.active(self.job_id):
            raise JobStopped()

    def checkpoint(self):
        self.alive()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise JobExpired()
        return remaining

    def optional_checkpoint(self):
        self.alive()
        remaining = self.deadline - time.monotonic() - self.reserve
        if remaining <= 0:
            raise StepExpired()
        return remaining

    def advance(self, message, progress):
        self.checkpoint()
        if not self.store.change(self.job_id, "running", message, progress):
            raise JobStopped()

    def usage(self):
        return self.store.job(self.job_id)["api_usage"]

    def step(self, key, status, message="", code=""):
        self.store.step(self.job_id, key, status, message, code)

    def saved(self, current):
        raw = self.store.materials.read(self.job["source"], "article.pdf")
        digest = hashlib.sha256(raw).hexdigest()
        if current["article"].get("sha256") and digest != current["article"]["sha256"]:
            raise MaterialError("Сохранённый PDF не соответствует профилю. Загрузите файл заново.")
        if self.job["source_sha256"] and digest != self.job["source_sha256"]:
            raise MaterialError("Сохранённый PDF изменился. Запустите новый разбор.")
        document = None
        try:
            cache = json.loads(self.store.materials.read(self.job["source"], "document.json"))
            if (cache.get("sha256") == digest and cache.get("version") == EXTRACTION_VERSION
                    and cache.get("parser") == extraction_signature()):
                document = cache["document"]
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            pass
        article = copy.deepcopy(current["article"])
        article["sha256"] = digest
        return {"pdf": raw, "article": article, "document": document}

    def save_material(self, raw, article, document):
        self.checkpoint()
        self.store.save_checkpoint(self.job, raw, article, document)
        if document is not None:
            self.document = document
            self.step("material", "completed")

    def publish(self, manuscript=None):
        # Publishing is local and has a reserved budget. Always honor explicit cancellation.
        self.alive()
        profile = self.profile
        profile.update(job_id=self.job_id, revision=uuid.uuid4().hex, steps=self.store.job(self.job_id)["steps"])
        if self.new_core:
            profile["usage"] = self.usage()
        elif self.job["operation"] in ("analogs", "references") or self.job["parent_job_id"]:
            profile["continuation_usage"] = self.usage()
        with self.store.lock:
            self.alive()
            self.store.materials.publish(self.job["source"], self.raw, document_text(self.document),
                                         profile, manuscript, document=self.document)
            with self.store.db:
                self.store.db.execute("UPDATE papers SET title=? WHERE id=?", (profile["article"]["title"], self.job["paper_id"]))
                self.store.db.execute("UPDATE jobs SET result_revision=?,result_status=?,source_sha256=?,updated=? WHERE id=?",
                                      (profile["revision"], profile["result_status"], profile["article"]["sha256"],
                                       datetime.now(timezone.utc).isoformat(), self.job_id))

    def optional(self, key, action, progress):
        self.alive()
        durable = self.resume_steps
        if self.job["parent_job_id"] and durable.get(key, {}).get("status") in ("completed", "not_applicable"):
            self.step(key, durable[key]["status"], durable[key].get("message", ""))
            return
        if self.job["operation"] in ("analogs", "references") and key != self.job["operation"]:
            self.step(key, "not_applicable")
            return
        before = copy.deepcopy(self.profile)
        self.step(key, "running")
        self.store.change(self.job_id, "running", STEP_NAMES[key], progress)
        try:
            self.optional_checkpoint()
            message = action()
            self.optional_checkpoint()
        except StepExpired:
            if key != "references":
                self.profile = before
            self.step(key, "skipped", "Недостаточно времени. Результат предыдущих этапов сохранён.", "time_limit")
        except (ServiceError, MaterialError) as error:
            if key != "references":
                self.profile = before
            self.step(key, "failed", str(error), "service_error")
        else:
            if message is not False:
                self.step(key, "completed", message or "")
        self.publish()

    def references(self):
        content, article = self.profile["content"], self.profile["article"]
        sources = []
        parts = split_sections(self.document["pages"])
        inputs = reference_inputs(content["references"], parts)
        if self.job["operation"] == "references" and inputs:
            model = Model(self.optional_http, self.worker.api_key, self.job["settings"], self.model.start_call, self.model.record_usage)
            try:
                result = model.ask("reference_titles", REFERENCE_SCHEMA, REFERENCE_INSTRUCTIONS, {"references": inputs})
                apply_reference_hints(content["references"], inputs, result["reference_hints"])
            except ServiceError as error:
                self.profile["reference_search_note"] = "Уточнение моделью не завершено: " + str(error)
            self.publish()
        failures = resolve_reference_titles(self.worker.publications, self.optional_http, content["references"], self.publish)
        if failures:
            raise ServiceError("; ".join(dict.fromkeys(failures)))
        records = _bibliography_records(self.worker.publications, self.optional_http,
                                       _ranked_references(content["references"], parts, self.profile["search_terms"]),
                                       sources, article["doi"])
        _enrich_references(content["references"], records)
        failures = [source["message"] for source in sources if source["status"] != "ok"]
        if failures:
            raise ServiceError("; ".join(failures))
        missing = sum(_reference_item(entry)["missing_title"] for entry in content["references"])
        if missing:
            self.step("references", "failed", f"Для {missing} записей название пока не установлено. Можно повторить уточнение.", "missing_titles")
            return False

    def analogs(self):
        if self.job["settings"]["max_api_calls"] <= self.usage()["calls"]:
            self.step("analogs", "skipped", ANALOGS_SKIPPED, "call_limit")
            return False
        model = Model(self.optional_http, self.worker.api_key, self.job["settings"], self.model.start_call, self.model.record_usage)
        content = self.profile["content"]
        search = search_analogs(self.worker.publications, self.optional_http, model, content, self.profile["article"],
                               split_sections(self.document["pages"]), self.document["links"],
                               self.profile["search_terms"], self.job["settings"]["language"])
        search["usage"] = self.usage()
        self.profile["analog_search"] = search
        self.profile["coverage"]["notes"] = [note for note in self.profile["coverage"]["notes"]
                                                if not note.startswith(LEGACY_SEARCH_NOTES)]
        if search["model_error"] or search["sources"] and all(source["status"] != "ok" for source in search["sources"]):
            self.step("analogs", "failed", search["model_error"] or "Источники поиска недоступны.", "service_error")
            return False

    def manuscript(self):
        article = self.profile["article"]
        if self.job["kind"] != "arxiv":
            self.step("manuscript", "not_applicable")
            return False
        if article.get("manuscript"):
            return "Исходник уже сохранён."
        source = select_manuscript(self.worker.publications.manuscript(self.optional_http, article))
        if source is None:
            self.step("manuscript", "not_applicable", "Основной файл в архиве не определён.")
            return False
        name, raw = source
        article["manuscript"] = {"name": name, "file": "manuscript" + Path(name).suffix.lower()}
        self.profile["coverage"]["notes"] = [note for note in self.profile["coverage"]["notes"]
                                                if not note.startswith("Исходник рукописи · ")]
        self.step("manuscript", "completed")
        self.publish(raw)
        return "Исходник сохранён."

    def run(self):
        self.advance("Подготовка задания", 1)
        job = self.job
        with self.store.lock:
            current = self.store.materials.profile(job["source"])
            if job["input_revision"] and (not current or current.get("revision", current["job_id"]) != job["input_revision"]):
                raise MaterialError("Профиль изменился после постановки задания. Запустите новый разбор.")
        for key in STEP_NAMES:
            self.step(key, "pending")
        cached = None
        if job["parent_job_id"]:
            cached = self.store.checkpoint(job["parent_job_id"], job["source"])
            if cached and job["source_sha256"] and hashlib.sha256(cached["pdf"]).hexdigest() != job["source_sha256"]:
                raise MaterialError("Материалы предыдущего задания изменились. Запустите новый разбор.")
        resume_core = bool(job["parent_job_id"] and current and current["content"]["summary"]
                           and current["job_id"] == job["parent_job_id"]
                           and current.get("steps", {}).get("core", {}).get("status") == "completed"
                           and (not cached or cached["article"].get("sha256") == current["article"].get("sha256")))
        if job["operation"] in ("analogs", "references") or resume_core:
            if not current or (not current["content"]["summary"] and job["operation"] != "references"):
                raise MaterialError("Сохранённый профиль с обзором недоступен.")
            self.profile = copy.deepcopy(current)
            if job["parent_job_id"]:
                self.resume_steps = copy.deepcopy(current.get("steps", {}))
            cached = self.saved(current)
            self.profile["article"] = cached["article"]
            self.raw = cached["pdf"]
            self.document = cached["document"] or extract_pdf(self.raw, self.checkpoint)
            self.save_material(self.raw, self.profile["article"], self.document)
            self.step("core", "completed" if current["content"]["summary"] else "not_applicable")
        else:
            if not cached and job["operation"] == "reanalyze":
                if not current:
                    raise MaterialError("Сохранённый PDF недоступен.")
                cached = self.saved(current)
            self.step("material", "running")
            self.step("core", "running")
            bundle = prepare_profile(job, self.worker.publications, self.http, self.model, self.advance,
                                     lambda version: self.store.set_version(self.job_id, version),
                                     lambda: self.store.uploaded_pdf(job["source"]), current, self.usage,
                                     cached=cached, save_material=self.save_material)
            if bundle is None:
                self.profile = copy.deepcopy(current)
                cached = self.saved(current)
                self.profile["article"] = cached["article"]
                self.raw = cached["pdf"]
                self.document = cached["document"] or extract_pdf(self.raw, self.checkpoint)
                self.save_material(self.raw, self.profile["article"], self.document)
                for key in ("core", "references", "analogs"):
                    self.step(key, "completed", "Сохранённая версия актуальна.")
                if self.profile["article"].get("manuscript"):
                    self.step("manuscript", "completed", "Исходник уже сохранён.")
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE jobs SET result_revision=?,result_status='ready' WHERE id=?",
                                              (current.get("revision", current["job_id"]), self.job_id))
                else:
                    self.optional("manuscript", self.manuscript, 90)
                self.store.change(self.job_id, "completed", "Сохранённая версия актуальна", 100)
                if not self.store.job(self.job_id)["retry"]["allowed"]:
                    self.store.discard_checkpoint(self.job_id)
                return
            self.raw, _, self.profile = bundle
            self.new_core = True
            if current and current["article"].get("sha256") == self.profile["article"]["sha256"] and current["article"].get("manuscript"):
                self.profile["article"]["manuscript"] = current["article"]["manuscript"]
            core_error = self.profile["processing_error"]
            if not self.profile["content"]["summary"]:
                self.step("core", "failed" if core_error else "blocked", core_error or "Нужен PDF с текстовым слоем.",
                          "model_error" if core_error else "no_text")
                self.publish()
                self.store.change(self.job_id, "failed" if core_error else "completed", "Сохранён частичный результат", 100, error=core_error or None)
                return
            self.step("core", "completed")
            self.publish()
        self.optional("references", self.references, 65)
        self.optional("analogs", self.analogs, 75)
        self.optional("manuscript", self.manuscript, 92)
        self.publish()
        pending = any(value["status"] not in ("completed", "not_applicable") for value in self.store.job(self.job_id)["steps"].values())
        self.store.change(self.job_id, "completed", "Основной профиль готов; есть незавершённые этапы" if pending else "Профиль сохранён", 100)
        if not pending:
            self.store.discard_checkpoint(self.job_id)
            if self.job["parent_job_id"]:
                self.store.discard_checkpoint(self.job["parent_job_id"])
