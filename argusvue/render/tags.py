"""Citation tags and the sentence-level enforcer.

Tags: `[c:123]` (claim), `[i:<indicator_id>:<YYYY-MM>]` or `[i:<id>:<YYYY-MM-DD>]` (indicator point),
`[run:123]` (agent run, used for open questions). `enforce` drops every prose sentence that carries no tag or
any invalid tag; headings, table rows, `>` notes, code fences and blank lines are exempt."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

TAG_RE = re.compile(r"\[(?:c:(?P<c>\d+)|i:(?P<i>[A-Za-z0-9_.\-]+):(?P<p>\d{4}-\d{2}(?:-\d{2})?)|run:(?P<r>\d+))\]")
_ANY_TAG_LIKE = r"(?:\s*\[(?:c|i|run):[^\]\s]*\])*"
_BOUNDARY = re.compile(rf"([.!?]['\")]*{_ANY_TAG_LIKE})\s+(?=[^\sa-z])")
_ABBREV = {"e.g", "i.e", "vs", "u.s", "approx", "no", "st", "mt", "dr", "inc", "co", "corp", "ltd", "jr", "sr", "mr", "mrs", "ms", "fig", "est"}
_BULLET = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_HRULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LABEL = re.compile(r"^(\**[^.!?\[\]]*:\**|\*\*[^*]+\*\*|_[^_]+_|)$")


class Tag(NamedTuple):
    kind: str            # 'c' | 'i' | 'run'
    ident: str           # claim id, indicator id, or run id
    period: str | None = None

    def __str__(self) -> str:
        return f"[i:{self.ident}:{self.period}]" if self.kind == "i" else f"[{self.kind}:{self.ident}]"


def tags_in(sentence: str) -> list[Tag]:
    out = []
    for m in TAG_RE.finditer(sentence):
        if m.group("c"):
            out.append(Tag("c", m.group("c")))
        elif m.group("i"):
            out.append(Tag("i", m.group("i"), m.group("p")))
        else:
            out.append(Tag("run", m.group("r")))
    return out


def _is_abbrev(chunk: str) -> bool:
    token = chunk.rsplit(None, 1)[-1] if chunk.strip() else ""
    if not token.endswith("."):
        return False
    bare = token[:-1].lower().lstrip("(\"'")
    return bare in _ABBREV or (len(bare) == 1 and bare.isalpha())


def split_sentences(paragraph: str) -> list[str]:
    """Split on . ! ? followed by whitespace and a non-lowercase start. Tags that directly follow the
    terminator ("rose. [c:1]") stay with the sentence they cite."""
    p = " ".join(paragraph.split())
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(p):
        if _is_abbrev(p[start:m.start(1) + 1]) and p[m.start(1)] == ".":
            continue
        out.append(p[start:m.end(1)].strip())
        start = m.end()
    tail = p[start:].strip()
    if tail:
        out.append(tail)
    return out


def _sentence_ok(sentence: str, valid: Callable[[Tag], bool]) -> bool:
    tags = tags_in(sentence)
    return bool(tags) and all(valid(t) for t in tags)


def _process(prefix: str, content: str, valid: Callable[[Tag], bool], dropped: list[str]) -> str | None:
    """Returns the cleaned text (without prefix), the original content when nothing was dropped, or None."""
    sentences = split_sentences(content)
    flags = [_sentence_ok(s, valid) for s in sentences]
    keep = [s for s, ok in zip(sentences, flags) if ok]
    dropped.extend(s for s, ok in zip(sentences, flags) if not ok)
    if len(keep) == len(sentences):
        return content
    return " ".join(keep) if keep else None


def enforce(markdown: str, *, valid: Callable[[Tag], bool]) -> tuple[str, list[str]]:
    lines = markdown.split("\n")
    out: list[str] = []
    dropped: list[str] = []
    unit: list[str] = []          # raw lines of the paragraph / bullet being collected
    unit_prefix: str | None = None
    in_code = False

    def flush() -> None:
        nonlocal unit, unit_prefix
        if not unit:
            return
        if unit_prefix is not None:
            first = _BULLET.match(unit[0]).group(2)          # type: ignore[union-attr]
            content = " ".join([first.strip()] + [u.strip() for u in unit[1:]])
            if _LABEL.match(content.strip()):
                out.extend(unit)
            else:
                res = _process(unit_prefix, content, valid, dropped)
                if res is content:
                    out.extend(unit)
                elif res:
                    out.append(unit_prefix + res)
        else:
            content = " ".join(u.strip() for u in unit)
            res = _process("", content, valid, dropped)
            if res is content:
                out.extend(unit)
            elif res:
                out.append(res)
        unit, unit_prefix = [], None

    for line in lines:
        s = line.strip()
        if s.startswith(("```", "~~~")):
            flush()
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        if not s or _HEADING.match(line) or s.startswith(("|", ">", "<!--")) or _HRULE.match(line):
            flush()
            out.append(line)
            continue
        m = _BULLET.match(line)
        if m:
            flush()
            unit, unit_prefix = [line], m.group(1)
        elif unit_prefix is not None and line[:1].isspace():
            unit.append(line)             # indented continuation of a bullet
        else:
            if unit_prefix is not None:
                flush()
            unit.append(line)
    flush()
    return "\n".join(out), dropped
