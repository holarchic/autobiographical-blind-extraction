"""`unmask`: two-coder prediction crosswalk (generation D), then the IPIP reveal, comparisons
and report (generation E). Every figure is computed by script from frozen files only, never by a model.
"""

from fractions import Fraction
from statistics import median
from types import MappingProxyType

from . import (
    artifacts,
    calibration,
    config,
    divergence,
    facetmap,
    invoke,
    ipip,
    lifecycle,
    report,
    seal,
    spotcheck,
    store,
    temporal,
)

__all__ = [
    "REPORTABLE",
    "STAGE",
    "compute",
    "deductive_domains",
    "domain_comparison",
    "estimate",
    "facet_comparison",
    "prediction_rates",
    "unmask",
]

REPORTABLE = frozenset({"report", "tentative"})
CODE_RANK = MappingProxyType({"none": 0, "partial": 1, "match": 2})
OUTCOME_OF_RANK = MappingProxyType({0: "unmatched", 1: "partial", 2: "matched"})


def estimate(ws: store.Workspace) -> lifecycle.Preflight:
    return lifecycle.Preflight(len(config.load(ws).crosswalk_coders), 0, "one crosswalk call per coder")


def _crosswalk(ws: store.Workspace, cfg: config.Config, book: dict) -> None:
    store.write_text_atomic(artifacts.path_of(ws, "constructs:all"), facetmap.render_definitions(book["constructs"]))
    preds = seal.predictions(ws.path("sealed/self-prediction.md").read_text(encoding="utf-8"))
    pairs = {(p, c["id"]) for p in preds for c in book["constructs"]}

    def validate(out) -> list[str]:
        got = [(x["prediction_id"], x["construct_id"]) for x in out["codes"]]
        problems = [] if len(got) == len(set(got)) and set(got) == pairs else \
            ["codes must cover every pair exactly once"]
        tagged = [x["prediction_id"] for x in out["predictions"]]
        if sorted(tagged) != sorted(preds):
            problems.append("tag every prediction exactly once")
        return problems + [f"{x['prediction_id']} lists a facet twice" for x in out["predictions"]
                           if len({f["facet"] for f in x["facets"]}) != len(x["facets"])]

    for lineage in cfg.crosswalk_coders:
        out = invoke.call(ws, cfg, invoke.CallRequest(
            stage="unmask", role="crosswalk", lineage=lineage, run=None, era=None, generation="D",
            slots={"constructs": "constructs:all", "predictions": "self_prediction:sealed", "facets": "facets:all"},
            validate=validate))
        store.write_json(ws.path(f"run/crosswalk/codes-{lineage}.json"),
                         {"coder": lineage, "codes": out["codes"], "predictions": out["predictions"]})


def prediction_rates(codes: dict[str, list[dict]], predictions: list[str], eligible: set[str]) -> dict:
    """Match rates of the self-prediction against the inductive constructs.

    `eligible` is the supported constructs of a reportable inductive arm. A prediction is matched
    when every coder codes some eligible construct `match`, partial when every coder's best is
    `partial`; coders who disagree make it ambiguous, and it leaves both rates.
    """
    coders = sorted(codes)
    table = {coder: {(x["prediction_id"], x["construct_id"]): x["code"] for x in codes[coder]} for coder in coders}
    outcome = {}
    for p in predictions:
        best = {max((CODE_RANK[code] for (pid, cid), code in table[coder].items() if pid == p and cid in eligible),
                    default=0) for coder in coders}
        outcome[p] = OUTCOME_OF_RANK[best.pop()] if len(best) == 1 else "ambiguous"
    decided = [p for p in predictions if outcome[p] != "ambiguous"]
    matched = [p for p in decided if outcome[p] == "matched"]
    constructs_matched = sorted(c for c in eligible if any(
        all(table[coder].get((p, c)) == "match" for coder in coders) for p in predictions))
    pairs = set().union(*(table[c].keys() for c in coders)) if coders else set()
    descriptive = sorted({(pid, cid) for coder in coders for (pid, cid), code in table[coder].items()
                          if code == "match" and cid not in eligible})
    return {
        "outcome": outcome, "matched": matched, "ambiguous": [p for p in predictions if outcome[p] == "ambiguous"],
        "predictions_decided": len(decided),
        "predictions_matched_rate": len(matched) / len(decided) if decided else None,
        "supported_eligible": len(eligible), "constructs_matched": constructs_matched,
        "supported_constructs_matched_rate": len(constructs_matched) / len(eligible) if eligible else None,
        "descriptive_matches": [{"prediction_id": p, "construct_id": c} for p, c in descriptive],
        "coder_agreement": (sum(1 for k in pairs if len({table[c].get(k) for c in coders}) == 1) / len(pairs))
        if pairs else None,
    }


def deductive_domains(collapsed: list[dict], instrument: dict) -> dict:
    """Facet = median of the lineage collapses (all present); domain = mean of at least 4 of 6 facets."""
    by_facet: dict[str, list] = {}
    lineages = {r["lineage"] for r in collapsed}
    for r in collapsed:
        by_facet.setdefault(r["facet"], []).append(r["value"])
    facets = {}
    for f in instrument["facets"]:
        vals = by_facet.get(f, [])
        complete = len(vals) == len(lineages) and all(v is not None for v in vals)
        facets[f] = Fraction(median(Fraction(v) for v in vals)) if complete else None
    domains = {}
    for d in instrument["domains"]:
        present = [v for f, v in facets.items() if f.startswith(d) and v is not None]
        domains[d] = ipip.mean(present) if len(present) >= ipip.DOMAIN_MIN_FACETS else None
    return {"facets": facets, "domains": domains}


def _num(value: Fraction | None) -> float | None:
    return None if value is None else float(value)


def domain_comparison(observer: dict, self_report: dict) -> dict:
    rows = {}
    for d, obs in observer["domains"].items():
        o_band, s_band = ipip.observer_band(obs), ipip.ipip_band(self_report["domains"][d])
        rows[d] = {"observer": _num(obs), "observer_band": o_band, "ipip": _num(self_report["domains"][d]),
                   "ipip_band": s_band, "same_band": (o_band == s_band) if o_band and s_band else None}
    compared = [r for r in rows.values() if r["same_band"] is not None]
    return {"domains": rows, "compared": len(compared),
            "agreement_rate": sum(r["same_band"] for r in compared) / len(compared) if compared else None}


def facet_comparison(observer: dict, self_report: dict, instrument: dict) -> dict:
    """Every facet, observer estimate against IPIP score by band: descriptive, never a headline result."""
    rows = {}
    for f in instrument["facets"]:
        obs, own = observer["facets"].get(f), self_report["facets"].get(f)
        o_band, s_band = ipip.observer_band(obs), ipip.ipip_band(own)
        rows[f] = {"observer": _num(obs), "observer_band": o_band, "ipip": _num(own), "ipip_band": s_band,
                   "same_band": (o_band == s_band) if o_band and s_band else None}
    compared = [r for r in rows.values() if r["same_band"] is not None]
    return {"facets": rows, "observer_missing": sum(r["observer"] is None for r in rows.values()),
            "ipip_missing": sum(r["ipip"] is None for r in rows.values()), "compared": len(compared),
            "same_band_rate": sum(r["same_band"] for r in compared) / len(compared) if compared else None}


def compute(ws: store.Workspace, cfg: config.Config) -> dict:
    """Every reported figure, from frozen files only; the recomputation check rebuilds the same way."""
    book = store.read_json(ws.path("run/codebook/codebook.json"))
    agree = store.read_json(ws.path("run/agreement.json"))
    coded = {c: store.read_json(ws.path(f"run/crosswalk/codes-{c}.json")) for c in cfg.crosswalk_coders}
    codes = {c: coded[c]["codes"] for c in cfg.crosswalk_coders}
    preds = list(seal.predictions(ws.path("sealed/self-prediction.md").read_text(encoding="utf-8")))
    inductive_ok = agree["inductive"]["verdict"] in REPORTABLE
    supported = {cid for cid, eras in agree["construct_status"].items() if "supported" in eras.values()}
    instrument = ipip.load_instrument(ws)
    self_report = ipip.score(ipip.parse_responses(ws.path("sealed/ipip-120.csv").read_text(encoding="utf-8"),
                                                  instrument), instrument)
    observer = deductive_domains(agree["deductive_collapsed"], instrument)
    facet_map = store.read_json(ws.path(facetmap.PATH))
    qualification = store.read_json(ws.path(invoke.QUALIFICATION_A))
    invalid = invoke.invalid_outputs(ws)
    trimmed = invoke.trimmed_outputs(ws)
    return {
        "corpus": store.read_json(ws.path("prepare/stats.json")),
        "calibration": calibration.summary(store.read_json(ws.path(calibration.PATH))),
        "spot_check": spotcheck.summary(store.read_json(ws.path(spotcheck.PATH))),
        "fingerprints": {lin: {k: v for k, v in q.items() if k != "evidence"} for lin, q in qualification.items()},
        "agreement": {k: agree[k] for k in ("inductive", "deductive", "deductive_lenient", "stances", "downgrades",
                                       "inductive_with_foils")}
        | {"integrity_audit": agree.get("integrity_audit")},
        "foils": agree["foils"],
        "audit_flags": agree["audit_flags"],
        "invalid_outputs": {"by_stage": invalid, "total": sum(invalid.values()), "max_attempts": invoke.MAX_ATTEMPTS},
        "trimmed_outputs": {"by_stage": trimmed, "total": sum(trimmed.values())},
        "constructs": [{"id": c["id"], "name": c["name"], "definition": c["definition"], "lineages": c["lineages"],
                        "status": agree["construct_status"][c["id"]]} for c in book["constructs"]],
        "temporal": temporal.shown(agree["temporal"]) | {"inductive_reportable": inductive_ok},
        "predictions": prediction_rates(codes, preds, supported if inductive_ok else set())
        | {"inductive_reportable": inductive_ok},
        "facet_bands": facet_comparison(observer, self_report, instrument),
        "deductive_vs_ipip": domain_comparison(observer, self_report)
        | {"deductive_reportable": agree["deductive"]["verdict"] in REPORTABLE},
        "correspondences": facetmap.correspondences(facet_map, book["constructs"], supported, self_report["facets"],
                                                    inductive_ok),
        "divergence": divergence.build_map(facet_map, supported, observer["facets"], self_report["facets"],
                                           {c: coded[c]["predictions"] for c in cfg.crosswalk_coders}, inductive_ok),
        "domain_names": instrument["domains"],
        "facet_names": instrument["facets"],
    }


def unmask(ws: store.Workspace) -> dict:
    gens = lifecycle.enter(ws, STAGE)
    invoke.check_method(ws)
    cfg = config.load(ws)
    if "D" not in gens:
        book = store.read_json(ws.path("run/codebook/codebook.json"))
        _crosswalk(ws, cfg, book)
        store.append_generation(ws, "D", [
            "run/crosswalk/constructs.md", *(f"run/crosswalk/codes-{c}.json" for c in cfg.crosswalk_coders),
            *(ws.rel(p) for g in ("run/calls/unmask/crosswalk/*.json", "run/incidents/unmask/**/*.json")
              for p in sorted(ws.root.glob(g)))])
    results = compute(ws, cfg)
    store.write_json(ws.path("report/results.json"), results)
    store.write_text_atomic(ws.path("report/report.md"), report.render(results))
    store.append_generation(ws, "E", ["report/results.json", "report/report.md"])
    return results


def _cli(ws: store.Workspace, args) -> lifecycle.CliResult:
    unmask(ws)
    return lifecycle.CliResult({"report": "report/report.md"})


STAGE = lifecycle.Stage("unmask", 5, "crosswalk, IPIP reveal, report", _cli, estimate, needs="C",
                        closed_after="E", checks_method=True)
