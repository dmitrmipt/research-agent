"""Select an arXiv manuscript from its source archive and display TeX safely."""
from __future__ import annotations

import gzip
import io
import re
import tarfile
import zipfile
import zlib
from html import escape
from pathlib import PurePosixPath
from urllib.parse import quote

SOURCE_LIMIT = 16 * 1024 * 1024
ARCHIVE_LIMIT = 48 * 1024 * 1024
TEX_LIMIT = 2 * 1024 * 1024
MEMBER_LIMIT = 512
SOURCE_FILES = ("manuscript.tex", "manuscript.docx")
SUPPLEMENT = re.compile(r"(?:^|[-_. ])(?:anc|supp(?:lement(?:ary)?)?|appendix)(?:$|[-_. 0-9])", re.I)
TEX_ROOT = re.compile(rb"\\(?:documentclass|documentstyle)\b|\\begin\s*\{\s*document\s*\}|\\bye\b")
TEX_TOKENS = re.compile(
    r"(?P<comment>%[^\r\n]*)"
    r"|(?P<math>\$\$|\$|\\[()[\]])"
    r"|(?P<command>\\(?:[a-zA-Z@]+\*?|[^\r\n]))"
    r"|(?P<group>[{}])"
)


def _source_path(name, size):
    path = PurePosixPath(name)
    if (path.is_absolute() or any(part.startswith(".") for part in path.parts)
            or "\\" in name or ":" in name):
        return None
    limit = {".tex": TEX_LIMIT, ".docx": SOURCE_LIMIT}.get(path.suffix.lower(), 0)
    return path if 0 < size <= limit else None


def _is_docx(archive):
    return {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist())


def _source_members(raw):
    stream = io.BytesIO(raw)
    if raw.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(stream) as archive:
            if _is_docx(archive):
                yield PurePosixPath("manuscript.docx"), raw
                return
            for member in archive.infolist()[:MEMBER_LIMIT]:
                if path := _source_path(member.filename, member.file_size):
                    yield path, archive.read(member)
        return

    try:
        archive = tarfile.open(fileobj=stream, mode="r:")
    except tarfile.ReadError:
        if len(raw) <= TEX_LIMIT and not raw.lstrip().startswith((b"<", b"%PDF")):
            yield PurePosixPath("manuscript.tex"), raw
        return
    with archive:
        for index, member in enumerate(archive):
            if index == MEMBER_LIMIT:
                break
            if member.isfile() and (path := _source_path(member.name, member.size)):
                yield path, archive.extractfile(member).read()


def select_manuscript(raw):
    """Return the name and original bytes of an identifiable main manuscript."""
    if len(raw) > SOURCE_LIMIT:
        return None
    tex, docx, size = [], [], 0
    try:
        if raw.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
                raw = archive.read(ARCHIVE_LIMIT + 1)
            if len(raw) > ARCHIVE_LIMIT:
                return None
        for path, data in _source_members(raw):
            size += len(data)
            if size > ARCHIVE_LIMIT:
                return None
            if any(SUPPLEMENT.search(part) for part in (*path.parts[:-1], path.stem)):
                continue
            named = path.stem.lower() in {"main", "article", "paper", "manuscript"}
            if path.suffix.lower() == ".tex":
                code = re.sub(rb"(?<!\\)%[^\r\n]*", b"", data)
                if TEX_ROOT.search(code):
                    tex.append(((named, bool(re.search(rb"\\title\b", code))), str(path), data))
            elif data.startswith(b"PK\x03\x04"):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if _is_docx(archive):
                        docx.append(((named,), str(path), data))
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error):
        return None

    candidates = sorted(tex or docx, key=lambda item: item[0], reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
        return candidates[0][1:]
    return None


def render_tex_preview(raw, name, snapshot):
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        source = raw.decode("latin-1")
    parts, offset = [], 0
    for match in TEX_TOKENS.finditer(source):
        parts.append(escape(source[offset:match.start()]))
        parts.append(f'<span class="tex-{match.lastgroup}">{escape(match[0])}</span>')
        offset = match.end()
    parts.append(escape(source[offset:]))
    title = escape(name)
    download = "manuscript.tex" + ("?snapshot=" + quote(snapshot, safe="") if snapshot else "")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · Исходник рукописи</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body><main class="profile-document manuscript-document">
  <h1>{title}</h1>
  <p><a class="secondary" href="{download}">Скачать .tex</a></p>
  <pre class="tex-code"><code>{''.join(parts)}</code></pre>
</main></body>
</html>
"""
