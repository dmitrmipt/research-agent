"""Repair PDF line wrapping in bibliography without flattening scientific compounds."""
import re

COMPOUND_PREFIXES = {"electron", "phonon", "spin", "self", "first", "second", "third", "type",
                     "two", "three", "four", "many", "single", "multi", "non", "low", "high",
                     "finite", "zero", "time", "well", "long", "short", "strong", "weak"}


def vocabulary(text):
    return {word.casefold() for word in re.findall(r"(?<![\w-])[^\W\d_]{3,}(?![\w-])", text)}


def reference_text(text, words=()):
    words = set(words)
    text = text.replace("\u00ad", "")
    text = re.sub(r"(https?):\s+(?=//)", r"\1:", text)
    identifier = r"(?:https?://|10\.\d{4,9}/)\S*"
    wrap = re.compile(rf"({identifier}(?<=[/:]))[ \t]*\r?\n[ \t]*(?=[A-Za-z0-9~])"
                      rf"|({identifier}(?<=\.))[ \t]*\r?\n[ \t]*(?=[a-z0-9])")
    while True:
        text, joined = wrap.subn(r"\1\2", text)
        if not joined:
            break

    def repair(match):
        left, gap, right = match.group(1, 2, 3)
        combined = (left + right).casefold()
        wrapped = "\n" in gap or "\r" in gap
        if re.search(r"(?:https?://|10\.\d{4,9}/)\S*$", text[:match.start()]):
            return left + "-" + right
        if combined in words:
            return left + right
        if wrapped and left.casefold() not in COMPOUND_PREFIXES and not (
                left.casefold() in words and right.casefold() in words):
            return left + right
        return left + "-" + ("" if wrapped else gap) + right

    text = re.sub(r"([^\W\d_]+)[-‐]\s*(\n\s*|[ \t]+)([a-zа-яё][^\W\d_]*)", repair, text)
    text = re.sub(r"(?<=\w)[-‐][ \t]*\r?\n[ \t]*(?=[A-ZА-ЯЁ0-9])", "-", text)
    return " ".join(text.split())
