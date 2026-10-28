# Architecture

Updated: 23/10/2026

Stdlib-only Python ≥ 3.10. `scripts/run.py` is the CLI; `scripts/abx/` holds one module per stage plus the shared
layers below. This version builds every stage end to end but not every planned requirement; [Not yet built](#not-yet-built)
lists the gaps.

## Layout

```
scripts/run.py              CLI: subcommands, --version, exit codes, all printing
scripts/abx/                the harness package
prompts/*.md                role templates (# abx-role: <role>, {{slot}} placeholders)
schemas/*.json              output schemas, the JSON Schema subset in schema.py
instruments/ipip-neo-120.json   IPIP-NEO-120 items, facets and keys
fixtures/franklin/          public-domain excerpt, redactions, eras, synthetic comparators
skills/autobiographical-blind-extraction/SKILL.md   drives the stages for an agent
tests/                      unittest suite; tests/fakes/ holds fake claude, codex and sandbox-exec
```

## Workspace

A workspace is a folder outside git and synced storage. `init` creates it:

```
config.json                 lineages (CLI paths, models, budgets, Codex disable set, retained tools), roles, canaries
text/original.txt           the user's text, read-only
.claude/settings.json       denies the orchestrating agent any access to sealed/
sealed/                     self-prediction.md, ipip-120.csv (+ items list), written by the user
prepare/                    redactions.json, eras.json (review point, both pre-filled), names-preview.txt, spot-check.json (review point 2), redacted.txt, offsets, segments, sentences,
                            chunks/, zoning/, calibration.json, calibration/<era>/<part>.txt (judge excerpts),
                            substrate/<era>.md, removed-sentences.json, retrospective.md, stats.json, receipt.json
prereg/                     prompts/, schemas/ (facet codes filled from the instrument) and instrument/ copied
                            from the repo, facets.md, foils/<era>.json and .md, sealed-hashes.json,
                            qualification-A.json, manifest.jsonl
run/                        qualification.json (until A), qualification-evidence/, probes/, requalification-audit.json,
                            calls/<stage>/<role>/<rater>.json, incidents/, discovery/, codebook/ (codebook.json
                            and .md, definitions.md, facetmap.json, foils.json, card.md), audit/, recode/,
                            deductive/ (per-era ledgers, aggregate ledgers, scores), crosswalk/, agreement.json
report/                     results.json, report.md
```

## Stages

| Module | Stage | Produces |
|---|---|---|
| `init.py` | `init` | workspace, default config (resolves the native Codex binary behind the npm launcher and the versioned Claude binary behind its self-updating symlink), sealed templates |
| `revise.py` | `revise` | declared so the command exists; refuses (not in this version) |
| `prepare.py` | `prepare` | staged method and review files on first run (an era proposal from `eras.py`, checked with the exact size check and retried under a smaller cap if it fails, its note carrying the era rule and an estimated per-era maximum; the preview gives each paragraph's word count and boundary kind); then redaction, segments/sentences, the era size check (`sizing.py`) before any model call, qualification, zoning in chunks, the zoning calibration (`calibration.py`), substrate, stats, the spot-check sample (`spotcheck.py`, outside the receipt) and a receipt hashing what it read (every staged prompt and schema included) and wrote (the calibration and its judges' call records included); then exit 1 if calibration failed |
| `seal.py` | `seal` | validates sealed/ and records its hashes; `verify_sealed` is called by every later stage |
| `freeze.py` | `freeze` | refuses a workspace qualified under an older isolation policy or method version, then a stale preparation (receipt mismatch), then a failed zoning calibration (exit 1), then waits for the approved spot check (`spotcheck.py`; checked again just before generation A, which pins the file to the digest checked), re-qualifies if a fingerprint moved, authors the foils (`foils.py`), rechecks the preparation and seal, then freezes generation A: method, substrate, foils and their audit view, offset map, seal hashes, statistics, the instrument copy, the facet catalog, the qualification baseline, the zoning calibration and its excerpts, and the zoning, calibration-judge and foil-author call records |
| `extract.py` | `extract` | discovery (past the schema's trait cap, `evidence.top_traits` keeps the traits with the most separate occurrences) → pool → codebook + validator → blind facet map (`facetmap.py`) → blind audit per era (`audit.py`) → card of constructs and kept foils (generation B) → recode with the recurrence bar → deductive ledgers and a strict and a lenient scoring from them → agreement, the stance sensitivity (`stance_sensitivity`: the lenient arm and strict against lenient), the temporal read (`temporal.py`), foil diagnostics and integrity audit (generation C, which also freezes the prepare, freeze and extract incidents) |
| `unmask.py` | `unmask` | two-coder crosswalk, construct matches and each prediction's facet tags (generation D, with its incidents), the calibration summary, IPIP scoring, comparisons (domain bands, descriptive facet bands, construct ↔ IPIP facet correspondences and the three-way map), invalid-output counts from the frozen incidents, trimmed-output counts from the frozen call records, results and report (generation E) |
| `status.py` | `status` | generations, call counts, incidents, next step as a code `run.py` renders (a failed calibration is its own code) |

Each stage module declares a `lifecycle.Stage` descriptor (name, order, arguments, preflight `estimate`, and lifecycle rules: the generation it needs, the one that closes it, whether it checks the seal, whether it refuses an older method version). Every `run` takes the target and the parsed arguments and returns a `lifecycle.CliResult`: the summary to show and the exit code, both decided by the stage (`extract` exits 1 when a gate fired; `lifecycle.returns` adapts a stage that just returns its summary). `run.py` discovers them with `pkgutil`, dispatches uniformly and only prints, verifying the manifest and, where the stage requires it, the method version before printing any estimate. Every workspace stage but `status` first passes `lifecycle.enter`, which verifies the manifest, the frozen files and (where required) the sealed hashes, and applies those rules. Generations B to D also freeze the call records behind their outputs.

## Shared layers

- **`store.py`**: the `Workspace` path holder, strict JSON (no NaN, no duplicate keys), atomic writes (temp file + rename), sha256, and the manifest: an append-only hash chain of generations, each mapping frozen paths to hashes. `verify_manifest` re-hashes every frozen file.
- **`errors.py`**: typed errors carrying their exit codes; `run.py` maps them and turns any other exception into exit 70, never 1.
- **`config.py`**: parses `config.json` once into typed `Config` and `Lineage` records; malformed config is a usage error. Each lineage keeps its config as canonical JSON text, whose hash is the fingerprint's config hash.
- **`evidence.py`**: the zoned-era index (each era's kept sentences, where any sentence and segment sits) and the citation rules every validator shares: `outside` (citations the zoned era lacks, each named with where it lies), `cited` and `apply_bar` (the recurrence and counter-evidence rule).
- **`card.py`**: the card item format raters see, constructs and foils alike: the atomic-name rule, item ids from one hash space, one line format, sorted by id.
- **`roles.py`**: the single table of role policy: each role's allowed artifact kinds, schema, labels it may take, whether it reads zoned material, whether it runs per era and whether in parts of at most 300 sentences (zoner, calibration judge), which inputs must belong to the calling rater, and, for a role that runs on one lineage (zoner, foil author, codebook author, auditor, facet mapper), the config role naming it.
- **`schema.py`**: a validator for the JSON Schema subset the schemas use; unsupported keywords are an error.
- **`eras.py`**: the era proposal, local and deterministic. Every paragraph boundary is a heading, a dated passage (a year 1500–2099) or a bare paragraph break; the count follows the era rule and the per-era cap (`cap_for(n)`, recomputed as the count grows); `partition` is an exact dynamic programme over (paragraphs, eras) choosing the fewest bare breaks, then the fewest dated cuts, then balance, then the earliest cuts, with a half-target floor relaxed when infeasible. Labels are left empty; each era records its basis and a date range.
- **`names.py`**: the candidate-name scan, local and deterministic: runs of up to three capitalised words seen other than at the start of a sentence or clause, title words marking a run titled, function words and a short list of non-names dropped; counts by redaction's whole-word matching; `prefill` maps names seen twice or after a title to `Name N` placeholders (one per person's full name); `preview` lists every candidate.
- **`spotcheck.py`**: the user's spot check of zoning: a seeded sample of up to 20 kept and 20 removed sentences (mixed counts as removed), allocated across eras with `calibration.allocate`, written once by `prepare`; `check` (for `freeze`) recomputes the sample and reads only ids, marks and approval; `summary` counts the marks for the report.
- **`text.py`**: redaction with an offset map, paragraph segments, sentences (code-point offsets), era validation, `label_coverage` (the exact-coverage validator the zoner and the calibration judge share), the zoning partition (`zoning_chunks`: consecutive segments within an era, at most `ZONING_CHUNK` sentences, a longer segment split at sentence boundaries), and the two renderings: numbered chunks for the zoner, zoned eras for raters.
- **`sizing.py`**: the era size check. For each era and per-era role it assembles, through `prompts.assemble`, the largest prompt the call could send (the era rendered all-kept, the staged template and schema, earlier eras' hypotheses at their maximum, the largest card the codebook and foil schemas allow (80 constructs plus 3 foils per era), the auditor's largest codebook and foils, and for the chunked roles the era's 300 longest rendered sentences, which no zoning chunk or excerpt part can exceed), on each lineage the role runs on (the configured one for the zoner, foil author and auditor), and fails before any model call naming the era, its words and the per-era maximum; `max_era_words` gives the eras template's estimate.
- **`artifacts.py`**: the artifact-id grammar (`kind:part:…`, each part checked by kind and arity; a zoning chunk is `chunk:<era>:<part>`, a calibration excerpt part `excerpt:<era>:<part>`) and its mapping to workspace files, contained in the workspace. Every prompt input is one of these files.
- **`withheld.py`**: gathers the text no prompt may carry (redacted names, removed sentences, self-prediction lines, IPIP answers) for the substring audit.
- **`lifecycle.py`**: the `Stage` descriptor and its `CliResult`, the stage-entry guard and the sealed-hash check.
- **`prompts.py`**: the allowlist check against `roles.py` (a per-era call must name its era; era-bound inputs and the schema must be its own), `assemble` (fills a template's slots from artifact ids and the role's declared one-line labels, records each input's hash) and the secondary audit (approved-redacted names whole-word, outside an unchanged shipped template; removed sentences for zoned-material consumers; self-prediction only in crosswalk prompts after generation C; IPIP answers nowhere).
- **`invoke.py`**: the only code that starts a model CLI, and the single call and probe boundary. `claude_argv` / `codex_argv` pin each lineage's invocation; `execute` runs one locked process on the pinned argv in an empty temp dir, and `execute_unlocked` is the positive controls' entry point (locked Claude calls with a temp `HOME` holding only the keychain and an identity-blank copy of the configured account profile, identity env overrides dropped, and an `identity_restored` event if the profile is rewritten; Codex with a temp `CODEX_HOME` and inside a `sandbox-exec` read allowlist), keeping partial output on a timeout; `parse_claude` / `parse_codex` reduce the stream to `Event` records plus every assistant, reasoning and diagnostic text (for canary scans only). `audit_events` reports forbidden kinds as fixed reason codes; `outcome` is the one success predicate (tool event, then timeout or failure, then registry evidence) used by fresh calls, probes, resume and the integrity audit. `call` takes a `CallRequest` of artifact ids, derives template labels and the reveal flag from the workspace, assembles and audits the prompt, holds the call to the frozen qualification baseline after generation A, checks the budget, reuses a record only when it is the same call, every input matches and its stored stream re-derives as a success, and records only validated successes with their events and exit code. Each execution is audited first (a tool event, CLI failure or timeout ends the call); an output failing its schema or validator is recorded as a numbered incident and resampled with the same prompt, up to `MAX_ATTEMPTS`; a request's `trim` runs before the schema, and a record keeps the `untrimmed_output` whenever it cut anything. `CallRequest.part` names a zoning chunk or calibration excerpt part within its era. `METHOD_VERSION` and `ISOLATION_POLICY` are fingerprint fields; `check_method` refuses an older workspace at freeze, extract and unmask; `invalid_outputs` counts invalid-output incidents per stage from the manifest's frozen artifacts, and `trimmed_outputs` the frozen call records holding an `untrimmed_output`. `probe` runs the leak probe (or its positive control), decides it and records it without reply content.
- **`capture.py`**: a loopback fake model provider (Responses-API SSE for Codex, Messages-API SSE for Claude) that answers with a scripted tool call and optionally forwards Codex's non-inference reads to the real backend. Handler threads post messages to one recorder (a request dropped before it is recorded becomes `incomplete` traffic); after the server joins them, `Capture.evidence` is a detached snapshot.
- **`qualify.py`**: per lineage, the request capture (no model) and the leak probe (one model call through `invoke.probe`). One predicate covers every locked capture: every request's registry (and, after the baseline, the baseline registry exactly), request model, transport rule, canaries, the shared success predicate and, for a scripted call, the tool's exact pinned result. File and MCP positive controls use `mcp_canary.py`; every capture's sanitized evidence is recorded, pass or fail. `leak_control` is the probe's own positive control, run in the live smoke.
- **`calibration.py`**: the zoning calibration at `prepare`. `sample` draws the held-out set (up to 60 zoner removals, keeps up to 120 in all, at least 100) stratified by zoner label and allocated across eras by `allocate` (one per era, then largest remainder, capped); `excerpt_parts` gives the judges each target's whole paragraph, clipped to its zoning chunk, so they label every sentence and cannot tell which are scored; `run` calls a calibration judge on every lineage per part and writes `calibration.json` only once all have finished; `score` gives false keeps over the zoner keeps both judges agree on and false removes over the agreed zoner removals, a target the judges split on counted apart as disputed, plus judge agreement over every target; `check` is the gate `prepare` and `freeze` raise (exit 1).
- **`foils.py`**: foil authoring at `freeze`, one call per era on the configured foil-author lineage; the validator (citations inside the zoned era, atomic names, each type's evidence rule: clear and near-miss foils expect absent, anchored foils present, in the authoring era only); the auditor's view rendered from the stored JSON; `endorsement`, the per-type, per-lineage rate of present verdicts in each kept foil's own era.
- **`facetmap.py`**: the blind facet map at `extract`: one call on the configured facet-mapper lineage over the construct names and definitions it renders (`definitions.md`, no rule or trace) and the facet catalog, mapping each construct to at most three facets with polarity, or none; stored as `facetmap.json` and frozen in generation B. At `unmask`, `correspondences` sets each supported construct's facets against the user's IPIP facet bands: consistent, inconsistent, neutral (mid) or n/a, per construct-facet pair, with unmapped supported constructs listed.
- **`divergence.py`**: the three-way map at `unmask`. Per IPIP-NEO facet, a pole for each view: blind (the supported constructs the facet map links there and the observer facet band; a construct conflict is never hidden), self (prediction tags both crosswalk coders share; the rest listed as ambiguous) and test (the IPIP facet band). `classify` checks divergence first (any conflict or opposite pair), then all three, blind and self, blind only, self only, test only. Descriptive; constructs give directions, never scores.
- **`temporal.py`**: the temporal read at `extract`. Per construct and era, `observed` (supported) or `contradicted` (every lineage's three-way collapse absent), else nothing decisive; fades and emergences are transitions between decisive eras; undecided eras after an observation are held by the ratchet assumption, in their own list; `now` reads the last era under that assumption. Stored in the agreement file (generation C); descriptive.
- **`audit.py`**: the blind audit at `extract`, one call per era on the auditor lineage (which must differ from the foil author's), over the codebook view it renders, that era's foils and the zoned era. Its validator requires each of the era's foils decided once and each flag to name a known construct and cite the era. Dropped foils leave the card; construct flags are reported only.
- **`agreement.py`**: Krippendorff's α (nominal, ordinal) over any hashable units, run collapse (mode / rounded median, per unit or across a lineage's runs with `collapse_runs`), the arm gate with its 50 % pairing rule, thresholds and reason codes, per-construct statuses (supported, contested, not-shown, one-lineage, no-verdict), and the integrity-audit runner: typed `Check` outcomes, every check run even after one fails on a harness error.
- **`ipip.py`**: IPIP-NEO-120 parsing and scoring, in exact fractions, and the equal-thirds bands.
- **`extract.py`** also exposes the read interface the verification uses: `load_ratings` returns the blind ratings keyed by `RaterKey`, and `arms` rebuilds both gates from them, mapping each recode verdict to present or not_present before runs collapse. `with_foils` is the diagnostic arm over constructs and kept foils, never a gate.
- **`report.py`**: renders `report.md` from `results.json`; it formats and never computes.

## Tests

- Unit tests per module; `tests/fakes/` emulates both CLIs' event streams and their measured behaviour against the capture provider, with switches for tool events, failures, leaks, version changes and interruption.
- `tests/test_capture_cli.py` runs qualification's capture against the real `claude` and `codex` binaries with a stand-in provider, model-free, and skips when a binary is absent.
- `tests/test_end_to_end.py` drives `run.py` stage by stage on the Franklin fixture with the fakes; `tests/recompute.py` recomputes every report figure from the frozen run files and is shared with `tests/live_smoke.sh`, which runs the same pipeline against the real CLIs.

## Not yet built

- **Preparation.** The per-era kept-word band is reported, not enforced; budget confirmation prints the estimate only.
- **Qualification.** File-read and app/MCP capability classes are qualified with positive controls; the image, web, browser and agent classes are not.
- **Codebook and audit.** The audit never drops or splits a construct; it drops foils and flags constructs.
- **Agreement and comparison.** No Cohen's κ.
- **Revision and resume.** `revise` exists as a stage and refuses: a change to a frozen artifact means a new workspace. A fingerprint change refuses the stage (exit 4) instead of invalidating and rerunning its outputs.
