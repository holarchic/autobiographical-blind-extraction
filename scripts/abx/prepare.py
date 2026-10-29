"""`prepare`: local redaction and eras (review point 1), qualification, zoning, zoning calibration, the substrate."""

import contextlib
import shutil
from pathlib import Path
from typing import NamedTuple

from . import (
    artifacts,
    calibration,
    config,
    eras,
    invoke,
    ipip,
    lifecycle,
    names,
    qualify,
    sizing,
    spotcheck,
    store,
    text,
    withheld,
)
from .errors import TooLarge, UsageError

__all__ = ["METHOD_STAMP", "RECEIPT", "STAGE", "estimate", "prepare", "receipt_problems", "review_doc", "stage_method"]

RECEIPT = "prepare/receipt.json"
RECEIPT_INPUTS = ("text/original.txt", "config.json", "prepare/redactions.json", "prepare/eras.json",
                  "prereg/facets.md")
# Every staged prompt and schema: zoning reads the zoner's, and the size check reads every per-era role's.
RECEIPT_METHOD_GLOBS = ("prereg/prompts/*.md", "prereg/schemas/*.json")
RECEIPT_OUTPUT_GLOBS = ("prepare/redacted.txt", "prepare/offsets.json", "prepare/segments.json",
                        "prepare/sentences.json", "prepare/stats.json", "prepare/removed-sentences.json",
                        "prepare/substrate/*.md", "prepare/chunks/*/*.txt", "prepare/zoning/*.json",
                        calibration.PATH, f"{calibration.EXCERPTS}/*/*.txt",
                        "run/calls/prepare/calibration_judge/*.json")

REPO = Path(__file__).resolve().parents[2]
METHOD_STAMP = "prereg/method.json"  # the method and isolation versions the staged prompts and schemas came from
PROPOSAL_RETRIES, PROPOSAL_SHRINK = 5, 0.10
NAMES_NOTE = ("A local scan pre-filled this list: names seen twice or after a title, each with a placeholder "
              "(names-preview.txt lists every candidate, likely places apart). Delete anything that is not a person "
              "you want hidden (a place, an organisation), change replacements as you like, add names it missed "
              "(nicknames, lowercase spellings, names only ever at the start of a sentence or line), then approve. "
              "Give every name for one person (a first name, a nickname, a kinship term) the same replacement, so the "
              "raters see one person. If the scan flags many words that are not names, list them in config.json "
              "\"not_names\", delete this file and names-preview.txt, and run prepare again.")
PROPOSAL_NOTE = ("This file is a proposal from headings, dated passages and size only; each era's basis says why it "
                 "starts where it does. Move the cuts to life events, and write each label yourself, without names: "
                 "labels are sent to the raters.")
THIN, STOP = 0.50, 0.25
ERA_RULE = ("You decide the eras, before any model call. Aim for at least 5, each a period of years with at least "
            "15k words and no more than the maximum below. Eras run in order, back to back, and every paragraph "
            "(segment) is in exactly one. Cut at life events: a move, a new school or job, a relationship starting or "
            "ending, a birth or death, a long gap in the writing. Never cut around traits you expect to see. A view "
            "you formed later (at 16, about your childhood) belongs to the era you formed it in: move it there in the "
            "text, or cut it, and start a new workspace with init. segments-preview.txt gives each paragraph's word "
            "count.")


def stage_method(ws: store.Workspace) -> None:
    """Copy the repo's prompts, schemas and IPIP instrument in, and derive the facet catalog raters see
    (codes and names only, never questionnaire items); freeze hashes them all in generation A.

    Staged files are kept once copied, edits included. Until qualification, when no call has used them, files staged
    by an older method or isolation version (by the stamp, or no stamp at all) are replaced by this version's."""
    current = {"isolation_policy": invoke.ISOLATION_POLICY, "method_version": invoke.METHOD_VERSION}
    stamp = ws.path(METHOD_STAMP)
    if not ws.path(invoke.QUALIFICATION).exists() and (not stamp.exists() or store.read_json(stamp) != current):
        for name in ("prompts", "schemas"):
            # Only absence is fine: a removal that fails must stop here, before older files are stamped as current.
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(ws.path(f"prereg/{name}"))
    for name in ("prompts", "schemas"):
        if not ws.path(f"prereg/{name}").exists():
            shutil.copytree(REPO / name, ws.path(f"prereg/{name}"))
    if not stamp.exists() or store.read_json(stamp) != current:
        store.write_json(stamp, current)
    if not ws.path(ipip.FROZEN_COPY).exists():
        ws.path(ipip.FROZEN_COPY).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ipip.INSTRUMENT, ws.path(ipip.FROZEN_COPY))
    facets = ipip.load_instrument(ws)["facets"]
    store.write_text_atomic(artifacts.path_of(ws, "facets:all"), "".join(f"- {c} {n}\n" for c, n in facets.items()))
    for name, path in (("ledger", ("facets", "items", "properties", "facet")),
                       ("scores", ("scores", "items", "properties", "facet")),
                       ("facetmap", ("constructs", "items", "properties", "facets", "items", "properties", "facet")),
                       ("crosswalk", ("predictions", "items", "properties", "facets", "items", "properties", "facet"))):
        schema_path = ws.path(f"prereg/schemas/{name}.json")
        schema = store.read_json(schema_path)
        node = schema["properties"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = {"type": "string", "enum": sorted(facets)}
        store.write_json(schema_path, schema)


class _Review(NamedTuple):
    """A review file as read once: the parsed document and the sha256 of the bytes read."""

    doc: dict
    sha256: str


class _Proposed(NamedTuple):
    """The eras to write and, when none passes the exact size check, why."""

    eras: list[dict]
    failure: str | None


def _review_files(ws: store.Workspace, cfg: config.Config, original: str) -> dict[str, _Review] | None:
    """Write the templates the user edits on first run. Returns each review file read exactly once when both are
    approved; None while either is not.
    Redaction and the size check use only this snapshot; model calls, zoning's included, read era labels from the
    file again, and the receipt holds that file to the approved bytes."""
    red, era_file = ws.path("prepare/redactions.json"), ws.path("prepare/eras.json")
    if not red.exists():
        candidates = names.scan(original, text.sentences(original, text.segment(original)), cfg.not_names)
        prefilled = names.prefill(candidates)
        # The preview first: an interruption leaves no redactions file, so the next run scans again.
        store.write_text_atomic(ws.path("prepare/names-preview.txt"), names.preview(candidates, prefilled))
        store.write_json(red, {"approved": False, "note": NAMES_NOTE, "names": prefilled})
    if not era_file.exists():
        segs = text.segment(original)
        sents = text.sentences(original, segs)
        chars_per_word = len(text.render_numbered(original, sents)) / max(len(original.split()), 1)
        limit = sizing.max_era_words(ws, cfg, 5, chars_per_word)
        proposed = _proposal(ws, cfg, original, segs, sents, chars_per_word)
        note = (f"{ERA_RULE} Maximum, estimated: about {limit} words for the sixth era under this config (earlier "
                "eras hold a little more, later ones less; `prepare` checks every era exactly before any call). "
                f"{PROPOSAL_NOTE}"
                + (f" This proposal does not pass the size check: {proposed.failure}" if proposed.failure else ""))
        store.write_json(era_file, {"approved": False, "note": note, "eras": proposed.eras})
        kinds = [b.kind for b in eras.candidates(original, segs)]
        preview = [f"{s.id} ({len(original[s.start:s.end].split())} words, {kind}): {original[s.start:s.end][:100]}"
                   for s, kind in zip(segs, kinds, strict=True)]
        store.write_text_atomic(ws.path("prepare/segments-preview.txt"), "\n".join(preview) + "\n")
    snapshot = {}
    for name in ("redactions.json", "eras.json"):
        raw = ws.path(f"prepare/{name}").read_bytes()
        snapshot[name] = _Review(_parse_review(name, raw), store.sha256_bytes(raw))
    return snapshot if all(review.doc["approved"] is True for review in snapshot.values()) else None


def _proposal(ws: store.Workspace, cfg: config.Config, original: str, segs: list[text.Segment],
              sents: list[text.Sentence], chars_per_word: float) -> _Proposed:
    """The era proposal, and the last failure when none passes the exact size check. The cap is an estimate, so a
    failing proposal is redone under a cap 10 % smaller, up to five times."""
    failure = None
    for retry in range(PROPOSAL_RETRIES + 1):
        scale = (1 - PROPOSAL_SHRINK) ** retry
        proposal = eras.propose(original, segs,
                                lambda n, s=scale: int(sizing.max_era_words(ws, cfg, n - 1, chars_per_word) * s))
        if not proposal.room:
            return _Proposed(proposal.eras, "this config's input budgets leave no room for any era text")
        try:
            sizing.check_eras(ws, cfg, proposal.eras, sents, original)
            return _Proposed(proposal.eras, None)
        except TooLarge as exc:  # only a size failure can be cured by a smaller cap
            failure = str(exc)
    return _Proposed(proposal.eras, failure)


def review_doc(ws: store.Workspace, name: str) -> dict:
    """A review file, parsed whole: an object with `approved` and its list or mapping, else a usage error."""
    return _parse_review(name, ws.path(f"prepare/{name}").read_bytes())


def _parse_review(name: str, raw: bytes) -> dict:
    try:
        doc = store.loads_strict(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsageError(f"{name} is not valid JSON") from exc
    key, kind = ("names", dict) if name == "redactions.json" else ("eras", list)
    if not isinstance(doc, dict) or not isinstance(doc.get(key), kind) or not isinstance(doc.get("approved"), bool):
        raise UsageError(f"{name}: expected an object with \"approved\" (true or false) and \"{key}\"")
    return doc


def estimate(ws: store.Workspace) -> lifecycle.Preflight:
    eras = review_doc(ws, "eras.json")["eras"] if ws.path("prepare/eras.json").exists() else []
    original = ws.path("text/original.txt").read_text(encoding="utf-8")
    segs = text.segment(original)
    text.validate_eras(eras, segs)
    sents = text.sentences(original, segs)
    chunks = sum(len(text.zoning_chunks([s for s in sents if s.segment_id in set(e.get("segment_ids", []))]))
                 for e in eras)
    return lifecycle.Preflight(chunks + 2 + 2 * len(eras), 0,
                               f"one zoning call per chunk of up to {text.ZONING_CHUNK} sentences, one leak probe per "
                               "lineage, and one calibration-judge call per era per lineage (more if an era's "
                               f"calibration excerpt runs past {text.ZONING_CHUNK} sentences)")


def _zone(ws: store.Workspace, cfg: config.Config, era: dict, sents: list[text.Sentence],
          redacted: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for part, chunk in enumerate(text.zoning_chunks(sents), start=1):
        aid = f"chunk:{era['id']}:{part}"
        store.write_text_atomic(artifacts.path_of(ws, aid), text.render_numbered(redacted, chunk))
        req = invoke.CallRequest(stage="prepare", role="zoner", lineage=cfg.zoner, run=None, era=era["id"],
                                 generation="A", slots={"chunk": aid},
                                 validate=text.label_coverage({s.id for s in chunk}), part=part)
        labels |= {lab["sentence_id"]: lab["label"] for lab in invoke.call(ws, cfg, req)["labels"]}
    return labels


def prepare(ws: store.Workspace) -> dict:
    lifecycle.enter(ws, STAGE)
    if ws.path(invoke.QUALIFICATION).exists():
        invoke.check_method(ws)  # a resumed older workspace lacks this method's staged prompts
    cfg = config.load(ws)
    original = ws.path("text/original.txt").read_text(encoding="utf-8")
    stage_method(ws)
    snapshot = _review_files(ws, cfg, original)
    if snapshot is None:
        return {"status": "review", "next": "approve_review_files"}
    redactions = snapshot["redactions.json"].doc["names"]
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in redactions.items()):
        raise UsageError("redactions.json: names must map each name to its replacement text")
    listed = text.name_pattern(redactions) if redactions else None
    holding = [k + 1 for k, r in enumerate(redactions.values()) if listed and listed.search(r)]
    if holding:
        raise UsageError(f"redactions.json: the replacement in entry {holding[0]} contains a name on the list, so that "
                         "name would reach every prompt; change the replacement")
    eras = snapshot["eras.json"].doc["eras"]
    if not all(isinstance(e, dict) for e in eras):
        raise UsageError("eras.json: eras must be a list of era objects")
    # An empty label is sent as the era id, so the id is what to check then.
    labelled = next((k + 1 for k, e in enumerate(eras) if listed and listed.search(text.effective_label(e))), None)
    if labelled:
        raise UsageError(f"eras.json: era entry {labelled}'s label (or its id, when the label is empty) holds a name "
                         "on the redaction list; labels are sent to the raters, so take the name out")
    redacted, spans = text.redact(original, redactions)
    segs = text.segment(redacted)
    sents = text.sentences(redacted, segs)
    text.validate_eras(eras, segs)
    store.write_text_atomic(ws.path("prepare/redacted.txt"), redacted)
    store.write_json(ws.path("prepare/offsets.json"), spans)
    store.write_json(ws.path("prepare/segments.json"), text.as_dicts(segs))
    store.write_json(ws.path("prepare/sentences.json"), text.as_dicts(sents))
    sizing.check_eras(ws, cfg, eras, sents, redacted)
    if not ws.path(invoke.QUALIFICATION).exists():
        qualify.qualify(ws, cfg)
    labels: dict[str, str] = {}
    for era in eras:
        era_sents = [s for s in sents if s.segment_id in set(era["segment_ids"])]
        labels |= _zone(ws, cfg, era, era_sents, redacted)
        store.write_json(ws.path(f"prepare/zoning/{era['id']}.json"),
                         {sid: labels[sid] for sid in (s.id for s in era_sents)})
    calibration.run(ws, cfg, eras, sents, redacted, labels)
    return _substrate(ws, cfg, eras, sents, redacted, labels, {f"prepare/{n}": r.sha256 for n, r in snapshot.items()})


def _substrate(ws, cfg: config.Config, eras, sents, redacted, labels, approved: dict[str, str]) -> dict:
    kept = {sid for sid, lab in labels.items() if lab == "keep"}
    kept_text = {redacted[s.start:s.end] for s in sents if s.id in kept}
    removed = [redacted[s.start:s.end] for s in sents if s.id not in kept]
    store.write_json(ws.path(withheld.REMOVED),
                     [r for r in removed if len(r) >= withheld.MIN_REMOVED_LEN and r not in kept_text])
    store.write_text_atomic(ws.path("prepare/retrospective.md"), "".join(
        f"[{s.id}] ({labels[s.id]}) {redacted[s.start:s.end]}\n" for s in sents if s.id not in kept))
    stats = {}
    for era in eras:
        era_sents = [s for s in sents if s.segment_id in set(era["segment_ids"])]
        body = text.render_zoned(redacted, era["segment_ids"], era_sents, kept)
        store.write_text_atomic(artifacts.path_of(ws, f"era:{era['id']}"), body)
        total = sum(len(redacted[s.start:s.end].split()) for s in era_sents)
        kept_words = sum(len(redacted[s.start:s.end].split()) for s in era_sents if s.id in kept)
        ratio = kept_words / total if total else 0.0
        stats[era["id"]] = {
            "sentences": len(era_sents), "segments": len(era["segment_ids"]), "words": total,
            "kept_words": kept_words, "kept_ratio": ratio,
            "mixed": sum(1 for s in era_sents if labels[s.id] == "mixed"),
            "removed": sum(1 for s in era_sents if labels[s.id] == "remove"),
            "band": "normal" if ratio >= THIN else "thin" if ratio >= STOP else "stop",
        }
    store.write_json(ws.path("prepare/stats.json"), stats)
    spotcheck.write(ws, eras, sents, labels, redacted)  # a review file: the user edits it, so not in the receipt
    receipt = _receipt(ws)
    changed = [rel for rel, digest in approved.items() if receipt[rel] != digest]
    if changed:
        raise UsageError(f"{', '.join(changed)} changed while `prepare` ran; rerun it")
    store.write_json(ws.path(RECEIPT), receipt)
    calibration.check(ws)  # after the receipt, so a failed calibration's evidence stays checked at `freeze`
    return {"status": "done", "stats": stats,
            "calibration": calibration.summary(store.read_json(ws.path(calibration.PATH)))}


def _receipt(ws: store.Workspace) -> dict:
    """Hashes of what this preparation read and wrote, so a later edit to either is caught at `freeze`."""
    outputs = [ws.rel(p) for g in (*RECEIPT_METHOD_GLOBS, *RECEIPT_OUTPUT_GLOBS) for p in sorted(ws.root.glob(g))]
    return {rel: store.sha256_file(ws.path(rel)) for rel in (*RECEIPT_INPUTS, *outputs)}


def receipt_problems(ws: store.Workspace) -> list[str]:
    """Inputs or outputs that changed since `prepare` last finished; empty when preparation is current."""
    if not ws.path(RECEIPT).exists():
        return ["`prepare` has not finished"]
    recorded, current = store.read_json(ws.path(RECEIPT)), _receipt(ws)
    return sorted(rel for rel in set(recorded) | set(current) if recorded.get(rel) != current.get(rel))


STAGE = lifecycle.Stage("prepare", 1, "redaction and eras review, qualification, zoning and its calibration",
                        lifecycle.returns(prepare),
                        estimate,
                        closed_after="A", verifies_seal=False)
