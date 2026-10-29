"""Zoning calibration at `prepare`: a held-out sample of the user's text, labelled blind by a judge on every lineage
and scored against the zoner. `freeze` refuses a workspace whose zoner failed.

The sample is stratified by the zoner's label (keep, or removed: remove and mixed) and then by era, and the rates are
conditional on that label and counted only where the two judges agree (a target they split on is disputed: in neither
count, and reported). They are sample rates against fixed limits: rounding and the one-per-era minimum give eras
unequal inclusion probabilities, so they are neither unbiased whole-text estimates nor confidence bounds.

A judge sees whole paragraphs around the sampled sentences and labels every sentence in them; only the sampled ones
are scored, so nothing in a judge's input shows which sentences count or how the zoner labelled any of them.
"""

import random
import shutil
from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple

from . import artifacts, config, eras, invoke, store, text
from .errors import GateFired, UsageError

__all__ = ["CONTEXT_YEARS", "EXCERPTS", "LIMITS", "MIN_HELD_OUT", "PATH", "Draw", "Target", "allocate", "check",
           "collapse", "excerpt_parts", "paragraph_context", "render_excerpt", "run", "sample", "score", "summary"]

PATH = "prepare/calibration.json"
EXCERPTS = artifacts.directory("excerpt")
HELD_OUT = 120
REMOVED_TARGETS = 60
MIN_HELD_OUT = 100
LIMITS: Mapping[str, float] = MappingProxyType({"false_keep": 0.05, "false_remove": 0.20})
STRATA = ("keep", "removed")


def collapse(label: str) -> str:
    """Remove and mixed are both removal (SPEC)."""
    return "keep" if label == "keep" else "removed"


def allocate(counts: Mapping[str, int], n: int) -> dict[str, int]:
    """`n` targets across eras (in `counts` order): one to every era with any, then the rest in proportion to each
    era's count by largest remainder (ties to the earlier era), never more than an era holds."""
    alloc = dict.fromkeys(counts, 0)
    n = min(n, sum(counts.values()))
    for era, count in counts.items():
        if count and sum(alloc.values()) < n:
            alloc[era] = 1
    while (left := n - sum(alloc.values())) > 0:
        room = [e for e in counts if counts[e] > alloc[e]]
        weight = sum(counts[e] for e in room)
        split = {e: divmod(left * counts[e], weight) for e in room}  # integers, so equal remainders tie exactly
        grant = {e: min(whole, counts[e] - alloc[e]) for e, (whole, _) in split.items()}
        for e in sorted((e for e in room if alloc[e] + grant[e] < counts[e]),
                        key=lambda e: (-split[e][1], room.index(e)))[:left - sum(grant.values())]:
            grant[e] += 1
        for e, g in grant.items():
            alloc[e] += g
    return alloc


class Draw(NamedTuple):
    targets: Mapping[str, list[str]]                    # era -> sampled sentence ids, in text order
    strata: Mapping[str, Mapping[str, Mapping[str, int]]]  # era -> stratum -> {population, sampled}


def sample(era_sents: Mapping[str, list[text.Sentence]], labels: Mapping[str, str], seed: str) -> Draw:
    """The held-out set: up to 60 zoner removals, keeps up to 120 in all, each stratum allocated across eras."""
    pools = {stratum: {era: sorted(s.id for s in sents if collapse(labels[s.id]) == stratum)
                       for era, sents in era_sents.items()} for stratum in STRATA}
    removed = min(REMOVED_TARGETS, sum(map(len, pools["removed"].values())))
    sizes = {"removed": removed, "keep": min(HELD_OUT - removed, sum(map(len, pools["keep"].values())))}
    rng = random.Random(seed)
    picked: dict[str, set[str]] = {era: set() for era in era_sents}
    strata: dict[str, dict] = {era: {} for era in era_sents}
    for stratum in STRATA:
        alloc = allocate({era: len(pool) for era, pool in pools[stratum].items()}, sizes[stratum])
        for era, pool in pools[stratum].items():
            picked[era] |= set(rng.sample(pool, alloc[era]))
            strata[era][stratum] = {"population": len(pool), "sampled": alloc[era]}
    return Draw({era: sorted(ids) for era, ids in picked.items()}, strata)


# The years of the nearest dated paragraph shown to a judge: its last few, so the line stays short.
CONTEXT_YEARS = 3


def paragraph_context(redacted: str, era_segments: list[text.Segment]) -> dict[str, str]:
    """For each paragraph of one era, the line a judge's excerpt puts before it: the nearest heading at or above it
    and the years of the nearest dated paragraph at or above it, both within the era (a heading or dated paragraph is
    its own nearest); "" when there is neither. It depends on the paragraph alone, never on which sentences are
    scored, and is drawn from the redacted text."""
    heading, years, out = "", (), {}
    for seg, boundary in zip(era_segments, eras.candidates(redacted, era_segments), strict=True):
        heading = boundary.heading or heading
        years = boundary.years[-CONTEXT_YEARS:] or years
        parts = ([f'nearest heading above: "{heading}"'] if heading else []) + \
            ([f"years in the nearest dated paragraph above: {', '.join(map(str, years))}"] if years else [])
        out[seg.id] = f"-- paragraph context: {'; '.join(parts)} --\n" if parts else ""
    return out


def render_excerpt(redacted: str, part: list[text.Sentence], context: Mapping[str, str]) -> str:
    """An excerpt part as a judge sees it: numbered sentences, each paragraph led by its context line."""
    out, last = [], None
    for s in part:
        if s.segment_id != last:
            out.append(context[s.segment_id])
            last = s.segment_id
        out.append(text.render_numbered(redacted, [s]))
    return "".join(out)


def excerpt_parts(era_sents: list[text.Sentence], targets: set[str]) -> list[list[text.Sentence]]:
    """Each target's paragraph, clipped to its zoning chunk, in text order and split like zoning chunks."""
    shown: set[str] = set()
    for chunk in text.zoning_chunks(era_sents):
        segments = {s.segment_id for s in chunk if s.id in targets}
        shown |= {s.id for s in chunk if s.segment_id in segments}
    return text.zoning_chunks([s for s in era_sents if s.id in shown])


class Target(NamedTuple):
    sentence_id: str
    era: str
    zoner: str
    judges: Mapping[str, str]  # lineage -> label


def _consensus(t: Target) -> str:
    votes = {collapse(label) for label in t.judges.values()}
    return votes.pop() if len(votes) == 1 else "disputed"


def _rate(count: int, of: int) -> dict:
    return {"count": count, "of": of, "rate": count / of if of else None}


def score(targets: list[Target]) -> dict:
    """False keeps over the agreed zoner keeps, false removes over the agreed zoner removals. A target the judges
    dispute is in neither count and is reported per zoner label; `held_out`, its minimum and judge agreement count
    every target."""
    by_zoner = {s: [t for t in targets if collapse(t.zoner) == s] for s in STRATA}
    agreed = {s: [t for t in rows if _consensus(t) != "disputed"] for s, rows in by_zoner.items()}
    rates = {"false_keep": _rate(sum(_consensus(t) == "removed" for t in agreed["keep"]), len(agreed["keep"])),
             "false_remove": _rate(sum(_consensus(t) == "keep" for t in agreed["removed"]), len(agreed["removed"]))}
    if len(targets) < MIN_HELD_OUT:
        reason = "too_small"
    elif any(r["rate"] is None for r in rates.values()):
        reason = "undefined"
    elif any(rates[k]["rate"] > limit for k, limit in LIMITS.items()):
        reason = "over_limit"
    else:
        reason = None
    agreement = {"three_label": sum(len(set(t.judges.values())) == 1 for t in targets),
                 "collapsed": sum(_consensus(t) != "disputed" for t in targets)}
    return {"limits": dict(LIMITS), "held_out": len(targets), **rates,
            "disputed": {s: len(by_zoner[s]) - len(agreed[s]) for s in STRATA},
            "judge_agreement": {**{k: v / len(targets) if targets else None for k, v in agreement.items()},
                                "of": len(targets)},
            "passed": reason is None, "reason": reason}


def _judge(ws: store.Workspace, cfg: config.Config, lineage: str, era: str, part: int, ids: set[str]) -> dict:
    req = invoke.CallRequest(stage="prepare", role="calibration_judge", lineage=lineage, run=None, era=era,
                             generation="A", slots={"excerpt": f"excerpt:{era}:{part}"},
                             validate=text.label_coverage(ids), part=part)
    return {lab["sentence_id"]: lab["label"] for lab in invoke.call(ws, cfg, req)["labels"]}


def run(ws: store.Workspace, cfg: config.Config, eras: list[dict], sents: list[text.Sentence], redacted: str,
        labels: Mapping[str, str]) -> dict:
    """Draw, excerpt, judge on every lineage, score; the record is written only once every judge call finished."""
    ws.path(PATH).unlink(missing_ok=True)
    if ws.path(EXCERPTS).exists():
        shutil.rmtree(ws.path(EXCERPTS))
    era_sents = {e["id"]: [s for s in sents if s.segment_id in set(e["segment_ids"])] for e in eras}
    segments = {seg.id: seg for seg in text.segment(redacted)}
    context = {e["id"]: paragraph_context(redacted, [segments[sid] for sid in e["segment_ids"]]) for e in eras}
    draw = sample(era_sents, labels, "calibration:" + store.sha256_text(redacted))
    judged: dict[str, dict[str, str]] = {lin: {} for lin in cfg.lineages}
    parts = []
    for era, era_list in era_sents.items():
        for n, part in enumerate(excerpt_parts(era_list, set(draw.targets[era])), start=1):
            store.write_text_atomic(artifacts.path_of(ws, f"excerpt:{era}:{n}"),
                                    render_excerpt(redacted, part, context[era]))
            ids = [s.id for s in part]
            parts.append({"era": era, "part": n, "sentence_ids": ids})
            for lin in cfg.lineages:
                judged[lin] |= _judge(ws, cfg, lin, era, n, set(ids))
    targets = [Target(sid, era, labels[sid], {lin: judged[lin][sid] for lin in cfg.lineages})
               for era, ids in draw.targets.items() for sid in ids]
    record = {**score(targets), "judges": list(cfg.lineages), "strata": draw.strata, "parts": parts,
              "targets": [{**t._asdict(), "judges": dict(t.judges)} for t in targets]}
    store.write_json(ws.path(PATH), record)
    return record


SUMMARY_KEYS = ("limits", "held_out", "false_keep", "false_remove", "disputed", "judge_agreement", "judges", "passed",
                "reason")


def summary(rec: dict) -> dict:
    """The record without its per-sentence rows, as the report carries it."""
    return {k: rec[k] for k in SUMMARY_KEYS}


def check(ws: store.Workspace) -> None:
    """The gate: no real run on a zoner that failed calibration."""
    if not ws.path(PATH).exists():
        raise UsageError("zoning calibration has not run; rerun `prepare`")
    rec = store.read_json(ws.path(PATH))
    if not rec["passed"]:
        fk, fr = rec["false_keep"], rec["false_remove"]
        raise GateFired(f"zoning calibration failed ({rec['reason']}: {rec['held_out']} held-out sentences, false "
                        f"keeps {fk['count']} of {fk['of']}, false removals {fr['count']} of {fr['of']}; see "
                        f"{PATH}); no real run is permitted on this zoner")
