"""Extract PDF text and maintain the current, portable profile of each paper."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from manuscripts import SOURCE_FILES
from bibliographic import reference_text
from math_markup import MATH_HEADER, escape_markdown, markdown_math
from pdf_text import pdfium_document, restore_word_spaces
from version import VERSION

PDF_LIMIT = 15 * 1024 * 1024
PAGE_LIMIT = 200
TEXT_LIMIT = 1_500_000
MANAGED_FILES = ("article.pdf", "text.txt", "profile.md", "profile.json", "document.json", *SOURCE_FILES)
PROFILE_VERSION = 10
EXTRACTION_VERSION = 2
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(v[1-9]\d{0,5})?", re.I | re.ASCII)
BRACKET_REFERENCE = re.compile(r"^[ \t]*\[(\d{1,4})\][ \t]+(?=\S)", re.M)
PAREN_REFERENCE = re.compile(r"^[ \t]*(\d{1,4})\)[ \t]+(?=\S)", re.M)
NUMBERED_REFERENCE = re.compile(r"^[ \t]*(?:\[(\d{1,4})\]|(\d{1,4})[.)])[ \t]+(?=\S)", re.M)
REFERENCE_SIGNAL = re.compile(r"\b(?:18|19|20)\d{2}\b|arxiv\s*:|10\.\d{4,9}/", re.I)
SUPPLEMENT_HEADING = re.compile(r"(?:supplement(?:ary|al) (?:materials?|information|appendix|data)|дополнительные материалы)(?: for\b.*)?$")
LETTERED_APPENDIX_HEADING = re.compile(r"^A\s+[A-Z][^\s,.;]*\s+[^,.;]{1,140}$")
BIBLIOGRAPHIC_YEAR = r"(?:18|19|20)\d{2}[a-z]?"
AUTHOR_YEAR_START = re.compile(rf"^[^\W\d_][^,\d]{{0,59}},\s+(?:[^\W\d_]\.\s*)+[^()]{{0,220}}\({BIBLIOGRAPHIC_YEAR}\)\.")
RMP_SURNAME = r"^(?P<surname>[^\W\d_][^,.\d()]{0,59}),\s+"
RMP_INITIALS = r"(?:[^\W\d_]\.\s*(?:-\s*[^\W\d_]\.\s*)?)+"
RMP_REFERENCE_LEAD = re.compile(rf"{RMP_SURNAME}{RMP_INITIALS},")
RMP_REFERENCE_START = re.compile(rf"{RMP_SURNAME}(?:{RMP_INITIALS}|[A-Z][^,.\d()]{{1,50}}),"
                                 rf"\s*(?:(?P<coauthors>[^\d]{{1,1000}}?),\s*)?"
                                 rf"(?P<year>{BIBLIOGRAPHIC_YEAR}),\s*")
RMP_TITLE = re.compile(r'[“"]([^”"]+)[”"],?\s*')
CITATION_ANCHOR = r"[0-9]{1,6}-[0-9]{1,6}-[0-9]{1,6}"
MATERIAL_ID = re.compile(r"(?:arxiv-[a-z0-9.-]+|pdf-[0-9a-f]{64})")
REFERENCE_YEAR_END = re.compile(rf"\b{BIBLIOGRAPHIC_YEAR}(?:[)\]]\.?|\.)$")
DISPLAY_YEAR = re.compile(rf"\((?P<parenthesized>{BIBLIOGRAPHIC_YEAR})\)|(?<![\w.])(?P<trailing>{BIBLIOGRAPHIC_YEAR})(?=\s*[)\]]?\.?\s*$)")
ABSTRACT_HEADING = re.compile(r"^(?:abstract|аннотация)\s*[:.—–-]\s*(\S.*)$", re.I)
ABSTRACT_END = re.compile(r"^(?:key\s?words|index terms|pacs|ключевые слова)\s*[:.—–-]", re.I)
REFERENCE_LINK_LINE = re.compile(r"^(?:(?:url|doi)\s*:?\s*)?(?:https?://|10\.\d{4,9}/)|^arxiv\s*:", re.I)
REFERENCE_AUTHOR = re.compile(r"^(?:and\s+)?(?:[A-ZА-ЯЁ]\.\s*(?:-\s*[A-ZА-ЯЁ]\.\s*)?)+[^\W\d_]\D*$")
AUTHOR_TITLE_SEPARATOR = re.compile(r"(?<!\b[^\W\d_])\.\s*(?=\S)")
SURNAME_INITIALS = r"[^,.;:\d]{1,85}?\s+[A-Z]{1,4}(?:\s+[A-Z]){0,3}"
VANCOUVER_AUTHORS = re.compile(rf"^(?P<authors>{SURNAME_INITIALS}(?:,\s*{SURNAME_INITIALS})*(?:,\s*et al)?)\.\s+")
VANCOUVER_PUBLICATION = re.compile(rf"^(?P<venue>.+?)\.\s*(?P<year>{BIBLIOGRAPHIC_YEAR});\s*(?P<coords>\S+)")
QUOTED_REFERENCE_TITLE = re.compile(r'^(?P<authors>[^“"]+?),\s*[“"](?P<title>[^”"]+)[”"]\s*[,.;]?\s*')
YEAR_BEFORE_PAGE = re.compile(rf"^.+?\d+\s*\({BIBLIOGRAPHIC_YEAR}\)\s*\d+\b")
REFERENCE_VENUE = re.compile(
    r"(?:^|,\s*)(?=(?i:in\s+|arxiv\b|doi:|https?://)|inProceedings)"
    rf"|(?:^|,\s*)(?P<journal>[^,()]{{2,100}}\d+\s*(?:\(\d+\))?\s*,\s*[A-Z]?\d[\w–—-]*\s*(?:\({BIBLIOGRAPHIC_YEAR}\)|,\s*{BIBLIOGRAPHIC_YEAR}\b)\.?)"
    r"|(?=\s*\([^()]*\b\d{4}\b\))"
)
SECTION_NAMES = {
    "abstract": "Аннотация", "аннотация": "Аннотация",
    "introduction": "Введение", "введение": "Введение", "background": "Контекст",
    "methods": "Методы", "methodology": "Методы", "materials and methods": "Методы",
    "experimental setup": "Методы", "experimental methods": "Методы", "measurement setup": "Методы",
    "experiment": "Методы", "методы": "Методы",
    "theory": "Теория", "theoretical model": "Теория", "теория": "Теория",
    "results": "Результаты", "результаты": "Результаты",
    "results and discussion": "Результаты и обсуждение",
    "discussion": "Обсуждение", "обсуждение": "Обсуждение",
    "conclusion": "Заключение", "conclusions": "Заключение", "заключение": "Заключение",
    "references": "Библиография", "bibliography": "Библиография", "литература": "Библиография",
    "список литературы": "Библиография", "acknowledgments": "Благодарности",
    "acknowledgements": "Благодарности", "appendix": "Приложение",
    "supplementary material": "Приложение", "data availability": "Данные",
    "code availability": "Код",
}


def extraction_signature():
    signature = [str(EXTRACTION_VERSION)]
    for package in ("pypdf", "pypdfium2"):
        try:
            installed = package_version(package)
        except PackageNotFoundError:
            installed = "unavailable"
        signature.extend((package, installed))
    return "/".join(signature)


class MaterialError(RuntimeError):
    pass


def clean_text(value):
    value = str(value or "").encode("utf-8", "replace").decode("utf-8")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def compact(value, limit=None):
    return " ".join(clean_text(value).split())[:limit]


def arxiv_id(value):
    """Return the canonical base ID and any explicitly supplied version."""
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("Укажите ID или ссылку arXiv.")
    value = re.sub(r"^arxiv:\s*", "", value.strip(), flags=re.I)
    if "://" in value:
        try:
            url = urlsplit(value)
        except ValueError:
            raise ValueError("Некорректная ссылка arXiv.") from None
        if url.scheme not in {"https", "http"} or url.netloc.lower() not in {"arxiv.org", "www.arxiv.org"}:
            raise ValueError("Укажите ссылку на arxiv.org.")
        match = re.fullmatch(r"/(?:abs|pdf)/(.+)/?", url.path)
        value = match[1].rstrip("/") if match else ""
    match = ARXIV_ID.fullmatch(re.sub(r"\.pdf$", "", value, flags=re.I))
    if not match:
        raise ValueError("Укажите ID вида 2409.10975 или ссылку arxiv.org/abs/…")
    return match[1].lower(), (match[2] or "").lower()


def web_link(value):
    if not isinstance(value, str) or not value or len(value) > 1500 or re.search(r"[\s<>\\]", value):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme in {"https", "http"} and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return ""


def _pdf_data(raw, *, full=False):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw), strict=False)
    encrypted = reader.is_encrypted and not reader.decrypt("")
    if encrypted:
        return {"metadata": {}, "pages": [], "page_count": None, "partial": True,
                "notes": ["PDF защищён паролем. Для извлечения нужен доступный PDF."], "links": []}
    page_count = len(reader.pages)
    try:
        metadata = reader.metadata or {}
        fields = {key: compact(metadata.get("/" + key), 1000) for key in ("Title", "Author", "Keywords", "DOI")}
    except (ValueError, TypeError, KeyError):
        fields = {}
    pages, links, notes = [], [], []
    remaining = TEXT_LIMIT
    clipped = False
    with pdfium_document(raw) as spacing_pdf:
        for index in range(page_count if full else min(page_count, PAGE_LIMIT)):
            page = reader.pages[index]
            try:
                text = clean_text(page.extract_text())
            except Exception:
                text = ""
            size = len(text) if full else min(remaining, 60_000)
            clipped |= len(text) > size
            text = restore_word_spaces(spacing_pdf, index, text[:size])
            if full:
                size = len(text)
            clipped |= len(text) > size
            pages.append(text[:size])
            remaining -= len(pages[-1])
            try:
                for annotation in list(page.get("/Annots") or [])[:200]:
                    action = annotation.get_object().get("/A")
                    url = web_link(action.get_object().get("/URI")) if action else ""
                    if url and len(links) < 250:
                        links.append({"url": url, "page": index + 1})
            except (ValueError, TypeError, AttributeError, KeyError):
                pass
            if not full and remaining <= 0:
                break
    missing = sum(not page for page in pages)
    if missing:
        notes.append(f"На {missing} из {len(pages)} просмотренных страниц текст извлечь не удалось.")
    if len(pages) < page_count or clipped:
        notes.append(f"Извлечение ограничено объёмом. Просмотрено страниц: {len(pages)} из {page_count}.")
    return {"metadata": fields, "pages": pages, "page_count": page_count,
            "partial": bool(missing or len(pages) < page_count or clipped), "notes": notes, "links": links,
            "full_extraction": full}


def extract_pdf(raw, checkpoint, *, full=False):
    """Run parsing in a child process so cancellation and the deadline can stop it."""
    checkpoint()
    if not raw.startswith(b"%PDF-") or not full and len(raw) > PDF_LIMIT:
        raise MaterialError("Источник не вернул PDF размером до 15 МБ.")
    env = {key: value for key, value in os.environ.items() if key.upper() != "OPENAI_API_KEY"}
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "extract-full" if full else "extract"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    with process:
        try:
            first = True
            while True:
                checkpoint()
                try:
                    output, _ = process.communicate(raw if first else None, timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            checkpoint()
            if process.returncode or not output:
                raise MaterialError("Не удалось прочитать структуру PDF. Проверьте файл.")
            document = json.loads(output)
            if "error" in document:
                raise MaterialError(document["error"])
            return document
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def _reference_start(lines, next_page):
    for index, line in enumerate(lines):
        pattern = BRACKET_REFERENCE if line.lstrip().startswith("[") else PAREN_REFERENCE
        marker = pattern.match(line)
        if not marker or marker[1] != "1":
            continue
        text = "\n".join(lines[index:] + next_page)
        markers = list(pattern.finditer(text))[:4]
        if [marker[1] for marker in markers[:3]] != ["1", "2", "3"]:
            continue
        ends = [marker.start() for marker in markers[1:]] + [len(text)]
        if sum(bool(REFERENCE_SIGNAL.search(text[marker.end():end]))
               for marker, end in zip(markers[:3], ends)) >= 2:
            return index
    return None


def ends_reference(line):
    return bool(REFERENCE_LINK_LINE.match(line) or (REFERENCE_YEAR_END.search(line) and not AUTHOR_YEAR_START.match(line)))


def full_author_names(text):
    """Recognize full-name author lists in bibliography text."""
    particles = {"and", "da", "de", "del", "di", "dos", "du", "la", "van", "von"}
    names = re.split(r",\s*(?:and\s+)?|\s+and\s+", text)
    for name in names:
        if name == "et al" and len(names) > 1:
            continue
        words = name.split()
        if len(words) < 2 or not all(word[0].isupper() or word in particles for word in words):
            return False
    return True


def split_sections(pages):
    """Identify headings and reference sequences while retaining the PDF pages."""
    parts = []
    section = "Основной текст"
    appended_supplement = False
    separate_supplement = False
    last_reference_line = ""
    for page, text in enumerate(pages, start=1):
        lines = []
        page_lines = text.splitlines()
        if appended_supplement:
            parts.append({"section": "Приложение", "page": page, "text": text,
                          "separate_supplement": separate_supplement})
            continue
        next_page = pages[page].splitlines() if page < len(pages) else []
        reference_start = _reference_start(page_lines, next_page) if section != "Библиография" else None
        for index, line in enumerate(page_lines):
            if appended_supplement:
                lines.append(line)
                continue
            line = line.strip()
            candidate = re.sub(r"^(?:\d+(?:\.\d+)*[.)]?|[IVX]+\.)\s+", "", line).casefold().rstrip(":")
            inline_abstract = ABSTRACT_HEADING.match(line) if section == "Основной текст" else None
            heading = "Аннотация" if inline_abstract else SECTION_NAMES.get(candidate)
            ends_abstract = section == "Аннотация" and ABSTRACT_END.match(line)
            if ends_abstract:
                heading = "Основной текст"
            supplement = SUPPLEMENT_HEADING.fullmatch(candidate)
            lettered_appendix = (section == "Библиография" and LETTERED_APPENDIX_HEADING.fullmatch(line)
                                 and ends_reference(last_reference_line) and not full_author_names(line))
            if supplement or lettered_appendix or re.fullmatch(r"appendix [a-z](?::.*)?", candidate):
                heading = "Приложение"
            if heading == "Приложение" and section == "Библиография":
                appended_supplement = True
                separate_supplement = bool(supplement)
            if index == reference_start:
                heading = "Библиография"
            if section == "Приложение" and heading != "Библиография":
                heading = None
            if heading:
                if lines:
                    parts.append({"section": section, "page": page, "text": "\n".join(lines)})
                    lines = []
                section = heading
                if inline_abstract and heading == "Аннотация":
                    line = inline_abstract[1]
                elif index != reference_start and not ends_abstract:
                    continue
            if line or section in {"Аннотация", "Библиография"} and lines and lines[-1]:
                lines.append(line)
            if section == "Библиография" and line and not line.isdecimal():
                last_reference_line = line
        if lines:
            part = {"section": section, "page": page, "text": "\n".join(lines)}
            if separate_supplement:
                part["separate_supplement"] = True
            parts.append(part)
    return parts


def main_text_pages(pages):
    """Keep the main manuscript, including its references and inline appendices."""
    for index, text in enumerate(pages):
        for match in re.finditer(r"^.*$", text, re.M):
            if SUPPLEMENT_HEADING.fullmatch(match[0].strip().casefold().rstrip(":")):
                prefix = text[:match.start()].strip()
                if prefix and not prefix.isdecimal():
                    return [*pages[:index], prefix], match[0].strip()
                return pages[:index], ""
    return pages, ""


def abstract_from_parts(parts):
    """Use the text between the abstract heading and the next recognized section."""
    for first, part in enumerate(parts):
        if part["section"] == "Аннотация":
            for last in range(first + 1, len(parts)):
                if parts[last]["section"] != "Аннотация":
                    return "\n".join(item["text"] for item in parts[first:last]).strip()
            break
    return ""


def document_text(document):
    return "\n\n".join(f"--- Страница {number} ---\n{text or '[Текст не извлечён]'}"
                       for number, text in enumerate(document["pages"], start=1)) + "\n"


def saved_pages(text):
    pages = re.split(r"^--- Страница \d+ ---\n", text, flags=re.M)[1:]
    return ["" if page == "[Текст не извлечён]" else page for page in map(str.strip, pages)]


def material_name(source):
    if source.startswith("arxiv:"):
        base, _ = arxiv_id(source)
        return "arxiv-" + base.replace("/", "-")
    if re.fullmatch(r"upload:[0-9a-f]{64}", source):
        return "pdf-" + source.removeprefix("upload:")
    raise ValueError("Неизвестный источник материала.")


def _material_identity(name):
    identity = name.rsplit(" [", 1)[1][:-1] if name.endswith("]") and " [" in name else name
    return identity if MATERIAL_ID.fullmatch(identity) else None


def _folder_label(value, limit):
    text = unicodedata.normalize("NFC", compact(re.sub(r'[<>:"/\\|?*]', " ", clean_text(value)))).strip(" .")
    encoded = text.encode("utf-16-le")
    if len(encoded) > 2 * limit:
        text = encoded[:2 * (limit - 1)].decode("utf-16-le", errors="ignore").rstrip(" .") + "…"
    return text


class MaterialLibrary:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._folders = {}
        for folder in self.root.iterdir():
            identity = _material_identity(folder.name)
            if identity and folder.is_dir():
                if identity in self._folders:
                    raise MaterialError(f"Найдено несколько папок для материала {identity}.")
                self._folders[identity] = self._direct(folder)
                self._recover(folder)
        self._rename_saved_folders()

    @staticmethod
    def _direct(path):
        if path.resolve() != path:
            raise MaterialError("Папка материала содержит перенаправление файловой системы.")
        return path

    def folder(self, source):
        identity = material_name(source)
        known = self._folders.get(identity)
        if known and known.is_dir():
            return self._direct(known)
        matches = [path for path in self.root.iterdir() if _material_identity(path.name) == identity and path.is_dir()]
        if len(matches) > 1:
            raise MaterialError(f"Найдено несколько папок для материала {identity}.")
        folder = self._direct(matches[0] if matches else self.root / identity)
        if matches:
            self._folders[identity] = folder
        return folder

    def _descriptive_folder(self, identity, article):
        suffix = f" [{identity}]"
        # Leave room for the temporary publication directories and managed filenames.
        max_units = min(170, 226 - len(str(self.root).encode("utf-16-le")) // 2)
        available = max_units - len(suffix)
        if available < 24:
            return self.root / identity
        authors = article.get("authors") or []
        first_author = _folder_label(authors[0], min(42, available // 2)) if authors else ""
        prefix = first_author + " — " if first_author else ""
        title = _folder_label(article.get("title"), available - len(prefix.encode("utf-16-le")) // 2)
        return self._direct(self.root / (prefix + title + suffix)) if title else self.root / identity

    def _rename_saved_folders(self):
        for identity, folder in self._folders.items():
            if folder.name != identity:
                continue
            try:
                profile = json.loads(self._direct(folder / "profile.json").read_text(encoding="utf-8"))
                destination = self._descriptive_folder(identity, profile["article"])
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if destination == folder or destination.exists():
                continue
            if self._direct(folder).parent != self.root or self._direct(destination).parent != self.root:
                raise MaterialError("Папка материала находится за пределами Materials.")
            try:
                self._move_with_retry(folder, destination)
            except OSError as error:
                print(f"Material directory rename skipped for {identity}: {type(error).__name__}", file=sys.stderr, flush=True)
            else:
                self._folders[identity] = destination

    def profile(self, source):
        path = self._direct(self.folder(source) / "profile.json")
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def read(self, source, filename):
        if filename not in MANAGED_FILES:
            raise ValueError("Неизвестный файл материала.")
        return self._direct(self.folder(source) / filename).read_bytes()

    def read_text(self, source):
        return self.read(source, "text.txt").decode("utf-8").replace("\r\n", "\n")

    def _discard(self, path):
        self._direct(path)
        if path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _move_with_retry(source, destination, *, replace=False):
        move = source.replace if replace else source.rename
        for delay in (0.05, 0.15, 0.30, 0.60):
            try:
                move(destination)
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (5, 32):
                    raise
                time.sleep(delay)
        move(destination)

    def _recover(self, folder):
        prepare = self._direct(folder / ".prepare")
        rollback = self._direct(folder / ".rollback")
        cleanup = self._direct(folder / ".cleanup")
        self._discard(cleanup)
        if rollback.is_dir():
            for name in MANAGED_FILES:
                if self._direct(rollback / "new" / name).is_file():
                    continue  # This file was never replaced.
                target = self._direct(folder / name)
                old = self._direct(rollback / "old" / name)
                if old.is_file():
                    restore = self._direct(rollback / "restore")
                    shutil.copyfile(old, restore)
                    self._move_with_retry(restore, target, replace=True)
                else:
                    target.unlink(missing_ok=True)
            self._move_with_retry(rollback, cleanup)
            self._discard(cleanup)
        self._discard(prepare)

    def publish(self, source, pdf, text, profile, manuscript=None, document=None):
        """Publish the current files or restore the previous profile after failure."""
        folder = self.folder(source)
        if not folder.exists():
            folder = self._descriptive_folder(material_name(source), profile["article"])
        folder.mkdir(exist_ok=True)
        self._folders[material_name(source)] = folder
        self._recover(folder)
        previous = self.profile(source)
        old_manuscript = previous["article"].get("manuscript", {}).get("file") if previous else None
        prepare, rollback, cleanup = (folder / name for name in (".prepare", ".rollback", ".cleanup"))
        try:
            new, old = prepare / "new", prepare / "old"
            new.mkdir(parents=True)
            old.mkdir()
            (new / "article.pdf").write_bytes(pdf)
            (new / "text.txt").write_text(text, encoding="utf-8")
            (new / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (new / "profile.md").write_text(render_markdown(profile), encoding="utf-8")
            if document is not None:
                cache = {"version": EXTRACTION_VERSION, "parser": extraction_signature(),
                         "sha256": profile["article"]["sha256"], "document": document}
                (new / "document.json").write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            elif previous and previous["article"].get("sha256") == profile["article"].get("sha256"):
                saved_cache = self._direct(folder / "document.json")
                if saved_cache.is_file():
                    shutil.copyfile(saved_cache, new / "document.json")
            filename = profile["article"].get("manuscript", {}).get("file")
            if filename in SOURCE_FILES:
                (new / filename).write_bytes(self.read(source, filename) if manuscript is None else manuscript)
            for name in MANAGED_FILES:
                previous = self._direct(folder / name)
                if previous.exists():
                    shutil.copyfile(previous, old / name)
            self._move_with_retry(prepare, rollback)
            for name in MANAGED_FILES:
                staged = rollback / "new" / name
                if staged.is_file():
                    self._move_with_retry(staged, folder / name, replace=True)
                elif (name == old_manuscript or name == "document.json") and (rollback / "old" / name).is_file():
                    (folder / name).unlink(missing_ok=True)
            self._move_with_retry(rollback, cleanup)
        except OSError:
            self._recover(folder)
            raise
        # The rename commits the bundle. Cleanup can be retried on startup.
        try:
            self._discard(cleanup)
        except OSError:
            pass


def _reference_year(text):
    years = list(DISPLAY_YEAR.finditer(text))
    if not years:
        return text, ""
    match = years[-1]
    year = (match["parenthesized"] or match["trailing"])[:4]
    if match["parenthesized"]:
        text = compact(text[:match.start()] + " " + text[match.end():])
    else:
        text = text[:match.start()].strip().lstrip("(")
    return text.strip(" ,;."), year


def _reference_publication(entry, details, year=""):
    details = re.split(r"\b(?:URL|DOI|arXiv)\s*:|https?://|\(Cited on pages?\b", details, maxsplit=1, flags=re.I)[0].rstrip(" ,;.")
    details, cited_year = _reference_year(details)
    publication = entry.get("publication", {})
    journal, volume, issue, pages = (publication.get(key) or "" for key in ("journal", "volume", "issue", "pages"))
    if journal and (volume or pages or not details):
        volume = volume + (f"({issue})" if issue else "")
        details = ", ".join(filter(None, (" ".join(filter(None, (journal, volume))), pages)))
    year = cited_year or year or publication.get("year") or entry.get("year", "")
    return details, f"({year})" if re.fullmatch(r"\d{4}", year) else ""


def _vancouver_reference(text):
    authors = VANCOUVER_AUTHORS.match(text)
    if not authors:
        return None
    body = text[authors.end():]
    separator = AUTHOR_TITLE_SEPARATOR.search(body)
    if not separator:
        return None
    title = body[:separator.start()]
    details = body[separator.end():]
    publication = VANCOUVER_PUBLICATION.match(details)
    if publication:
        venue = f'{publication["venue"]}, {publication["coords"].rstrip(".")}'
        return authors["authors"], title, venue, publication["year"][:4]
    if re.match(r"(?:In:\s|Zenodo\b)", details):
        venue = details.split("Available from:", 1)[0]
        date = re.search(rf";\s*({BIBLIOGRAPHIC_YEAR})\.(?=\s|$)", venue)
        if date:
            venue = (venue[:date.start()] + "," + venue[date.end():]).rstrip(" ,.")
        return authors["authors"], title, venue, date[1][:4] if date else ""
    return None


def _reference_item(entry):
    raw = re.sub(r"\b([A-Z])\s+\.", r"\1.", entry["text"])
    raw = re.sub(r"\s*\(Cited on pages? [^()]+\)\.?\s*$", "", raw)
    chunks = [chunk.strip() for chunk in raw.split(",")]
    author_count = 0
    for chunk in chunks:
        name = re.sub(r"(?<=\w)[-´`]\s+(?=\w)", "", re.sub(r"^and\s+", "", chunk))
        if (REFERENCE_AUTHOR.match(chunk) and full_author_names(name)) or (author_count and chunk.casefold() == "et al."):
            author_count += 1
        else:
            break
    authors = ", ".join(chunks[:author_count]) or entry.get("authors", "")
    citation = ", ".join(chunks[author_count:])
    author_end = AUTHOR_TITLE_SEPARATOR.search(citation)
    name_line = citation[:author_end.start()] if author_end else citation
    remainder = citation[author_end.end():] if author_end else ""
    year = ""
    author_year = (AUTHOR_YEAR_START.match(raw) if not re.match(r"^(?:and\s+)?[A-ZА-ЯЁ]\.", raw) else None)
    rmp = RMP_REFERENCE_START.match(raw) if not entry["number"] else None
    quoted_reference = QUOTED_REFERENCE_TITLE.match(raw)
    vancouver = _vancouver_reference(raw)
    colon_authors, colon, after_colon = raw.partition(": ")
    if rmp:
        authors = raw[:rmp.start("year")].rstrip(" ,")
        year = rmp["year"][:4]
        details = raw[rmp.end():]
        quoted = RMP_TITLE.match(details)
        title = quoted[1].rstrip(",") if quoted else ""
        if quoted:
            details = details[quoted.end():]
    elif quoted_reference:
        authors = quoted_reference["authors"] if full_author_names(quoted_reference["authors"]) else authors
        title = quoted_reference["title"].strip(" ,;.")
        details = raw[quoted_reference.end():]
    elif vancouver:
        authors, title, details, year = vancouver
    elif colon and full_author_names(colon_authors) and (YEAR_BEFORE_PAGE.match(after_colon) or REFERENCE_LINK_LINE.match(after_colon)):
        authors, title, details = colon_authors, "", after_colon
    elif author_year:
        authors, year = author_year[0].rsplit("(", 1)
        authors, year = authors.strip(" ,"), year[:4]
        title, _, details = raw[author_year.end():].strip().partition(". ")
    elif author_end and full_author_names(re.sub(r"\s+et al\.?$", "", name_line)):
        authors = ", ".join([*chunks[:author_count], name_line])
        title, *venue = re.split(r"(?<!\b[A-Z])\.\s*(?=[A-Z]|arXiv\b)", remainder, maxsplit=1)
        details = venue[0] if venue else ""
    else:
        venue = REFERENCE_VENUE.search(citation)
        title = citation[:venue.start()].strip() if venue else "" if YEAR_BEFORE_PAGE.match(citation) else citation
        details = (venue["journal"] or citation[venue.start():].lstrip(", ")) if venue else "" if title else citation
    if not details or REFERENCE_LINK_LINE.match(details):
        remaining, trailing_year = _reference_year(title.rstrip(" ,;."))
        if trailing_year:
            name, comma, venue = remaining.rpartition(", ")
            title, details = (name, venue) if comma else (remaining, details)
            year = trailing_year
    title = title.strip(" ,;.")
    if (REFERENCE_LINK_LINE.match(title) or re.match(r"^(?:and\s+)?[A-ZА-ЯЁ]\.\s", title)
            or (not authors and re.search(r"\b(?:URL|DOI)\s*:|https?://", title, re.I))
            or re.match(r"^(?:Nature(?:\s*\([^)]*\))?|Science|Phys(?:ical)?\.?\s*Rev(?:iew)?\.?[^,]*)\s*\d", title, re.I)):
        title = ""
    if not title and not details:
        details = citation
    details, year = _reference_publication(entry, details, year)
    missing = not (entry.get("title") or title)
    label = entry.get("title") or title or "Название не установлено"
    citations = []
    for location in entry.get("citations", []):
        page, section = location["page"], location["section"]
        label_suffix = f" · {section}" if section != "Основной текст" else ""
        citations.append({"page": page, "label": f"с. {page}{label_suffix}",
                          "anchor": location.get("anchor", ""), "context": location.get("context", "")})
    return {"label": label, "text": " ".join(filter(None, (details, year))), "href": web_link(entry["url"]),
            "number": entry["number"], "authors": authors, "citations": citations, "missing_title": missing}


def _analog_pdf_page(analog):
    page = analog.get("page")
    if not page:
        return ""
    if url := web_link(analog.get("pdf_url")):
        return url.split("#", 1)[0] + f"#page={page}"
    if analog["source"] != "arXiv":
        return ""
    try:
        identifier, version = arxiv_id(analog["url"])
    except ValueError:
        return ""
    return f"https://arxiv.org/pdf/{identifier}{version}#page={page}"


def profile_sections(profile):
    """One display structure for the browser and the portable Markdown export."""
    article, content, coverage = (profile[key] for key in ("article", "content", "coverage"))

    def item(label, text="", href=""):
        return {"label": label, "text": text, "href": href}

    metadata = [item("Авторы", ", ".join(article["authors"]) or "Не установлены"),
                item("Версия", article["version"] or "Загруженный PDF"),
                item("Дата публикации", article["published"] or "Не установлена")]
    if article.get("updated") and article["updated"] != article["published"]:
        metadata.append(item("Дата версии", article["updated"]))
    if article["url"]:
        metadata.append(item("Страница источника", article["url"], article["url"]))
    if article["doi"]:
        metadata.append(item("DOI", article["doi"], "https://doi.org/" + article["doi"]))
    if article["filename"]:
        metadata.append(item("Имя исходного файла", article["filename"]))
    usage = profile["usage"]
    page_count = coverage["page_count"]
    pages = f'{coverage["read_pages"]} из {page_count}' if page_count is not None else str(coverage["read_pages"])
    coverage_items = [item("Основа анализа", coverage["basis"]), item("Страницы с текстом", pages)]
    coverage_details = [item("API", f'{usage["model"] or "Модель не вызывалась"} · запросов: {usage["calls"]} · вход: {usage["input_tokens"]} · выход: {usage["output_tokens"]} токенов')]
    if profile["questions"]:
        coverage_details.append(item("Фокус внимания", profile["questions"]))
    selection = coverage.get("selection")
    if profile.get("reference_search_note"):
        coverage_details.append(item("Уточнение названий", profile["reference_search_note"]))
    if selection:
        budget = selection["budget_chars"]
        scope = ("Выбран полный текст с иллюстрациями." if profile.get("analysis", {}).get("depth") == "vision"
                 else f'Лимит — {budget} символов.' if budget is not None else "Выбран полный текст.")
        label = "Передано модели" if usage["calls"] else "Подготовлено для модели"
        coverage_items.append(item(label, f'{selection["selected_fragments"]} из {selection["available_fragments"]} фрагментов · '
                                   f'{selection["selected_chars"]} из {selection["available_chars"]} символов. {scope}'))
        coverage_details.append(item("Страницы основных фрагментов", ", ".join(map(str, selection["pages"])) or "Только аннотация"))
        if selection.get("reference_records"):
            coverage_details.append(item("Дополнительно для библиографии", f'{selection["reference_records"]} записей и контекст цитирования · страницы '
                                       + ", ".join(map(str, selection["reference_pages"]))))
        if selection["sampled"]:
            coverage_details.append(item("Отбор текста", "Общий охват и фрагменты по вопросам (локальный поиск по терминам)."
                                       if selection["question_focused"] else "Общий охват разделов статьи."))
    if coverage["sections"]:
        coverage_details.append(item("Распознанные разделы", ", ".join(coverage["sections"])))
    if usage["reported_calls"] < usage["calls"]:
        coverage_details.append(item("Данные об использовании", "Для части запросов данные о токенах не получены."))
    claims = [
        {**item(claim["statement"]), "byline": claim["basis"], "details": [
            *([item("Пояснение", claim["explanation"])] if claim.get("explanation") else []),
            {**item("Подтверждающий фрагмент", f'«{claim["evidence"]}»'),
             "location": claim["location"], "page": claim.get("page")},
            *([item("Проверить", claim["check"])] if claim["check"] else []),
        ]} for claim in content["claims"]
    ]
    summary = content["summary"]
    if profile["schema_version"] < 5 and summary:
        summary = [" ".join(summary)]
    answers = []
    for reply in content.get("answers", []):
        prefix = {"insufficient": "Недостаточно данных в выбранных фрагментах. ",
                  "unverified": "Подтверждение источником не сохранено или не найдено. ", "missing": ""}.get(reply["status"], "")
        answer = item(reply["question"], prefix + reply["answer"])
        answer["details"] = [item("Ответ", answer["text"])] + [
            {**item("Источник", f'«{source["evidence"]}»'), "location": source["location"], "page": source.get("page")}
            for source in reply.get("sources", [])]
        answers.append(answer)
    glossary = [item(definition["term"], "\n".join(filter(None, (definition["explanation"], definition["role"]))))
                for definition in content.get("glossary", [])]
    subsections = [{"key": key, "title": title, "items": items, "math": math} for key, title, items, math in (
        ("answers", "Ответы на вопросы", answers, "display"), ("glossary", "Словарь терминов", glossary, "inline"),
    ) if items]
    analogs = [
        {"label": analog["title"], "href": web_link(analog["url"]),
         "byline": " · ".join(filter(None, (analog["authors"], analog["date"][:4]))),
         "details": [item("Сходство", analog["similarity"]), item("Различие", analog["difference"]),
                     item("Основа сравнения", analog["basis"]),
                     {**item("Подтверждающий фрагмент", f'«{analog["evidence"]}»', _analog_pdf_page(analog)),
                      "location": analog["location"]}]}
        for analog in content["analogs"] if analog.get("page")
    ]
    keywords = [item(word["term"], f'{word["weight"]}% · {word["role"]}') for word in content["keywords"]]
    if article["author_keywords"]:
        keywords.append(item("Ключевые слова авторов", ", ".join(article["author_keywords"])))
    topics = content["topics"]
    illustrations = [
        {**item(f'Рисунок {record["number"]}'), "byline": f'Страница {record["page"]} PDF',
         "details": [{**item("Что показано", record["description"]),
                      "location": f'Страница {record["page"]} PDF', "page": record["page"]},
                     item("Связь с текстом", record["connection"]), item("Результат", record["result"])]}
        for record in content.get("illustrations", [])
    ]
    resources = [item(resource["kind"], f'Страница {resource["page"]} · {resource["url"]}', resource["url"])
                 for resource in content["resources"]]
    references = [_reference_item(entry) for entry in content.get("references", [])]
    search = profile.get("analog_search")
    search_notes, search_items = [], []
    if search:
        search_notes.append(f'Найдено уникальных работ: {search["records"]}. Для сравнения прочитан основной текст PDF: '
                            f'{search["full_text"]}.')
        if search["proposed"] is not None:
            search_notes.append(f'Модель предложила {search["proposed"]}, сохранено {search["accepted"]}.')
        elif search["model_error"]:
            search_notes.append(f'Сравнение не завершено. {search["model_error"]}')
        else:
            search_notes.append("Для сравнения требуется доступный полный текст кандидата.")
        if search["preserved"]:
            search_notes.append("Показаны аналоги из предыдущего поиска.")
        if "usage" in search:
            attempt_usage = search["usage"]
            search_items.append(item("API при повторе", f'Запросов к модели: {attempt_usage["calls"]}. '
                                     f'Вход: {attempt_usage["input_tokens"]}, выход: {attempt_usage["output_tokens"]} токенов.'))
        for source in search["sources"]:
            message = source.get("message") or f'Найдено записей: {source["count"]}.'
            if source.get("retry_after") is not None:
                message += f' Пауза при проверке: {source["retry_after"]} с.'
            search_items.append(item(source["label"], message))
        for failure in search["pdf_errors"]:
            search_items.append(item(f'PDF · {failure["title"]}', failure["reason"]))
        for rejected in search["rejected"]:
            search_items.append(item(f'Отбор · {rejected["title"]}', rejected["reason"]))
    return [
        {"key": "article", "title": "О статье", "paragraphs": [], "items": metadata},
        {"key": "coverage", "title": "Что прочитано", "paragraphs": coverage["notes"], "items": coverage_items,
         "steps": profile.get("steps", {}),
         "subsections": [{"key": "coverage-details", "title": "Подробности чтения", "items": coverage_details}]},
        {"key": "overview", "title": "Краткий обзор", "paragraphs": summary or ["Обзор недоступен."],
         "items": [], "subsections": subsections, "math": "display"},
        {"key": "keywords", "title": "Ключевые слова и темы", "paragraphs": [] if keywords or topics else ["Не выделены."],
         "items": keywords, "topics": topics, "math": "inline"},
        {"key": "claims", "title": "Основные утверждения", "paragraphs": [] if claims else ["Утверждения с подтверждающими фрагментами не выделены."], "items": claims, "math": "display"},
        *([{"key": "illustrations", "title": "Анализ иллюстраций", "items": illustrations, "math": "display",
            "paragraphs": [] if illustrations else ["В основном тексте рисунки не выделены."]}]
          if "illustrations" in content else []),
        {"key": "references", "title": "Список литературы", "paragraphs": [] if references else ["Записи не выделены из доступного текста."], "items": references},
        {"key": "analogs", "title": "Ближайшие аналоги", "paragraphs": [] if analogs else ["Достаточно обоснованных аналогов не найдено."], "items": analogs, "math": "display"},
        *([{"key": "search", "title": "Как искали аналоги", "paragraphs": search_notes, "items": search_items}] if search else []),
        {"key": "resources", "title": "Данные, код и приложения", "paragraphs": [] if resources else ["Ссылки в извлечённом материале не найдены."], "items": resources},
    ]


def _markdown_text(text, math=None):
    return escape_markdown(compact(text), math)


def _markdown_link(label, href, math=None):
    text = _markdown_text(label, math)
    return f"[{text}](<{href}>)" if web_link(href) else text


def _markdown_item(key, item, math=None):
    label = _markdown_link(item["label"], item["href"], "inline" if math else None)
    if key == "references":
        number = _markdown_text(f'[{item["number"]}]') + " " if item["number"] else ""
        lines = [f"- {number}**{label}**"]
        if item["authors"]:
            lines.append("  " + _markdown_text(item["authors"]))
        if item["text"]:
            lines.append("  " + _markdown_text(item["text"]))
        for place in item["citations"]:
            fragment = f'page={place["page"]}'
            if place["anchor"]:
                fragment += "&citation=" + place["anchor"]
            context = f' · {_markdown_text(place["context"])}' if place["context"] else ""
            lines.append(f'  В тексте · [{_markdown_text(place["label"])}](article.pdf#{fragment}){context}')
        return [line + "  " for line in lines[:-1]] + lines[-1:]
    if item.get("details"):
        lines = ["- **" + label + "**"]
        if item.get("byline"):
            lines.append("  " + _markdown_text(item["byline"]))
        for detail in item["details"]:
            text = _markdown_text(detail["text"], "inline" if math and "location" in detail else math)
            if detail.get("location"):
                location = _markdown_link(detail["location"], detail["href"])
                if detail.get("page"):
                    location = f'[{location}](article.pdf#page={detail["page"]})'
                text = location + " · " + text
            lines.append(f'  **{detail["label"]}** · {text}')
        return lines
    parts = item["text"].splitlines()
    return ["- **" + label + "**" + (" — " + _markdown_text(parts[0], math) if parts else ""),
            *("  " + _markdown_text(part, math) for part in parts[1:])]


def render_markdown(profile):
    files = "[Исходный PDF](article.pdf)"
    if profile["article"].get("manuscript", {}).get("file") == "manuscript.tex":
        files += " · [Исходник рукописи .tex](manuscript.tex)"
    lines = ["# " + _markdown_text(profile["article"]["title"]), "", files, ""]
    for section in profile_sections(profile):
        lines.extend(["## " + section["title"], ""])
        for paragraph in section["paragraphs"]:
            lines.extend([_markdown_text(paragraph, section.get("math")), ""])
        for item in section["items"]:
            lines.extend(_markdown_item(section["key"], item, section.get("math")))
        if section.get("topics"):
            lines.extend(["", "### Темы", "", *["- " + _markdown_text(topic, "inline") for topic in section["topics"]]])
        if steps := section.get("steps"):
            labels = {"material": "Материалы", "core": "Основной профиль", "references": "Библиография",
                      "analogs": "Аналоги", "manuscript": "Исходник"}
            statuses = {"pending": "Ожидает", "running": "Выполняется", "completed": "Готово", "skipped": "Пропущено",
                        "failed": "Ошибка", "blocked": "Требуется действие", "not_applicable": "Не требуется"}
            lines.extend(["", "### Этапы обработки", ""])
            for key, step in steps.items():
                message = " · " + _markdown_text(step["message"]) if step.get("message") else ""
                lines.append(f'- **{labels.get(key, key)}** — {statuses.get(step["status"], step["status"])}{message}')
        lines.append("")
        for subsection in section.get("subsections", []):
            lines.extend(["### " + subsection["title"], ""])
            for item in subsection["items"]:
                lines.extend(_markdown_item(subsection["key"], item, subsection.get("math")))
            lines.append("")
    return MATH_HEADER + "\n".join(lines).rstrip() + "\n"


def render_markdown_preview(data, snapshot):
    source = data.decode("utf-8-sig").replace("\r\n", "\n")
    legacy = not source.startswith(MATH_HEADER)
    source = source.removeprefix(MATH_HEADER)
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        query = f"?snapshot={quote(snapshot, safe='')}" if snapshot else ""
        content = ("<h1>Форматированный просмотр недоступен</h1>"
                   "<p>Установите зависимости в окружение сервера командой "
                   "<code>python -m pip install -r requirements.txt</code> и перезапустите Research Agent.</p>"
                   f'<p><a href="profile.md{query}">Скачать профиль .md</a></p>')
    else:
        parser = MarkdownIt("js-default", {"breaks": True})
        parser.use(markdown_math, legacy=legacy)
        tokens = parser.parse(source)
        for token in tokens:
            for child in token.children or []:
                if child.type == "link_open":
                    path, marker, fragment = child.attrGet("href").partition("#")
                    citation = re.fullmatch(rf"page=(\d+)&citation=({CITATION_ANCHOR})", fragment)
                    if path == "article.pdf" and citation:
                        query = urlencode({"snapshot": snapshot, "citation": citation[2]})
                        child.attrSet("href", f"article.pdf?{query}#page={citation[1]}")
                        continue
                    if path == "manuscript.tex":
                        path = "manuscript.html"
                        child.attrSet("href", path)
                    if snapshot and path in {"article.pdf", "text.txt", "manuscript.html"}:
                        child.attrSet("href", f"{path}?snapshot={quote(snapshot, safe='')}{marker}{fragment}")
        content = parser.renderer.render(tokens, parser.options, {})
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Профиль статьи · Research Agent {VERSION}</title>
  <link rel="stylesheet" href="/style.css">
  <script src="/vendor/katex/katex.min.js" defer></script><script src="/math.js" defer></script>
</head>
<body><main class="profile-document">
  <p class="profile-document-version">Research Agent {VERSION}</p>
  {content}
</main></body>
</html>
"""


def citation_location(data, anchor):
    if not re.fullmatch(CITATION_ANCHOR, anchor):
        return None
    index, start, end = map(int, anchor.split("-"))
    pages = saved_pages(data.decode("utf-8-sig").replace("\r\n", "\n"))
    parts = split_sections(pages)
    if index >= len(parts) or not start < end <= len(parts[index]["text"]):
        return None
    part = parts[index]
    return part["page"], part["text"], start, end


if __name__ == "__main__" and sys.argv[1:] in (["extract"], ["extract-full"]):
    try:
        full = sys.argv[1] == "extract-full"
        result = _pdf_data(sys.stdin.buffer.read(-1 if full else PDF_LIMIT + 1), full=full)
    except ImportError:
        result = {"error": "Для извлечения текста установите pypdf: python -m pip install -r requirements.txt."}
    except Exception:
        result = {"error": "Не удалось прочитать структуру PDF. Проверьте файл."}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
