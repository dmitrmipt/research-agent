"""Recover missing word spaces without changing pypdf's reading order."""
import ctypes
import re
import threading
import unicodedata
from contextlib import closing, contextmanager

PDFIUM_LOCK = threading.Lock()  # PDFium cannot be used by concurrent threads in one process.
_CONTEXT = 12  # Normalized characters required on each side of a proposed space.
_INVISIBLE = "\u00ad\u200b\u200c\u200d\ufeff"
_WORD_CHAR = re.compile(r"[^\W\d_]")


@contextmanager
def pdfium_document(raw):
    try:
        import pypdfium2
    except (ImportError, OSError):
        yield None
        return
    with PDFIUM_LOCK:
        try:
            document = pypdfium2.PdfDocument(raw)
        except pypdfium2.PdfiumError:
            yield None
        else:
            with document:
                yield document


def _glyph_stream(text):
    chars, offsets = [], []
    for offset, char in enumerate(text):
        for glyph in unicodedata.normalize("NFKC", char):
            if not glyph.isspace() and glyph not in _INVISIBLE:
                chars.append(glyph)
                offsets.append(offset)
    return "".join(chars), offsets


def _prose_letter(char):
    return char.isalpha() and unicodedata.name(char, "").startswith(("LATIN ", "CYRILLIC "))


def _same_line(api, text_page, left, right):
    left_size = api.FPDFText_GetFontSize(text_page, left)
    right_size = api.FPDFText_GetFontSize(text_page, right)
    tolerance = min(left_size, right_size) * .15
    if tolerance <= 0 or abs(left_size - right_size) > tolerance:
        return False
    x, left_y, right_y = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
    return (api.FPDFText_GetCharOrigin(text_page, left, x, left_y)
            and api.FPDFText_GetCharOrigin(text_page, right, x, right_y)
            and abs(left_y.value - right_y.value) <= tolerance)


def _insert_spaces(text, alternate, text_page, api):
    base, base_offsets = _glyph_stream(text)
    other, other_offsets = _glyph_stream(alternate)
    aligned = base == other
    positions = set()
    for index in range(_CONTEXT, len(other) - _CONTEXT + 1):
        left, right = other_offsets[index - 1:index + 1]
        gap = alternate[left + 1:right]
        if not gap or gap.strip(" \t") or not (_prose_letter(alternate[left]) and _prose_letter(alternate[right])):
            continue
        start = index - _CONTEXT
        context = other[start:index + _CONTEXT]
        match = start if aligned else base.find(context)
        if match < 0:
            continue
        split = match + _CONTEXT
        before, after = base_offsets[split - 1:split + 1]
        if after != before + 1 or not (_prose_letter(text[before]) and _prose_letter(text[after])):
            continue
        if (base.find(context, match + 1) != -1 or other.find(context) != start
                or other.find(context, start + 1) != -1):
            continue
        # Isolated letters are often variables. The single-letter words a and I are allowed.
        if alternate[left] not in "aAI" and (left == 0 or not _WORD_CHAR.match(alternate, left - 1)):
            continue
        if alternate[right] not in "aAI" and not _WORD_CHAR.match(alternate, right + 1):
            continue
        if _same_line(api, text_page, left, right):
            positions.add(after)
    if not positions:
        return text
    boundaries = [0, *sorted(positions), len(text)]
    return " ".join(text[start:end] for start, end in zip(boundaries, boundaries[1:]))


def restore_word_spaces(document, index, text):
    if not text or document is None or index >= len(document):
        return text
    from pypdfium2 import PdfiumError, raw as api

    try:
        with closing(document[index]) as page, closing(page.get_textpage()) as text_page:
            count = text_page.count_chars()
            if not 0 < count <= 100_000:
                return text
            codes = (api.FPDFText_GetUnicode(text_page, char) for char in range(count))
            alternate = "".join(chr(code) if 0 < code <= 0x10ffff else "\u200b" for code in codes)
            return _insert_spaces(text, alternate, text_page, api)
    except PdfiumError:
        return text
