# autobiographical-blind-extraction

Personality analysis of your diary or memoir by blind Claude Code and Codex raters, checked against your own Big Five test

Run a blind analysis over a body of text about you. Diaries, an autobiography project, even years of scattered writing about different events in your life.

Get back a list of your main personality traits:

- **Consistent:** present through all your life.
- **Fading:** present in earlier years but not later.
- **Emerging:** present in later years but not earlier.

> [!NOTE]
> This is a fun exercise. Its results should not be taken as clinical or medical advice.

## Why

Why not just feed your diary to an agent and ask it for some psychoanalysis? What I found was the judgement is coloured by the agent's memory, AGENTS.md files, account holder info and other leaks.

This tool does three things:

1. **Strips hindsight.** A zoner removes sentences where the author interprets their past self ("looking back, I was…"). Raters judge what the person did, not how they later explained it.
2. **Uses two blind raters, Claude and Codex.** Each runs as a clean-room CLI process with no tools, instruction files or account identity. It never sees who you are, your sealed self-prediction or any later era of your life.
3. **Counts a trait only if both model families find it in separate episodes.** The method and codebook are frozen before any results exist, and scripts compute every number.

**When not to use it:**

- You want advice, a diagnosis or a type. The report describes patterns and agreement, not a clinical assessment. A psychologist can ask you questions and notice what your writing leaves out.
- You want a quick read. Pasting your diary into a chatbot is faster and fine for a conversation. It just isn't blind.
- You only want your Big Five scores. Just do the test directly [IPIP-NEO-120](https://ipip.ori.org).
- Your record is short. Testing 12k and 50k words both produced poor results in tests.
- You're on Linux or Windows. Sorry, but PRs welcome.
- You want to compare people. One subject per workspace.
- You want to psycho-analyse a famous person, by feeding in their autobiography. Example Ben Franklin. Technically possible but the text is already in the models' training data, so expect the result to be coloured.

## Tested on a synthetic memoir, as well as my own diary

Not going to publish my own results but I also had Claude and Codex write a 155k-word fictional memoir: traits planted in every era, traits that fade or appear partway through, hindsight sentences to strip, and decoys. The pass marks were fixed before the run.

| Check | Result |
|---|---|
| Hindsight sentences removed / plain ones kept | 42/42, 12/12 |
| Fades and emergences found at the right era | 4/4 |
| Traits planted in every era, found in at least 5 of 6 | 3/4 |
| Traits never shown, never reported | 2/2 |
| Later eras leaking into earlier ones | none |

The record and the key: [`fixtures/synthetic-memoir/`](fixtures/synthetic-memoir/SOURCE.md). The report: [`examples/synthetic-memoir/`](examples/synthetic-memoir/report.md).

## What you get

`report/report.md` gives:

- how far the two model families agree, and how often they got planted test items wrong
- each trait both found, era by era: consistent, fading, emerging, or held only by assumption
- how your sealed predictions matched
- the blind estimate against your IPIP-NEO-120, by domain and facet
- where the blind read, your predictions and your questionnaire agree and diverge

## Requirements

- macOS. The Codex read sandbox uses `sandbox-exec` and the Claude login comes from the keychain.
- Python 3.10+, standard library only.
- [Claude Code](https://code.claude.com) 2.1.303+ and [Codex CLI](https://github.com/openai/codex) 0.160.1+, both logged in. Calls run on your own subscriptions: at least Max 5x / Pro, the cheap plans are unlikely to be enough.
- Your writing: at least 100k words across 5 or more life eras of 15k words or more, each a period of years. Plain UTF-8 `.txt` or `.md`, so export PDF and Word first.

## Install and use

Paste this into your coding agent:

```text
Clone https://github.com/holarchic/autobiographical-blind-extraction at tag v0.11.0.
Follow skills/autobiographical-blind-extraction/SKILL.md to run the blind extraction on my writing in <path>.
Stop at each review point for me, and never open anything in the workspace's sealed/ folder.
```

The agent runs the steps below, stops three times for you, and never reads your sealed files.

| # | Agent runs | Check |
|---|---|---|
| 1 | `git clone --branch v0.11.0 https://github.com/holarchic/autobiographical-blind-extraction` | `python3 scripts/run.py --version` prints `0.11.0 (<commit>)` |
| 2 | `python3 scripts/run.py init <ws> --text <files> --canary <tokens>` | `<ws>` sits outside any git repo or synced folder |
| 3 | `python3 scripts/run.py prepare <ws>` | stops for review 1 |
| 4 | `python3 scripts/run.py prepare <ws>` | exit 0, then review 2 |
| 5 | `python3 scripts/run.py seal <ws>`, after review 3 | prints the prediction and answer counts |
| 6 | `python3 scripts/run.py freeze <ws>` | exit 0 |
| 7 | `python3 scripts/run.py extract <ws>` | exit 0, or 1 if an arm isn't reportable |
| 8 | `python3 scripts/run.py unmask <ws>` | exit 0 and `report/report.md` |

`python3 scripts/run.py status <ws>` says where a workspace stands. Every model-calling stage prints its call estimate first.

Your three reviews:

1. **Names and eras.** Check the names a local scan pre-filled for redaction and add any it missed. Move the proposed era cuts to life events (a move, a new job, a birth or death) and label each era without names. Nothing is sent before you approve both files.
2. **Zoning.** Read the 20 kept and 20 removed sentences in `prepare/spot-check.json`, mark any wrongly labelled, and approve. Your marks are counted in the report and never change what the raters see.
3. **Sealed comparators.** Write your predictions in `sealed/self-prediction.md` and answer `sealed/ipip-120.csv` in your own editor.

**Canaries** are short words only your own profile or instruction files contain, such as a nickname or a test score. The leak probe checks no rater knows them, so give a hyphenated one in its spaced form too and check none appears in your record.

| Exit | Meaning |
|---|---|
| 0 | stage done |
| 1 | a gate fired: from `prepare` or `freeze` the zoner failed its check and the run stops; from `extract` the report says which arm isn't reportable |
| 2 | usage or preflight failure (a missing login, a bad config, a probe that could not run) |
| 3 | isolation or leak failure; stop, don't work around it |
| 4 | a frozen or sealed file or a CLI changed since qualification; start a new workspace. Don't upgrade Codex mid-run |
| 5 | a model call failed; rerun the stage and completed calls are reused |
| 70 | a harness bug; please report it |

## How it works

- **Isolation.** Before your text is sent, each CLI points at a local fake provider and the harness reads the request it actually sends. Claude calls run with your account identity blanked, Codex calls inside a macOS read sandbox, and a leak probe checks both end to end.
- **Zoning check.** A Claude judge and a Codex judge label about 120 sampled sentences blind. If more than 5 % of kept ones are reflection, or more than 20 % of cut ones are events, the run stops.
- **Evidence bar.** A trait counts in an era only with two occurrences in separate, non-adjacent passages.
- **Foils.** Test items with a known answer are mixed in with the real traits, after a second model family audits each era. The report says how often raters got them wrong.
- **Two scoring stances.** Raters score the questionnaire facets strictly (each score cites that facet's evidence) and leniently. Only the strict scores feed the comparisons.

Structure: [`ARCHITECTURE.md`](ARCHITECTURE.md), including what this version leaves out.

## Behaviour notes

- **Cost.** Franklin's 61k-word *Autobiography* took about 125 calls and 2 hours. The 155k-word synthetic memoir took about 180 calls and 3 hours 20 minutes.
- **Resuming.** Rerun a stage after exit 5. Finished calls are reused when every input still matches.
- **CLI versions.** A newer Codex can fail qualification (exit 3) until its pinned tool results are updated. Claude Code's self-updates don't affect a run in progress.
- **Isolation limit.** Claude's request capture uses a dummy API key, so it can't see what Claude Code adds only for logged-in accounts.
- **Famous texts aren't blind.** Models know published autobiographies and restore redacted names from memory.

## Development

```bash
python3 -m unittest                      # offline, about 20 minutes; ends in OK
ruff check .                             # All checks passed!
tests/live_smoke.sh "$TMPDIR/abx-live"   # live: the Franklin pipeline, ~60 calls on the user's subscriptions
```

Ask the user before the live smoke test. Pull requests and bug reports are welcome: see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Credits

- IPIP-NEO-120 items and key: Johnson, J. A. (2014). Measuring thirty facets of the Five Factor Model with a 120-item public domain inventory. *Journal of Research in Personality*, 51, 78–89. Public domain, via [ipip.ori.org](https://ipip.ori.org).
- Krippendorff's α follows Krippendorff (2011), *Computing Krippendorff's Alpha-Reliability*, and reproduces its worked examples.
- Franklin fixture: *Autobiography of Benjamin Franklin*, Project Gutenberg eBook #20203, with synthetic comparators. Example report: [`examples/franklin/`](examples/franklin/report.md).
- Synthetic memoir: written for this repo by Claude Opus 5.5 and Codex, chapter by chapter from briefs. Every person and place in it is invented.

## Attribution

For those who care about such things, the majority of the code in this repo was produced by Claude Opus 5.5 (Anthropic), with @holarchic guiding Claude, providing insights, direction and critical review throughout the process.

## Licence

MIT. See [LICENSE](LICENSE).
