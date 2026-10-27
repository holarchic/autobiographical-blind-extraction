"""The zoned eras' sentence index and the citation rules every validator shares (extract, foils, audit).

A model cites sentences by id. A citation counts only if the sentence is in the zoned era the
call saw, and a recurring trait only if its occurrences sit in distinct, non-adjacent segments.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NamedTuple

from . import store

__all__ = ["MAX_NAMED", "Bar", "Index", "apply_bar", "cited", "load"]

# The most ids one validation message names per category; the stray-citation count is always complete.
MAX_NAMED = 20


@dataclass(frozen=True)
class Index:
    """Each era's segments, its kept (zoned) sentences, and where every sentence and segment sits."""

    eras: tuple[Mapping, ...]
    sentences: Mapping[str, dict]
    seg_index: Mapping[str, int]
    kept: Mapping[str, frozenset[str]]

    @property
    def era_ids(self) -> list[str]:
        return [e["id"] for e in self.eras]

    def seg_of(self, sentence_id: str) -> int:
        return self.seg_index[self.sentences[sentence_id]["segment_id"]]

    def where(self, sentence_id: str, era: str) -> str:
        """Where a sentence cited from `era` but outside its zoned text lies: zoned out of that same era
        (`removed:<era>`), in another era (`era:<id>`), or nowhere (`unknown`)."""
        s = self.sentences.get(sentence_id)
        home = next((e["id"] for e in self.eras if s and s["segment_id"] in e["segment_ids"]), None)
        return "unknown" if home is None else f"removed:{home}" if home == era else f"era:{home}"

    def outside(self, era: str, ids: list[str]) -> list[str]:
        """A validation problem naming every cited id the zoned era does not contain, or none."""
        bad = [i for i in ids if i not in self.kept[era]]
        if not bad:
            return []
        named = ", ".join(f"{i} ({self.where(i, era)})" for i in bad[:MAX_NAMED])
        return [f"cites {len(bad)} sentence id(s) outside the zoned era: {named}"]


def load(ws: store.Workspace) -> Index:
    eras = tuple(store.read_json(ws.path("prepare/eras.json"))["eras"])
    kept = {e["id"]: frozenset(sid for sid, lab in store.read_json(ws.path(f"prepare/zoning/{e['id']}.json")).items()
                               if lab == "keep") for e in eras}
    return Index(eras=tuple(MappingProxyType(e) for e in eras),
                 sentences=MappingProxyType({s["id"]: s for s in store.read_json(ws.path("prepare/sentences.json"))}),
                 seg_index=MappingProxyType({s["id"]: i for i, s in
                                             enumerate(store.read_json(ws.path("prepare/segments.json")))}),
                 kept=MappingProxyType(kept))


def cited(node, keys: tuple[str, ...]) -> list[str]:
    """Sentence ids cited anywhere under the given keys."""
    if isinstance(node, dict):
        return [x for k, v in node.items() for x in (v if k in keys and isinstance(v, list) else cited(v, keys))]
    if isinstance(node, list):
        return [x for v in node for x in cited(v, keys)]
    return []


def top_traits(out, cap: int):
    """A discovery output keeping at most `cap` traits: those cited in the most distinct occurrences, then the most
    distinct sentences, then the earliest listed, in their listed order. An output it cannot rank comes back unchanged
    for the schema to reject."""
    traits = out.get("traits") if isinstance(out, dict) else None
    if not isinstance(traits, list) or len(traits) <= cap:
        return out
    scores = []
    for t in traits:
        occ = t.get("occurrences") if isinstance(t, dict) else None
        if not isinstance(occ, list) or not all(isinstance(o, dict) and isinstance(o.get("sentence_ids"), list)
                                                for o in occ):
            return out
        cited = {frozenset(s for s in o["sentence_ids"] if isinstance(s, str)) for o in occ}
        scores.append((len(cited), len(set().union(*cited))))
    ranked = sorted(range(len(traits)), key=lambda i: (-scores[i][0], -scores[i][1], i))
    return {**out, "traits": [traits[i] for i in sorted(ranked[:cap])]}


class Bar(NamedTuple):
    verdict: str
    downgrade: str | None  # why a present or absent verdict became insufficient, or None


def apply_bar(v: dict, seg_of) -> Bar:
    """The script's verdict: present needs two occurrences in distinct, non-adjacent segments; absent a counter."""
    if v["verdict"] == "present":
        segs = [{seg_of(s) for s in occ["sentence_ids"]} for occ in v["occurrences"]]
        separate = any(all(abs(a - b) >= 2 for a in x for b in y) for i, x in enumerate(segs) for y in segs[i + 1:])
        return Bar("present", None) if separate else Bar("insufficient", "present_without_two_separate_episodes")
    if v["verdict"] == "absent":
        return Bar("absent", None) if v["counter_sentence_ids"] else Bar("insufficient",
                                                                         "absent_without_counter_sentence")
    return Bar("insufficient", None)
