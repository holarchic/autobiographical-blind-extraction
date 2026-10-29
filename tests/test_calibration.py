import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from abx import calibration, freeze, invoke, prepare, prompts, seal, sizing, status, store
from abx import text as abx_text
from abx.errors import CallFailed, GateFired, IsolationError, UsageError

from . import REPO
from .helpers import FIXTURE, FakeWorkspace, approve_spot_check, build
from .recompute import _calibration

JUDGE_RECORDS = "run/calls/prepare/calibration_judge/*.json"


def _t(zoner: str, claude: str, codex: str, sid: str = "s00001") -> calibration.Target:
    return calibration.Target(sid, "e1", zoner, {"claude": claude, "codex": codex})


class ScoreTest(unittest.TestCase):
    def test_rates_are_hand_computed_over_agreed_targets_with_disputes_counted_apart(self):
        targets = [_t("keep", "keep", "keep"), _t("keep", "keep", "mixed"), _t("keep", "remove", "mixed"),
                   _t("keep", "keep", "keep"), _t("remove", "remove", "remove"), _t("mixed", "mixed", "remove"),
                   _t("remove", "keep", "remove"), _t("remove", "keep", "keep")]
        got = calibration.score(targets)
        self.assertEqual(got["false_keep"], {"count": 1, "of": 3, "rate": 1 / 3})  # the agreed removal; one disputed
        self.assertEqual(got["false_remove"], {"count": 1, "of": 3, "rate": 1 / 3})  # the agreed keep; one disputed
        self.assertEqual(got["disputed"], {"keep": 1, "removed": 1})
        self.assertEqual(got["held_out"], 8)
        self.assertEqual(got["judge_agreement"], {"three_label": 4 / 8, "collapsed": 6 / 8, "of": 8})
        self.assertEqual((got["passed"], got["reason"]), (False, "too_small"))

    def test_limits_hold_at_their_boundary_and_fail_past_it(self):
        def run(false_keeps: int, false_removes: int, disputes: int = 0) -> dict:
            keeps = [_t("keep", *(("remove", "remove") if i < false_keeps else ("keep", "keep"))) for i in range(60)]
            removed = [_t("remove", *(("keep", "keep") if i < false_removes else ("remove", "remove")))
                       for i in range(60)]
            split = [_t("keep", "keep", "remove"), _t("remove", "keep", "remove")] * disputes
            return calibration.score(keeps + removed + split)

        self.assertEqual((run(3, 12)["passed"], run(3, 12)["reason"]), (True, None))  # exactly 5 % and 20 %
        self.assertEqual(run(4, 12)["reason"], "over_limit")
        self.assertEqual(run(3, 13)["reason"], "over_limit")
        padded = run(3, 12, disputes=10)  # disputes never move a rate, only the counts beside it
        self.assertEqual((padded["false_keep"], padded["false_remove"]), (run(3, 12)["false_keep"],
                                                                         run(3, 12)["false_remove"]))
        self.assertEqual((padded["held_out"], padded["disputed"]), (140, {"keep": 10, "removed": 10}))

    def test_an_undefined_denominator_or_a_small_set_fails(self):
        self.assertEqual(calibration.score([_t("keep", "keep", "keep")] * 120)["reason"], "undefined")
        self.assertEqual(calibration.score([_t("remove", "remove", "remove")] * 120)["reason"], "undefined")
        all_split = [_t("keep", "keep", "keep")] * 100 + [_t("remove", "keep", "remove")] * 20
        got = calibration.score(all_split)
        self.assertEqual((got["reason"], got["disputed"]), ("undefined", {"keep": 0, "removed": 20}))
        small = [_t("keep", "keep", "keep")] * 50 + [_t("remove", "remove", "remove")] * 49
        self.assertEqual(calibration.score(small)["reason"], "too_small")


class AllocateTest(unittest.TestCase):
    def test_minimum_then_proportional_by_largest_remainder(self):
        self.assertEqual(calibration.allocate({"a": 1, "b": 999}, 60), {"a": 1, "b": 59})
        self.assertEqual(calibration.allocate({"a": 2, "b": 100, "c": 5}, 20), {"a": 1, "b": 17, "c": 2})

    def test_caps_redistribute_and_ties_go_to_the_earlier_era(self):
        self.assertEqual(calibration.allocate({"a": 3, "b": 3}, 10), {"a": 3, "b": 3})
        self.assertEqual(calibration.allocate({"a": 2, "b": 50, "c": 50}, 40), {"a": 2, "b": 19, "c": 19})
        self.assertEqual(calibration.allocate({"a": 1, "b": 1}, 1), {"a": 1, "b": 0})
        self.assertEqual(calibration.allocate({"a": 0, "b": 5}, 3), {"a": 0, "b": 3})
        self.assertEqual(calibration.allocate({"a": 4, "b": 4, "c": 4}, 5), {"a": 2, "b": 2, "c": 1})
        # Equal remainders (39/72 for e1 and e2) tie exactly, so the earlier era wins.
        self.assertEqual(calibration.allocate({"e1": 7, "e2": 31, "e3": 34}, 60), {"e1": 7, "e2": 25, "e3": 28})


def _era_sents(n: int, start: int = 1, per_segment: int = 4) -> list[abx_text.Sentence]:
    return [abx_text.Sentence(f"s{i:05d}", f"p{(i - 1) // per_segment + 1:04d}", 0, 0) for i in range(start, start + n)]


def _zoner(sid: str) -> str:
    n = int(sid[1:])
    return "remove" if n % 9 == 0 else "mixed" if n % 9 == 1 else "keep"


class SampleTest(unittest.TestCase):
    def setUp(self):
        self.eras = {"e1": _era_sents(300), "e2": _era_sents(150, start=301)}
        self.labels = {s.id: _zoner(s.id) for sents in self.eras.values() for s in sents}

    def test_strata_sizes_and_allocation_are_recorded_and_deterministic(self):
        draw = calibration.sample(self.eras, self.labels, "seed")
        self.assertEqual(draw, calibration.sample(self.eras, self.labels, "seed"))
        self.assertNotEqual(draw.targets, calibration.sample(self.eras, self.labels, "other").targets)
        picked = [sid for ids in draw.targets.values() for sid in ids]
        removed = [sid for sid in picked if self.labels[sid] != "keep"]
        self.assertEqual((len(picked), len(removed)), (120, 60))
        self.assertTrue(any(self.labels[sid] == "mixed" for sid in removed))
        self.assertEqual(draw.strata["e1"]["removed"], {"population": 67, "sampled": 40})
        self.assertEqual(draw.strata["e2"]["keep"], {"population": 117, "sampled": 20})
        for era, ids in draw.targets.items():
            self.assertEqual(ids, sorted(ids))
            self.assertLessEqual(set(ids), {s.id for s in self.eras[era]})

    def test_few_removals_are_all_taken_and_keeps_fill_to_the_target(self):
        labels = {sid: ("remove" if lab == "remove" else "keep") for sid, lab in self.labels.items()}
        labels = {sid: lab if int(sid[1:]) < 200 else "keep" for sid, lab in labels.items()}
        draw = calibration.sample(self.eras, labels, "seed")
        picked = [sid for ids in draw.targets.values() for sid in ids]
        self.assertEqual(sum(labels[s] != "keep" for s in picked), 22)
        self.assertEqual(len(picked), 120)


class ExcerptTest(unittest.TestCase):
    def test_targets_sit_in_whole_paragraphs_split_like_zoning_chunks(self):
        sents = _era_sents(40, per_segment=5)
        with mock.patch.object(abx_text, "ZONING_CHUNK", 12):
            parts = calibration.excerpt_parts(sents, {"s00003", "s00017", "s00038"})
        ids = [s.id for part in parts for s in part]
        self.assertEqual(ids, [f"s{i:05d}" for i in [*range(1, 6), *range(16, 21), *range(36, 41)]])
        self.assertTrue(all(len(part) <= 12 for part in parts))

    def test_which_sentence_in_a_paragraph_is_scored_never_changes_the_excerpt(self):
        sents = _era_sents(20)
        self.assertEqual(calibration.excerpt_parts(sents, {"s00005"}), calibration.excerpt_parts(sents, {"s00008"}))

    def test_a_paragraph_longer_than_a_chunk_is_clipped_to_the_targets_chunk(self):
        sents = _era_sents(30, per_segment=30)
        with mock.patch.object(abx_text, "ZONING_CHUNK", 10):
            parts = calibration.excerpt_parts(sents, {"s00014"})
        self.assertEqual([s.id for part in parts for s in part], [f"s{i:05d}" for i in range(11, 21)])


class ParagraphContextTest(unittest.TestCase):
    TEXT = ("Before anything.\n\nCHAPTER TWO\n\nIn 1998, 2019, 2020 and 2021 we moved.\n\n"
            "Later I saw it plainly. It was hard.\n\nANOTHER PART\n\nNo date here at all.")

    def setUp(self):
        self.segs = abx_text.segment(self.TEXT)
        self.sents = abx_text.sentences(self.TEXT, self.segs)
        self.context = calibration.paragraph_context(self.TEXT, self.segs)

    def test_each_paragraph_carries_the_nearest_heading_and_dated_paragraph_above_it(self):
        years = "years in the nearest dated paragraph above: 2019, 2020, 2021"
        self.assertEqual(list(self.context.values()), [
            "",
            '-- paragraph context: nearest heading above: "CHAPTER TWO" --\n',
            f'-- paragraph context: nearest heading above: "CHAPTER TWO"; {years} --\n',
            f'-- paragraph context: nearest heading above: "CHAPTER TWO"; {years} --\n',
            f'-- paragraph context: nearest heading above: "ANOTHER PART"; {years} --\n',
            f'-- paragraph context: nearest heading above: "ANOTHER PART"; {years} --\n'])

    def test_the_excerpt_leads_each_paragraph_with_its_line_whichever_sentence_is_scored(self):
        later = [s for s in self.sents if s.segment_id == self.segs[3].id]
        rendered = [calibration.render_excerpt(self.TEXT, part, self.context)
                    for targets in ({later[0].id}, {later[1].id})
                    for part in calibration.excerpt_parts(self.sents, targets)]
        self.assertEqual(rendered[0], rendered[1])
        self.assertTrue(rendered[0].startswith(self.context[self.segs[3].id]))
        self.assertEqual(rendered[0].count("-- paragraph context"), 1)
        first = calibration.excerpt_parts(self.sents, {self.sents[0].id})[0]
        self.assertTrue(calibration.render_excerpt(self.TEXT, first, self.context).startswith("[s00001]"))


class JudgeAllowlistTest(unittest.TestCase):
    def test_a_judge_gets_only_its_eras_excerpt(self):
        prompts.check_allowlist("calibration_judge", ["excerpt:e1:1"], era="e1")
        for bad, era in (("chunk:e1:1", "e1"), ("era:e1", "e1"), ("excerpt:e2:1", "e1"), ("excerpt:e1:1", None)):
            with self.subTest(bad=bad), self.assertRaises(IsolationError):
                prompts.check_allowlist("calibration_judge", [bad], era=era)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWorkspace()

    def tearDown(self):
        self.fw.close()

    def _seal(self, ws):
        for name in ("self-prediction.md", "ipip-120.csv"):
            ws.path(f"sealed/{name}").write_text((FIXTURE / name).read_text())
        seal.seal(ws)
        approve_spot_check(ws)

    def test_a_passing_zoner_is_calibrated_and_frozen_with_its_evidence(self):
        ws = build(self.fw, until="prepare")
        rec = store.read_json(ws.path(calibration.PATH))
        self.assertTrue(rec["passed"])
        self.assertEqual((rec["held_out"], rec["false_keep"]["count"], rec["false_remove"]["count"]), (120, 0, 0))
        self.assertLess(rec["judge_agreement"]["three_label"], 1)  # the fake codex judge calls mixed sentences remove
        self.assertEqual(rec["judge_agreement"]["collapsed"], 1)
        redacted = ws.path("prepare/redacted.txt").read_text()
        sentences = {s["id"]: s for s in store.read_json(ws.path("prepare/sentences.json"))}
        for part in rec["parts"]:
            body = ws.path(f"{calibration.EXCERPTS}/{part['era']}/{part['part']}.txt").read_text()
            # the text and ids, plus paragraph context lines, nothing else
            context = [line for line in body.splitlines() if line.startswith("-- ")]
            self.assertTrue(all(line.startswith("-- paragraph context: ") and line.endswith(" --") for line in context))
            self.assertEqual("".join(line + "\n" for line in body.splitlines() if not line.startswith("-- ")),
                             "".join(f"[{sid}] {redacted[sentences[sid]['start']:sentences[sid]['end']]}\n"
                                     for sid in part["sentence_ids"]))
        self.assertEqual(_calibration(ws)[1], [])
        self._seal(ws)
        freeze.freeze(ws)
        frozen = {p for e in store.verify_manifest(ws) for p in e["artifacts"]}
        judges = {ws.rel(p) for p in ws.root.glob(JUDGE_RECORDS)}
        self.assertEqual(len(judges), 2 * len(rec["parts"]))
        self.assertLessEqual({calibration.PATH, *judges, *(f"{calibration.EXCERPTS}/{p['era']}/{p['part']}.txt"
                                                          for p in rec["parts"])}, frozen)

    def test_disputes_travel_from_judge_records_into_results_and_report(self):
        from abx import unmask

        from .recompute import problems

        with mock.patch.dict(os.environ, {"ABX_FAKE_JUDGE_FLIP": "codex:7"}):
            ws = build(self.fw)
            unmask.unmask(ws)
        rec = store.read_json(ws.path(calibration.PATH))
        self.assertTrue(rec["disputed"]["keep"] and rec["disputed"]["removed"], rec["disputed"])
        self.assertEqual((rec["false_keep"]["count"], rec["false_remove"]["count"]), (0, 0))
        self.assertEqual(rec["false_keep"]["of"] + rec["false_remove"]["of"] + sum(rec["disputed"].values()), 120)
        self.assertEqual(store.read_json(ws.path("report/results.json"))["calibration"]["disputed"], rec["disputed"])
        self.assertIn(f"the judges split on {rec['disputed']['keep']} kept and {rec['disputed']['removed']} cut",
                      ws.path("report/report.md").read_text())
        self.assertEqual(problems(ws), [])

    def test_a_failing_zoner_stops_prepare_and_freeze_with_exit_1(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import run as cli

        with mock.patch.dict(os.environ, {"ABX_FAKE_JUDGE": "claude:keep,codex:keep"}), \
                self.assertRaises(GateFired):
            build(self.fw, until="prepare")
        ws = store.Workspace(self.fw.ws.root.parent / "w")
        rec = store.read_json(ws.path(calibration.PATH))
        self.assertEqual((rec["passed"], rec["reason"], rec["false_remove"]["rate"]), (False, "over_limit", 1.0))
        self.assertEqual(prepare.receipt_problems(ws), [])
        self.assertEqual(status.status(ws)["next"], "calibration_failed")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["prepare", str(ws.root)]), 1)
        self._seal(ws)
        with self.assertRaises(GateFired):
            freeze.freeze(ws)
        self.assertEqual(store.frozen_generations(ws), set())

    def test_an_interrupted_calibration_leaves_no_record_and_resumes_its_finished_calls(self):
        real, judged = invoke.call, []

        def third_judge_fails(ws, cfg, req):
            if req.role == "calibration_judge":
                judged.append(req.identity)
                if len(judged) == 3:
                    raise CallFailed("interrupted")
            return real(ws, cfg, req)

        with mock.patch.object(invoke, "call", side_effect=third_judge_fails), self.assertRaises(CallFailed):
            build(self.fw, until="prepare")
        ws = store.Workspace(self.fw.ws.root.parent / "w")
        self.assertFalse(ws.path(calibration.PATH).exists())
        done = len(list(ws.root.glob(JUDGE_RECORDS)))
        self.assertEqual(done, 2)
        before = len(self.fw.prompts_logged())
        prepare.prepare(ws)
        rec = store.read_json(ws.path(calibration.PATH))
        self.assertEqual(len(self.fw.prompts_logged()) - before, 2 * len(rec["parts"]) - done)
        # A later rerun interrupted before its judges finish leaves no earlier record to certify the new evidence.
        sorted(ws.root.glob(JUDGE_RECORDS))[-1].unlink()
        judged.clear()
        with mock.patch.object(invoke, "call", side_effect=third_judge_fails), self.assertRaises(CallFailed):
            prepare.prepare(ws)
        self.assertFalse(ws.path(calibration.PATH).exists())

    def test_a_changed_judge_record_or_target_is_caught(self):
        ws = build(self.fw, until="prepare")
        rec = store.read_json(ws.path(calibration.PATH))
        target = rec["targets"][0]
        flipped = {**rec, "targets": [{**target, "judges": {**target["judges"], "claude": "mixed"
                                                            if target["judges"]["claude"] != "mixed" else "keep"}},
                                      *rec["targets"][1:]]}
        store.write_json(ws.path(calibration.PATH), flipped)
        self.assertEqual(len(_calibration(ws)[1]), 1)
        repeated = {**rec, "targets": [rec["targets"][0], *rec["targets"][:-1]]}
        store.write_json(ws.path(calibration.PATH), repeated)
        summary, problems = _calibration(ws)
        self.assertEqual((problems, summary["held_out"]), (["calibration: a target is repeated"], 119))
        for judges in (["claude"], ["claude", "claude"]):
            store.write_json(ws.path(calibration.PATH), {**rec, "judges": judges})
            self.assertIn(f"calibration: judges {judges} are not ['claude', 'codex']", _calibration(ws)[1])
        one_judge = {**rec, "targets": [{**t, "judges": {"claude": t["judges"]["claude"]}} for t in rec["targets"]]}
        store.write_json(ws.path(calibration.PATH), one_judge)
        self.assertEqual(len(_calibration(ws)[1]), len(rec["targets"]))
        store.write_json(ws.path(calibration.PATH), rec)
        self.assertEqual(_calibration(ws), (calibration.summary(rec), []))
        record = sorted(ws.root.glob(JUDGE_RECORDS))[0]
        doubled = store.read_json(record)
        doubled["output"]["labels"].append(doubled["output"]["labels"][0])
        original = record.read_bytes()
        store.write_json(record, doubled)
        self.assertIn("exactly once", _calibration(ws)[1][0])
        record.write_bytes(original)
        record.write_text(record.read_text().replace('"keep"', '"mixed"', 1))
        self._seal(ws)
        with self.assertRaisesRegex(UsageError, "calibration_judge"):
            freeze.freeze(ws)

    def test_the_judge_is_sized_before_any_call(self):
        cfg = {**self.fw.cfg}
        cfg["lineages"] = {**cfg["lineages"], "codex": {**cfg["lineages"]["codex"], "input_budget_tokens": 9000,
                                                        "reserve_output_tokens": 8000}}
        self.fw.cfg = cfg
        with self.assertRaisesRegex(UsageError, "calibration_judge on codex"):
            build(self.fw, until="prepare")
        self.assertEqual(self.fw.prompts_logged(), [])

    def test_the_size_bound_covers_every_chunk_and_excerpt_part(self):
        ws = build(self.fw, until="prepare")
        redacted = ws.path("prepare/redacted.txt").read_text()
        sents = [abx_text.Sentence(**s) for s in store.read_json(ws.path("prepare/sentences.json"))]
        rec = store.read_json(ws.path(calibration.PATH))
        for era in store.read_json(ws.path("prepare/eras.json"))["eras"]:
            mine = [s for s in sents if s.segment_id in set(era["segment_ids"])]
            chunks = [abx_text.render_numbered(redacted, c) for c in abx_text.zoning_chunks(mine)]
            self.assertTrue(all(len(b) <= len(sizing.longest_part(redacted, mine)) for b in chunks), era["id"])
            segs = {s.id: s for s in abx_text.segment(redacted)}
            context = calibration.paragraph_context(redacted, [segs[sid] for sid in era["segment_ids"]])
            excerpts = [ws.path(f"{calibration.EXCERPTS}/{p['era']}/{p['part']}.txt").read_text()
                        for p in rec["parts"] if p["era"] == era["id"]]
            bound = len(sizing.longest_part(redacted, mine, context))
            self.assertTrue(all(len(b) <= bound for b in excerpts), era["id"])
            self.assertTrue(any("-- paragraph context:" in b for b in excerpts), era["id"])


if __name__ == "__main__":
    unittest.main()
