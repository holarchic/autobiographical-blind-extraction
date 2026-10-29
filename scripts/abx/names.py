"""The candidate-name scan `prepare` runs on first use: local and deterministic, no model. It pre-fills the unapproved
redaction list with placeholders and lists every candidate in a preview; the user deletes what isn't a person, adds
what it missed, and approves.

A candidate is a run of one to three capitalised words seen somewhere other than the start of a sentence or a line,
where capitals are ordinary. A title word before a run marks it titled and is not part of it. A candidate only ever
seen right after `in`, `from` or `to` is likely a place: listed, not pre-filled. Counts use the whole-word,
case-sensitive matching that redaction uses.
"""

import re
from collections.abc import Sequence
from typing import NamedTuple

from .text import Sentence, name_pattern

__all__ = ["Candidate", "prefill", "preview", "scan"]

MAX_RUN = 3
# The first stem no chosen name uses, so no placeholder contains a name on the list. The last holds a digit, which no
# scanned word does, so it can never collide.
PLACEHOLDER_STEMS = ("Name", "Person", "Someone", "Party", "Name0")
TITLES = frozenset({"Mr", "Mrs", "Ms", "Miss", "Dr", "Sir", "Lady", "Lord", "Aunt", "Uncle", "Captain", "Rev", "St"})
FUNCTION_WORDS = frozenset({"The", "A", "An", "And", "But", "Or", "In", "On", "At", "To", "Of", "For", "With", "My",
                            "Our", "His", "Her", "Their", "That", "This", "These", "Those", "Without", "When", "If",
                            "As", "So", "It", "We", "He", "She", "They", "You", "There", "Here", "What", "Which",
                            "Who", "Not", "No", "All", "Some"})
NOT_NAMES = frozenset({"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
                       "February", "March", "April", "May", "June", "July", "August", "September", "October",
                       "November", "December", "God", "Christmas", "Easter", "English", "French", "Mother", "Father",
                       "Mum", "Dad"}) | TITLES | frozenset({
    # Nationalities, languages and faiths that are never given names (so not Christian or Norman).
    "American", "British", "Scottish", "Irish", "Welsh", "German", "Dutch", "Spanish", "Italian", "Portuguese", "Greek",
    "Turkish", "Kurdish", "Arabic", "Persian", "Russian", "Polish", "Swedish", "Norwegian", "Danish", "Finnish",
    "Swiss", "Austrian", "Belgian", "Hungarian", "Czech", "Romanian", "Bulgarian", "Serbian", "Croatian", "Ukrainian",
    "Chinese", "Japanese", "Korean", "Indian", "Pakistani", "Australian", "Canadian", "Mexican", "Brazilian",
    "African", "European", "Asian", "Latin", "Muslim", "Jewish", "Catholic", "Protestant"})
# A run right after one of these is likely a place.
LOCATIVES = frozenset({"in", "from", "to"})
_TOKEN = re.compile(r"[^\W\d_](?:[^\W\d_]|['’\-])*")
_CONTRACTION = re.compile(r"I['’]")
# A bracketed span on one line (a source label, an editor's note) is not prose; its words are never scanned.
_BRACKETED = re.compile(r"\[[^\[\]\n]*\]")
_OPENERS = " \t\n\"'“‘([{"
# A capital after one of these inside a sentence starts a clause, not a name.
_CLAUSE_BREAKS = ":;—–-"
# A title word stands alone: not the tail of a longer word (`O'Miss`, `Smith-Miss`); an opening quote before it is fine.
_ENDS_IN_TITLE = re.compile(rf"(?<![\w\-])(?<!\w['’])(?:{'|'.join(sorted(TITLES))})\.$")


class Candidate(NamedTuple):
    name: str
    count: int
    titled: bool
    first_segment: str
    place_like: bool = False  # every sighting right after `in`, `from` or `to`, and never titled


class _Word(NamedTuple):
    text: str
    start: int
    end: int
    possessive: bool


def _words(body: str) -> list[_Word]:
    out = []
    for m in _TOKEN.finditer(body):
        raw = m.group(0)
        word = raw[:-2] if raw.endswith(("'s", "’s")) else raw.rstrip("'’-")
        out.append(_Word(word, m.start(), m.end(), word != raw.rstrip("-")))
    return out


def _capitalised(word: str) -> bool:
    return bool(word) and word[0].isupper() and any(c.islower() for c in word) and not _CONTRACTION.match(word)


class _Sighting(NamedTuple):
    name: str
    titled: bool
    locative: bool


def _sightings(body: str, not_names: frozenset[str]) -> list[_Sighting]:
    """Each run in one sentence that does not start it or a line in it."""
    words = _words(body)
    out = []
    i = 0
    while i < len(words):
        word = words[i]
        gap = body[word.end:words[i + 1].start] if i + 1 < len(words) else ""
        titled = word.text in TITLES and gap in (" ", ". ") and _capitalised(words[i + 1].text) and \
            words[i + 1].text not in TITLES
        if titled:
            i += 1
        elif not _capitalised(word.text):
            i += 1
            continue
        before = body[:words[i].start].rstrip(_OPENERS)
        line_start = "\n" in body[len(before):words[i].start]
        initial = not titled and (not before or before[-1] in _CLAUSE_BREAKS or line_start)
        locative = i > 0 and words[i - 1].text in LOCATIVES and body[words[i - 1].end:words[i].start] == " "
        run = [words[i]]
        while len(run) < MAX_RUN and not run[-1].possessive and i + 1 < len(words) and \
                body[words[i].end:words[i + 1].start] == " " and _capitalised(words[i + 1].text):
            i += 1
            run.append(words[i])
        while i + 1 < len(words) and body[words[i].end:words[i + 1].start] == " " and \
                _capitalised(words[i + 1].text) and not words[i].possessive:
            i += 1  # a run longer than MAX_RUN words keeps its first ones; the rest are not a run of their own
        i += 1
        texts = [w.text for w in run]
        while texts and (texts[0] in FUNCTION_WORDS or texts[0] in not_names):
            texts.pop(0)
        if initial or not texts or (len(texts) == 1 and (texts[0] in NOT_NAMES or texts[0] in not_names)):
            continue
        out.append(_Sighting(" ".join(texts), titled, locative and not titled))
    return out


def _units(text: str, sentences: Sequence[Sentence]) -> list[tuple[str, str]]:
    """(segment id, text) per sentence, with a sentence that ends in a dotted title joined to the next one in its
    paragraph: the splitter ends a sentence at `Miss.` but the scan must see the name after it."""
    units: list[tuple[str, str]] = []
    joining = False
    start = 0
    for sent in sentences:
        if joining and units and units[-1][0] == sent.segment_id:
            units[-1] = (sent.segment_id, text[start:sent.end])  # the original spacing between the two
        else:
            start = sent.start
            units.append((sent.segment_id, text[start:sent.end]))
        joining = bool(_ENDS_IN_TITLE.search(units[-1][1]))
    return units


def scan(text: str, sentences: Sequence[Sentence], not_names: Sequence[str] = ()) -> list[Candidate]:
    """Every candidate, in order of first sighting. `not_names` (the user's) are dropped as non-names would be, and
    stripped from the front of a run as function words are."""
    first: dict[str, str] = {}
    titled: set[str] = set()
    elsewhere: set[str] = set()  # seen at least once not right after a locative
    # Blanked over the whole text, before it is cut into sentences, so a full stop inside a label cannot split it;
    # offsets are kept, and counts read the original.
    masked = _BRACKETED.sub(lambda m: " " * len(m.group(0)), text)
    for segment_id, body in _units(masked, sentences):
        for seen in _sightings(body, frozenset(not_names)):
            first.setdefault(seen.name, segment_id)
            if seen.titled:
                titled.add(seen.name)
            if not seen.locative:
                elsewhere.add(seen.name)
    return [Candidate(name, len(name_pattern([name]).findall(text)), name in titled, segment, name not in elsewhere)
            for name, segment in first.items()]


def prefill(candidates: Sequence[Candidate]) -> dict[str, str]:
    """Placeholders for the candidates seen twice or after a title and not place-like, numbered by first sighting; a
    one-word candidate that is a word of exactly one chosen multi-word candidate shares its placeholder."""
    chosen = [c.name for c in candidates if (c.count >= 2 or c.titled) and not c.place_like]
    multi = [n for n in chosen if " " in n]
    owner = {n: owners[0] for n in chosen if " " not in n
             for owners in [[m for m in multi if n in m.split(" ")]] if len(owners) == 1}
    stem = next(s for s in PLACEHOLDER_STEMS if not any(s in n.split(" ") for n in chosen))
    placeholders: dict[str, str] = {}
    for name in chosen:
        if name not in owner:
            placeholders[name] = f"{stem} {len(placeholders) + 1}"
    for name, full in owner.items():
        placeholders[name] = placeholders[full]
    return {name: placeholders[name] for name in chosen}


def preview(candidates: Sequence[Candidate], names: dict[str, str]) -> str:
    """Pre-filled candidates by count, then likely places, then those seen once, both left out."""
    kept = sorted((c for c in candidates if c.name in names), key=lambda c: (-c.count, c.name))
    places = sorted((c for c in candidates if c.name not in names and c.place_like), key=lambda c: (-c.count, c.name))
    rest = [c for c in candidates if c.name not in names and not c.place_like]
    lines = ["Pre-filled in redactions.json (name, times in the text, replacement, first paragraph seen mid-sentence):"]
    lines += [f"{c.name}\t{c.count}\t{names[c.name]}\t{c.first_segment}" + ("\t(after a title)" if c.titled else "")
              for c in kept]
    lines += ["", "Seen only right after in, from or to: likely places, not pre-filled (name, times in the text, first "
              "paragraph; add any that are people you want hidden):"]
    lines += [f"{c.name}\t{c.count}\t{c.first_segment}" for c in places]
    lines += ["", "Seen once and not pre-filled (add any that are people you want hidden):"]
    lines += [f"{c.name}\t{c.first_segment}" for c in rest]
    return "\n".join(lines) + "\n"
