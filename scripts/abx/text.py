"""Deterministic text units: redaction with an offset map, paragraph segments, sentences, eras.

All offsets are Unicode code points into the string they name (original or redacted).
Every unit downstream is addressed in redacted coordinates.
"""

import re
from dataclasses import asdict, dataclass

from .artifacts import ERA_ID
from .errors import UsageError

__all__ = ["CLOSERS", "REMOVED_MARK", "ZONING_CHUNK", "Segment", "Sentence", "as_dicts", "effective_label",
           "label_coverage", "name_pattern", "redact", "render_numbered", "render_zoned", "segment", "sentences",
           "to_original", "validate_eras", "zoning_chunks"]

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n\s*")
# A sentence ends at . ! ? (plus any closing quotes or brackets) followed by whitespace.
CLOSERS = "\"'\u2019\u201d)]"
_SENTENCE_END = re.compile(rf"[.!?]+[{re.escape(CLOSERS)}]*(?=\s)")
# Tokens whose trailing full stop is not a sentence end.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "st", "capt", "col", "gen", "rev", "esq", "jr", "sr", "vs", "etc", "&c",
})

REMOVED_MARK = "[…]"


@dataclass(frozen=True)
class Segment:
    id: str
    start: int
    end: int


@dataclass(frozen=True)
class Sentence:
    id: str
    segment_id: str
    start: int
    end: int


def as_dicts(units) -> list[dict]:
    return [asdict(u) for u in units]


def name_pattern(names) -> re.Pattern:
    """Whole-word, case-sensitive matching of any of `names`, longest first: what redaction replaces and what the
    prompt audit looks for. `names` is non-empty."""
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def redact(original: str, names: dict[str, str]) -> tuple[str, list[dict]]:
    """Replace whole-word, case-sensitive names, longest first, in one pass.

    Returns the redacted text and one span per replacement mapping redacted [start, end)
    to original [start, end), so any redacted offset can be traced back.
    """
    if not names:
        return original, []
    for name in names:
        if not name.strip():
            raise UsageError("redactions.json: empty name")
    pieces, spans, cursor, shift = [], [], 0, 0
    for m in name_pattern(names).finditer(original):
        replacement = names[m.group(0)]
        pieces += [original[cursor:m.start()], replacement]
        red_start = m.start() + shift
        spans.append({"red_start": red_start, "red_end": red_start + len(replacement),
                      "orig_start": m.start(), "orig_end": m.end()})
        shift += len(replacement) - (m.end() - m.start())
        cursor = m.end()
    pieces.append(original[cursor:])
    return "".join(pieces), spans


def to_original(pos: int, spans: list[dict]) -> int:
    """Map a redacted offset to the original. Inside a replacement it maps to the name's start."""
    shift = 0
    for s in spans:
        if pos < s["red_start"]:
            break
        if pos < s["red_end"]:
            return s["orig_start"]
        shift = s["orig_end"] - s["red_end"]
    return pos + shift


def segment(text: str) -> list[Segment]:
    """Paragraphs split on blank lines, trimmed of surrounding whitespace."""
    segments, start = [], 0
    for brk in [*_PARAGRAPH_BREAK.finditer(text), None]:
        end = brk.start() if brk else len(text)
        chunk = text[start:end]
        lead = len(chunk) - len(chunk.lstrip())
        trail = len(chunk.rstrip())
        if trail > lead:
            segments.append(Segment(f"p{len(segments) + 1:04d}", start + lead, start + trail))
        start = brk.end() if brk else len(text)
    return segments


def _is_abbreviation(text: str, dot: int) -> bool:
    word = re.search(r"([&\w]+)$", text[:dot])
    if not word:
        return False
    token = word.group(1)
    return token.lower() in _ABBREVIATIONS or (len(token) == 1 and token.isupper())


def sentences(text: str, segments: list[Segment]) -> list[Sentence]:
    out = []
    for seg in segments:
        start = seg.start
        body = text[seg.start:seg.end]
        for m in _SENTENCE_END.finditer(body):
            if m.group(0).rstrip(CLOSERS) == "." and _is_abbreviation(body, m.start()):
                continue
            end = seg.start + m.end()
            out.append(Sentence(f"s{len(out) + 1:05d}", seg.id, start, end))
            start = end
            while start < seg.end and text[start].isspace():
                start += 1
        if start < seg.end:
            out.append(Sentence(f"s{len(out) + 1:05d}", seg.id, start, seg.end))
    return out


def effective_label(era: dict) -> str:
    """What raters see for an era: its label, or its id when the label is empty."""
    label = era.get("label")
    return label if isinstance(label, str) and label else str(era.get("id", ""))


def validate_eras(eras: list[dict], segments: list[Segment]) -> None:
    """Every segment in exactly one era, eras contiguous and in text order."""
    order = [s.id for s in segments]
    seen: list[str] = []
    ids = set()
    for era in eras:
        if not isinstance(era, dict):
            raise UsageError("eras.json: every era is an object")
        for key in ("id", "label", "date_range", "segment_ids"):
            if key not in era:
                raise UsageError(f"eras.json: era missing {key!r}")
        if not isinstance(era["id"], str) or not ERA_ID.fullmatch(era["id"]):
            raise UsageError("eras.json: era ids are 1-32 letters, digits, '-' or '_', starting with a letter or digit")
        if not isinstance(era["label"], str) or not isinstance(era["date_range"], str) or \
                not isinstance(era["segment_ids"], list) or not all(isinstance(x, str) for x in era["segment_ids"]):
            raise UsageError("eras.json: label and date_range are text, segment_ids a list of segment ids")
        # Era ids name files; on a case-insensitive disk `e1` and `E1` are one file.
        if era["id"].casefold() in ids:
            raise UsageError("eras.json: era ids must differ by more than letter case")
        ids.add(era["id"].casefold())
        if not era["segment_ids"]:
            raise UsageError(f"eras.json: era {era['id']!r} has no segments")
        seen += era["segment_ids"]
    if seen != order:
        raise UsageError("eras.json: eras must cover every segment exactly once, in text order")


# The most sentences one zoning call labels, so every label list stays small and checkable.
ZONING_CHUNK = 300


def zoning_chunks(sents: list[Sentence], limit: int | None = None) -> list[list[Sentence]]:
    """An era's sentences in consecutive chunks of at most `limit`, breaking between segments where it can and
    inside a segment longer than `limit` only at sentence boundaries. Deterministic: estimate, preflight and
    zoning all use this one partition."""
    limit = limit or ZONING_CHUNK
    chunks: list[list[Sentence]] = []
    for seg_id in dict.fromkeys(s.segment_id for s in sents):
        seg = [s for s in sents if s.segment_id == seg_id]
        if chunks and len(chunks[-1]) + len(seg) <= limit:
            chunks[-1] += seg
            continue
        chunks += [seg[i:i + limit] for i in range(0, len(seg), limit)]
    return chunks


def label_coverage(expected: set[str]):
    """The validator for a sentence-label output (the zoner's, a calibration judge's): every sentence it was shown
    labelled exactly once, and nothing else."""
    def validate(out: dict) -> list[str]:
        got = [lab["sentence_id"] for lab in out["labels"]]
        if len(got) != len(set(got)) or set(got) != expected:
            return [f"labels must cover the chunk's {len(expected)} sentence ids exactly once"]
        return []
    return validate


def render_numbered(text: str, sents: list[Sentence]) -> str:
    """The zoner's view: every sentence of a chunk, one per line, prefixed by its id."""
    return "".join(f"[{s.id}] {text[s.start:s.end]}\n" for s in sents)


def render_zoned(text: str, segment_ids: list[str], sents: list[Sentence], kept: set[str]) -> str:
    """A rater's view of one era: kept sentences under segment headers, removed runs as one mark."""
    by_segment: dict[str, list[Sentence]] = {}
    for s in sents:
        by_segment.setdefault(s.segment_id, []).append(s)
    lines = []
    for seg_id in segment_ids:
        lines.append(f"## {seg_id}")
        removed_run = False
        for s in by_segment.get(seg_id, []):
            if s.id in kept:
                lines.append(f"[{s.id}] {text[s.start:s.end]}")
                removed_run = False
            elif not removed_run:
                lines.append(REMOVED_MARK)
                removed_run = True
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
