import re
import unittest
from fractions import Fraction

from abx import facetmap, prompts, report, schema, store, unmask
from abx.errors import IsolationError

from .helpers import FakeWorkspace, build


def _m(cid: str, *facets: tuple[str, str]) -> dict:
    return {"construct_id": cid, "facets": [{"facet": f, "polarity": p} for f, p in facets]}


class ValidateTest(unittest.TestCase):
    def test_each_construct_mapped_once_with_no_facet_twice(self):
        ids = ["C01", "C02"]
        self.assertEqual(facetmap.validate({"constructs": [_m("C01", ("N1", "+")), _m("C02")]}, ids), [])
        for bad in ([_m("C01")], [_m("C01"), _m("C01"), _m("C02")], [_m("C01"), _m("C02"), _m("C03")]):
            with self.subTest(bad=bad):
                self.assertEqual(len(facetmap.validate({"constructs": bad}, ids)), 1)
        self.assertEqual(facetmap.validate({"constructs": [_m("C01", ("N1", "+"), ("N1", "-")), _m("C02")]}, ids),
                         ["C01 lists a facet twice"])
        self.assertEqual(facetmap.validate({"constructs": []}, []), [])  # an empty codebook maps to nothing

    def test_the_staged_schema_takes_known_facets_up_to_three(self):
        fw = FakeWorkspace()
        self.addCleanup(fw.close)
        s = store.read_json(build(fw, until="prepare").path("prereg/schemas/facetmap.json"))
        three = _m("C01", ("N1", "+"), ("C5", "-"), ("O2", "+"))
        self.assertEqual(schema.errors({"constructs": [three]}, s), [])
        self.assertTrue(schema.errors({"constructs": [_m("C01", ("X9", "+"))]}, s))
        self.assertTrue(schema.errors({"constructs": [{**three, "facets": [*three["facets"], three["facets"][0]]}]}, s))
        self.assertTrue(schema.errors({"constructs": [_m("C01", ("N1", "high"))]}, s))

    def test_the_mapper_sees_names_and_definitions_only(self):
        rendered = facetmap.render_definitions([{"id": "C01", "name": "Thrift", "definition": "Saves money.",
                                                 "sufficiency_rule": "SECRET RULE", "trace": ["c001"]}])
        self.assertEqual(rendered, "- C01: Thrift. Saves money.\n")
        prompts.check_allowlist("facet_mapper", ["definitions:B", "facets:all"])
        for bad in ("codebook:B", "card:B", "era:e1", "pool:all"):
            with self.subTest(bad=bad), self.assertRaises(IsolationError):
                prompts.check_allowlist("facet_mapper", [bad])


CONSTRUCTS = tuple({"id": f"C0{i}", "name": f"Trait {i}"} for i in range(1, 7))
MAP = {"C01": [{"facet": "N1", "polarity": "+"}, {"facet": "C5", "polarity": "-"}],
       "C02": [{"facet": "E1", "polarity": "-"}], "C03": [{"facet": "O2", "polarity": "+"}],
       "C04": [], "C05": [{"facet": "A1", "polarity": "+"}], "C06": [{"facet": "N1", "polarity": "+"}]}
IPIP = {"N1": Fraction(11, 3), "C5": Fraction(7, 3), "E1": Fraction(8, 3), "O2": None, "A1": Fraction(2)}


class CorrespondenceTest(unittest.TestCase):
    def test_each_result_class_by_hand(self):
        got = facetmap.correspondences(MAP, list(CONSTRUCTS), {"C01", "C02", "C03", "C04", "C05"}, IPIP, True)
        self.assertEqual([(r["construct_id"], r["facet"], r["band"], r["result"]) for r in got["rows"]], [
            ("C01", "C5", "low", "consistent"),    # minus with low; 7/3 is low exactly
            ("C01", "N1", "high", "consistent"),   # + with high; 11/3 is high exactly
            ("C02", "E1", "mid", "neutral"),
            ("C03", "O2", None, "n/a"),
            ("C05", "A1", "low", "inconsistent"),  # + with low
        ])
        self.assertEqual(got["counts"], {"consistent": 2, "inconsistent": 1, "neutral": 1, "n/a": 1})
        self.assertEqual(got["unmapped"], ["C04"])
        self.assertNotIn("C06", {r["construct_id"] for r in got["rows"]})  # not supported
        self.assertEqual(got["rows"][1]["ipip"], 11 / 3)

    def test_nothing_supported_gives_zero_pairs(self):
        got = facetmap.correspondences({}, [], set(), {}, False)
        self.assertEqual((got["rows"], got["unmapped"], sum(got["counts"].values())), ([], [], 0))


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWorkspace()
        self.ws = build(self.fw)
        unmask.unmask(self.ws)

    def tearDown(self):
        self.fw.close()

    def test_the_map_is_frozen_in_b_exactly_as_the_mapper_returned_it(self):
        ws = self.ws
        frozen = {p for e in store.verify_manifest(ws) if e["gen"] == "B" for p in e["artifacts"]}
        record = next(ws.root.glob("run/calls/extract/facet_mapper/*.json"))
        self.assertLessEqual({facetmap.PATH, "run/codebook/definitions.md", ws.rel(record)}, frozen)
        rec = store.read_json(record)
        self.assertEqual(sorted(rec["input_artifacts"]),
                         ["definitions:B", "facets:all", "schema:facetmap", "template:facet_mapper"])
        stored = store.read_json(ws.path(facetmap.PATH))
        self.assertEqual(stored, {m["construct_id"]: m["facets"] for m in rec["output"]["constructs"]})
        self.assertTrue(any(len(v) > 1 for v in stored.values()) and any(not v for v in stored.values()))
        book = store.read_json(ws.path("run/codebook/codebook.json"))
        self.assertEqual(list(stored), [c["id"] for c in book["constructs"] if c["kind"] == "trait"])
        self.assertTrue(any(c["kind"] == "event" for c in book["constructs"]))  # the event is never mapped

    def test_rows_stay_under_a_descriptive_notice_when_the_inductive_arm_is_not_reportable(self):
        results = store.read_json(self.ws.path("report/results.json"))
        self.assertTrue(results["correspondences"]["rows"])
        notice = "The inductive arm is not reportable; the rows below are descriptive only."
        for reportable in (True, False):
            r = {**results, "correspondences": {**results["correspondences"], "inductive_reportable": reportable}}
            section = report.render(r).split("## Constructs against your IPIP facets")[1].split("## Limits")[0]
            self.assertEqual(notice in section, not reportable)
            self.assertEqual(len(re.findall(r"^\| C\d", section, re.M)), len(results["correspondences"]["rows"]))


if __name__ == "__main__":
    unittest.main()
