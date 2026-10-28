import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from abx import agreement, config, evidence, extract, invoke, lifecycle, store, unmask
from abx.errors import CallFailed, IntegrityError, IsolationError, UsageError

from .helpers import FIXTURE, FakeWorkspace, build


class RecurrenceBarTest(unittest.TestCase):
    def bar(self, v):
        return evidence.apply_bar(v, {"s1": 1, "s2": 2, "s3": 3, "s9": 9}.__getitem__)

    def test_one_long_episode_across_adjacent_segments_is_insufficient(self):
        v = {"verdict": "present", "occurrences": [{"sentence_ids": ["s1"]}, {"sentence_ids": ["s2"]}],
             "counter_sentence_ids": []}
        self.assertEqual(self.bar(v)[0], "insufficient")

    def test_two_separate_episodes_qualify(self):
        v = {"verdict": "present", "occurrences": [{"sentence_ids": ["s1"]}, {"sentence_ids": ["s9"]}],
             "counter_sentence_ids": []}
        self.assertEqual(self.bar(v), ("present", None))
        v["occurrences"][0]["sentence_ids"] = ["s1", "s9"]
        self.assertEqual(self.bar(v)[0], "insufficient")

    def test_absent_needs_a_counter_sentence(self):
        self.assertEqual(self.bar({"verdict": "absent", "occurrences": [], "counter_sentence_ids": []})[0],
                         "insufficient")
        self.assertEqual(self.bar({"verdict": "absent", "occurrences": [], "counter_sentence_ids": ["s3"]})[0],
                         "absent")


def _trait(name, *occurrences):
    return {"trait": name, "alexander_id": "frequency",
            "occurrences": [{"sentence_ids": list(ids)} for ids in occurrences]}


class TopTraitsTest(unittest.TestCase):
    def test_at_or_under_the_cap_is_untouched(self):
        out = {"bias_memo": "m", "traits": [_trait("a", ["s00001"])] * 3, "hypotheses": []}
        self.assertIs(evidence.top_traits(out, 3), out)

    def test_keeps_the_most_repeated_in_listed_order(self):
        traits = [_trait("once", ["s00001"]), _trait("thrice", ["s00002"], ["s00003"], ["s00004"]),
                  _trait("twice", ["s00005"], ["s00006"]), _trait("also once", ["s00007"])]
        out = evidence.top_traits({"bias_memo": "m", "traits": traits, "hypotheses": ["h"]}, 2)
        self.assertEqual([t["trait"] for t in out["traits"]], ["thrice", "twice"])
        self.assertEqual((out["bias_memo"], out["hypotheses"]), ("m", ["h"]))

    def test_ties_go_to_more_distinct_sentences_then_the_earlier_listed(self):
        traits = [_trait("first", ["s00001"]), _trait("wider", ["s00002", "s00003", "s00002"]),
                  _trait("second", ["s00004"])]
        out = evidence.top_traits({"traits": traits}, 2)
        self.assertEqual([t["trait"] for t in out["traits"]], ["first", "wider"])

    def test_a_repeated_citation_counts_once(self):
        traits = [_trait("echo", ["s00001"], ["s00001"], ["s00001"]), _trait("two", ["s00002"], ["s00003"])]
        self.assertEqual([t["trait"] for t in evidence.top_traits({"traits": traits}, 1)["traits"]], ["two"])

    def test_an_unrankable_output_is_left_for_the_schema(self):
        out = {"traits": [_trait("a", ["s00001"]), {"trait": "b"}, _trait("c", ["s00002"])]}
        self.assertIs(evidence.top_traits(out, 2), out)
        self.assertEqual(evidence.top_traits("not json", 2), "not json")

    def test_the_prompt_states_the_schemas_cap(self):
        schema = json.loads((FIXTURE.parents[1] / "schemas/discovery.json").read_text())
        cap = schema["properties"]["traits"]["maxItems"]
        self.assertIn(f"at most {cap}", (FIXTURE.parents[1] / "prompts/discovery.md").read_text())


class DiscoveryChecksTest(unittest.TestCase):
    """A trait the cap cuts is still checked: cutting it never hides an invalid output."""

    class Ix:
        def outside(self, era, ids):
            return ["outside"] if "s99999" in ids else []

    def parse(self, traits):
        schema = json.loads((FIXTURE.parents[1] / "schemas/discovery.json").read_text())
        validate, trim = extract.discovery_checks(schema, self.Ix(), "e1")
        out = {"bias_memo": "m", "traits": traits, "hypotheses": []}
        return invoke.parse_output(json.dumps(out), schema, validate, trim)

    def test_an_over_cap_output_is_cut_to_the_most_repeated(self):
        out, problems = self.parse([_trait(f"trait {i}", [f"s{i:05d}"], [f"s{i + 100:05d}"]) for i in range(25)]
                                   + [_trait("weak", ["s00500"])])
        self.assertEqual((problems, len(out["traits"])), ([], 25))
        self.assertNotIn("weak", [t["trait"] for t in out["traits"]])

    def test_an_invalid_cut_trait_still_fails(self):
        strong = [_trait(f"trait {i}", [f"s{i:05d}"], [f"s{i + 100:05d}"]) for i in range(25)]
        for weak in (_trait("outside", ["s99999"]), {"trait": "no citations", "alexander_id": "frequency",
                                                       "occurrences": []}, _trait("bad id", ["x"])):
            with self.subTest(weak=weak["trait"]):
                self.assertNotEqual(self.parse([*strong, weak])[1], [])


class CodebookValidatorTest(unittest.TestCase):
    def book(self, constructs, appendix=()):
        return {"constructs": [{"id": f"C{i:02d}", "name": n, "definition": "d" * 10, "sufficiency_rule": "r" * 10,
                                "trace": t} for i, (n, t) in enumerate(constructs, 1)], "appendix": list(appendix)}

    def test_clean_book_passes(self):
        self.assertEqual(extract.validate_codebook(self.book([("Thrift", ["c001"]), ("Wit", ["c002"])]),
                                                   {"c001", "c002"}), [])

    def test_compound_names_and_unplaced_candidates_fail(self):
        for name in ("Thrift / wit", "Thrift & wit", "Thrift; wit", "Thrift, then wit", "Thrift vs wit"):
            with self.subTest(name=name):
                problems = extract.validate_codebook(self.book([(name, ["c001"])]), {"c001", "c002"})
                self.assertTrue(any("not atomic" in p for p in problems))
                self.assertTrue(any("c002" in p for p in problems))

    def test_one_trait_with_two_objects_is_atomic(self):
        for name in ("Seeks and accepts advice from strangers", "Satirical, lampooning bent",
                     "Longing for escape or the sea"):
            with self.subTest(name=name):
                self.assertEqual(extract.validate_codebook(self.book([(name, ["c001"])]), {"c001"}), [])

    def test_split_needs_two_traces_and_excluded_none(self):
        ok = self.book([("Thrift", ["c001"]), ("Wit", ["c001"])],
                       [{"candidate_ref": "c001", "disposition": "split", "reason": "two claims"},
                        {"candidate_ref": "c002", "disposition": "excluded", "reason": "uncited"}])
        self.assertEqual(extract.validate_codebook(ok, {"c001", "c002"}), [])
        bad = self.book([("Thrift", ["c001"]), ("Wit", ["c001"])])
        self.assertTrue(extract.validate_codebook(bad, {"c001"}))

    def test_an_empty_codebook_is_a_valid_outcome(self):
        empty = {"constructs": [], "appendix": [{"candidate_ref": "c001", "disposition": "excluded", "reason": "x"}]}
        self.assertEqual(extract.validate_codebook(empty, {"c001"}), [])
        self.assertEqual(extract.validate_codebook({"constructs": [], "appendix": []}, set()), [])
        from abx import agreement
        self.assertEqual(agreement.arm({}, [], "nominal").reason, "no_units")

    def test_a_construct_must_trace_a_candidate(self):
        book = self.book([("Thrift", ["c001"]), ("Invented", [])])
        self.assertTrue(any("traces no candidate" in p for p in extract.validate_codebook(book, {"c001"})))

    def test_split_within_one_construct_is_not_a_split(self):
        one = self.book([("Thrift", ["c001", "c001"])],
                        [{"candidate_ref": "c001", "disposition": "split", "reason": "two claims"}])
        problems = extract.validate_codebook(one, {"c001"})
        self.assertTrue(any("repeats" in p for p in problems))
        self.assertTrue(any("not placed" in p for p in problems))


class ValidationDetailTest(unittest.TestCase):
    """An invalid output's incident names what failed, so a stray citation can be told from a typo."""

    def context(self):
        eras = ({"id": "e1", "segment_ids": ["p1", "p2"]}, {"id": "e2", "segment_ids": ["p3"]})
        sentences = {"s1": {"segment_id": "p1"}, "s2": {"segment_id": "p2"}, "s3": {"segment_id": "p3"}}
        return evidence.Index(eras=eras, sentences=sentences, seg_index={},
                              kept={"e1": frozenset({"s1"}), "e2": frozenset({"s3"})})

    def test_out_of_era_citations_are_named_with_where_they_lie(self):
        problems = self.context().outside("e1", ["s1", "s2", "s3", "s9"])
        self.assertEqual(problems, ["cites 3 sentence id(s) outside the zoned era: s2 (removed:e1), s3 (era:e2), "
                                    "s9 (unknown)"])
        self.assertEqual(self.context().outside("e1", ["s1"]), [])

    def test_coverage_names_missing_repeated_and_unknown_items(self):
        problems = extract.coverage({"K1", "K2", "K3"}, ["K1", "K1", "K4"])
        self.assertEqual(problems, ["verdicts must cover every card item exactly once: missing K2, K3; "
                                    "repeated K1; not on the card K4"])
        self.assertEqual(extract.coverage({"K1"}, ["K1"]), [])


class InductiveArmTest(unittest.TestCase):
    """The binary reading through `extract.arms`: verdicts map before runs collapse."""

    def arm(self, by_rater: dict[tuple[str, int], list[str]]):
        verdicts = {extract.RaterKey(lin, run, "e1"): tuple({"item_id": f"K00000{i + 1}", "verdict": v}
                                                            for i, v in enumerate(vs))
                    for (lin, run), vs in by_rater.items()}
        book = {"constructs": [{"item_id": f"K00000{i}"} for i in (1, 2, 3)]}
        return extract.arms(extract.Ratings(verdicts, {}, {}), book, ["e1"], [])[0]

    def test_absent_and_insufficient_both_map_to_not_present_before_collapse(self):
        # claude's runs on K2: absent, insufficient, present -> mode not_present (2 of 3), not a three-way tie.
        claude = [["present", "absent", "insufficient"], ["present", "insufficient", "insufficient"],
                  ["present", "present", "absent"]]
        codex = [["present", "insufficient", "absent"]] * 3
        arm = self.arm({**{("claude", r + 1): v for r, v in enumerate(claude)},
                        **{("codex", r + 1): v for r, v in enumerate(codex)}})
        units = [extract.Unit(f"K00000{i}", "e1") for i in (1, 2, 3)]
        self.assertEqual([arm.collapsed["claude"][u] for u in units], ["present", "not_present", "not_present"])
        self.assertEqual([arm.collapsed["codex"][u] for u in units], ["present", "not_present", "not_present"])
        self.assertEqual(arm.paired_fraction, 1.0)

    def test_all_insufficient_output_pairs_fully_with_undefined_alpha(self):
        arm = self.arm({(lin, r): ["insufficient"] * 3 for lin in ("claude", "codex") for r in (1, 2, 3)})
        self.assertEqual(arm.paired_fraction, 1.0)
        self.assertIsNone(arm.alpha)

    def test_missing_runs_still_leave_one_lineage(self):
        arm = self.arm({**{("claude", r): ["present"] * 3 for r in (1, 2, 3)}, ("codex", 1): ["present"] * 3})
        unit = extract.Unit("K000001", "e1")
        self.assertIsNone(arm.collapsed["codex"][unit])
        self.assertEqual(agreement.status([arm.collapsed[lin][unit] for lin in ("claude", "codex")]), "one-lineage")
        self.assertEqual(arm.paired_fraction, 0.0)


class ExtractPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = FakeWorkspace()
        cls.ws = build(cls.fw)
        cls.result = store.read_json(cls.ws.path("run/agreement.json"))

    @classmethod
    def tearDownClass(cls):
        cls.fw.close()

    def logged(self):
        return [json.loads(line) for line in self.fw.prompts_logged()]

    def test_generations_and_statuses(self):
        self.assertEqual([e["gen"] for e in store.verify_manifest(self.ws)], ["A", "B", "C"])
        statuses = {s for per_era in self.result["construct_status"].values() for s in per_era.values()}
        self.assertTrue(statuses <= {"supported", "contested", "not-shown", "one-lineage", "no-verdict"}, statuses)
        self.assertTrue({"supported", "not-shown"} <= statuses, statuses)
        self.assertIsNotNone(self.result["inductive"]["alpha"])
        self.assertIsNotNone(self.result["deductive"]["alpha"])
        self.assertGreater(self.result["downgrades"], 0)
        if self.result["deductive"]["integrity_audit_due"]:
            self.assertEqual(set(self.result["integrity_audit"]), {"requalify", "events", "allowlist", "manifest"})
        book = store.read_json(self.ws.path("run/codebook/codebook.json"))
        lineages = {tuple(c["lineages"]) for c in book["constructs"]}
        self.assertEqual(lineages, {("claude",), ("codex",), ("claude", "codex")})

    def test_discovery_is_future_blind(self):
        e2_ids = set(store.read_json(self.ws.path("prepare/zoning/e2.json")))
        for rec_path in self.ws.root.glob("run/calls/extract/discovery/*-e1.json"):
            rec = store.read_json(rec_path)
            self.assertEqual(set(rec["input_artifacts"]), {"template:discovery", "schema:discovery", "era:e1"})
        for entry in self.logged():
            if "abx-role: discovery" in entry["prompt"] and "Era: Boston" in entry["prompt"]:
                self.assertFalse(any(f"[{sid}]" in entry["prompt"] for sid in e2_ids))
        for rec_path in self.ws.root.glob("run/calls/extract/discovery/*-e2.json"):
            rec = store.read_json(rec_path)
            hyp = [a for a in rec["input_artifacts"] if a.startswith("hypotheses:")]
            self.assertEqual(hyp, [f"hypotheses:{rec['lineage']}:{rec['run']}:e1"])

    def test_deductive_calls_carry_no_inductive_artifact(self):
        for rec_path in self.ws.root.glob("run/calls/extract/deductive_*/*.json"):
            kinds = {a.split(":")[0] for a in store.read_json(rec_path)["input_artifacts"]}
            self.assertLessEqual(kinds, {"template", "schema", "era", "ledgers", "facets"})

    def test_no_sealed_content_in_any_prompt(self):
        sealed = [line.split(":", 1)[1].strip() for line in
                  self.ws.path("sealed/self-prediction.md").read_text().splitlines() if line.startswith("P")]
        for entry in self.logged():
            for s in sealed:
                self.assertNotIn(s, entry["prompt"])
            self.assertNotIn("i001,", entry["prompt"])

    def test_a_repeated_recode_record_is_refused(self):
        first = sorted(self.ws.root.glob("run/recode/*.json"))[0]
        copy = first.with_name("zz-" + first.name)
        copy.write_bytes(first.read_bytes())
        try:
            with self.assertRaises(IntegrityError):
                extract.load_ratings(self.ws)
        finally:
            copy.unlink()

    def test_call_counts(self):
        roles = [e["prompt"].splitlines()[0] for e in self.logged()]
        self.assertEqual(roles.count("# abx-role: discovery"), 12)
        self.assertEqual(roles.count("# abx-role: recode"), 12)
        self.assertEqual(roles.count("# abx-role: deductive_ledger"), 12)
        self.assertEqual(roles.count("# abx-role: deductive_scorer"), 6)
        self.assertEqual(roles.count("# abx-role: deductive_scorer_lenient"), 6)
        self.assertEqual(roles.count("# abx-role: codebook_author"), 1)


class AuditEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWorkspace()
        self.ws = build(self.fw)

    def tearDown(self):
        self.fw.close()

    def test_integrity_audit_reads_call_record_events(self):
        c = extract.context(self.ws)
        clean = extract.integrity_audit(c)
        self.assertTrue(all(check["ok"] for check in clean.values()), clean)
        path = next(iter(sorted(self.ws.root.glob("run/calls/extract/recode/*.json"))))
        rec = store.read_json(path)
        rec["events"].append({"type": "item.completed", "detail": "command_execution", "tools": [], "mcp_servers": [],
                              "registry_present": True})
        store.write_json(path, rec)
        tampered = extract.integrity_audit(c)
        self.assertFalse(tampered["events"]["ok"])
        self.assertFalse(tampered["manifest"]["ok"])
        self.assertTrue(tampered["allowlist"]["ok"])


class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWorkspace()
        self.ws = build(self.fw, until="freeze")

    def tearDown(self):
        self.fw.close()

    def test_frozen_prompt_edit_exits_4_before_any_call(self):
        before = len(self.fw.prompts_logged())
        path = self.ws.path("prereg/prompts/recode.md")
        path.write_text(path.read_text() + "\nBe generous.\n")
        with self.assertRaises(IntegrityError) as cm:
            extract.extract(self.ws)
        self.assertEqual(cm.exception.exit_code, 4)
        self.assertIn("recode.md", str(cm.exception))
        self.assertEqual(len(self.fw.prompts_logged()), before)

    def test_sealed_change_exits_4(self):
        path = self.ws.path("sealed/self-prediction.md")
        path.write_text(path.read_text() + "P9: an afterthought.\n")
        with self.assertRaises(IntegrityError):
            extract.extract(self.ws)
        with self.assertRaises(IntegrityError):
            lifecycle.verify_sealed(self.ws)

    def test_scorer_citing_evidence_outside_its_ledgers_fails_the_call(self):
        with mock.patch.dict(os.environ, {"ABX_FAKE_STRAY_CITATION": "1"}), self.assertRaises(CallFailed) as cm:
            extract.extract(self.ws)
        self.assertIn("deductive_scorer", str(cm.exception))

    def test_scorer_may_not_borrow_another_facets_evidence(self):
        with mock.patch.dict(os.environ, {"ABX_FAKE_CROSS_FACET": "1"}), self.assertRaises(CallFailed):
            extract.extract(self.ws)

    def test_sealed_prediction_cannot_reach_any_call_before_generation_c(self):
        from abx import invoke

        req = invoke.CallRequest(stage="unmask", role="crosswalk", lineage="claude", run=None, era=None,
                                 generation="D", slots={"constructs": "constructs:all",
                                                        "predictions": "self_prediction:sealed"})
        with self.assertRaises(IsolationError):
            invoke.call(self.ws, self.fw.config, req)


class MethodVersionTest(unittest.TestCase):
    """A workspace made under an older method version is refused before any stage writes, the model-free
    report path included."""

    def test_extract_and_a_resumed_unmask_refuse_an_older_method(self):
        fw = FakeWorkspace()
        newer = mock.patch.object(invoke, "METHOD_VERSION", invoke.METHOD_VERSION + 1)
        try:
            ws = build(fw, until="freeze")
            calls = len(fw.prompts_logged())
            with newer, self.assertRaises(IntegrityError):
                extract.extract(ws)
            self.assertEqual(len(fw.prompts_logged()), calls)
            self.assertFalse(ws.path("run/discovery").exists())
            extract.extract(ws)
            with mock.patch.object(unmask.report, "render", side_effect=RuntimeError("stop")):
                with self.assertRaises(RuntimeError):
                    unmask.unmask(ws)
            self.assertIn("D", store.frozen_generations(ws))
            with newer, self.assertRaises(IntegrityError):
                unmask.unmask(ws)
            self.assertFalse(ws.path("report/report.md").exists())
        finally:
            fw.close()


    def test_the_cli_refuses_an_older_method_before_its_estimate_reads_the_config(self):
        """An older workspace's config lacks the current roles; the method refusal (exit 4) must come first."""
        sys.path.insert(0, str(FIXTURE.parent.parent / "scripts"))
        import run as cli

        fw = FakeWorkspace()
        try:
            ws = build(fw, until="freeze")
            older = mock.patch.object(invoke, "METHOD_VERSION", invoke.METHOD_VERSION + 1)
            old_config = mock.patch.object(config, "load", side_effect=UsageError("roles is missing 'foil_author'"))
            for stage in ("extract", "unmask"):
                with older, old_config, redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main([stage, str(ws.root)]), 4, err.getvalue())
        finally:
            fw.close()


class ResumeTest(unittest.TestCase):
    def test_interrupted_extract_resumes_without_repeats_and_matches_clean_run(self):
        clean_fw = FakeWorkspace()
        try:
            clean = store.read_json(build(clean_fw).path("run/agreement.json"))
        finally:
            clean_fw.close()
        fw = FakeWorkspace()
        try:
            ws = build(fw, until="freeze")

            def model_calls():
                return [e for e in map(json.loads, fw.prompts_logged()) if "abx-role: probe" not in e["prompt"]]

            calls_before = len(model_calls())
            state = fw.log / "state-claude.json"
            done = json.loads(state.read_text())["calls"]
            with mock.patch.dict(os.environ, {"ABX_FAKE_FAIL_AFTER": str(done + 4)}), self.assertRaises(CallFailed):
                extract.extract(ws)
            failed_attempts = len(model_calls()) - calls_before
            records = set(map(str, ws.root.glob("run/calls/extract/**/*.json")))
            extract.extract(ws)
            resumed = store.read_json(ws.path("run/agreement.json"))
            total = len(model_calls()) - calls_before
            self.assertEqual(failed_attempts, len(records) + 1)
            # 52 extract calls: 2 auditors, 1 facet mapper, a strict and a lenient scorer per rater
            self.assertEqual(total, failed_attempts + 52 - len(records))
            for key in ("inductive", "deductive", "deductive_lenient", "stances", "construct_status"):
                self.assertEqual(resumed[key], clean[key])
        finally:
            fw.close()



class CliResultTest(unittest.TestCase):
    def test_a_fired_gate_exits_1_and_reportable_arms_exit_0(self):
        def arms(inductive, deductive):
            return {"inductive": {"verdict": inductive, "alpha": 0.5},
                    "deductive": {"verdict": deductive, "alpha": 0.9}}
        for verdicts, code in ((("report", "tentative"), 0), (("not_reportable", "report"), 1)):
            with self.subTest(verdicts=verdicts), mock.patch.object(extract, "extract", return_value=arms(*verdicts)):
                out = extract.STAGE.run(None, None)
            self.assertEqual(out.exit_code, code)
            self.assertEqual(out.summary["gates"], {"inductive": verdicts[0], "deductive": verdicts[1]})


if __name__ == "__main__":
    unittest.main()
