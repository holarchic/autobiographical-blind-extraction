"""`report.md` from `results.json`. Formatting only: every number arrives already computed."""

from types import MappingProxyType

from .agreement import MIN_PAIRED, REPORT, TENTATIVE
from .divergence import CLASSES

__all__ = ["render"]

VERDICT_TEXT = MappingProxyType({"report": f"reportable (α ≥ {REPORT:.2f})",
                "tentative": f"tentative ({TENTATIVE:.3f} ≤ α < {REPORT:.2f})", "not_reportable": "not reportable"})
REASON_TEXT = MappingProxyType({"no_units": "there is nothing to rate (an empty codebook)",
               "no_pairable_unit": "no unit has values from both lineages",
               "no_variation": "the ratings do not vary, so agreement is undefined",
               "fewer_lineages": "fewer than two lineages rated",
               "low_pairing": f"fewer than {MIN_PAIRED:.0%} of units carry a value from both lineages"})
STANCE_REASON_TEXT = MappingProxyType({"no_pairable_unit": "no lineage-facet score has a value under both stances",
                      "no_variation": REASON_TEXT["no_variation"]})


def num(value, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def pct(value) -> str:
    return "n/a" if value is None else f"{value:.0%}"


AUDIT_TEXT = MappingProxyType({"requalified": "both lineages requalified with unchanged fingerprints",
              "fingerprint_moved": "a fingerprint moved since the frozen baseline",
              "clean": "{records} call records audited, none with a tool event",
              "tool_events": "{records_with_tool_events} call record(s) and {tool_event_incidents} incident(s) "
                             "with tool events",
              "within_allowlists": "{records} call records within their roles' allowlists",
              "outside_allowlist": "{outside} of {records} call records outside their role's allowlist",
              "verified": "manifest chain verified across {generations} generations"})


def _check_text(check: dict) -> str:
    template = AUDIT_TEXT.get(check["code"], "could not complete ({code})")
    return template.format(code=check["code"], **check["counts"])


def _arm(name: str, arm: dict) -> list[str]:
    stab = ", ".join(f"{lin} {num(v)}" for lin, v in sorted(arm["stability"].items()))
    lines = [f"- **{name}** ({arm['metric']}, {arm['units']} units): α = {num(arm['alpha'])}, "
             f"{VERDICT_TEXT[arm['verdict']]}."]
    if arm["reason"]:
        lines.append(f"  - Undefined: {REASON_TEXT.get(arm['reason'], arm['reason'])}.")
    lines.append(f"  - Units with both lineages: {pct(arm['paired_fraction'])}; raw agreement on them: "
                 f"{pct(arm['raw_agreement'])}; missing: {pct(arm['missing_rate'])}; "
                 f"stability α within lineage: {stab}.")
    return lines


def _stances(a: dict) -> list[str]:
    """The lenient stance as a sensitivity result: never a gate, never a comparison input."""
    lenient, s = a["deductive_lenient"], a["stances"]
    stab = ", ".join(f"{lin} {num(v)}" for lin, v in sorted(lenient["stability"].items()))
    shift = "n/a" if s["mean_shift"] is None else f"{s['mean_shift']:+.2f}"
    lines = [f"- Deductive facets, lenient stance (sensitivity, not a gate; {lenient['units']} units): "
             f"α = {num(lenient['alpha'])}; units with both lineages: {pct(lenient['paired_fraction'])}; raw "
             f"agreement on them: {pct(lenient['raw_agreement'])}; stability α within lineage: {stab}."]
    if lenient["reason"]:
        lines.append(f"  - Undefined: {REASON_TEXT.get(lenient['reason'], lenient['reason'])}.")
    lines += [f"- Strict against lenient scoring (sensitivity; {s['paired']} of {s['units']} lineage-facet scores "
             f"paired): α = {num(s['alpha'])}; the same score in {pct(s['equal_rate'])}; lenient minus strict "
             f"on average: {shift}."]
    if s["reason"]:
        lines.append(f"  - Undefined: {STANCE_REASON_TEXT.get(s['reason'], s['reason'])}.")
    return lines


def _calibration(c: dict) -> str:
    def rate(key: str) -> str:
        x = c[key]
        return f"{x['count']} of {x['of']} ({pct(x['rate'])}; limit {c['limits'][key]:.0%})"
    a, d = c["judge_agreement"], c["disputed"]
    return (f"- Zoning calibration: {c['held_out']} held-out sentences labelled blind by {' and '.join(c['judges'])} "
            f"judges; where both judges agree, false keeps {rate('false_keep')} and false removals "
            f"{rate('false_remove')}; the judges split on {d['keep']} kept and {d['removed']} cut sentences, which are "
            f"left out and unresolved; they gave the same label to {pct(a['three_label'])} and agreed on keep or "
            f"remove for {pct(a['collapsed'])}. These are rates on the sample, against limits fixed in advance, not "
            "confidence bounds.")


def _correspondences(r: dict) -> list[str]:
    c = r["correspondences"]
    out = ["## Constructs against your IPIP facets", "",
           "A separate model mapped each construct to the questionnaire facets it bears on, from the construct's name "
           "and definition alone, with a direction (+ for the facet's high pole). Each supported construct's facets "
           "are set against your IPIP facet scores. A construct counts when it is supported in at least one era of "
           "the record, while the questionnaire describes you now, so a mismatch may be change over time. Narrative "
           "tends to over-read traits shown in charged scenes and to miss traits that show as absence.", ""]
    if not c["inductive_reportable"]:
        out += ["The inductive arm is not reportable; the rows below are descriptive only.", ""]
    n = c["counts"]
    out.append(f"- Construct–facet pairs: {n['consistent']} consistent, {n['inconsistent']} inconsistent, "
               f"{n['neutral']} neutral (mid band), {n['n/a']} without a facet score.")
    out.append(f"- Supported constructs mapped to no facet: {', '.join(c['unmapped']) or 'none'}.")
    if c["rows"]:
        out += ["", "| Construct | Facet | Direction | IPIP (1–5) | Band | Result |", "|---|---|---|---|---|---|"]
        out += [f"| {row['construct_id']} {row['name']} | {row['facet']} {r['facet_names'][row['facet']]} | "
                f"{row['polarity']} | {num(row['ipip'], 2)} | {row['band'] or 'n/a'} | {row['result']} |"
                for row in c["rows"]]
    return [*out, ""]


def _facet_bands(r: dict) -> list[str]:
    fb = r["facet_bands"]
    n = len(fb["facets"])
    out = ["### Facets, descriptive", "",
           "The same comparison for each facet. Descriptive only, not a headline result: a facet rests on fewer "
           "scores than a domain, and the domain table above is the comparison this report stands on.", "",
           f"- Observer estimates missing for {fb['observer_missing']} of {n} facets; IPIP scores missing for "
           f"{fb['ipip_missing']} of {n}. Same band in {pct(fb['same_band_rate'])} of {fb['compared']} facets "
           "compared.", "",
           "| Facet | Observer (1–7) | Band | IPIP (1–5) | Band | Same band |", "|---|---|---|---|---|---|"]
    for f, row in fb["facets"].items():
        same = "n/a" if row["same_band"] is None else ("yes" if row["same_band"] else "no")
        out.append(f"| {f} {r['facet_names'][f]} | {num(row['observer'], 2)} | {row['observer_band'] or 'n/a'} | "
                   f"{num(row['ipip'], 2)} | {row['ipip_band'] or 'n/a'} | {same} |")
    return [*out, ""]


def _signed(items: list[dict], key: str) -> str:
    return ", ".join(f"{i[key]}{'+' if i['polarity'] == '+' else '−'}" for i in items) or "—"


def _score(value, band: str | None) -> str:
    return f"{num(value, 2)} {band}" if band else num(value, 2)


def _divergence(r: dict) -> list[str]:
    m = r["divergence"]
    out = ["## Where the three views meet", "",
           "Each questionnaire facet gets a direction (high or low) from three views. Blind: the supported constructs "
           "mapped to it, and the observer estimate (1–7). Self: your predictions, tagged to facets by both crosswalk "
           "coders and counted only where they agree. Test: your IPIP answers (1–5). Divergent facets come first: any "
           "two views pointing opposite ways, or a view whose own evidence points both ways (shown, not explained). "
           "\"Only\" compares the blind view with your predictions; the test column says whether the questionnaire "
           "agrees. Test-only facets are questionnaire signals neither other view raised, which is no evidence that "
           "either missed them.", ""]
    if not m["inductive_reportable"]:
        out += ["The inductive arm is not reportable; the constructs below are descriptive only.", ""]
    out.append("- Facets: " + ", ".join(f"{m['counts'][c]} {c}" for c in CLASSES) + ".")
    shared = ", ".join(f"{a['prediction_id']} {a['facet']}" for a in m["ambiguous"]) or "none"
    out.append(f"- Prediction tags the two coders did not share (placed nowhere): {shared}.")
    if m["rows"]:
        out += ["", "| View | Facet | Constructs | Observer | Blind | Predictions | Self | IPIP | Test |",
                "|---|---|---|---|---|---|---|---|---|"]
        out += [f"| {row['class']} | {row['facet']} {r['facet_names'][row['facet']]} | "
                f"{_signed(row['constructs'], 'construct_id')} | {_score(row['observer'], row['observer_band'])} | "
                f"{row['blind'] or '—'} | {_signed(row['predictions'], 'prediction_id')} | {row['self'] or '—'} | "
                f"{_score(row['ipip'], row['ipip_band'])} | {row['test'] or '—'} |"
                for row in m["rows"]]
    return [*out, ""]


NOW_TEXT = MappingProxyType({"observed": "observed in {era}", "held": "held (last observed in {era})",
                            "faded": "faded in {era}", "never observed": "never observed"})


def _transitions(rows: list[dict], key: str) -> str:
    return ", ".join(f"{row['construct_id']} {t['from']} → {t['to']}" for row in rows for t in row[key]) or "none"


def _temporal(r: dict) -> list[str]:
    t = r["temporal"]
    out = ["## Over time", "",
           "An era counts as observed for a construct when both lineages find it there, and as contradicted only when "
           "both find evidence against it there. Anything else (silence, a split, a missing verdict) is evidence "
           "neither way. A trait stated once and then taken for granted stops producing scenes, so an undecided era "
           "after an observation is held by assumption: listed in its own column, never counted as observed.", ""]
    if not t["inductive_reportable"]:
        out += ["The inductive arm is not reportable; this section is descriptive only.", ""]
    out += [f"- Fades (observed, then contradicted): {_transitions(t['constructs'], 'fades')}.",
            f"- Emergences (contradicted, then observed): {_transitions(t['constructs'], 'emergences')}.",
            "- Now reads the last era of the record under that assumption: use it for who the writer was at the end "
            "of the record, and support in any era (what every comparison in this report uses) for lifelong traits. "
            "Now cannot establish who you are today; your questionnaire remains the present-day measure.",
            f"- Constructs with no observed or contradicted era, left out of the table: {t['undecided']}."]
    if t["constructs"]:
        names = {c["id"]: c["name"] for c in r["constructs"]}

        def eras(row: dict, reading: str) -> str:
            return ", ".join(e for e, v in row["eras"].items() if v == reading) or "—"

        out += ["", "| Id | Construct | Observed in | Contradicted in | Held by assumption | Now |",
                "|---|---|---|---|---|---|"]
        out += [f"| {row['construct_id']} | {names[row['construct_id']]} | {eras(row, 'observed')} | "
                f"{eras(row, 'contradicted')} | {', '.join(row['held']) or '—'} | "
                f"{NOW_TEXT[row['now']['state']].format(era=row['now']['era'])} |" for row in t["constructs"]]
    return [*out, ""]


FOIL_TYPES = (("clear", "Clear foils", "false-positive rate"), ("near_miss", "Near-miss foils", "false-positive rate"),
              ("anchored", "Anchored foils", "recovery rate"))


def _foils(r: dict) -> list[str]:
    f = r["foils"]
    dropped = ", ".join(f"{d['era']} {d['id']}" for d in f["dropped"]) or "none"
    out = ["## Foils and the blind audit", "",
           "Foils are test items written from one era with an answer set in advance (absent for clear and near-miss "
           "foils, present for anchored ones), and mixed into the card so raters cannot tell them from constructs. "
           "The rates count every rater run's verdict in the foil's own era. They are descriptive: replicate runs are "
           "not independent samples, and the audit lineage chose which foils stayed, so they do not rank the "
           "lineages.", "",
           f"- Foils written: {f['authored']}; dropped by the audit as unclear: {dropped}."]
    for key, label, rate in FOIL_TYPES:
        t = f["endorsement"][key]
        cells = "; ".join(f"{lin} {pct(c['rate'])} ({c['present']} of {c['verdicts']} verdicts)"
                          for lin, c in sorted(t["by_lineage"].items()))
        out.append(f"- {label} ({rate}, {t['items']} kept): {cells}.")
    flagged = ", ".join(f"{cid} ({', '.join(eras)})" for cid, eras in r["audit_flags"].items()) or "none"
    out += [f"- Constructs the audit flagged with counter-evidence their definition does not account for: {flagged}. "
            "Flags are reported, not acted on; the construct table shows what raters found.", ""]
    return out


def render(r: dict) -> str:
    out = ["# Blind extraction report", "",
           "## This is not a clinical assessment", "",
           "This report describes patterns that two families of language models found, blind, in a written life "
           "record, "
           "and how they line up with your own predictions and questionnaire answers. It is not a diagnosis, a "
           "psychological evaluation or advice. The claim it supports is agreement between raters, never objectivity. "
           "If anything in it is distressing, set it down and talk to someone you trust.", "",
           "## How it was made", ""]
    for era, s in r["corpus"].items():
        out.append(f"- Era {era}: {s['sentences']} sentences in {s['segments']} segments; zoning kept "
                   f"{pct(s['kept_ratio'])} of {s['words']} words ({s['band']}); {s['mixed']} mixed and "
                   f"{s['removed']} reflective sentences removed.")
    out.append(_calibration(r["calibration"]))
    sc = r["spot_check"]
    out.append(f"- Your spot check of zoning: you read {sc['kept']['shown']} kept and {sc['removed']['shown']} removed "
               f"sentences and marked {sc['kept']['marked']} kept and {sc['removed']['marked']} removed as wrongly "
               "labelled. Marks are reported, never applied.")
    for lin, fp in sorted(r["fingerprints"].items()):
        out.append(f"- {lin}: {fp['cli_version']}, model {fp['resolved_model']}.")
    out += ["", "## Agreement (computed by script)", ""]
    out += _arm("Inductive constructs", r["agreement"]["inductive"])
    out += _arm("Deductive facets (strict)", r["agreement"]["deductive"])
    out += _stances(r["agreement"])
    d = r["agreement"]["inductive_with_foils"]
    out.append(f"- Constructs plus foils (diagnostic, not a gate; {d['units']} units): α = {num(d['alpha'])}; units "
               f"with both lineages: {pct(d['paired_fraction'])}; raw agreement on them: {pct(d['raw_agreement'])}.")
    out.append(f"- Verdicts downgraded to insufficient by the recurrence and counter-evidence rules: "
               f"{r['agreement']['downgrades']}.")
    n = r["invalid_outputs"]
    out.append(f"- Invalid outputs (failed their schema or validator; each call gets up to {n['max_attempts']} "
               f"attempts): {n['total']} ({', '.join(f'{stage} {count}' for stage, count in n['by_stage'].items())}).")
    n = r["trimmed_outputs"]
    out.append(f"- Outputs over a schema cap, cut by script (discovery keeps the traits with the most separate "
               f"occurrences; the call record keeps the full output): {n['total']} "
               f"({', '.join(f'{stage} {count}' for stage, count in n['by_stage'].items())}).")
    audit = r["agreement"]["integrity_audit"]
    if audit:
        verdict = "every check passed" if all(c["ok"] for c in audit.values()) else "a check failed"
        checks = "; ".join(f"{name}: {_check_text(check)}" for name, check in audit.items())
        out.append(f"- Very high agreement triggered the integrity audit: {verdict} ({checks}). High agreement is "
                   "reported with the caveat that both lineages share a method and may share training data.")
    eras = list(r["corpus"])
    out += ["", *_foils(r), "## Constructs", "", f"| Id | Construct | Found by | {' | '.join(eras)} |",
            "|---|---|---|" + "---|" * len(eras)]
    for c in r["constructs"]:
        out.append(f"| {c['id']} | {c['name']} | {', '.join(c['lineages'])} | "
                   + " | ".join(c["status"][e] for e in eras) + " |")
    out += ["", "Supported: both lineages find the construct in the era. Contested: they disagree. Not-shown: "
            "neither finds it. One-lineage: one side has no verdict; never counted as supported. No-verdict: neither "
            "lineage has one (tied or missing runs).", "", *_temporal(r)]
    p = r["predictions"]
    out += ["## Your predictions", ""]
    if not p["inductive_reportable"]:
        out.append("The inductive arm is not reportable, so no construct is eligible to match a prediction.")
    out += [f"- Predictions matched: {len(p['matched'])} of {p['predictions_decided']} decided "
            f"({pct(p['predictions_matched_rate'])}); ambiguous (coders disagree, excluded): "
            f"{', '.join(p['ambiguous']) or 'none'}.",
            f"- Supported constructs matched to a prediction: {len(p['constructs_matched'])} of "
            f"{p['supported_eligible']} ({pct(p['supported_constructs_matched_rate'])}).",
            f"- Coder agreement on prediction-construct pairs: {pct(p['coder_agreement'])}."]
    for pid, outcome in p["outcome"].items():
        out.append(f"  - {pid}: {outcome}")
    if p["descriptive_matches"]:
        out.append(f"- Descriptive only (matches to constructs that are not supported): "
                   f"{', '.join(m['prediction_id'] + '~' + m['construct_id'] for m in p['descriptive_matches'])}.")
    d = r["deductive_vs_ipip"]
    out += ["", "## Observer estimate vs your IPIP-NEO-120", "",
            "The observer estimate covers the whole record; your questionnaire describes you now. "
            "Both your predictions "
            "and the questionnaire are self-report, so this is not independent triangulation.", ""]
    if not d["deductive_reportable"]:
        out += ["The deductive arm is not reportable; the rows below are descriptive only.", ""]
    out += ["| Domain | Observer (1–7) | Band | IPIP (1–5) | Band | Same band |", "|---|---|---|---|---|---|"]
    for dom, row in d["domains"].items():
        same = "n/a" if row["same_band"] is None else ("yes" if row["same_band"] else "no")
        out.append(f"| {r['domain_names'][dom]} | {num(row['observer'], 2)} | {row['observer_band'] or 'n/a'} | "
                   f"{num(row['ipip'], 2)} | {row['ipip_band'] or 'n/a'} | {same} |")
    out += ["", f"Same band in {pct(d['agreement_rate'])} of {d['compared']} domains compared.", "",
            *_facet_bands(r), *_correspondences(r), *_divergence(r), "## Limits", "",
            "- Raters saw one era at a time, so patterns that only show across eras are not captured.",
            "- Discovery used a fixed panel; saturation was not established.",
            "- Narrative over-reads emotionally charged scenes and misses traits that show as absence.",
            "- Models may already know a published text; on one, this run tests the method's plumbing, "
            "not its validity.",
            ""]
    return "\n".join(out)
