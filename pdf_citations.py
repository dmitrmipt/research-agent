"""Map saved citation offsets to PDF text rectangles and add a transient highlight."""
import io
import math
import re
import unicodedata
from contextlib import closing

from pdf_text import PDFIUM_LOCK

_DASHES = str.maketrans("‐‑‒–—−", "------")
_INVISIBLE = "\u00ad\u200b\u200c\u200d\ufeff"


def _normalized_character(char):
    return "".join(part for part in unicodedata.normalize("NFKC", char).casefold().translate(_DASHES)
                   if not part.isspace() and part not in _INVISIBLE)


def _normalized(text):
    return "".join(map(_normalized_character, text))


def _matching_offset(page, text, start, end):
    before, marker, after = map(_normalized, (text[:start], text[start:end], text[end:]))
    if not marker or len(marker) > 1500:
        return None
    section = before + marker + after
    section_offset = page.find(section)
    if section_offset >= 0 and page.find(section, section_offset + 1) == -1:
        return section_offset + len(before), len(marker)

    left, right = before[-96:], after[:96]

    def prefix_length(expected, found):
        return next((i for i, (a, b) in enumerate(zip(expected, found)) if a != b), min(len(expected), len(found)))

    ranked = []
    for hit in re.finditer(re.escape(marker), page):
        score = (prefix_length(left[::-1], page[max(0, hit.start() - len(left)):hit.start()][::-1])
                 + prefix_length(right, page[hit.end():hit.end() + len(right)]))
        ranked.append((score, hit.start()))
    ranked.sort(reverse=True)
    if ranked and ranked[0][0] >= min(12, len(left) + len(right)):
        score, offset = ranked[0]
        if len(ranked) == 1 or score > ranked[1][0]:
            return offset, len(marker)
    return None


def _rectangles(pdfium, raw, page_number, text, start, end):
    with PDFIUM_LOCK, pdfium.PdfDocument(raw) as pdf:
        if not 1 <= page_number <= len(pdf):
            return []
        with closing(pdf[page_number - 1]) as page, closing(page.get_textpage()) as textpage:
            chars, indices = [], []
            count = textpage.count_chars()
            if count > 100_000:
                return []
            for index in range(count):
                codepoint = pdfium.raw.FPDFText_GetUnicode(textpage, index)
                if not 0 < codepoint <= 0x10FFFF:
                    continue
                normalized = _normalized_character(chr(codepoint))
                chars.append(normalized)
                indices.extend([index] * len(normalized))
            match = _matching_offset("".join(chars), text, start, end)
            if match is None:
                return []
            offset, length = match
            first, last = indices[offset], indices[offset + length - 1]
            count = textpage.count_rects(first, last - first + 1)
            return [textpage.get_rect(i) for i in range(count)]


def highlighted_pdf(raw, page_number, text, start, end):
    """Return an annotated PDF, or the original bytes when no match is reliable."""
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader, PdfWriter
        from pypdf.annotations import Highlight
        from pypdf.errors import PdfReadError
        from pypdf.generic import ArrayObject, FloatObject
    except ImportError:
        return raw
    try:
        boxes = _rectangles(pdfium, raw, page_number, text, start, end)
        if not boxes or any(not all(map(math.isfinite, box)) or box[0] >= box[2] or box[1] >= box[3] for box in boxes):
            return raw
        quads = ArrayObject([FloatObject(value) for left, bottom, right, top in boxes
                             for value in (left, top, right, top, left, bottom, right, bottom)])
        bounds = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                  max(box[2] for box in boxes), max(box[3] for box in boxes))
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(raw)))
        writer.add_annotation(page_number - 1, Highlight(rect=bounds, quad_points=quads, highlight_color="ffd34f"))
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except (pdfium.PdfiumError, PdfReadError, OSError, ValueError, KeyError, IndexError):
        return raw
