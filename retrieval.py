"""Local, page-preserving text selection with a shared character budget."""
from __future__ import annotations

import math
import re
from collections import Counter

VERSION = 1
CHUNK_SIZE = 2000
OVERLAP = 200
BUDGETS = {"brief": 16_000, "standard": 30_000, "deep": 48_000, "full": None, "vision": None}
EXCLUDED = {"Библиография", "Благодарности"}
# Small, explicit cross-language aliases; no remote translation or inference.
ALIASES = {
    "калибров": "calibration calibrated calibrate", "шум": "noise",
    "температур": "temperature", "измер": "measurement measured measurements",
    "метод": "method methods", "образ": "sample specimen",
    "погреш": "uncertainty error", "ошиб": "error uncertainty",
    "спектр": "spectrum spectra spectral", "результ": "result results",
    "услов": "condition conditions", "огранич": "limitation limitations",
}


def compact(text):
    return " ".join(text.split())


def chunks(parts):
    result = []
    for part in parts:
        if part["section"] in EXCLUDED:
            continue
        paragraphs = [compact(text) for text in re.split(r"\n\s*\n", part["text"]) if compact(text)]
        text = " ".join(paragraphs)
        paragraph_ends, offset = [], 0
        for paragraph in paragraphs:
            offset += len(paragraph)
            paragraph_ends.append(offset)
            offset += 1
        start = 0
        while start < len(text):
            end = min(start + CHUNK_SIZE, len(text))
            if end < len(text):
                paragraph_end = next((edge for edge in reversed(paragraph_ends) if start + CHUNK_SIZE // 2 <= edge <= end), None)
                boundaries = list(re.finditer(r"[.!?。！？](?:\s|$)", text[start:end]))
                if paragraph_end is not None:
                    end = paragraph_end
                elif boundaries and boundaries[-1].end() >= CHUNK_SIZE // 2:
                    end = start + boundaries[-1].end()
                else:
                    space = text.rfind(" ", start + CHUNK_SIZE // 2, end)
                    if space > start:
                        end = space
            body = text[start:end].strip()
            if body:
                result.append({"id": f"s{len(result) + 1}", "page": part["page"],
                               "section": part["section"], "text": body, "offset": start})
            if end == len(text):
                break
            overlap_start = max(start + 1, end - OVERLAP)
            sentences = list(re.finditer(r"[.!?。！？]\s+", text[overlap_start:end]))
            if sentences:
                overlap_start += sentences[0].end()
            else:
                space = text.find(" ", overlap_start, end)
                if space != -1:
                    overlap_start = space + 1
            start = overlap_start
    return result


def words(text, expand=False):
    tokens = re.findall(r"[^\W_]+", text.casefold())
    if expand:
        tokens += [alias for stem, aliases in ALIASES.items()
                   if any(token.startswith(stem) for token in tokens) for alias in aliases.split()]
    return tokens


def ranked(snippets, query):
    counts = [Counter(words(item["text"])) for item in snippets]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(1, len(lengths)) or 1
    terms = set(words(query, expand=True))
    frequency = {term: sum(term in count for count in counts) for term in terms}
    scored = []
    for index, count in enumerate(counts):
        score = 0
        for term in terms:
            tf = count[term]
            idf = math.log(1 + (len(counts) - frequency[term] + .5) / (frequency[term] + .5))
            score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * lengths[index] / average))
        if score:
            scored.append((score, index))
    return [index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))]


def overview_order(snippets):
    def spread(indices):
        if not indices:
            return []
        order = [indices[0], indices[-1]]
        for level in range(math.ceil(math.log2(len(indices) + 1)) + 1):
            for i in range(2 ** level):
                index = round((i + .5) * len(indices) / 2 ** level - .5)
                if 0 <= index < len(indices):
                    order.append(indices[index])
        return list(dict.fromkeys(order))

    groups = []
    for sections in ({"Введение", "Аннотация"}, {"Методы", "Теория"},
                     {"Результаты", "Результаты и обсуждение", "Обсуждение"}, {"Заключение"}):
        indices = [i for i, snippet in enumerate(snippets) if snippet["section"] in sections]
        if indices:
            groups.append(spread(indices))
    # Unrecognized main-text headings still precede an appended supplement.
    fallback = spread([i for i, snippet in enumerate(snippets) if snippet["section"] != "Приложение"])
    fallback += spread([i for i, snippet in enumerate(snippets) if snippet["section"] == "Приложение"])
    result = []
    for offset in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if offset < len(group):
                result.append(group[offset])
    return list(dict.fromkeys(result + fallback))


def select(parts, abstract, budget, focus="", questions=()):
    available = chunks(parts)
    abstract_text = compact(abstract)[:4000 if budget is None else min(4000, budget)]
    intro = ([{"id": "abstract", "page": None, "section": "Аннотация arXiv", "text": abstract_text}]
             if abstract_text else [])
    total = sum(len(item["text"]) for item in available) + len(abstract_text)
    limit = total if budget is None else budget
    remaining = limit - len(abstract_text)
    selected, used = [], set()

    def take(order, allowance):
        spent = 0
        for index in order:
            size = len(available[index]["text"])
            if index not in used and size <= allowance - spent:
                selected.append(index)
                used.add(index)
                spent += size
        return spent

    queries = [item["text"] for item in questions] + ([focus] if focus.strip() else [])
    if total <= limit:
        take(range(len(available)), remaining)
    else:
        general = overview_order(available)
        query_orders = [ranked(available, query) for query in queries]
        if query_orders:
            quota = remaining // 2
            each = quota // len(query_orders)
            for order in query_orders:
                remaining -= take(order, each)
            # Select general context after reserving material for each question.
            remaining -= take(general, (limit - len(abstract_text)) // 2)
            merged = [index for offset in range(max(map(len, query_orders), default=0))
                      for order in query_orders if offset < len(order) for index in [order[offset]]]
            remaining -= take(merged, remaining)
        remaining -= take(general, remaining)
    result = intro + [available[index] for index in sorted(selected)]
    report = {"method": "bm25-and-sections", "version": VERSION, "budget_chars": budget,
              "selected_chars": sum(len(item["text"]) for item in result), "available_chars": total,
              "selected_fragments": len(result), "available_fragments": len(available) + len(intro),
              "pages": sorted({item["page"] for item in result if item["page"]}),
              "sampled": len(selected) < len(available), "question_focused": bool(queries),
              "chunk_size": CHUNK_SIZE, "overlap": OVERLAP}
    return result, report
