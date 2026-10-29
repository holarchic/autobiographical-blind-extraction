"""Outputs the fake CLIs generate, by role, from the prompt they receive.

Deterministic in (prompt, lineage, occurrence), and shaped so the offline smoke exercises
every path: lineage-specific traits (one-lineage discovery), contested and not-shown verdicts
(insufficient and absent agree under the binary reading), run-to-run drift, a fade, an emergence
and a held era, insufficient facets, and prediction matches.
"""

import hashlib
import json
import os
import re

ROLE_LINE = re.compile(r"^# abx-role: ([a-z_]+)", re.M)
SENT = re.compile(r"^\[(s\d{5})\]", re.M)
TRAITS = ["Thrift with money", "Love of reading", "Drive to argue", "Self-directed study", "Resentment of authority",
          "Practical ingenuity", "Concern for reputation", "Willingness to leave home"]
# Things that happened to the writer, not traits: both lineages discover them and the codebook author tags them
# events, so they are recoded and reported but kept out of the gate and every comparison. Both lineages agree on them
# in every era, so the events alpha is 1 while the gates are not. ABX_FAKE_KINDS=all_events or all_traits tags every
# construct one way, for the empty-side cases.
EVENTS = ["Death of a parent", "Serious illness"]
FACETS = [f"{d}{i}" for d in "NEOAC" for i in range(1, 7)]
# Verdicts in the first era and in every later one, for both lineages and every run, so the temporal read has a fade,
# an emergence and a held era whatever the hashes give.
TRAJECTORIES = {"Thrift with money": {"first": "present", "later": "absent"},
                "Concern for reputation": {"first": "absent", "later": "present"},
                "Willingness to leave home": {"first": "present", "later": "insufficient"},
                "Death of a parent": {"first": "present", "later": "present"},
                "Serious illness": {"first": "present", "later": "absent"}}


def h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def role_of(prompt: str) -> str:
    m = ROLE_LINE.search(prompt)
    return m.group(1) if m else "unknown"


def segments(prompt: str) -> list[list[str]]:
    """Sentence ids grouped by `## pNNNN` header, in prompt order."""
    groups: list[list[str]] = []
    for line in prompt.splitlines():
        if re.match(r"^## p\d{4}$", line):
            groups.append([])
        elif groups and SENT.match(line):
            groups[-1].append(line[1:7])
    return [g for g in groups if g]


def era_label(prompt: str) -> str:
    m = re.search(r"^Era: (.+)$", prompt, re.M)
    return m.group(1) if m else ""


def _two_episodes(segs: list[list[str]]) -> list[dict]:
    return [{"sentence_ids": [segs[0][0]]}, {"sentence_ids": [segs[-1][0]]}]


def discovery(prompt, lineage, occurrence):
    segs = segments(prompt)
    names = (TRAITS[:6] if lineage == "claude" else TRAITS[2:]) + EVENTS
    return {"bias_memo": "I lean toward reading recurring conduct as disposition.",
            "traits": [{"trait": n, "alexander_id": "frequency", "occurrences": _two_episodes(segs)} for n in names],
            "hypotheses": [f"The next era will show more {names[0].lower()}."]}


def codebook_author(prompt, lineage, occurrence):
    groups: dict[str, list[str]] = {}
    for ref, trait, _ in re.findall(r"^(c\d{3,4}) \| (.+?) \| (\w+)$", prompt, re.M):
        groups.setdefault(trait, []).append(ref)
    kinds = os.environ.get("ABX_FAKE_KINDS")

    def kind(trait):
        return "event" if kinds == "all_events" or (kinds != "all_traits" and trait in EVENTS) else "trait"
    return {"constructs": [{"id": f"C{i + 1:02d}", "name": trait, "kind": kind(trait),
                            "definition": f"Shows {trait.lower()} in conduct.",
                            "sufficiency_rule": f"Two separate episodes of {trait.lower()}.", "trace": refs}
                           for i, (trait, refs) in enumerate(groups.items())],
            "appendix": []}


def foil_author(prompt, lineage, occurrence):
    """A clear and a near-miss foil, plus an anchored one when the era has paragraphs far enough apart."""
    segs = segments(prompt)
    era = era_label(prompt)
    out = [{"id": "F1", "type": "clear", "name": f"Love of solitude {era}", "definition": "Seeks time alone.",
            "sufficiency_rule": "Two separate retreats from company.", "support": [],
            "counter_sentence_ids": [segs[0][0]]},
           {"id": "F2", "type": "near_miss", "name": f"Fondness for gambling {era}",
            "definition": "Stakes money on chance.", "sufficiency_rule": "Two separate wagers.",
            "support": [{"sentence_ids": [segs[0][0]], "event": "a risky bargain"}],
            "counter_sentence_ids": [segs[-1][0]]}]
    if len(segs) >= 3:
        out.append({"id": "F3", "type": "anchored", "name": f"Habit of early rising {era}",
                    "definition": "Starts the day early.", "sufficiency_rule": "Two separate early starts.",
                    "support": [{"sentence_ids": [segs[0][0]], "event": "one morning"},
                                {"sentence_ids": [segs[-1][0]], "event": "another morning"}],
                    "counter_sentence_ids": []})
    return {"foils": out}


def auditor(prompt, lineage, occurrence):
    """Keeps every foil but the near-miss of one Franklin era (by a hash of its label). Flags the first construct in
    every era, or with ABX_FAKE_PLANTED only in an era whose text holds that planted sentence, citing it."""
    segs = segments(prompt)
    era = era_label(prompt)
    decisions = [{"foil_id": fid, "decision": "drop" if (kind == "near_miss" and h(era, "drop") % 2 == 0) else "keep",
                  "reason": "judged against the era"} for fid, kind in re.findall(r"^- (F\d) \((\w+),", prompt, re.M)]
    constructs = re.findall(r"^- (C\d{2}): ", prompt, re.M)
    planted = os.environ.get("ABX_FAKE_PLANTED")
    if planted:
        hit = re.search(rf"^\[(s\d{{5}})\] .*{re.escape(planted)}", prompt, re.M)
        flags = [{"construct_id": constructs[0], "counter_sentence_ids": [hit.group(1)], "reason": "contradicts it"}] \
            if hit and constructs else []
    else:
        flags = [{"construct_id": constructs[0], "counter_sentence_ids": [segs[0][0]], "reason": "contradicts it"}] \
            if constructs else []
    return {"foils": decisions, "flags": flags}


def facet_mapper(prompt, lineage, occurrence):
    """The first construct maps to no facet; the rest to one or two facets by a hash of the name, either polarity."""
    out = []
    for i, (cid, name) in enumerate(re.findall(r"^- (C\d{2}): (.+?)\. ", prompt, re.M)):
        two = [FACETS[h(name) % 30], FACETS[h(name, "b") % 30]][:1 + h(name) % 2]
        picks = [] if i == 0 else list(dict.fromkeys(two))
        out.append({"construct_id": cid, "facets": [{"facet": f, "polarity": "+" if h(name, f) % 2 else "-"}
                                                    for f in picks]})
    return {"constructs": out}


def recode(prompt, lineage, occurrence):
    segs = segments(prompt)
    era = era_label(prompt)
    first_era = re.search(r"^## p0001$", prompt, re.M) is not None
    verdicts = []
    for item, name in re.findall(r"^- (K[0-9a-f]{6}): (.+?)\. ", prompt, re.M):
        base = "present" if h(name, era) % 3 else "absent"
        if lineage == "codex" and name == "Self-directed study":
            base = "absent" if base == "present" else "present"
        if lineage == "codex" and name == "Resentment of authority" and base == "absent":
            base = "insufficient"  # insufficient and absent agree under the binary reading
        if occurrence == 2 and h(name, era, "drift") % 4 == 0 and base != "insufficient":
            base = "absent" if base == "present" else "present"
        if name in TRAJECTORIES:
            base = TRAJECTORIES[name]["first" if first_era else "later"]
        episodes = _two_episodes(segs)
        if name == "Practical ingenuity" and lineage == "claude" and occurrence == 1:
            episodes = [{"sentence_ids": [segs[0][0]]}, {"sentence_ids": [segs[1][0]]}]  # one scene, adjacent
        verdicts.append({"item_id": item, "verdict": base,
                         "occurrences": [{**o, "event": "an episode"} for o in episodes] if base == "present" else [],
                         "counter_sentence_ids": [segs[0][0]] if base == "absent" else []})
    return {"verdicts": verdicts}


def ledger(prompt, lineage, occurrence):
    segs = segments(prompt)
    facets = []
    for f in FACETS:
        pole = h(f) % 3
        evidence = [] if pole == 2 else [{"sentence_ids": [segs[i][0]], "pole": "high" if pole == 0 else "low",
                                          "note": "conduct in the scene"} for i in (0, -1)]
        facets.append({"facet": f, "evidence": evidence})
    return {"facets": facets}


def scorer(prompt, lineage, occurrence):
    body = prompt[prompt.index("Ledgers:") + len("Ledgers:"):]
    ledgers = json.loads(body)
    tally: dict[str, int] = dict.fromkeys(FACETS, 0)
    cited: dict[str, list[str]] = {f: [] for f in FACETS}
    for era in ledgers:
        for fac in era["facets"]:
            for ev in fac["evidence"]:
                tally[fac["facet"]] += 1 if ev["pole"] == "high" else -1
                cited[fac["facet"]] += ev["sentence_ids"]
    scores = []
    donor = next((f for f in FACETS if cited[f]), None)
    for f in FACETS:
        if not cited[f] and os.environ.get("ABX_FAKE_CROSS_FACET") and donor:
            scores.append({"facet": f, "score": 7, "cited_sentence_ids": cited[donor][:1]})
            continue
        if not cited[f]:
            score = None
        else:
            score = 4 + tally[f]
            if lineage == "codex" and h(f) % 4 == 0:
                score += 1
            if occurrence == 1 and h(f, "run") % 5 == 0:
                score -= 1
            score = max(1, min(7, score))
            if lineage == "codex" and os.environ.get("ABX_FAKE_STRICT_MIRROR"):
                score = 8 - score
        stray = ["s99999"] if os.environ.get("ABX_FAKE_STRAY_CITATION") and score is not None else []
        scores.append({"facet": f, "score": score, "cited_sentence_ids": (cited[f] + stray)[:12]})
    return {"scores": scores}


def lenient_scorer(prompt, lineage, occurrence):
    """The strict fake's scores, nudged by a hash, plus facets the strict fake left insufficient scored from another
    facet's evidence, as a charitable reading would."""
    strict = scorer(prompt, lineage, occurrence)["scores"]
    donor = next((s["cited_sentence_ids"][:1] for s in strict if s["cited_sentence_ids"]), [])
    if os.environ.get("ABX_FAKE_LENIENT_SAME"):
        return {"scores": [{"facet": s["facet"], "score": 1 + h(s["facet"]) % 7, "cited_sentence_ids": donor}
                           for s in strict]}
    out = []
    for s in strict:
        if s["score"] is None:
            scored = donor and h(s["facet"], lineage, "lenient") % 2 == 0
            out.append({"facet": s["facet"], "score": 5 if scored else None,
                        "cited_sentence_ids": donor if scored else []})
            continue
        nudge = (h(s["facet"], lineage, occurrence, "lenient") % 3) - 1 if h(s["facet"]) % 2 else 0
        out.append({**s, "score": max(1, min(7, s["score"] + nudge))})
    return {"scores": out}


def crosswalk(prompt, lineage, occurrence):
    constructs = re.findall(r"^- (C\d{2}): (.+?)\.", prompt, re.M)
    predictions = re.findall(r"^(P\d{1,3}):\s*(\S.*)$", prompt, re.M)
    codes = []
    for pid, ptext in predictions:
        for cid, name in constructs:
            words = set(re.findall(r"[a-z]{5,}", name.lower()))
            hit = any(w in ptext.lower() for w in words)
            code = "match" if hit else "none"
            if lineage == "codex" and hit and h(pid, cid) % 3 == 0:
                code = "partial"
            codes.append({"prediction_id": pid, "construct_id": cid, "code": code, "polarity": "same"})
    # Each prediction tags one or two facets by a hash of its text; the codex coder flips the polarity of the
    # first prediction's first tag, so the coders disagree there.
    tags = []
    for i, (pid, ptext) in enumerate(predictions):
        picks = list(dict.fromkeys([FACETS[h(ptext) % 30], FACETS[h(ptext, "b") % 30]][:1 + h(ptext) % 2]))
        facets = [{"facet": f, "polarity": "+" if h(ptext, f) % 2 else "-"} for f in picks]
        if lineage == "codex" and i == 0 and facets:
            facets[0]["polarity"] = "-" if facets[0]["polarity"] == "+" else "+"
        tags.append({"prediction_id": pid, "facets": facets})
    return {"codes": codes, "predictions": tags}


def zoner_label(sentence_id: str) -> str:
    n = int(sentence_id[1:])
    return "remove" if n % 9 == 0 else "mixed" if n % 9 == 1 else "keep"


def calibration_judge(prompt, lineage, occurrence):
    """Agrees with the fake zoner once collapsed; codex calls mixed sentences remove, so three-label agreement drops.
    ABX_FAKE_JUDGE (`lineage:label,…`) makes a lineage give every sentence that label; ABX_FAKE_JUDGE_FLIP
    (`lineage:n`) makes it flip keep and remove on every nth sentence, so the judges dispute those."""
    forced = dict(item.split(":") for item in filter(None, os.environ.get("ABX_FAKE_JUDGE", "").split(",")))
    flips = dict(item.split(":") for item in filter(None, os.environ.get("ABX_FAKE_JUDGE_FLIP", "").split(",")))
    labels = []
    for i in SENT.findall(prompt):
        label = forced.get(lineage) or zoner_label(i)
        if lineage in flips and int(i[1:]) % int(flips[lineage]) == 0:
            label = "remove" if label == "keep" else "keep"
        labels.append({"sentence_id": i, "label": "remove" if lineage == "codex" and label == "mixed" else label})
    return {"labels": labels}


ROLES = {"calibration_judge": calibration_judge, "foil_author": foil_author, "auditor": auditor,
         "facet_mapper": facet_mapper,
         "discovery": discovery, "codebook_author": codebook_author, "recode": recode, "deductive_ledger": ledger,
         "deductive_scorer": scorer, "deductive_scorer_lenient": lenient_scorer, "crosswalk": crosswalk}


def respond(prompt: str, lineage: str, occurrence: int, leak: str | None):
    role = role_of(prompt)
    if role == "probe":
        return {"answer": leak or "NONE"}
    if role == "echo":
        return {"answer": f"{lineage}:{occurrence}"}
    if role == "zoner":
        return {"labels": [{"sentence_id": i, "label": zoner_label(i)} for i in SENT.findall(prompt)]}
    if role in ROLES:
        return ROLES[role](prompt, lineage, occurrence)
    raise SystemExit(f"fake: no response for role {role!r}")
