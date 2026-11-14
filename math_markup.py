"""Preserve TeX fragments when exporting and previewing profile Markdown."""
import re
from html import escape


# Keep in sync with the delimiter matcher in static/math.js.
MATH = re.compile(
    r"(?<!\\)(?:\$\$([^$]{1,1500})\$\$|\\\[([\s\S]{1,1500}?)\\\]"
    r"|\\\(([^\r\n]{1,1500}?)\\\)"
    r"|(?<!\$)\$(?!\s)((?:\\[^\r\n]|[^$\\\r\n]){1,1500}?)(?<!\s)\$(?![\d$]))"
)
MARKDOWN_SPECIAL = re.compile(r"([\\`*_$\[\]<>#|])")
MATH_HEADER = "<!-- research-agent: math-v1 -->\n"


def _normalized_fragment(fragment, mode):
    tex = fragment[fragment.lastindex].strip()
    if not tex:
        return None
    delimiter = "$$" if mode == "display" and fragment.lastindex in (1, 2) else "$"
    return delimiter + tex + delimiter


def escape_markdown(text, math=None):
    """Escape prose while retaining math delimiters in model-authored fields."""
    if math is None:
        return MARKDOWN_SPECIAL.sub(r"\\\1", text)
    result = []
    end = 0
    for fragment in MATH.finditer(text):
        result.append(MARKDOWN_SPECIAL.sub(r"\\\1", text[end:fragment.start()]))
        result.append(_normalized_fragment(fragment, math) or MARKDOWN_SPECIAL.sub(r"\\\1", fragment[0]))
        end = fragment.end()
    result.append(MARKDOWN_SPECIAL.sub(r"\\\1", text[end:]))
    return "".join(result)


def _math_source(source):
    return f'<span class="profile-math-source">{escape(source)}</span>'


def markdown_math(parser, legacy=False):
    def inline_rule(state, silent):
        if state.src[state.pos] not in "$\\":
            return False
        fragment = MATH.match(state.src, state.pos, state.posMax)
        if not fragment or not fragment[fragment.lastindex].strip():
            return False
        if not silent:
            state.push("profile_math", "span", 0).content = fragment[0]
        state.pos = fragment.end()
        return True

    def render_rule(tokens, index, options, env):
        return _math_source(tokens[index].content)

    if legacy:
        # Old profile exports escaped TeX as Markdown prose. Decode that escaping
        # with the standard inline parser before marking formulas for the browser.
        modes = {"Краткий обзор": "display", "Ответы на вопросы": "display", "Основные утверждения": "display",
                 "Ближайшие аналоги": "display", "Словарь терминов": "inline", "Ключевые слова и темы": "inline"}

        def mark_legacy(state):
            mode = None
            for index, token in enumerate(state.tokens):
                if token.type == "heading_open":
                    mode = modes.get(state.tokens[index + 1].content)
                elif mode and token.type == "inline":
                    for child in token.children or ():
                        if child.type == "text" and MATH.search(child.content):
                            child.type = "legacy_profile_math"
                            child.meta["math_mode"] = "inline" if "«" in child.content else mode

        def render_legacy(tokens, index, options, env):
            token = tokens[index]
            result = []
            end = 0
            for fragment in MATH.finditer(token.content):
                result.append(escape(token.content[end:fragment.start()]))
                normalized = _normalized_fragment(fragment, token.meta["math_mode"])
                result.append(_math_source(normalized) if normalized else escape(fragment[0]))
                end = fragment.end()
            result.append(escape(token.content[end:]))
            return "".join(result)

        parser.core.ruler.after("text_join", "legacy_profile_math", mark_legacy)
        parser.renderer.rules["legacy_profile_math"] = render_legacy
    else:
        parser.inline.ruler.before("escape", "profile_math", inline_rule)
    parser.renderer.rules["profile_math"] = render_rule
