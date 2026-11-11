"""Bounded requests to arXiv, Crossref, and the OpenAI Responses API."""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from manuscripts import SOURCE_LIMIT
from materials import PDF_LIMIT, arxiv_id, clean_text, compact, web_link
from version import VERSION

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_SITE = "https://arxiv.org"
CROSSREF = "https://api.crossref.org/works"
RESPONSES = "https://api.openai.com/v1/responses"
ATOM = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
DOI = re.compile(r"10\.\d{4,9}/[a-z0-9._;()/:+%\-]+", re.I | re.ASCII)
READ_LIMIT = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 220_000
ARXIV_QUERY_INTERVAL = 3.05


class ServiceError(RuntimeError):
    def __init__(self, message, status=None, *, retry_after=None, deferred=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.deferred = deferred


def retry_delay(value):
    if not value:
        return None
    try:
        if value.isascii() and value.isdecimal():
            return int(value)
        date = parsedate_to_datetime(value)
        return max(0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target, origin = urlsplit(newurl), urlsplit(request.full_url)
        if target.scheme != "https" or target.netloc != origin.netloc:
            raise HTTPError(request.full_url, code, "Unsafe redirect", headers, fp)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.opener = build_opener(SameHostRedirect())

    def read(self, url, service, *, limit=READ_LIMIT, timeout=15, data=None, headers=None):
        request = Request(url, data=data, headers={"User-Agent": f"ResearchAgent/{VERSION} (local scholarly reading tool)",
                                                   **(headers or {})})
        try:
            with self.opener.open(request, timeout=min(timeout, self.checkpoint())) as response:
                result = bytearray()
                while True:
                    self.checkpoint()
                    chunk = response.read1(min(65_536, limit + 1 - len(result)))
                    if not chunk:
                        return bytes(result)
                    result.extend(chunk)
                    if len(result) > limit:
                        raise ServiceError(f"{service}: ответ превышает допустимый размер.")
        except HTTPError as exc:
            exc.close()
            self.checkpoint()
            if service == "OpenAI":
                hint = {401: " Проверьте ключ API.", 403: " Проверьте права доступа.",
                        429: " Достигнут лимит запросов или средств."}.get(exc.code, "")
            else:
                hint = " Сервис ограничил число запросов." if exc.code == 429 else ""
            delay = retry_delay(exc.headers.get("Retry-After")) if exc.headers and exc.code == 429 else None
            raise ServiceError(f"{service}: HTTP {exc.code}.{hint}", status=exc.code, retry_after=delay) from None
        except (URLError, OSError, HTTPException):
            self.checkpoint()
            raise ServiceError(f"{service}: не удалось получить ответ. Проверьте сеть и повторите вручную.") from None

    def json(self, url, service, **kwargs):
        raw = self.read(url, service, **kwargs)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ServiceError(f"{service}: ответ не удалось прочитать как JSON.") from None
        if not isinstance(value, dict):
            raise ServiceError(f"{service}: неожиданный формат ответа.")
        return value


def doi(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", compact(value), flags=re.I)
    value = value.rstrip(".,;").lower()
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value if len(value) <= 250 and DOI.fullmatch(value) else ""


def doi_url(value):
    return "https://doi.org/" + quote(value, safe="/")


def arxiv_url(base, version="", kind="abs"):
    return f"{ARXIV_SITE}/{kind}/{base}{version}"


class AbstractMeta(HTMLParser):
    """Read only citation metadata from the official unversioned abs page."""
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attributes):
        if tag == "meta":
            attrs = dict(attributes)
            key = attrs.get("name") or attrs.get("property")
            content = attrs.get("content")
            if key and content:
                self.values.setdefault(key.lower(), []).append(content)

    def first(self, *keys):
        return next((self.values[key][0] for key in keys if key in self.values), "")


def parse_atom(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ServiceError("arXiv: не удалось прочитать метаданные.") from None
    records = []
    for entry in root.findall("atom:entry", ATOM):
        def field(name):
            return compact(entry.findtext(name, default="", namespaces=ATOM))

        try:
            base, version = arxiv_id(field("atom:id"))
        except ValueError:
            continue
        if not version or not field("atom:title"):
            continue
        records.append({
            "arxiv_id": base, "version": version, "title": field("atom:title")[:500],
            "authors": [compact(node.text, 200) for node in entry.findall("atom:author/atom:name", ATOM)][:50],
            "published": field("atom:published")[:10], "updated": field("atom:updated")[:10],
            "abstract": clean_text(entry.findtext("atom:summary", default="", namespaces=ATOM)), "doi": doi(field("arxiv:doi")),
            "url": arxiv_url(base, version), "source": "arXiv",
        })
    return records


def parse_arxiv_page(raw, base):
    meta = AbstractMeta()
    meta.feed(raw.decode("utf-8", errors="replace"))
    try:
        found, version = arxiv_id(meta.first("og:url"))
    except ValueError:
        raise ServiceError("arXiv: страница не сообщила точную версию статьи.") from None
    title = compact(meta.first("citation_title", "og:title"), 500)
    if found != base or not version or not title:
        raise ServiceError("arXiv: не удалось подтвердить последнюю версию статьи.")
    return {"arxiv_id": base, "version": version, "title": title,
            "authors": [compact(author, 200) for author in meta.values.get("citation_author", [])][:50],
            "published": compact(meta.first("citation_date", "dc.date"), 32).replace("/", "-"),
            "updated": "", "abstract": clean_text(meta.first("og:description", "dc.description")),
            "doi": doi(meta.first("citation_doi")), "url": arxiv_url(base, version), "source": "arXiv"}


def crossref_record(record):
    if not isinstance(record, dict):
        return None
    identifier = doi(record.get("DOI"))
    titles = record.get("title") or []
    title = compact(titles[0], 500) if isinstance(titles, list) else ""
    if not identifier or not title:
        return None
    dates = (record.get("published") or {}).get("date-parts") or []
    date = "-".join(f"{part:02d}" for part in dates[0] if type(part) is int) if dates else ""
    journals = record.get("container-title") or []
    resource = record.get("resource") or {}
    primary = resource.get("primary", {}) if isinstance(resource, dict) else {}
    publisher_url = web_link(primary.get("URL")) if isinstance(primary, dict) else ""
    return {"arxiv_id": "", "version": "", "doi": identifier, "title": title, "published": date,
            "authors": [compact(f'{a.get("given", "")} {a.get("family", "")}', 200)
                        for a in (record.get("author") or [])[:50] if isinstance(a, dict)],
            "abstract": compact(html.unescape(re.sub(r"<[^>]*>", " ", str(record.get("abstract") or ""))), 6000),
            "url": doi_url(identifier), "publisher_url": publisher_url, "source": "Crossref",
            "publication": {"journal": compact(journals[0], 300) if isinstance(journals, list) and journals else "",
                            "volume": compact(record.get("volume"), 30), "issue": compact(record.get("issue"), 30),
                            "pages": compact(record.get("page") or record.get("article-number"), 80), "year": date[:4]}}


def _citation_words(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W\d_]+|\d+", text))


def _matches_citation(text, entry, work):
    authors = entry.get("author") or []
    family = _citation_words(authors[0].get("family")) if authors and isinstance(authors[0], dict) else ""
    if not family or f" {family} " not in text:
        return False
    title = _citation_words(work["title"])
    if len(title.split()) >= 3 and f" {title} " in text:
        return True
    volume = _citation_words(entry.get("volume"))
    page = _citation_words(re.split(r"[-–—]", entry.get("page") or entry.get("article-number") or "")[0])
    year = work["published"][:4]
    if not (volume and page and year):
        return False
    for key in ("container-title", "short-container-title"):
        for name in entry.get(key) or []:
            journal = _citation_words(name)
            if journal and re.search(rf" {re.escape(journal)} {re.escape(volume)}"
                                     rf"(?: \d{{1,3}})? {re.escape(page)} {re.escape(year)} ", text):
                return True
    return False


class Publications:
    def __init__(self):
        self.last_arxiv_query = None
        self.arxiv_retry_at = 0

    def atom(self, http, **params):
        remaining = self.arxiv_retry_at - time.monotonic()
        if remaining > 0:
            raise ServiceError("arXiv: запрос отложен после HTTP 429.", status=429,
                               retry_after=remaining, deferred=True)
        if self.last_arxiv_query is not None:
            while (delay := self.last_arxiv_query + ARXIV_QUERY_INTERVAL - time.monotonic()) > 0:
                time.sleep(min(delay, .15, http.checkpoint()))
        self.last_arxiv_query = time.monotonic()
        try:
            return parse_atom(http.read(ARXIV_API + "?" + urlencode(params), "arXiv", timeout=8))
        except ServiceError as error:
            if error.status == 429:
                self.arxiv_retry_at = time.monotonic() + (60 if error.retry_after is None else error.retry_after)
            raise

    def latest(self, http, base):
        try:
            records = self.atom(http, id_list=base, max_results=1)
            exact = next((record for record in records if record["arxiv_id"] == base), None)
            if exact:
                return exact
        except ServiceError:
            pass
        return self.arxiv_page(http, base)

    def arxiv_page(self, http, base):
        return parse_arxiv_page(http.read(arxiv_url(base), "Страница arXiv", timeout=8), base)

    def pdf(self, http, record):
        identifier, version = arxiv_id(record["arxiv_id"] + record["version"])
        if not version:
            raise ServiceError("arXiv: для загрузки нужна подтверждённая версия PDF.")
        return http.read(arxiv_url(identifier, version, "pdf"), "PDF arXiv", limit=PDF_LIMIT, timeout=20)

    def manuscript(self, http, record):
        identifier, version = record["arxiv_id"], record["version"]
        if not version:
            raise ServiceError("arXiv: для загрузки исходника нужна подтверждённая версия.")
        return http.read(arxiv_url(identifier, version, "src"), "Исходник arXiv", limit=SOURCE_LIMIT, timeout=6)

    def arxiv_search(self, http, terms):
        query = " OR ".join(f'all:"{term}"' for term in terms)
        return self.atom(http, search_query=query, max_results=8, sortBy="relevance")

    def crossref_search(self, http, terms):
        fields = "type,resource,container-title,volume,issue,page,article-number"
        return [work for entry in self._crossref_items(http, " ".join(terms), 8, fields)
                if isinstance(entry, dict) and entry.get("type") in
                {"journal-article", "proceedings-article", "posted-content", "book-chapter"}
                and (work := crossref_record(entry))]

    def _crossref_items(self, http, text, rows, fields):
        params = {"query.bibliographic": text, "rows": rows,
                  "select": "DOI,title,author,published,abstract," + fields}
        payload = http.json(CROSSREF + "?" + urlencode(params), "Поиск Crossref", timeout=8)
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise ServiceError("Crossref: неожиданный формат результатов поиска.")
        return message["items"]

    def crossref_citation(self, http, citation):
        fields = "container-title,short-container-title,volume,issue,page,article-number,resource"
        text = " " + _citation_words(citation) + " "
        matches = {work["doi"]: work for entry in self._crossref_items(http, compact(citation, 600), 3, fields)
                   if (work := crossref_record(entry)) and _matches_citation(text, entry, work)}
        return next(iter(matches.values())) if len(matches) == 1 else None

    def crossref_reference(self, http, identifier):
        payload = http.json(CROSSREF + "/" + quote(identifier, safe=""), "Ссылка Crossref", timeout=6)
        work = crossref_record(payload.get("message"))
        return work if work and work["doi"] == identifier else None


def object_schema(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
KIND = {"type": "string", "enum": ["measurement", "calculation", "interpretation", "other"]}


def array(items):
    return {"type": "array", "items": items}


PROFILE_SCHEMA = object_schema(
    summary=array(STRING),
    answers=array(object_schema(question_id=STRING, answer=STRING,
                                status={"type": "string", "enum": ["answered", "insufficient"]})),
    glossary=array(object_schema(term=STRING, explanation=STRING, role=STRING)),
    keywords=array(object_schema(term=STRING, importance={"type": "integer"}, role=STRING)),
    topics=array(STRING),
    claims=array(object_schema(statement=STRING, kind=KIND, snippet=STRING, evidence=STRING, locator=STRING,
                                check=STRING, explanation=STRING)),
    search_terms=array(STRING),
)
ANALOG_SCHEMA = object_schema(
    analogs=array(object_schema(candidate_id=STRING, similarity=STRING, difference=STRING,
                                 snippet=STRING)),
)


class Model:
    def __init__(self, http, key, settings, start_call, record_usage):
        self.http = http
        self.key = key
        self.settings = settings
        self.start_call = start_call
        self.record_usage = record_usage

    def ask(self, name, schema, instructions, inputs):
        if not self.key:
            raise ServiceError("Для подготовки профиля задайте переменную OPENAI_API_KEY и перезапустите сервер.")
        payload = {
            "model": self.settings["model"], "store": False,
            "instructions": instructions, "input": json.dumps(inputs, ensure_ascii=False),
            "max_output_tokens": self.settings["max_output_tokens"],
            "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
        }
        if payload["model"] == "gpt-6-luna":
            payload["reasoning"] = {"effort": "low"}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_PROMPT_BYTES:
            raise ServiceError("Запрос к модели превышает установленный предел входных данных.")
        self.http.checkpoint()
        self.start_call()
        response = self.http.json(RESPONSES, "OpenAI", data=raw, timeout=75,
                                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        self.record_usage(response.get("usage"))
        if response.get("status") != "completed":
            if (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
                raise ServiceError("Ответ модели достиг лимита выходных токенов. Можно изменить лимит и запустить заново.")
            raise ServiceError("Модель не завершила ответ.")
        pieces = []
        for output in response.get("output", []):
            if output.get("type") != "message":
                continue
            for part in output.get("content", []):
                if part.get("type") == "refusal":
                    raise ServiceError("Модель отказалась формировать результат.")
                if part.get("type") == "output_text":
                    pieces.append(part.get("text", ""))
        try:
            value = json.loads("".join(pieces))
        except (ValueError, TypeError):
            raise ServiceError("Модель вернула нечитаемые структурированные данные.") from None
        if not isinstance(value, dict):
            raise ServiceError("Модель вернула неожиданный формат профиля.")
        return value
