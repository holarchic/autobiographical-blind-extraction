import os
import unittest
from unittest import mock

from abx import config, evidence, extract, foils, freeze, invoke, schema, seal, store
from abx.errors import CallFailed, IntegrityError, UsageError

from .helpers import FIXTURE, FakeWorkspace, approve_spot_check, build


def _index() -> evidence.Index:
    """One era of five paragraphs, one kept sentence each, and a second era."""
    eras = ({"id": "e1", "segment_ids": ["p1", "p2", "p3", "p4", "p5"]}, {"id": "e2", "segment_ids": ["p6"]})
    sentences = {f"s0000{i}": {"segment_id": f"p{i}"} for i in range(1, 7)}
    return evidence.Index(eras=eras, sentences=sentences, seg_index={f"p{i}": i for i in range(1, 7)},
                          kept={"e1": frozenset(f"s0000{i}" for i in range(1, 6)), "e2": frozenset({"s00006"})})


def _foil(kind: str, support=(), counter=(), name="Love of solitude", fid="F1") -> dict:
    return {"id": fid, "type": kind, "name": name, "definition": "Seeks time alone.",
            "sufficiency_rule": "Two separate retreats.", "counter_sentence_ids": list(counter),
            "support": [{"sentence_ids": [s], "event": "an episode"} for s in support]}


class FoilEvidenceTest(unittest.TestCase):
    def problems(self, *items):
        return foils.validate({"foils": list(items)}, _index(), "e1")

    def test_each_type_passes_with_its_evidence(self):
        self.assertEqual(self.problems(_foil("clear", counter=["s00001"])), [])
        self.assertEqual(self.problems(_foil("near_miss", support=["s00001"], counter=["s00004"])), [])
        self.assertEqual(self.problems(_foil("anchored", support=["s00001", "s00003"])), [])

    def test_each_type_fails_without_its_evidence(self):
        for bad in (_foil("clear"), _foil("clear", support=["s00001"], counter=["s00002"]),
                    _foil("near_miss", counter=["s00002"]), _foil("near_miss", support=["s00001"]),
                    _foil("anchored", support=["s00001", "s00002"]), _foil("anchored", support=["s00001"])):
            with self.subTest(bad=bad):
                self.assertEqual(len(self.problems(bad)), 1)

    def test_citations_outside_the_zoned_era_fail_before_the_recurrence_check(self):
        problems = self.problems(_foil("anchored", support=["s00001", "s99999"]))
        self.assertEqual(problems, ["cites 1 sentence id(s) outside the zoned era: s99999 (unknown)"])
        self.assertIn("era:e2", self.problems(_foil("clear", counter=["s00006"]))[0])

    def test_names_must_be_atomic_and_ids_unique(self):
        self.assertIn("F1: name is not atomic", self.problems(_foil("clear", counter=["s00001"], name="Thrift / wit")))
        self.assertIn("foil ids repeat", self.problems(_foil("clear", counter=["s00001"]),
                                                       _foil("clear", counter=["s00002"])))

    def test_the_schema_takes_one_to_three_foils(self):
        fw = FakeWorkspace()
        self.addCleanup(fw.close)
        s = store.read_json(fw.ws.path("prereg/schemas/foils.json"))
        one = _foil("clear", counter=["s00001"])
        self.assertEqual(schema.errors({"foils": [one]}, s), [])
        self.assertTrue(schema.errors({"foils": []}, s))
        self.assertTrue(schema.errors({"foils": [{**one, "id": f"F{i}"} for i in (1, 2, 3, 1)]}, s))

    def test_a_colliding_foil_item_id_takes_the_next_free_one(self):
        with mock.patch.object(foils.card, "item_id", side_effect=lambda key, salt: f"K<{key}>"):
            taken = {"K<foil:e1:F1>"}
            self.assertEqual(foils.free_id("foil:e1:F1", "salt", taken), "K<foil:e1:F1:1>")
            self.assertEqual(foils.free_id("foil:e2:F1", "salt", taken), "K<foil:e2:F1>")
            self.assertEqual(taken, {"K<foil:e1:F1>", "K<foil:e1:F1:1>", "K<foil:e2:F1>"})

    def test_auditor_must_differ_from_the_foil_author(self):
        fw = FakeWorkspace()
        self.addCleanup(fw.close)
        with self.assertRaises(UsageError):
            config.parse({**fw.cfg, "roles": {**fw.cfg["roles"], "auditor": fw.cfg["roles"]["foil_author"]}})


def _ratings(rows: dict) -> extract.Ratings:
    """{(lineage, run, era): {item_id: verdict}} as named ratings."""
    return extract.Ratings({extract.RaterKey(*k): tuple({"item_id": i, "verdict": v} for i, v in items.items())
                            for k, items in rows.items()}, {}, {})


class FoilRatesTest(unittest.TestCase):
    def test_rates_are_hand_computed_per_type_and_lineage(self):
        kept = [{"type": "clear", "era": "e1", "item_id": "Ka"}, {"type": "anchored", "era": "e2", "item_id": "Kb"}]
        verdicts = _ratings({
            ("claude", 1, "e1"): {"Ka": "present", "Kb": "present"},   # Kb outside its era: ignored
            ("claude", 2, "e1"): {"Ka": "insufficient", "Kb": "present"},  # e.g. a present downgraded by the bar
            ("claude", 1, "e2"): {"Ka": "present", "Kb": "present"},   # Ka outside its era: ignored
            ("claude", 2, "e2"): {"Ka": "present", "Kb": "absent"},
            ("codex", 1, "e1"): {"Ka": "absent", "Kb": "absent"},
            ("codex", 1, "e2"): {"Ka": "absent", "Kb": "present"},
        }).verdicts
        got = foils.endorsement(verdicts, kept, ["claude", "codex"])
        self.assertEqual(got["clear"], {"items": 1, "by_lineage": {
            "claude": {"verdicts": 2, "present": 1, "rate": 0.5},
            "codex": {"verdicts": 1, "present": 0, "rate": 0.0}}})
        self.assertEqual(got["anchored"]["by_lineage"]["claude"], {"verdicts": 2, "present": 1, "rate": 0.5})
        self.assertEqual(got["near_miss"], {"items": 0, "by_lineage": {
            "claude": {"verdicts": 0, "present": 0, "rate": None},
            "codex": {"verdicts": 0, "present": 0, "rate": None}}})

    def test_all_foils_dropped_leaves_every_rate_unavailable(self):
        got = foils.endorsement(_ratings({("claude", 1, "e1"): {"K1": "present"}}).verdicts, [], ["claude"])
        self.assertTrue(all(t["by_lineage"]["claude"]["rate"] is None for t in got.values()))

    def test_the_gate_uses_constructs_only(self):
        """Acceptance: constructs-only alpha fails while constructs plus foils passes; the arm is not reportable."""
        constructs = {"K1": ("present", "not"), "K2": ("not", "present"), "K3": ("present", "present")}
        foil_items = {f"F{i}": ("present", "present") if i % 2 else ("not", "not") for i in range(30)}
        rows = {}
        for run in (1, 2):
            for lin, side in (("claude", 0), ("codex", 1)):
                for era in ("e1", "e2"):
                    rows[(lin, run, era)] = {i: ("present" if v[side] == "present" else "absent")
                                             for i, v in {**constructs, **foil_items}.items()}
        ratings = _ratings(rows)
        book = {"constructs": [{"item_id": i, "kind": "trait"} for i in constructs]}
        gate, _ = extract.arms(ratings, book, ["e1", "e2"], [])
        pooled = extract.with_foils(ratings, book, [{"item_id": i} for i in foil_items], ["e1", "e2"])
        self.assertEqual((len(gate.units), len(pooled.units)), (6, 66))
        self.assertEqual(gate.verdict, "not_reportable")
        self.assertGreaterEqual(pooled.alpha, 0.8)


class FreezeAuthorsFoilsTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWorkspace()
        self.ws = build(self.fw, until="prepare")
        for name in ("self-prediction.md", "ipip-120.csv"):
            self.ws.path(f"sealed/{name}").write_text((FIXTURE / name).read_text())
        seal.seal(self.ws)
        approve_spot_check(self.ws)

    def tearDown(self):
        self.fw.close()

    def test_foils_are_frozen_in_generation_a_with_their_audit_view(self):
        freeze.freeze(self.ws)
        frozen = {p for e in store.verify_manifest(self.ws) for p in e["artifacts"]}
        for era in ("e1", "e2"):
            stored = store.read_json(foils.path(self.ws, era))
            self.assertTrue(stored["foils"])
            for f in stored["foils"]:
                self.assertEqual(f["expected"], {era: foils.EXPECTED[f["type"]]})
                self.assertRegex(f["item_id"], r"^K[0-9a-f]{6}$")
            self.assertEqual(self.ws.path(f"prereg/foils/{era}.md").read_text(),
                             foils.render_for_audit(stored["foils"], era))
            self.assertLessEqual({f"prereg/foils/{era}.json", f"prereg/foils/{era}.md",
                                  f"run/calls/freeze/foil_author/claude-r0-{era}.json"}, frozen)

    def test_an_interrupted_freeze_resumes_reusing_finished_calls(self):
        real, made = invoke.call, []

        def second_fails(*args, **kwargs):
            made.append(args[2].era)
            if len(made) == 2:
                raise CallFailed("interrupted")
            return real(*args, **kwargs)

        with mock.patch.object(invoke, "call", side_effect=second_fails), self.assertRaises(CallFailed):
            freeze.freeze(self.ws)
        self.assertNotIn("A", store.frozen_generations(self.ws))
        record = self.ws.path("run/calls/freeze/foil_author/claude-r0-e1.json")
        before = record.read_bytes()
        prompts_before = len(self.fw.prompts_logged())
        freeze.freeze(self.ws)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(len(self.fw.prompts_logged()), prompts_before + 1)  # only era e2 is authored again
        self.assertIn("A", store.frozen_generations(self.ws))

    def test_three_invalid_outputs_fail_freeze_without_generation_a(self):
        with mock.patch.dict(os.environ, {"ABX_FAKE_INVALID": "foil_author:3"}), self.assertRaises(CallFailed):
            freeze.freeze(self.ws)
        self.assertNotIn("A", store.frozen_generations(self.ws))
        self.assertEqual(len(list(self.ws.root.glob("run/incidents/freeze/foil_author/*.json"))), 3)

    def test_a_sealed_change_during_authoring_prevents_generation_a(self):
        real = foils.author

        def tamper(ws, *args):
            real(ws, *args)
            ws.path("sealed/self-prediction.md").write_text("changed\n")

        with mock.patch.object(foils, "author", side_effect=tamper), self.assertRaises(IntegrityError):
            freeze.freeze(self.ws)
        self.assertNotIn("A", store.frozen_generations(self.ws))


if __name__ == "__main__":
    unittest.main()
