"""The facet map: one blind call at `extract` linking each construct to the IPIP-NEO facets it would show on, and the
construct-level correspondences with the user's IPIP facet scores at `unmask`.

The mapper sees only construct names and definitions and the facet catalog, never a rule, a trace, the record or
anything sealed. A construct maps to at most three facets, each with a polarity (+ for the facet's high pole), or to
none. The comparison is per construct-facet pair, because one construct can match one facet and not another.
"""

from collections.abc import Mapping
from fractions import Fraction

from . import artifacts, config, invoke, ipip, store

__all__ = ["PATH", "correspondences", "render_definitions", "run", "validate"]

PATH = "run/codebook/facetmap.json"  # construct id -> [{facet, polarity}], every trait construct a key
RESULTS = ("consistent", "inconsistent", "neutral", "n/a")


def render_definitions(constructs: list[Mapping]) -> str:
    """The construct view the mapper and the crosswalk coders get: id, name and definition, nothing else."""
    return "".join(f"- {c['id']}: {c['name']}. {c['definition']}\n" for c in constructs)


def validate(out: dict, construct_ids: list[str]) -> list[str]:
    """Each construct mapped exactly once, none unknown, no facet twice within one construct."""
    mapped = [m["construct_id"] for m in out["constructs"]]
    problems = [] if sorted(mapped) == sorted(construct_ids) else \
        [f"map each construct exactly once ({', '.join(construct_ids) or 'none'})"]
    return problems + [f"{m['construct_id']} lists a facet twice" for m in out["constructs"]
                       if len({f["facet"] for f in m["facets"]}) != len(m["facets"])]


def run(ws: store.Workspace, cfg: config.Config, constructs: list[Mapping]) -> dict[str, list[dict]]:
    """Map every construct on the configured lineage and store the map."""
    store.write_text_atomic(artifacts.path_of(ws, "definitions:B"), render_definitions(constructs))
    ids = [c["id"] for c in constructs]
    out = invoke.call(ws, cfg, invoke.CallRequest(
        stage="extract", role="facet_mapper", lineage=cfg.facet_mapper, run=None, era=None, generation="B",
        slots={"definitions": "definitions:B", "facets": "facets:all"}, validate=lambda o: validate(o, ids)))
    mapping = {m["construct_id"]: m["facets"] for m in out["constructs"]}
    stored = {cid: mapping[cid] for cid in ids}
    store.write_json(ws.path(PATH), stored)
    return stored


def _result(polarity: str, band: str | None) -> str:
    if band is None:
        return "n/a"
    if band == "mid":
        return "neutral"
    return "consistent" if (polarity == "+") == (band == "high") else "inconsistent"


def correspondences(facet_map: Mapping[str, list[dict]], constructs: list[Mapping], supported: set[str],
                    ipip_facets: Mapping[str, Fraction | None], reportable: bool) -> dict:
    """Each supported construct's mapped facets against the user's IPIP facet bands."""
    rows, unmapped = [], []
    for c in sorted(constructs, key=lambda c: c["id"]):
        if c["id"] not in supported:
            continue
        if not facet_map[c["id"]]:
            unmapped.append(c["id"])
        for f in sorted(facet_map[c["id"]], key=lambda f: f["facet"]):
            score = ipip_facets[f["facet"]]
            band = ipip.ipip_band(score)
            rows.append({"construct_id": c["id"], "name": c["name"], "facet": f["facet"], "polarity": f["polarity"],
                         "ipip": None if score is None else float(score), "band": band,
                         "result": _result(f["polarity"], band)})
    return {"rows": rows, "counts": {r: sum(row["result"] == r for row in rows) for r in RESULTS},
            "unmapped": unmapped, "inductive_reportable": reportable}
