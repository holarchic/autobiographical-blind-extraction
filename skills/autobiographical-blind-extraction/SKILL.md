---
name: autobiographical-blind-extraction
description: Run a pre-registered, blind, two-lineage (Claude + Codex) trait extraction over the user's own diary or autobiography, and compare it with their sealed self-prediction and IPIP-NEO-120. Use when the user asks to run the blind extraction on their life record, diary or autobiography, or invokes this skill by name.
---

# Blind extraction from a life record

You drive `scripts/run.py` stage by stage for the user. The harness does every model call in clean-room processes
and computes every number; your job is to run the stages in order, stop at each review point, and relay what the
harness prints.

## Hard rules

- **Never open, read, list, grep or summarise anything under `<workspace>/sealed/`**, and never ask the user what
  they wrote there. The workspace's `.claude/settings.json` denies it; treat that as a rule, not an obstacle.
- Never paraphrase, edit or "clean up" the user's text, and never author predictions, codebook entries or any input
  the harness feeds to a rater.
- Do not interpret results beyond what `report/report.md` says. It is not a clinical assessment; say so if asked.
- Each model-calling stage prints a call estimate; tell the user before running it. Live runs use their Claude and
  Codex subscriptions.

## Stages

Run from the folder holding `scripts/run.py`. `<ws>` is the workspace; it must sit outside any git repository or
synced folder.

1. `python3 scripts/run.py init <ws> --text <file>... [--canary <term>...]`
   The text is plain UTF-8 `.txt` or `.md` (Markdown is read as plain text; PDF, Word and scans must be exported to
   text first), with a blank line between paragraphs. Several files are joined in the order given, so list them
   oldest first. Canaries are words that only the user's own profile or instruction files would contain (a nickname, a test
   result). Ask the user for two or three; the leak probe checks no rater knows them. The check is a case-insensitive
   substring match on what each CLI sends and replies, so prefer short, distinctive tokens over sentences (a model
   paraphrases), give a hyphenated token in its spaced form too, and check none appears in the text itself.
   Don't upgrade either CLI until the run is finished: after `freeze`, a changed CLI stops the next stage (exit 4).
   `init` stores the versioned binary behind Claude Code's self-updating symlink, so the native installer's updates
   leave the run alone; an install replaced in place (npm), and Codex, rely on the user not upgrading.
2. `python3 scripts/run.py prepare <ws>` — the first run writes `prepare/redactions.json`, `prepare/eras.json` and
   `prepare/segments-preview.txt`, then stops. **Review point 1:** the user reviews the third-party names a local
   scan pre-filled (`"names": {"Full Name": "Name 1"}`; `prepare/names-preview.txt` lists every candidate, likely
   places apart): deletes places and anything that isn't a person to hide, adds names it missed, gives every name
   for one person (first name, nickname, kinship term) the same replacement so raters see one person. If the scan
   flags many non-names (common in text that isn't English), the user lists them in `config.json` `"not_names"`,
   deletes `redactions.json` and `names-preview.txt`, and reruns `prepare`. Then checks the proposed eras (cut at headings and dated passages within the
   size limits; each era's `"basis"` says why), moves the cuts to life events, writes each era's label without names,
   and sets `"approved": true` in both files. The user decides the eras, not you: help them find the breaks, never
   choose for them. The `"note"` in
   `eras.json` states the rule (at least 5 eras of at least 15k words each, under the estimated maximum; in order,
   back to back, every segment in exactly one; cut at life events such as a move, a new school or job, a
   relationship, a birth or death, a gap in the writing; never around traits the user expects), and the preview
   gives each paragraph's word count. Nothing leaves the machine before that. Run `prepare` again: it checks every
   era's size before any model call (exit 2 names an era that is too big and its maximum; split it), then qualifies
   both CLIs, zones the text, has a Claude judge and a Codex judge label a held-out sample blind to check the zoner,
   and prints per-era kept ratios and the calibration result. Exit 1 from `prepare` means the zoner failed that
   calibration: stop, show the user the rates in `prepare/calibration.json`, and say no real run is permitted on this
   zoner. **Review point 2:** ask the user to read `prepare/spot-check.json` (20 kept and 20 removed sentences), mark
   any that are wrongly labelled (`"wrong": true`) and set `"approved": true`; the note in the file says what counts as
   wrong. Never fill it in for them. They may also skim `prepare/retrospective.md` (what was removed) and a few eras of
   `prepare/substrate/` (what raters will see). `freeze` waits for the approval.
3. **Review point 3:** the user fills in `sealed/self-prediction.md` and answers `sealed/ipip-120.csv` (items are
   in `sealed/ipip-120-items.md`) in their own editor. Then `python3 scripts/run.py seal <ws>`.
4. `python3 scripts/run.py freeze <ws>` — refuses with exit 1 if the zoner failed calibration; otherwise one
   foil-author call per era (test items with a known answer, mixed into
   the card later so rater errors can be measured), then pre-registers the method, substrate and foils.
5. `python3 scripts/run.py extract <ws>` — prints its call estimate first (discovery, a facet map, one audit per era,
   recode, ledgers, scoring). Exit 1 means a reliability gate fired: the run is still
   valid and the report will say which arm is not reportable.
6. `python3 scripts/run.py unmask <ws>` — the crosswalk, the IPIP reveal, then `report/report.md`.

`python3 scripts/run.py status <ws>` says where a workspace stands and what to run next.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | stage done | next stage |
| 1 | a gate fired | from `prepare` or `freeze`: the zoner failed calibration; stop, no real run. From `extract`: an arm is not reportable; continue, the report states it |
| 2 | usage or preflight failure | read the message; fix and rerun |
| 3 | isolation or leak failure | stop; show the user the message; do not work around it |
| 4 | a frozen or sealed file changed | stop; a new run needs a new workspace |
| 5 | a model call failed | rerun the same stage; completed calls are reused |
