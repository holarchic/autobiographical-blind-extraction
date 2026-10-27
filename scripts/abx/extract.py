"""`extract`: the blind engine, from discovery to the agreement gates (generations B and C).

Discovery (lineage x run x era, future-blind) -> pooled candidates -> clean-room codebook
author + validator -> blind facet map -> blind audit per era -> card of constructs and kept foils (generation B)
-> recode -> deductive ledgers, then a strict and a lenient scoring from them -> agreement, the stance sensitivity
and the temporal read by script -> integrity audit when agreement is very high (generation C). Every model
call is a `CallRequest` through `invoke.call`.
"""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, NamedTuple

from . import (
    agreement,
    artifacts,
    audit,
    card,
    config,
    evidence,
    facetmap,
    foils,
    invoke,
    ipip,
    lifecycle,
    prompts,
    qualify,
    store,
    temporal,
)
from . import schema as jsonschema
from .errors import AbxError, IntegrityError

__all__ = ["REQUALIFICATION_AUDIT", "STAGE", "STANCES", "Context", "RaterKey", "Ratings", "Stance",
           "StanceComparison", "StanceName", "StanceUnit", "Unit", "arms", "context", "coverage", "estimate",
           "extract", "integrity_audit", "load_ratings", "score_problems", "stance_sensitivity", "validate_codebook",
           "with_foils"]

REQUALIFICATION_AUDIT = "run/requalification-audit.json"


class RaterKey(NamedTuple):
    lineage: str
    run: int
    era: str | None


StanceName = Literal["strict", "lenient"]


class Stance(NamedTuple):
    """A deductive scoring stance: its scorer role and the prefix of its score files."""

    name: StanceName
    role: str
    file: str


class StanceUnit(NamedTuple):
    lineage: str
    facet: str


class StanceComparison(NamedTuple):
    """Strict against lenient scoring; the rates are None when no unit has both stances' values."""

    alpha: float | None
    reason: str
    units: int
    paired: int
    equal_rate: float | None
    mean_shift: float | None


STANCES = (Stance("strict", "deductive_scorer", "scores"), Stance("lenient", "deductive_scorer_lenient", "lenient"))


class Unit(NamedTuple):
    """An inductive agreement unit: one card item in one era."""

    item_id: str
    era: str


@dataclass(frozen=True)
class Context:
    """What every step of one `extract` shares: the workspace, config, zoned-era index and facets."""

    ws: store.Workspace
    cfg: config.Config
    ix: evidence.Index
    facets: tuple[str, ...]

    @property
    def runs(self) -> range:
        return range(1, self.cfg.runs + 1)


def context(ws: store.Workspace) -> Context:
    """What every extract step reads: the workspace, its config, the era index and the facet codes."""
    return Context(ws=ws, cfg=config.load(ws), ix=evidence.load(ws), facets=tuple(ipip.load_instrument(ws)["facets"]))


def coverage(items: set[str], got: list[str]) -> list[str]:
    """Each card item exactly once, naming what is missing, repeated or not on the card."""
    missing = sorted(items - set(got))
    repeated = sorted({i for i in got if got.count(i) > 1})
    unknown = sorted(set(got) - items)
    if not (missing or repeated or unknown):
        return []
    parts = [f"{label} {', '.join(ids[:evidence.MAX_NAMED])}" for label, ids in
             (("missing", missing), ("repeated", repeated), ("not on the card", unknown)) if ids]
    return [f"verdicts must cover every card item exactly once: {'; '.join(parts)}"]


def estimate(ws: store.Workspace) -> lifecycle.Preflight:
    cfg = config.load(ws)
    eras = len(store.read_json(ws.path("prepare/eras.json"))["eras"])
    return lifecycle.Preflight(len(cfg.lineages) * cfg.runs * (3 * eras + len(STANCES)) + 2 + eras, 2,
                               "discovery, recode and ledger per rater and era, a strict and a lenient scorer per "
                               "rater, one codebook "
                               "author, one facet mapper, one auditor per era; if agreement is very high, one leak "
                               "probe per lineage for the integrity audit")


# --- discovery ---------------------------------------------------------------------------------------------------

def discovery_checks(schema: dict, ix: evidence.Index, era: str) -> tuple[Callable, Callable]:
    """One era's discovery validator and trim: every trait, kept or cut, must pass the schema bar its trait cap and
    cite only the zoned era; the trim then keeps the most repeated."""
    uncapped = copy.deepcopy(schema)
    cap = uncapped["properties"]["traits"].pop("maxItems")

    def validate(o) -> list[str]:
        return jsonschema.errors(o, uncapped) or ix.outside(era, evidence.cited(o, ("sentence_ids",)))
    return validate, lambda o: evidence.top_traits(o, cap)


def _discovery(c: Context) -> None:
    schema = store.loads_strict(artifacts.resolver(c.ws)("schema:discovery"))
    for lineage in c.cfg.lineages:
        for run in c.runs:
            hypotheses: list[str] = []
            for k, era in enumerate(c.ix.era_ids):
                slots = {"era": f"era:{era}"}
                if k > 0:
                    slots["hypotheses"] = f"hypotheses:{lineage}:{run}:{c.ix.era_ids[k - 1]}"
                validate, trim = discovery_checks(schema, c.ix, era)
                out = invoke.call(c.ws, c.cfg, invoke.CallRequest(
                    stage="extract", role="discovery", lineage=lineage, run=run, era=era, generation="B", slots=slots,
                    validate=validate, trim=trim))
                store.write_json(artifacts.path_of(c.ws, f"discovery:{lineage}:{run}:{era}"), out)
                hypotheses += out["hypotheses"]
                store.write_text_atomic(artifacts.path_of(c.ws, f"hypotheses:{lineage}:{run}:{era}"),
                                        "".join(f"- {h}\n" for h in hypotheses) or "- (none)\n")


def _pool(c: Context) -> dict[str, dict]:
    """Every discovery candidate under an opaque ref; the map back to its rater stays local."""
    candidates = {}
    for lineage in c.cfg.lineages:
        for run in c.runs:
            for era in c.ix.era_ids:
                for t in store.read_json(artifacts.path_of(c.ws, f"discovery:{lineage}:{run}:{era}"))["traits"]:
                    ref = f"c{len(candidates) + 1:03d}"
                    candidates[ref] = {"lineage": lineage, "run": run, "era": era, "trait": t["trait"],
                                       "alexander_id": t["alexander_id"]}
    store.write_json(c.ws.path("run/discovery/pool.json"), candidates)
    store.write_text_atomic(artifacts.path_of(c.ws, "pool:all"), "".join(
        f"{ref} | {cand['trait']} | {cand['alexander_id']}\n" for ref, cand in candidates.items()))
    return candidates


# --- codebook ----------------------------------------------------------------------------------------------------

def validate_codebook(out: dict, refs: set[str]) -> list[str]:
    """No always-compound marker in a name, unique ids, at most 80, and every candidate placed exactly once."""
    problems = []
    cons = out["constructs"]
    ids = [c["id"] for c in cons]
    if len(ids) != len(set(ids)):
        problems.append("duplicate construct ids")
    if len(cons) > card.MAX_CONSTRUCTS:
        problems.append(f"more than {card.MAX_CONSTRUCTS} constructs")
    problems += [f"{c['id']}: name is not atomic" for c in cons if card.COMPOUND_NAME.search(c["name"])]
    problems += [f"{c['id']}: traces no candidate" for c in cons if not c["trace"]]
    problems += [f"{c['id']}: a candidate repeats in its trace" for c in cons
                 if len(c["trace"]) != len(set(c["trace"]))]
    holders: dict[str, set[str]] = {}
    for c in cons:
        for ref in c["trace"]:
            holders.setdefault(ref, set()).add(c["id"])
    appendix: dict[str, str] = {}
    for a in out["appendix"]:
        if a["candidate_ref"] in appendix:
            problems.append(f"{a['candidate_ref']} appears twice in the appendix")
        appendix[a["candidate_ref"]] = a["disposition"]
    unknown = (set(holders) | set(appendix)) - refs
    if unknown:
        problems.append(f"{len(unknown)} unknown candidate ref(s)")
    for ref in sorted(refs):
        n, disp = len(holders.get(ref, ())), appendix.get(ref)
        placed = {None: n == 1, "merged": n == 1, "excluded": n == 0, "split": n >= 2}.get(disp, False)
        if not placed:
            problems.append(f"{ref} is not placed exactly once ({n} constructs, appendix {disp})")
    return problems


def _codebook(c: Context, candidates: dict[str, dict]) -> dict:
    salt = store.sha256_file(c.ws.path("prepare/redacted.txt"))
    foil_ids = [f["item_id"] for era in c.ix.era_ids for f in foils.load(c.ws, era)]

    def validate(o):
        problems = validate_codebook(o, set(candidates))
        ids = [card.item_id(con["id"], salt) for con in o["constructs"]] + foil_ids
        return problems + (["card item ids collide; the author must rename a construct"]
                           if len(set(ids)) != len(ids) else [])

    out = invoke.call(c.ws, c.cfg, invoke.CallRequest(
        stage="extract", role="codebook_author", lineage=c.cfg.codebook_author, run=None, era=None, generation="B",
        slots={"pool": "pool:all"}, validate=validate))
    constructs = [{**con, "lineages": sorted({candidates[ref]["lineage"] for ref in con["trace"]}),
                   "item_id": card.item_id(con["id"], salt)} for con in out["constructs"]]
    book = {"constructs": constructs, "appendix": out["appendix"]}
    store.write_json(c.ws.path("run/codebook/codebook.json"), book)
    return book


def _card(c: Context, book: dict) -> None:
    """Audit, then the card: constructs and the foils the audit kept, alike and sorted by item id."""
    kept = audit.run(c.ws, c.cfg, c.ix, book["constructs"])["kept"]
    store.write_text_atomic(artifacts.path_of(c.ws, "card:B"), card.render([*book["constructs"], *kept]))


# --- recode ------------------------------------------------------------------------------------------------------

def _recode(c: Context, book: dict) -> None:
    items = {i["item_id"] for i in [*book["constructs"], *store.read_json(c.ws.path(audit.FOILS))["kept"]]}
    for lineage in c.cfg.lineages:
        for run in c.runs:
            for era in c.ix.era_ids:

                def validate(o, era=era):
                    return coverage(items, [v["item_id"] for v in o["verdicts"]]) or \
                        c.ix.outside(era, evidence.cited(o, ("sentence_ids", "counter_sentence_ids")))

                out = invoke.call(c.ws, c.cfg, invoke.CallRequest(
                    stage="extract", role="recode", lineage=lineage, run=run, era=era, generation="C",
                    slots={"card": "card:B", "era": f"era:{era}"}, validate=validate))
                verdicts = []
                for v in out["verdicts"]:
                    final, reason = evidence.apply_bar(v, c.ix.seg_of)
                    verdicts.append({"item_id": v["item_id"], "rater_verdict": v["verdict"], "verdict": final,
                                     "downgrade": reason})
                store.write_json(c.ws.path(f"run/recode/{lineage}-r{run}-{era}.json"),
                                 {"lineage": lineage, "run": run, "era": era, "verdicts": verdicts})


# --- deductive ---------------------------------------------------------------------------------------------------

def _deductive(c: Context) -> None:
    facets = sorted(c.facets)
    for lineage in c.cfg.lineages:
        for run in c.runs:
            ledgers, cited = [], {f: set() for f in facets}
            for era in c.ix.era_ids:

                def validate(o, era=era):
                    if sorted(f["facet"] for f in o["facets"]) != facets:
                        return ["the ledger must list all 30 facets once"]
                    return c.ix.outside(era, evidence.cited(o, ("sentence_ids",)))

                out = invoke.call(c.ws, c.cfg, invoke.CallRequest(
                    stage="extract", role="deductive_ledger", lineage=lineage, run=run, era=era, generation="C",
                    slots={"era": f"era:{era}", "facets": "facets:all"}, validate=validate))
                store.write_json(artifacts.path_of(c.ws, f"ledger:{lineage}:{run}:{era}"), out)
                ledgers.append({"era": era, **out})
                for entry in out["facets"]:
                    cited[entry["facet"]] |= set(evidence.cited(entry, ("sentence_ids",)))
            store.write_json(artifacts.path_of(c.ws, f"ledgers:{lineage}:{run}"), ledgers)

            by_facet = {f: frozenset(ids) for f, ids in cited.items()}
            for stance in STANCES:
                out = invoke.call(c.ws, c.cfg, invoke.CallRequest(
                    stage="extract", role=stance.role, lineage=lineage, run=run, era=None, generation="C",
                    slots={"ledgers": f"ledgers:{lineage}:{run}", "facets": "facets:all"},
                    validate=lambda o, stance=stance, cited=by_facet: score_problems(o, facets, cited, stance.name)))
                store.write_json(c.ws.path(f"run/deductive/{stance.file}-{lineage}-r{run}.json"),
                                 {"lineage": lineage, "run": run, "stance": stance.name, "scores": out["scores"]})


def score_problems(out: dict, facets: list[str], cited: Mapping[str, frozenset[str]],
                   stance: StanceName) -> list[str]:
    """Every facet once; a score cites ledger evidence: the strict stance only that facet's own, which a facet
    without any must leave insufficient; the lenient stance any of the rater's ledger sentences."""
    if sorted(s["facet"] for s in out["scores"]) != facets:
        return ["scores must cover all 30 facets once"]
    anywhere = frozenset().union(*cited.values())
    problems = []
    for s in out["scores"]:
        if s["score"] is None:
            continue
        given = set(s["cited_sentence_ids"])
        if stance == "lenient":
            if not given or not given <= anywhere:
                problems.append(f"{s['facet']}: citations must come from this rater's ledgers")
        elif not cited[s["facet"]]:
            problems.append(f"{s['facet']}: scored without ledger evidence for that facet")
        elif not given or not given <= cited[s["facet"]]:
            problems.append(f"{s['facet']}: citations must come from that facet's ledger evidence")
    return problems


# --- agreement ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Ratings:
    """The blind ratings as named records, read from the run files; the report's recomputation uses this too."""

    verdicts: Mapping[RaterKey, tuple[dict, ...]]
    scores: Mapping[RaterKey, Mapping[str, int | None]]
    lenient: Mapping[RaterKey, Mapping[str, int | None]]


def _stance_scores(ws: store.Workspace, stance: Stance) -> Mapping[RaterKey, Mapping[str, int | None]]:
    scores = {}
    for path in sorted(ws.root.glob(f"run/deductive/{stance.file}-*.json")):
        rec = store.read_json(path)
        if rec.get("stance") != stance.name:
            raise IntegrityError(f"{ws.rel(path)} holds {rec.get('stance')!r} scores, not {stance.name!r}")
        key = RaterKey(rec["lineage"], rec["run"], None)
        by_facet = {s["facet"]: s["score"] for s in rec["scores"]}
        if key in scores or len(by_facet) != len(rec["scores"]):
            raise IntegrityError(f"{ws.rel(path)} repeats a rater or a facet")
        scores[key] = MappingProxyType(by_facet)
    return MappingProxyType(scores)


def load_ratings(ws: store.Workspace) -> Ratings:
    verdicts = {}
    for path in sorted(ws.root.glob("run/recode/*.json")):
        rec = store.read_json(path)
        key = RaterKey(rec["lineage"], rec["run"], rec["era"])
        if key in verdicts:
            raise IntegrityError(f"{ws.rel(path)} repeats the recode of {key.lineage} run {key.run} era {key.era}")
        verdicts[key] = tuple(rec["verdicts"])
    by_stance = {stance.name: _stance_scores(ws, stance) for stance in STANCES}
    return Ratings(MappingProxyType(verdicts), by_stance["strict"], by_stance["lenient"])


def arms(ratings: Ratings, book: dict, era_ids: list[str],
         facets: list[str]) -> tuple[agreement.ArmResult, agreement.ArmResult]:
    """The inductive (constructs-only) and deductive (strict) arms, from named ratings. The inductive value asks
    whether the era shows the construct: absent and insufficient verdicts, after the citation rules, are both
    not_present."""
    units = [Unit(con["item_id"], era) for con in book["constructs"] for era in era_ids]
    return agreement.arm(_binary_runs(ratings), units, "nominal"), agreement.arm(_score_runs(ratings.scores), facets,
                                                                                 "ordinal")


def _score_runs(scores: Mapping[RaterKey, Mapping[str, int | None]]) -> dict:
    runs: dict = {}
    for key, by_facet in scores.items():
        runs.setdefault(key.lineage, {})[key.run] = by_facet
    return runs


def stance_sensitivity(ratings: Ratings, facets: list[str],
                       strict: agreement.ArmResult) -> tuple[agreement.ArmResult, StanceComparison]:
    """The lenient arm, and strict (the gate's arm, passed in) against lenient: ordinal alpha with the stances as
    raters over every (lineage, facet), each value that stance's rounded-median collapse, with no coverage threshold;
    plus the equal share and the mean lenient minus strict score over the paired units. Sensitivity only."""
    lenient = agreement.arm(_score_runs(ratings.lenient), facets, "ordinal")
    units = [StanceUnit(lin, f) for lin in sorted(set(strict.collapsed) | set(lenient.collapsed)) for f in facets]
    matrix = {stance: {u: arm.collapsed.get(u.lineage, {}).get(u.facet) for u in units}
              for stance, arm in (("strict", strict), ("lenient", lenient))}
    paired = [u for u in units if matrix["strict"][u] is not None and matrix["lenient"][u] is not None]
    alpha, reason = agreement.alpha(matrix, "ordinal")
    shifts = [matrix["lenient"][u] - matrix["strict"][u] for u in paired]
    return lenient, StanceComparison(alpha, reason, len(units), len(paired),
                                     sum(s == 0 for s in shifts) / len(shifts) if shifts else None,
                                     sum(shifts) / len(shifts) if shifts else None)


def with_foils(ratings: Ratings, book: dict, kept: list[dict], era_ids: list[str]) -> agreement.ArmResult:
    """The diagnostic arm: the inductive reading over constructs and kept foils in every era. Never a gate."""
    units = [Unit(item["item_id"], era) for item in [*book["constructs"], *kept] for era in era_ids]
    return agreement.arm(_binary_runs(ratings), units, "nominal")


def _runs(ratings: Ratings, value: Callable[[str], str]) -> dict:
    """{lineage: {run: {unit: value(verdict)}}} over every rated item."""
    runs: dict = {}
    for key, rows in ratings.verdicts.items():
        cell = runs.setdefault(key.lineage, {}).setdefault(key.run, {})
        for v in rows:
            cell[Unit(v["item_id"], key.era)] = value(v["verdict"])
    return runs


def _binary_runs(ratings: Ratings) -> dict:
    return _runs(ratings, lambda verdict: "present" if verdict == "present" else "not_present")


def _arm_json(arm: agreement.ArmResult) -> dict:
    return {"metric": arm.metric, "alpha": arm.alpha, "verdict": arm.verdict, "reason": arm.reason,
            "paired_fraction": arm.paired_fraction, "raw_agreement": arm.raw_agreement,
            "missing_rate": arm.missing_rate, "stability": {lin: arm.stability[lin] for lin in sorted(arm.stability)},
            "units": len(arm.units), "integrity_audit_due": arm.integrity_audit_due}


def _agree(c: Context, book: dict) -> dict:
    ratings = load_ratings(c.ws)
    inductive, deductive_arm = arms(ratings, book, c.ix.era_ids, list(c.facets))
    lineages = sorted(inductive.collapsed)
    status = {con["id"]: {era: agreement.status([inductive.collapsed[lin][Unit(con["item_id"], era)]
                                                for lin in lineages]) for era in c.ix.era_ids}
              for con in book["constructs"]}
    three_way = agreement.collapse_runs(_runs(ratings, lambda verdict: verdict), inductive.units,
                                        agreement.collapse_mode)
    lenient, stances = stance_sensitivity(ratings, list(c.facets), deductive_arm)
    sensitivity = {k: v for k, v in _arm_json(lenient).items() if k != "integrity_audit_due"}
    result = {
        "inductive": _arm_json(inductive), "deductive": _arm_json(deductive_arm),
        "deductive_lenient": sensitivity, "stances": stances._asdict(),
        "construct_status": status,
        "temporal": temporal.read(book["constructs"], status, three_way, c.ix.era_ids),
        "inductive_collapsed": [{"lineage": lin, "item_id": u.item_id, "era": u.era, "value": v}
                                for lin in lineages for u, v in inductive.collapsed[lin].items()],
        "deductive_collapsed": [{"lineage": lin, "facet": f, "value": v} for lin in sorted(deductive_arm.collapsed)
                                for f, v in deductive_arm.collapsed[lin].items()],
        "downgrades": sum(1 for rows in ratings.verdicts.values() for v in rows if v["downgrade"]),
        **_diagnostics(c, book, ratings, lineages),
    }
    if inductive.integrity_audit_due or deductive_arm.integrity_audit_due:
        result["integrity_audit"] = integrity_audit(c)
        if not all(check["ok"] for check in result["integrity_audit"].values()):
            store.write_json(c.ws.path("run/agreement.json"), result)
            raise IntegrityError("integrity audit failed after very high agreement; results invalidated")
    store.write_json(c.ws.path("run/agreement.json"), result)
    return result


def _diagnostics(c: Context, book: dict, ratings: Ratings, lineages: list[str]) -> dict:
    """Foil and audit figures: reported, never part of a gate."""
    audited = store.read_json(c.ws.path(audit.FOILS))
    diag = with_foils(ratings, book, audited["kept"], c.ix.era_ids)
    return {
        "inductive_with_foils": {"metric": diag.metric, "alpha": diag.alpha, "units": len(diag.units),
                                 "paired_fraction": diag.paired_fraction, "raw_agreement": diag.raw_agreement,
                                 "missing_rate": diag.missing_rate},
        "foils": {"authored": sum(len(foils.load(c.ws, era)) for era in c.ix.era_ids), "dropped": audited["dropped"],
                  "endorsement": foils.endorsement(ratings.verdicts, audited["kept"], lineages)},
        "audit_flags": audit.flags(c.ws, c.ix.era_ids),
    }


def integrity_audit(c: Context) -> dict:
    """Requalify, then re-audit every call record's events and inputs, and the manifest chain."""
    records = sorted(c.ws.root.glob("run/calls/**/*.json"))
    Check = agreement.Check

    def requalify():
        fresh = qualify.qualify(c.ws, c.cfg, out=REQUALIFICATION_AUDIT)
        baseline = invoke.qualified(c.ws)
        moved = [lin for lin, q in fresh.items()
                 if any(q.get(k) != baseline[lin].get(k) for k in invoke.FINGERPRINT_KEYS)]
        return Check(not moved, "fingerprint_moved" if moved else "requalified", {"lineages_moved": len(moved)})

    def events():
        def tool_events(path):
            rec = store.read_json(path)
            evs = invoke.decode_events(rec.get("events"))
            if evs is None:
                return ["unreadable"]
            state = invoke.outcome(rec["lineage"], rec.get("exit"), evs)
            return [] if state == "success" else [state]

        hits = sum(1 for p in records if tool_events(p))
        incidents = sum(1 for p in sorted(c.ws.root.glob("run/incidents/**/*.json"))
                        if store.read_json(p)["kind"] == "tool_event")
        ok = not hits and not incidents
        return Check(ok, "clean" if ok else "tool_events",
                     {"records": len(records), "records_with_tool_events": hits, "tool_event_incidents": incidents})

    def allowlists():
        outside = 0
        for path in records:
            rec = store.read_json(path)
            rater = (rec["lineage"], rec["run"]) if rec["run"] is not None else None
            try:
                prompts.check_allowlist(rec["role"], list(rec["input_artifacts"]), rater=rater, era=rec["era"],
                                        era_order=c.ix.era_ids)
            except AbxError:
                outside += 1
        return Check(not outside, "within_allowlists" if not outside else "outside_allowlist",
                     {"records": len(records), "outside": outside})

    def manifest():
        return Check(True, "verified", {"generations": len(store.verify_manifest(c.ws))})

    return agreement.integrity_audit({"requalify": requalify, "events": events, "allowlist": allowlists,
                                      "manifest": manifest})


def extract(ws: store.Workspace) -> dict:
    gens = lifecycle.enter(ws, STAGE)
    invoke.check_method(ws)
    c = context(ws)
    if "B" not in gens:
        _discovery(c)
        candidates = _pool(c)
        book = _codebook(c, candidates)
        facetmap.run(ws, c.cfg, book["constructs"])
        _card(c, book)
        store.append_generation(ws, "B", [
            "run/discovery/pool.json", "run/discovery/pool.md", "run/codebook/codebook.json",
            "run/codebook/codebook.md", audit.FOILS, "run/codebook/card.md", facetmap.PATH,
            "run/codebook/definitions.md",
            *(ws.rel(p) for g in ("run/discovery/*-r*-*.json", "run/discovery/*.hypotheses.md", "run/audit/*.json",
                                  "run/calls/extract/discovery/*.json", "run/calls/extract/codebook_author/*.json",
                                  "run/calls/extract/auditor/*.json", "run/calls/extract/facet_mapper/*.json")
              for p in sorted(ws.root.glob(g)))])
    if "C" in gens:
        return store.read_json(ws.path("run/agreement.json"))
    book = store.read_json(ws.path("run/codebook/codebook.json"))
    _recode(c, book)
    _deductive(c)
    result = _agree(c, book)
    outputs = [ws.rel(p) for g in ("run/recode/*.json", "run/deductive/*.json", "run/calls/extract/recode/*.json",
                                   "run/calls/extract/deductive_*/*.json", REQUALIFICATION_AUDIT,
                                   "run/incidents/prepare/**/*.json", "run/incidents/freeze/**/*.json",
                                   "run/incidents/extract/**/*.json")
               for p in sorted(ws.root.glob(g))]
    store.append_generation(ws, "C", ["run/agreement.json", *outputs])
    return result


def _cli(ws: store.Workspace, args) -> lifecycle.CliResult:
    """The gates and their alpha; a gate that fired (an arm not reportable) is exit 1, a result, not a crash."""
    result = extract(ws)
    gates = {arm: result[arm]["verdict"] for arm in ("inductive", "deductive")}
    return lifecycle.CliResult({"gates": gates, "alpha": {arm: result[arm]["alpha"] for arm in gates}},
                               1 if "not_reportable" in gates.values() else 0)


STAGE = lifecycle.Stage("extract", 4, "the blind engine, to generation C", _cli, estimate, needs="A",
                        closed_after="E", checks_method=True)
