import os
import unittest
from unittest import mock

from abx import agreement, extract, facetmap, report, store, unmask
from abx import schema as jsonschema

from . import REPO, recompute
from .helpers import FakeWorkspace, build

TRAITS = ("K00001", "K00002")
EVENT = "K00003"


def ratings(event: dict[str, list[str]]) -> extract.Ratings:
    """Two traits and one event over e1 and e2, two runs per lineage with the same verdicts. Trait verdicts are fixed:
    claude P N P P, codex P N N P (T1 e1, T1 e2, T2 e1, T2 e2); `event` gives each lineage's e1 and e2 verdicts."""
    traits = {"claude": ["present", "absent", "present", "present"],
              "codex": ["present", "absent", "absent", "present"]}
    verdicts = {}
    for lin in ("claude", "codex"):
        for run in (1, 2):
            for e, era in enumerate(("e1", "e2")):
                rows = [{"item_id": t, "verdict": traits[lin][2 * i + e]} for i, t in enumerate(TRAITS)]
                verdicts[extract.RaterKey(lin, run, era)] = (*rows, {"item_id": EVENT, "verdict": event[lin][e]})
    return extract.Ratings(verdicts, {}, {})


BOOK = {"constructs": [{"id": "C01", "item_id": TRAITS[0], "kind": "trait"},
                       {"id": "C02", "item_id": TRAITS[1], "kind": "trait"},
                       {"id": "C03", "item_id": EVENT, "kind": "event"}]}


class KindArmTest(unittest.TestCase):
    def test_the_gate_is_traits_only_and_event_verdicts_never_move_it(self):
        # Hand alpha, nominal, two coders, four paired units (P,P) (N,N) (P,N) (P,P): o_PP 4, o_NN 2, o_PN = o_NP 1;
        # n_P 5, n_N 3, n 8; D_o 2/8; D_e 2*5*3/(8*7) = 30/56; alpha = 1 - (1/4)/(30/56) = 8/15.
        split = ratings({"claude": ["present", "present"], "codex": ["present", "absent"]})
        agreed = ratings({"claude": ["absent", "present"], "codex": ["absent", "present"]})
        for r in (split, agreed):
            gate = extract.arms(r, BOOK, ["e1", "e2"], [])[0]
            self.assertEqual(len(gate.units), 4)
            self.assertAlmostEqual(gate.alpha, 8 / 15)
            self.assertFalse(gate.integrity_audit_due)
        # Events: (P,P) (P,N) gives D_o 2/4, D_e 2*3*1/(4*3), alpha 0; (N,N) (P,P) gives alpha 1.
        self.assertAlmostEqual(extract.events_arm(split, BOOK, ["e1", "e2"]).alpha, 0.0)
        events = extract.events_arm(agreed, BOOK, ["e1", "e2"])
        self.assertAlmostEqual(events.alpha, 1.0)
        self.assertTrue(events.integrity_audit_due)  # due on the arm, but `_agree` never reads it for events
        self.assertAlmostEqual(extract.with_foils(agreed, BOOK, [], ["e1", "e2"]).alpha, 8 / 15)

    def test_a_codebook_with_no_traits_has_no_gate_units(self):
        only_events = {"constructs": [c for c in BOOK["constructs"] if c["kind"] == "event"]}
        gate = extract.arms(ratings({"claude": ["absent", "present"], "codex": ["absent", "present"]}), only_events,
                            ["e1", "e2"], [])[0]
        self.assertEqual((gate.units, gate.reason, gate.verdict), ([], "no_units", "not_reportable"))
        no_events = {"constructs": [c for c in BOOK["constructs"] if c["kind"] == "trait"]}
        events = extract.events_arm(ratings({"claude": ["absent"] * 2, "codex": ["absent"] * 2}), no_events, ["e1"])
        self.assertEqual((events.units, events.alpha), ([], None))

    def test_a_construct_without_a_kind_fails_the_schema(self):
        schema = store.loads_strict((REPO / "schemas" / "codebook.json").read_text())
        con = {"id": "C01", "name": "Thrift", "kind": "trait", "definition": "d" * 10, "sufficiency_rule": "r" * 10,
               "trace": ["c001"]}
        self.assertEqual(jsonschema.errors({"constructs": [con], "appendix": []}, schema), [])
        for bad in ({k: v for k, v in con.items() if k != "kind"}, {**con, "kind": "state"}):
            with self.subTest(bad=bad):
                self.assertTrue(jsonschema.errors({"constructs": [bad], "appendix": []}, schema))


class KindPipelineTest(unittest.TestCase):
    """The fake codebook tags two of ten constructs as events that both lineages agree on in every era."""

    @classmethod
    def setUpClass(cls):
        cls.fw = FakeWorkspace()
        # Strict scores mirrored across lineages keep the deductive gate under the integrity trigger.
        with mock.patch.dict(os.environ, {"ABX_FAKE_STRICT_MIRROR": "1"}):
            cls.ws = build(cls.fw)
            cls.results = unmask.unmask(cls.ws)
        cls.book = store.read_json(cls.ws.path("run/codebook/codebook.json"))
        cls.events = {c["id"] for c in cls.book["constructs"] if c["kind"] == "event"}
        cls.traits = {c["id"] for c in cls.book["constructs"] if c["kind"] == "trait"}
        cls.report = cls.ws.path("report/report.md").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.fw.close()

    def test_the_fixture_has_both_kinds(self):
        self.assertEqual(len(self.events), 2)
        self.assertGreater(len(self.traits), 0)

    def test_events_reach_no_comparison(self):
        r = self.results
        self.assertEqual(set(store.read_json(self.ws.path(facetmap.PATH))), self.traits)
        for rel in ("run/codebook/definitions.md", "run/crosswalk/constructs.md"):
            listed = {line[2:5] for line in self.ws.path(rel).read_text().splitlines() if line.startswith("- C")}
            self.assertEqual(listed, self.traits, rel)
        self.assertFalse({row["construct_id"] for row in r["temporal"]["constructs"]} & self.events)
        self.assertFalse({row["construct_id"] for row in r["correspondences"]["rows"]} & self.events)
        self.assertFalse(set(r["correspondences"]["unmapped"]) & self.events)
        self.assertFalse({c["construct_id"] for row in r["divergence"]["rows"] for c in row["constructs"]}
                         & self.events)
        supported_traits = {c["id"] for c in r["constructs"]
                            if c["kind"] == "trait" and "supported" in c["status"].values()}
        self.assertEqual(r["predictions"]["supported_eligible"], len(supported_traits))
        self.assertFalse(set(r["predictions"]["constructs_matched"]) & self.events)

    def test_events_are_recoded_and_reported_apart(self):
        r = self.results
        eras = list(r["corpus"])
        status = {c["name"]: c["status"] for c in r["constructs"] if c["id"] in self.events}
        self.assertEqual(status["Death of a parent"], dict.fromkeys(eras, "supported"))
        self.assertEqual(status["Serious illness"], {eras[0]: "supported", **dict.fromkeys(eras[1:], "not-shown")})
        section = self.report.split("## Things that happened")[1].split("\n## ")[0]
        traits_table = self.report.split("## Traits")[1].split("\n## ")[0]
        for cid in self.events:
            self.assertIn(f"| {cid} |", section)
            self.assertNotIn(f"| {cid} |", traits_table)
        self.assertEqual(r["agreement"]["inductive"]["units"], len(self.traits) * len(eras))
        self.assertEqual(r["agreement"]["inductive_events"]["units"], len(self.events) * len(eras))

    def test_a_perfect_events_alpha_triggers_no_integrity_audit(self):
        a = self.results["agreement"]
        self.assertGreaterEqual(a["inductive_events"]["alpha"], agreement.INTEGRITY_TRIGGER)
        self.assertLess(a["inductive"]["alpha"], agreement.INTEGRITY_TRIGGER)
        self.assertLess(a["deductive"]["alpha"], agreement.INTEGRITY_TRIGGER)
        self.assertIsNone(a["integrity_audit"])
        self.assertIn("- Events (diagnostic, not a gate;", self.report)

    def test_every_figure_recomputes(self):
        self.assertEqual(recompute.problems(self.ws), [])


class OneKindPipelineTest(unittest.TestCase):
    def run_with(self, kinds: str) -> tuple[store.Workspace, dict]:
        fw = FakeWorkspace()
        self.addCleanup(fw.close)
        with mock.patch.dict(os.environ, {"ABX_FAKE_KINDS": kinds}):
            ws = build(fw)
            results = unmask.unmask(ws)
        self.assertEqual(recompute.problems(ws), [])
        return ws, results

    def test_a_codebook_of_events_only(self):
        ws, r = self.run_with("all_events")
        self.assertEqual((r["agreement"]["inductive"]["units"], r["agreement"]["inductive"]["reason"]),
                         (0, "no_units"))
        self.assertGreater(r["agreement"]["inductive_events"]["units"], 0)
        self.assertEqual(r["temporal"]["constructs"], [])
        self.assertEqual(r["predictions"]["supported_eligible"], 0)
        self.assertIn(report.REASON_TEXT["no_units"], ws.path("report/report.md").read_text())

    def test_a_codebook_of_traits_only(self):
        ws, r = self.run_with("all_traits")
        self.assertEqual(r["agreement"]["inductive_events"]["units"], 0)
        self.assertIsNone(r["agreement"]["inductive_events"]["alpha"])
        self.assertIn("None: every construct is a trait.", ws.path("report/report.md").read_text())
