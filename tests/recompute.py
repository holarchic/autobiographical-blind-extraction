"""Rebuild every report figure from a finished workspace's raw files, independently of `unmask.compute`.

Shared by the offline smoke (`test_end_to_end`) and the live smoke (`live_smoke.sh`). Corpus
statistics come from the text units and zoning labels, agreement from the raw ratings, comparisons
from the sealed inputs and crosswalk codes. The rebuilt results must equal `results.json` field by
field, and `report.md` must equal the report rendered from them, so every number is checked in its
own row.
"""

import itertools
import json
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from abx import agreement, calibration, config, extract, facetmap, invoke, ipip, prompts, report, seal, store, unmask
from abx.errors import AbxError, IntegrityError

__all__ = ["compare", "expected", "problems"]


def _corpus(ws: store.Workspace) -> dict:
    redacted = ws.path("prepare/redacted.txt").read_text(encoding="utf-8")
    sentences = store.read_json(ws.path("prepare/sentences.json"))
    stats = {}
    for era in store.read_json(ws.path("prepare/eras.json"))["eras"]:
        labels = store.read_json(ws.path(f"prepare/zoning/{era['id']}.json"))
        mine = [s for s in sentences if s["segment_id"] in set(era["segment_ids"])]
        words = {s["id"]: len(redacted[s["start"]:s["end"]].split()) for s in mine}
        total = sum(words.values())
        kept = sum(n for sid, n in words.items() if labels[sid] == "keep")
        ratio = kept / total if total else 0.0
        stats[era["id"]] = {"sentences": len(mine), "segments": len(era["segment_ids"]), "words": total,
                            "kept_words": kept, "kept_ratio": ratio,
                            "mixed": sum(1 for lab in labels.values() if lab == "mixed"),
                            "removed": sum(1 for lab in labels.values() if lab == "remove"),
                            "band": "normal" if ratio >= 0.5 else "thin" if ratio >= 0.25 else "stop"}
    return stats


class _Judged(NamedTuple):
    era: str | None
    label: str | None


def _calibration(ws: store.Workspace) -> tuple[dict, list[str]]:
    """The calibration rebuilt from evidence: each recorded part's labels from both judges' frozen call records, each
    target's zoner label from the frozen zoning. Returns the summary and every way `calibration.json` departs from
    it: a part or target outside its era, a judge labelling a sentence twice or not at all, a repeated target, or a
    stored label that differs from its record."""
    rec = store.read_json(ws.path(calibration.PATH))
    eras = store.read_json(ws.path("prepare/eras.json"))["eras"]
    zoning = {e["id"]: store.read_json(ws.path(f"prepare/zoning/{e['id']}.json")) for e in eras}
    roster = sorted(config.load(ws).lineages)  # one judge per configured lineage, whatever the record claims
    out = [] if sorted(rec["judges"]) == roster else [f"calibration: judges {rec['judges']} are not {roster}"]
    judged = {lin: {} for lin in roster}
    for part in rec["parts"]:
        if not set(part["sentence_ids"]) <= set(zoning[part["era"]]):
            out.append(f"calibration: {part['era']} part {part['part']} holds sentences from another era")
        for lin in roster:
            path = ws.path(f"run/calls/prepare/calibration_judge/{lin}-r0-{part['era']}.{part['part']}.json")
            rows = store.read_json(path)["output"]["labels"]
            ids = [lab["sentence_id"] for lab in rows]
            if len(ids) != len(set(ids)) or sorted(ids) != sorted(part["sentence_ids"]):
                out.append(f"calibration: {lin}'s labels for {part['era']} part {part['part']} do not cover its "
                           "sentences exactly once")
            judged[lin] |= {lab["sentence_id"]: _Judged(part["era"], lab["label"]) for lab in rows}
    ids = [t["sentence_id"] for t in rec["targets"]]
    if len(ids) != len(set(ids)):
        out.append("calibration: a target is repeated")
    counts = {"fk": 0, "kept": 0, "fr": 0, "removed": 0, "same": 0, "same_side": 0, "kept_all": 0, "removed_all": 0}
    for t in {t["sentence_id"]: t for t in rec["targets"]}.values():
        sid, era = t["sentence_id"], t["era"]
        labels = {lin: judged[lin].get(sid, _Judged(None, None)) for lin in roster}
        if sid not in zoning[era] or t["zoner"] != zoning[era][sid] or sorted(t["judges"]) != roster or \
                any(labels[lin] != _Judged(era, t["judges"][lin]) for lin in roster):
            out.append(f"calibration: target {sid} does not match its zoning and judge records")
            continue
        votes = [j.label for j in labels.values()]
        sides = {"keep" if v == "keep" else "removed" for v in votes}
        counts["same"] += len(set(votes)) == 1
        counts["same_side"] += len(sides) == 1
        stratum = "kept" if zoning[era][sid] == "keep" else "removed"
        counts[f"{stratum}_all"] += 1
        if len(sides) > 1:
            continue  # disputed: in neither rate
        counts[stratum] += 1
        if stratum == "kept":
            counts["fk"] += sides == {"removed"}
        else:
            counts["fr"] += sides == {"keep"}
    n = counts["kept_all"] + counts["removed_all"]
    fk = {"count": counts["fk"], "of": counts["kept"],
          "rate": counts["fk"] / counts["kept"] if counts["kept"] else None}
    fr = {"count": counts["fr"], "of": counts["removed"],
          "rate": counts["fr"] / counts["removed"] if counts["removed"] else None}
    if n < 100:
        reason = "too_small"
    elif fk["rate"] is None or fr["rate"] is None:
        reason = "undefined"
    elif fk["rate"] > .05 or fr["rate"] > .2:
        reason = "over_limit"
    else:
        reason = None
    return {"limits": {"false_keep": .05, "false_remove": .2}, "held_out": n, "false_keep": fk, "false_remove": fr,
            "disputed": {"keep": counts["kept_all"] - counts["kept"],
                         "removed": counts["removed_all"] - counts["removed"]},
            "judge_agreement": {"three_label": counts["same"] / n if n else None,
                                "collapsed": counts["same_side"] / n if n else None, "of": n},
            "judges": roster, "passed": reason is None, "reason": reason}, out


def _mapper_output(ws: store.Workspace) -> dict:
    """The configured mapper's own call record, which generation B must have frozen."""
    rel = _record(ws, "extract", "facet_mapper", config.load(ws).facet_mapper, "B")
    if rel not in {p for e in store.read_manifest(ws) if e["gen"] == "B" for p in e["artifacts"]}:
        raise AbxError(f"{rel} is not frozen in generation B")
    rec = store.read_json(ws.path(rel))
    return {m["construct_id"]: m["facets"] for m in rec["output"]["constructs"]}


def _correspondences(ws: store.Workspace, book: dict, supported: set[str], facets: dict, reportable: bool) -> dict:
    """Rebuilt from the mapper's own call record and the IPIP facet scores."""
    mapping = _mapper_output(ws)
    rows, unmapped = [], []
    for con in sorted(book["constructs"], key=lambda x: x["id"]):
        if con["id"] not in supported:
            continue
        if not mapping[con["id"]]:
            unmapped.append(con["id"])
        for f in sorted(mapping[con["id"]], key=lambda x: x["facet"]):
            score = facets[f["facet"]]
            band = None if score is None else "low" if score * 3 <= 7 else "high" if score * 3 >= 11 else "mid"
            result = ("n/a" if band is None else "neutral" if band == "mid"
                      else "consistent" if (f["polarity"] == "+") == (band == "high") else "inconsistent")
            rows.append({"construct_id": con["id"], "name": con["name"], "facet": f["facet"],
                         "polarity": f["polarity"], "ipip": None if score is None else float(score), "band": band,
                         "result": result})
    counts = {k: sum(r["result"] == k for r in rows) for k in ("consistent", "inconsistent", "neutral", "n/a")}
    return {"rows": rows, "counts": counts, "unmapped": unmapped, "inductive_reportable": reportable}


def _coder_outputs(ws: store.Workspace) -> dict:
    """Each crosswalk coder's own call output, which generation D must have frozen."""
    frozen = {p for e in store.read_manifest(ws) if e["gen"] == "D" for p in e["artifacts"]}
    out = {}
    for coder in config.load(ws).crosswalk_coders:
        rel = _record(ws, "unmask", "crosswalk", coder, "D")
        if rel not in frozen:
            raise AbxError(f"{rel} is not frozen in generation D")
        out[coder] = store.read_json(ws.path(rel))["output"]
    return out


def _divergence(ws: store.Workspace, supported: set[str], by_lineage: dict, facets: dict, reportable: bool) -> dict:
    """The three-way map rebuilt from the coders' and the mapper's call records, the collapsed deductive scores and
    the IPIP facet scores, without `divergence.py`."""
    mapping = _mapper_output(ws)
    tags = {coder: {(p["prediction_id"], f["facet"]): f["polarity"] for p in out["predictions"] for f in p["facets"]}
            for coder, out in _coder_outputs(ws).items()}
    keys = sorted(set().union(*tags.values()))
    agreed, ambiguous = {}, []
    for pid, facet in keys:
        given = [t.get((pid, facet)) for t in tags.values()]
        if given.count(given[0]) == len(given):
            agreed.setdefault(facet, []).append({"prediction_id": pid, "polarity": given[0]})
        else:
            ambiguous.append({"prediction_id": pid, "facet": facet})

    def side(signs):
        poles = {"high" if x == "+" else "low" for x in signs}
        return None if not poles else "conflict" if len(poles) == 2 else poles.pop()

    def band(value, low, high):
        return None if value is None else "low" if value <= low else "high" if value >= high else "mid"

    order = ["divergent", "all three", "blind and self", "blind only", "self only", "test only"]
    rows = []
    for facet in sorted(facets):
        constructs = [{"construct_id": cid, "polarity": f["polarity"]} for cid in sorted(supported)
                      for f in mapping[cid] if f["facet"] == facet]
        vals = [by_lineage[lin].get(facet) for lin in sorted(by_lineage)]
        obs = sum(Fraction(v) for v in vals) / len(vals) if vals and None not in vals else None
        o_band, t_band = band(obs, 3, 5), band(facets[facet], Fraction(7, 3), Fraction(11, 3))
        ind = side([c["polarity"] for c in constructs])
        ded = o_band if o_band in ("high", "low") else None
        blind = "conflict" if ind == "conflict" or (ind and ded and ind != ded) else ind or ded
        predictions = agreed.get(facet, [])
        self_ = side([p["polarity"] for p in predictions])
        test = t_band if t_band in ("high", "low") else None
        present = [x for x in (blind, self_, test) if x]
        if not present:
            continue
        if "conflict" in present or len(set(present)) > 1:
            kind = "divergent"
        elif blind and self_:
            kind = "all three" if test else "blind and self"
        else:
            kind = "blind only" if blind else "self only" if self_ else "test only"
        rows.append({"facet": facet, "class": kind, "constructs": constructs, "inductive": ind,
                     "observer": None if obs is None else float(obs), "observer_band": o_band,
                     "predictions": predictions, "ipip": None if facets[facet] is None else float(facets[facet]),
                     "ipip_band": t_band, "blind": blind, "self": self_, "test": test})
    rows.sort(key=lambda r: (order.index(r["class"]), r["facet"]))
    return {"rows": rows, "counts": {c: sum(r["class"] == c for r in rows) for c in order}, "ambiguous": ambiguous,
            "inductive_reportable": reportable}


def _record(ws: store.Workspace, stage: str, role: str, lineage: str, generation: str) -> str:
    """Where `invoke` keeps a single-lineage call's record, by the call's own identity."""
    req = invoke.CallRequest(stage=stage, role=role, lineage=lineage, run=None, era=None, generation=generation,
                             slots={})
    return ws.rel(invoke.record_path(ws, req))


# The stages whose call records exist when `extract` runs its integrity audit.
AUDITED = ("prepare", "freeze", "extract")


def _audit(ws: store.Workspace, eras: list[str]) -> dict:
    """The integrity audit as it must have run inside `extract`, before generation C: over the prepare, freeze and
    extract call records, with generations A and B frozen, against the frozen qualification baseline."""
    records = [p for stage in AUDITED for p in sorted(ws.root.glob(f"run/calls/{stage}/**/*.json"))]
    bad_events = outside = 0
    for path in records:
        rec = store.read_json(path)
        evs = invoke.decode_events(rec.get("events"))
        if evs is None or invoke.outcome(rec["lineage"], rec.get("exit"), evs) != "success":
            bad_events += 1
        rater = (rec["lineage"], rec["run"]) if rec["run"] is not None else None
        try:
            prompts.check_allowlist(rec["role"], list(rec["input_artifacts"]), rater=rater, era=rec["era"],
                                    era_order=eras)
        except AbxError:
            outside += 1
    incidents = sum(1 for stage in AUDITED for p in ws.root.glob(f"run/incidents/{stage}/**/*.json")
                    if store.read_json(p)["kind"] == "tool_event")
    fresh = store.read_json(ws.path(extract.REQUALIFICATION_AUDIT))
    baseline = store.read_json(ws.path(invoke.QUALIFICATION_A))
    moved = sum(1 for lin, q in fresh.items() if any(q.get(k) != baseline[lin].get(k) for k in invoke.FINGERPRINT_KEYS))
    clean = not bad_events and not incidents
    return {
        "requalify": {"ok": not moved, "code": "fingerprint_moved" if moved else "requalified",
                      "counts": {"lineages_moved": moved}},
        "events": {"ok": clean, "code": "clean" if clean else "tool_events",
                   "counts": {"records": len(records), "records_with_tool_events": bad_events,
                              "tool_event_incidents": incidents}},
        "allowlist": {"ok": not outside, "code": "within_allowlists" if not outside else "outside_allowlist",
                      "counts": {"records": len(records), "outside": outside}},
        "manifest": {"ok": True, "code": "verified", "counts": {"generations": 2}},
    }


def _arm(arm: agreement.ArmResult) -> dict:
    return {"metric": arm.metric, "alpha": arm.alpha, "verdict": arm.verdict, "reason": arm.reason,
            "paired_fraction": arm.paired_fraction, "raw_agreement": arm.raw_agreement,
            "missing_rate": arm.missing_rate, "stability": dict(sorted(arm.stability.items())),
            "units": len(arm.units), "integrity_audit_due": arm.integrity_audit_due}


def _facet_bands(by_lineage: dict, self_report: dict, instrument: dict) -> dict:
    """Facet bands rebuilt from the lineage collapses and the IPIP facet scores with their own thresholds; the observer
    estimate is the two lineages' midpoint, which is their median (the config requires exactly two lineages)."""
    def band(value, low, high):
        return None if value is None else "low" if value <= low else "high" if value >= high else "mid"

    rows = {}
    for f in instrument["facets"]:
        vals = [by_lineage[lin].get(f) for lin in sorted(by_lineage)]
        obs = sum(Fraction(v) for v in vals) / len(vals) if vals and None not in vals else None
        own = self_report["facets"].get(f)
        o_band, s_band = band(obs, 3, 5), band(own, Fraction(7, 3), Fraction(11, 3))
        rows[f] = {"observer": None if obs is None else float(obs), "observer_band": o_band,
                   "ipip": None if own is None else float(own), "ipip_band": s_band,
                   "same_band": o_band == s_band if o_band and s_band else None}
    compared = [r["same_band"] for r in rows.values() if r["same_band"] is not None]
    return {"facets": rows, "observer_missing": [r["observer"] for r in rows.values()].count(None),
            "ipip_missing": [r["ipip"] for r in rows.values()].count(None), "compared": len(compared),
            "same_band_rate": compared.count(True) / len(compared) if compared else None}


def _spot_check(ws: store.Workspace) -> dict:
    """The user's marks, counted straight from the frozen file."""
    doc = store.read_json(ws.path("prepare/spot-check.json"))
    return {group: {"shown": len(doc[group]), "marked": [x["wrong"] for x in doc[group]].count(True)}
            for group in ("kept", "removed")}


def _stance_runs(ws: store.Workspace, prefix: str, stance: str) -> dict:
    """{lineage: {run: {facet: score}}} straight from the score files, checking each file's stance."""
    runs: dict = {}
    for path in sorted(ws.root.glob(f"run/deductive/{prefix}-*.json")):
        rec = store.read_json(path)
        if rec["stance"] != stance:
            raise IntegrityError(f"{path.name} holds {rec['stance']!r} scores, not {stance!r}")
        runs.setdefault(rec["lineage"], {})[rec["run"]] = {s["facet"]: s["score"] for s in rec["scores"]}
    return runs


def _median(values: list) -> int | None:
    valid = sorted(v for v in values if v is not None)
    if len(valid) < 2:
        return None
    mid = len(valid) // 2
    m = Fraction(valid[mid]) if len(valid) % 2 else Fraction(valid[mid - 1] + valid[mid], 2)
    return int(m + Fraction(1, 2))


def _stances(ws: store.Workspace, facets: list[str]) -> tuple[dict, dict]:
    """The lenient arm and strict against lenient, rebuilt from the score files with their own collapse."""
    strict, lenient = _stance_runs(ws, "scores", "strict"), _stance_runs(ws, "lenient", "lenient")
    arm = _arm(agreement.arm(lenient, facets, "ordinal"))
    del arm["integrity_audit_due"]
    collapsed = {name: {(lin, f): _median([by_run[r].get(f) for r in by_run]) for lin, by_run in runs.items()
                        for f in facets} for name, runs in (("strict", strict), ("lenient", lenient))}
    units = sorted(set(collapsed["strict"]) | set(collapsed["lenient"]))
    matrix = {name: {u: collapsed[name].get(u) for u in units} for name in collapsed}
    paired = [u for u in units if matrix["strict"][u] is not None and matrix["lenient"][u] is not None]
    alpha, reason = agreement.alpha(matrix, "ordinal")
    shifts = [matrix["lenient"][u] - matrix["strict"][u] for u in paired]
    return arm, {"alpha": alpha, "reason": reason, "units": len(units), "paired": len(paired),
                 "equal_rate": sum(1 for s in shifts if s == 0) / len(shifts) if shifts else None,
                 "mean_shift": sum(shifts) / len(shifts) if shifts else None}


def _invalid_outputs(ws: store.Workspace) -> dict[str, int]:
    """Counted from the incidents generations C and D froze, never from whatever the folder holds now."""
    manifest = [json.loads(line) for line in ws.path(store.MANIFEST).read_text().splitlines() if line.strip()]
    frozen = {e["gen"]: [rel for rel in e["artifacts"] if rel.startswith("run/incidents/")] for e in manifest}
    stages = {"prepare": frozen.get("C", []), "freeze": frozen.get("C", []), "extract": frozen.get("C", []),
              "unmask": frozen.get("D", [])}
    return {stage: sum(1 for rel in rels if rel.startswith(f"run/incidents/{stage}/")
                       and json.loads(ws.path(rel).read_text())["kind"] == "invalid_output")
            for stage, rels in stages.items()}


def _trimmed_outputs(ws: store.Workspace) -> dict[str, int]:
    """Counted from the call records any generation froze."""
    manifest = [json.loads(line) for line in ws.path(store.MANIFEST).read_text().splitlines() if line.strip()]
    records = [rel for e in manifest for rel in e["artifacts"] if rel.startswith("run/calls/")]
    return {stage: sum(1 for rel in records if rel.startswith(f"run/calls/{stage}/")
                       and "untrimmed_output" in json.loads(ws.path(rel).read_text()))
            for stage in ("prepare", "freeze", "extract", "unmask")}


def _foils(ws: store.Workspace, eras: list[str], ratings: extract.Ratings, lineages: list[str]) -> dict:
    """Kept foils re-derived from the authored foils and each era's audit, then their endorsement counted."""
    authored = {e: store.read_json(ws.path(f"prereg/foils/{e}.json"))["foils"] for e in eras}
    audits = {e: store.read_json(ws.path(f"run/audit/{e}.json")) for e in eras}
    drops = {e: {d["foil_id"]: d["reason"] for d in audits[e]["foils"] if d["decision"] == "drop"} for e in eras}
    kept = [{**f, "era": e} for e in eras for f in authored[e] if f["id"] not in drops[e]]
    endorsement = {}
    for kind in ("clear", "near_miss", "anchored"):
        mine = [f for f in kept if f["type"] == kind]
        by = {}
        for lin in lineages:
            got = [v["verdict"] for key, rows in ratings.verdicts.items() if key.lineage == lin
                   for f in mine if key.era == f["era"] for v in rows if v["item_id"] == f["item_id"]]
            present = got.count("present")
            by[lin] = {"verdicts": len(got), "present": present, "rate": present / len(got) if got else None}
        endorsement[kind] = {"items": len(mine), "by_lineage": by}
    flags: dict[str, list[str]] = {}
    for e in eras:
        for f in audits[e]["flags"]:
            flags.setdefault(f["construct_id"], []).append(e)
    return {"kept": kept, "foils": {"authored": sum(map(len, authored.values())),
                                    "dropped": [{"era": e, "id": i, "reason": r} for e in eras
                                                for i, r in sorted(drops[e].items())],
                                    "endorsement": endorsement},
            "audit_flags": dict(sorted(flags.items()))}


def _with_foils(ratings: extract.Ratings, book: dict, kept: list[dict], eras: list[str]) -> agreement.ArmResult:
    """The diagnostic arm rebuilt here: binary values, construct and kept-foil units in every era."""
    runs: dict = {}
    for key, rows in ratings.verdicts.items():
        cell = runs.setdefault(key.lineage, {}).setdefault(key.run, {})
        for v in rows:
            cell[(v["item_id"], key.era)] = "present" if v["verdict"] == "present" else "not_present"
    units = [(item["item_id"], era) for item in [*book["constructs"], *kept] for era in eras]
    return agreement.arm(runs, units, "nominal")


class _EraReading(NamedTuple):
    era: str
    reading: str


def _temporal(ratings: extract.Ratings, book: dict, eras: list[str], status: dict, reportable: bool) -> dict:
    """The temporal read, rebuilt with its own three-way collapse and its own walk over each construct's eras."""
    votes: dict = {}
    for key, rows in ratings.verdicts.items():
        for v in rows:
            votes.setdefault((key.lineage, v["item_id"], key.era), []).append(v["verdict"])
    lineages = sorted({key.lineage for key in ratings.verdicts})

    def absent(lineage, item_id, era):
        counts = Counter(votes.get((lineage, item_id, era), [])).most_common()
        return (sum(n for _, n in counts) >= 2 and counts[0][0] == "absent"
                and (len(counts) == 1 or counts[1][1] < counts[0][1]))

    rows = []
    for con in book["constructs"]:
        seq = ["observed" if status[con["id"]][e] == "supported"
               else "contradicted" if all(absent(lin, con["item_id"], e) for lin in lineages) else None for e in eras]
        decisive = [_EraReading(e, r) for e, r in zip(eras, seq, strict=True) if r]
        pairs = list(itertools.pairwise(decisive))
        fades = [{"from": a.era, "to": b.era} for a, b in pairs
                 if (a.reading, b.reading) == ("observed", "contradicted")]
        emergences = [{"from": a.era, "to": b.era} for a, b in pairs
                      if (a.reading, b.reading) == ("contradicted", "observed")]
        held = [e for i, e in enumerate(eras) if seq[i] is None and [r for r in seq[:i] if r][-1:] == ["observed"]]
        observed = [e for e, r in zip(eras, seq, strict=True) if r == "observed"]
        if seq[-1] == "observed":
            now = {"state": "observed", "era": eras[-1]}
        elif eras[-1] in held:
            now = {"state": "held", "era": observed[-1]}
        elif decisive and decisive[-1].reading == "contradicted" and observed:
            now = {"state": "faded", "era": fades[-1]["to"]}
        else:
            now = {"state": "never observed", "era": None}
        if any(seq):
            rows.append({"construct_id": con["id"], "eras": dict(zip(eras, seq, strict=True)), "fades": fades,
                         "emergences": emergences, "held": held, "now": now})
    return {"constructs": rows, "undecided": len(book["constructs"]) - len(rows), "inductive_reportable": reportable}


def expected(ws: store.Workspace) -> dict:
    """The results `unmask` must have produced, rebuilt from raw ratings and sealed inputs."""
    cfg = config.load(ws)
    book = store.read_json(ws.path("run/codebook/codebook.json"))
    eras = [e["id"] for e in store.read_json(ws.path("prepare/eras.json"))["eras"]]
    instrument = ipip.load_instrument(ws)
    inductive, deductive_arm = extract.arms(extract.load_ratings(ws), book, eras, list(instrument["facets"]))
    ratings = extract.load_ratings(ws)
    lineages = sorted(inductive.collapsed)
    status = {c["id"]: {e: agreement.status([inductive.collapsed[lin][extract.Unit(c["item_id"], e)]
                                            for lin in lineages]) for e in eras} for c in book["constructs"]}
    collapsed = [{"lineage": lin, "facet": f, "value": v}
                 for lin in sorted(deductive_arm.collapsed) for f, v in deductive_arm.collapsed[lin].items()]
    self_report = ipip.score(ipip.parse_responses(ws.path("sealed/ipip-120.csv").read_text(), instrument), instrument)
    inductive_ok = inductive.verdict in unmask.REPORTABLE
    supported = {cid for cid, by_era in status.items() if "supported" in by_era.values()}
    codes = {c: store.read_json(ws.path(f"run/crosswalk/codes-{c}.json"))["codes"] for c in cfg.crosswalk_coders}
    preds = list(seal.predictions(ws.path("sealed/self-prediction.md").read_text()))
    audit = _audit(ws, eras) if inductive.integrity_audit_due or deductive_arm.integrity_audit_due else None
    invalid = _invalid_outputs(ws)
    trimmed = _trimmed_outputs(ws)
    lenient_arm, stances = _stances(ws, list(instrument["facets"]))
    foiled = _foils(ws, eras, ratings, lineages)
    calibrated, _ = _calibration(ws)
    diag = _with_foils(ratings, book, foiled["kept"], eras)
    return {
        "corpus": _corpus(ws),
        "calibration": calibrated,
        "spot_check": _spot_check(ws),
        "fingerprints": {lin: {k: v for k, v in q.items() if k != "evidence"}
                         for lin, q in store.read_json(ws.path(invoke.QUALIFICATION_A)).items()},
        "agreement": {"inductive": _arm(inductive), "deductive": _arm(deductive_arm),
                      "deductive_lenient": lenient_arm, "stances": stances,
                      "downgrades": sum(1 for rows in ratings.verdicts.values() for v in rows if v["downgrade"]),
                      "integrity_audit": audit,
                      "inductive_with_foils": {"metric": diag.metric, "alpha": diag.alpha, "units": len(diag.units),
                                               "paired_fraction": diag.paired_fraction,
                                               "raw_agreement": diag.raw_agreement, "missing_rate": diag.missing_rate}},
        "foils": foiled["foils"],
        "audit_flags": foiled["audit_flags"],
        "constructs": [{"id": c["id"], "name": c["name"], "definition": c["definition"], "lineages": c["lineages"],
                        "status": status[c["id"]]} for c in book["constructs"]],
        "temporal": _temporal(ratings, book, eras, status, inductive_ok),
        "predictions": unmask.prediction_rates(codes, preds, supported if inductive_ok else set())
        | {"inductive_reportable": inductive_ok},
        "facet_bands": _facet_bands(deductive_arm.collapsed, self_report, instrument),
        "deductive_vs_ipip": unmask.domain_comparison(unmask.deductive_domains(collapsed, instrument), self_report)
        | {"deductive_reportable": deductive_arm.verdict in unmask.REPORTABLE},
        "correspondences": _correspondences(ws, book, supported, self_report["facets"], inductive_ok),
        "divergence": _divergence(ws, supported, deductive_arm.collapsed, self_report["facets"], inductive_ok),
        "domain_names": instrument["domains"],
        "facet_names": instrument["facets"],
        "invalid_outputs": {"by_stage": invalid, "total": sum(invalid.values()), "max_attempts": 3},
        "trimmed_outputs": {"by_stage": trimmed, "total": sum(trimmed.values())},
    }


def _diff(got, want, path="") -> list[str]:
    if isinstance(want, dict) and isinstance(got, dict):
        out = [f"{path}.{k}: missing" for k in want if k not in got]
        out += [f"{path}.{k}: unexpected" for k in got if k not in want]
        return out + [d for k in want if k in got for d in _diff(got[k], want[k], f"{path}.{k}")]
    if isinstance(want, list) and isinstance(got, list) and len(got) == len(want):
        return [d for i, (g, w) in enumerate(zip(got, want, strict=True)) for d in _diff(g, w, f"{path}[{i}]")]
    return [] if got == want else [f"{path}: reported {got!r}, recomputed {want!r}"]


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _num(v, digits=3) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _rows(want: dict) -> list[str]:
    """The lines report.md must contain, written here from the rebuilt values without the production renderer."""
    rows = []
    for era, st in want["corpus"].items():
        rows.append(f"- Era {era}: {st['sentences']} sentences in {st['segments']} segments; zoning kept "
                    f"{_pct(st['kept_ratio'])} of {st['words']} words ({st['band']}); {st['mixed']} mixed and "
                    f"{st['removed']} reflective sentences removed.")
    for label, key in (("Inductive constructs", "inductive"), ("Deductive facets (strict)", "deductive")):
        a = want["agreement"][key]
        rows.append(f"- **{label}** ({a['metric']}, {a['units']} units): α = {_num(a['alpha'])}, ")
        rows.append(f"  - Units with both lineages: {_pct(a['paired_fraction'])}; raw agreement on them: "
                    f"{_pct(a['raw_agreement'])}; missing: {_pct(a['missing_rate'])}; stability α within lineage: "
                    + ", ".join(f"{lin} {_num(v)}" for lin, v in sorted(a["stability"].items())) + ".")
    sc = want["spot_check"]
    rows.append(f"- Your spot check of zoning: you read {sc['kept']['shown']} kept and {sc['removed']['shown']} "
                f"removed sentences and marked {sc['kept']['marked']} kept and {sc['removed']['marked']} removed as "
                "wrongly labelled. Marks are reported, never applied.")
    lenient, st = want["agreement"]["deductive_lenient"], want["agreement"]["stances"]
    rows.append(f"- Deductive facets, lenient stance (sensitivity, not a gate; {lenient['units']} units): "
                f"α = {_num(lenient['alpha'])}; units with both lineages: {_pct(lenient['paired_fraction'])}; raw "
                f"agreement on them: {_pct(lenient['raw_agreement'])}; stability α within lineage: "
                + ", ".join(f"{lin} {_num(v)}" for lin, v in sorted(lenient["stability"].items())) + ".")
    shift = "n/a" if st["mean_shift"] is None else f"{st['mean_shift']:+.2f}"
    rows.append(f"- Strict against lenient scoring (sensitivity; {st['paired']} of {st['units']} lineage-facet scores "
                f"paired): α = {_num(st['alpha'])}; the same score in {_pct(st['equal_rate'])}; lenient minus strict "
                f"on average: {shift}.")
    c = want["calibration"]
    rows.append(f"- Zoning calibration: {c['held_out']} held-out sentences labelled blind by "
                + " and ".join(c["judges"]) + f" judges; where both judges agree, false keeps "
                f"{c['false_keep']['count']} of {c['false_keep']['of']} ({_pct(c['false_keep']['rate'])}; limit 5%) "
                f"and false removals {c['false_remove']['count']} of {c['false_remove']['of']} "
                f"({_pct(c['false_remove']['rate'])}; limit 20%); the judges split on {c['disputed']['keep']} kept and "
                f"{c['disputed']['removed']} cut sentences, which are left out and unresolved; they gave the same "
                f"label to {_pct(c['judge_agreement']['three_label'])} and agreed on keep or remove for "
                f"{_pct(c['judge_agreement']['collapsed'])}.")
    rows.append(f"rules: {want['agreement']['downgrades']}.")
    n = want["invalid_outputs"]["by_stage"]
    rows.append(f"up to 3 attempts): {sum(n.values())} (prepare {n['prepare']}, freeze {n['freeze']}, "
                f"extract {n['extract']}, unmask {n['unmask']}).")
    n = want["trimmed_outputs"]["by_stage"]
    rows.append(f"the call record keeps the full output): {sum(n.values())} (prepare {n['prepare']}, "
                f"freeze {n['freeze']}, extract {n['extract']}, unmask {n['unmask']}).")
    audit = want["agreement"]["integrity_audit"]
    if audit:
        ev, al = audit["events"]["counts"], audit["allowlist"]["counts"]
        rows.append(f"events: {ev['records']} call records audited, none with a tool event" if audit["events"]["ok"]
                    else f"events: {ev['records_with_tool_events']} call record(s)")
        rows.append(f"allowlist: {al['records']} call records within their roles' allowlists"
                    if audit["allowlist"]["ok"] else f"allowlist: {al['outside']} of {al['records']} call records")
        generations = audit["manifest"]["counts"]["generations"]
        rows.append(f"manifest: manifest chain verified across {generations} generations")
    d = want["agreement"]["inductive_with_foils"]
    rows.append(f"- Constructs plus foils (diagnostic, not a gate; {d['units']} units): α = {_num(d['alpha'])}; units "
                f"with both lineages: {_pct(d['paired_fraction'])}; raw agreement on them: {_pct(d['raw_agreement'])}.")
    f = want["foils"]
    rows.append(f"- Foils written: {f['authored']}; dropped by the audit as unclear: "
                + (", ".join(f"{d['era']} {d['id']}" for d in f["dropped"]) or "none") + ".")
    for kind, label, rate in (("clear", "Clear foils", "false-positive"), ("near_miss", "Near-miss foils",
                                                                          "false-positive"),
                              ("anchored", "Anchored foils", "recovery")):
        t = f["endorsement"][kind]
        rows.append(f"- {label} ({rate} rate, {t['items']} kept): " + "; ".join(
            f"{lin} {_pct(c['rate'])} ({c['present']} of {c['verdicts']} verdicts)"
            for lin, c in sorted(t["by_lineage"].items())) + ".")
    rows.append("the audit flagged with counter-evidence their definition does not account for: "
                + (", ".join(f"{cid} ({', '.join(e)})" for cid, e in want["audit_flags"].items()) or "none") + ".")
    eras = list(want["corpus"])
    for c in want["constructs"]:
        rows.append(f"| {c['id']} | {c['name']} | {', '.join(c['lineages'])} | "
                    + " | ".join(c["status"][e] for e in eras) + " |")
    t = want["temporal"]
    for key, label in (("fades", "Fades (observed, then contradicted)"),
                       ("emergences", "Emergences (contradicted, then observed)")):
        rows.append(f"- {label}: " + (", ".join(f"{r['construct_id']} {x['from']} → {x['to']}"
                                                for r in t["constructs"] for x in r[key]) or "none") + ".")
    rows.append("Now cannot establish who you are today; your questionnaire remains the present-day measure.")
    rows.append(f"- Constructs with no observed or contradicted era, left out of the table: {t['undecided']}.")
    names = {c["id"]: c["name"] for c in want["constructs"]}
    for r in t["constructs"]:
        state, era = r["now"]["state"], r["now"]["era"]
        now = {"observed": f"observed in {era}", "held": f"held (last observed in {era})", "faded": f"faded in {era}",
               "never observed": "never observed"}[state]
        rows.append(f"| {r['construct_id']} | {names[r['construct_id']]} | "
                    + (", ".join(e for e, v in r["eras"].items() if v == "observed") or "—") + " | "
                    + (", ".join(e for e, v in r["eras"].items() if v == "contradicted") or "—") + " | "
                    + (", ".join(r["held"]) or "—") + f" | {now} |")
    if not t["inductive_reportable"]:
        rows.append("The inductive arm is not reportable; this section is descriptive only.")
    p = want["predictions"]
    rows.append(f"- Predictions matched: {len(p['matched'])} of {p['predictions_decided']} decided "
                f"({_pct(p['predictions_matched_rate'])})")
    rows.append(f"- Supported constructs matched to a prediction: {len(p['constructs_matched'])} of "
                f"{p['supported_eligible']} ({_pct(p['supported_constructs_matched_rate'])}).")
    rows.append(f"- Coder agreement on prediction-construct pairs: {_pct(p['coder_agreement'])}.")
    d = want["deductive_vs_ipip"]
    for dom, row in d["domains"].items():
        same = "n/a" if row["same_band"] is None else ("yes" if row["same_band"] else "no")
        rows.append(f"| {want['domain_names'][dom]} | {_num(row['observer'], 2)} | {row['observer_band'] or 'n/a'} | "
                    f"{_num(row['ipip'], 2)} | {row['ipip_band'] or 'n/a'} | {same} |")
    rows.append(f"Same band in {_pct(d['agreement_rate'])} of {d['compared']} domains compared.")
    fb = want["facet_bands"]
    rows.append(f"- Observer estimates missing for {fb['observer_missing']} of {len(fb['facets'])} facets; IPIP scores "
                f"missing for {fb['ipip_missing']} of {len(fb['facets'])}. Same band in {_pct(fb['same_band_rate'])} "
                f"of {fb['compared']} facets compared.")
    for f, row in fb["facets"].items():
        same = "n/a" if row["same_band"] is None else ("yes" if row["same_band"] else "no")
        rows.append(f"| {f} {want['facet_names'][f]} | {_num(row['observer'], 2)} | {row['observer_band'] or 'n/a'} | "
                    f"{_num(row['ipip'], 2)} | {row['ipip_band'] or 'n/a'} | {same} |")
    c = want["correspondences"]
    n = c["counts"]
    rows.append(f"- Construct–facet pairs: {n['consistent']} consistent, {n['inconsistent']} inconsistent, "
                f"{n['neutral']} neutral (mid band), {n['n/a']} without a facet score.")
    rows.append("- Supported constructs mapped to no facet: " + (", ".join(c["unmapped"]) or "none") + ".")
    for row in c["rows"]:
        rows.append(f"| {row['construct_id']} {row['name']} | {row['facet']} {want['facet_names'][row['facet']]} | "
                    f"{row['polarity']} | {_num(row['ipip'], 2)} | {row['band'] or 'n/a'} | {row['result']} |")
    if not c["inductive_reportable"]:
        rows.append("The inductive arm is not reportable; the rows below are descriptive only.")
    m = want["divergence"]
    rows.append("- Facets: " + ", ".join(f"{m['counts'][k]} {k}" for k in
                                          ("divergent", "all three", "blind and self", "blind only", "self only",
                                           "test only")) + ".")
    rows.append("- Prediction tags the two coders did not share (placed nowhere): "
                + (", ".join(f"{a['prediction_id']} {a['facet']}" for a in m["ambiguous"]) or "none") + ".")

    def signed(items, key):
        return ", ".join(f"{i[key]}{'+' if i['polarity'] == '+' else '−'}" for i in items) or "—"

    def score(value, band):
        return f"{_num(value, 2)} {band}" if band else _num(value, 2)

    for row in m["rows"]:
        rows.append(f"| {row['class']} | {row['facet']} {want['facet_names'][row['facet']]} | "
                    f"{signed(row['constructs'], 'construct_id')} | {score(row['observer'], row['observer_band'])} | "
                    f"{row['blind'] or '—'} | {signed(row['predictions'], 'prediction_id')} | {row['self'] or '—'} | "
                    f"{score(row['ipip'], row['ipip_band'])} | {row['test'] or '—'} |")
    if not m["inductive_reportable"]:
        rows.append("The inductive arm is not reportable; the constructs below are descriptive only.")
    return rows


def compare(results: dict, report_md: str, want: dict) -> list[str]:
    """Field-by-field differences; rows report.md must show; then any line differing from the rebuilt report."""
    plain = json.loads(json.dumps(want))
    out = _diff(results, plain)
    out += [f"report.md lacks the row {row[:60]!r}…" for row in _rows(plain) if row not in report_md]
    rendered = report.render(plain).splitlines()
    actual = report_md.splitlines()
    out += [f"report.md line {i + 1} differs from its recomputation" for i in range(max(len(rendered), len(actual)))
            if (rendered[i] if i < len(rendered) else None) != (actual[i] if i < len(actual) else None)]
    return out


def problems(ws: store.Workspace) -> list[str]:
    try:
        gens = [e["gen"] for e in store.verify_manifest(ws)]
    except AbxError as exc:
        return [f"manifest: {exc}"]
    out = [] if gens == ["A", "B", "C", "D", "E"] else [f"generations {gens}"]
    report_md = ws.path("report/report.md").read_text()
    if [h for h in report_md.splitlines() if h.startswith("## ")][:1] != ["## This is not a clinical assessment"]:
        out.append("report's first section is not the non-clinical statement")
    out += _calibration(ws)[1]
    for coder, output in _coder_outputs(ws).items():
        if store.read_json(ws.path(f"run/crosswalk/codes-{coder}.json"))["predictions"] != output["predictions"]:
            out.append(f"crosswalk: {coder}'s stored prediction tags differ from its call record")
    if store.read_json(ws.path(facetmap.PATH)) != _mapper_output(ws):
        out.append("facet map: the stored map differs from the mapper's call record")
    return out + compare(store.read_json(ws.path("report/results.json")), report_md, expected(ws))


if __name__ == "__main__":
    found = problems(store.Workspace(Path(sys.argv[1]).resolve()))
    print("\n".join(found) or "every report figure recomputes")
    sys.exit(1 if found else 0)
