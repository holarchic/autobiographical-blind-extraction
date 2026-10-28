"""Offline smoke: the whole CLI, stage by stage, on the Franklin fixture with fake CLIs.

Asserts that every stage completes, the report exists and is non-vacuous, every report
figure equals its recomputation from the frozen run files, the manifest verifies, and no
sealed content reached a prompt before generation C (self-prediction only in crosswalk
prompts, IPIP answers in none).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from abx import invoke, report, store

from . import REPO, recompute
from .helpers import FAKE_ENV, FAKES, FIXTURE, write_logins

RUN = REPO / "scripts" / "run.py"


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="abx-e2e-")))
        cls.ws_path = cls.tmp / "ws"
        cls.log = cls.tmp / "fakelog"
        # Every recode and crosswalk prompt is answered with non-JSON once, so resampling runs through the real CLI.
        cls.env = {**os.environ, "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}", "ABX_FAKE_LOG": str(cls.log),
                   "PYTHONDONTWRITEBYTECODE": "1", "ABX_FAKE_INVALID": "foil_author:1,recode:1,crosswalk:1"}
        cls.codes = {}
        cls.run_stage("init", "--text", str(FIXTURE / "autobiography.txt"), "--canary", "ABXPROFILECANARY")
        cfg_path = cls.ws_path / "config.json"
        cfg = json.loads(cfg_path.read_text())
        logins = write_logins(cls.tmp)
        cfg["lineages"]["claude"]["profile_file"] = str(logins.claude_profile)
        cfg["lineages"]["codex"].update(sandbox_exec=str(FAKES / "sandbox-exec"), auth_file=str(logins.codex_auth),
                                        capture_forward=False)
        cfg["env_passthrough"] = FAKE_ENV
        cfg_path.write_text(json.dumps(cfg, indent=1))
        cls.run_stage("prepare")
        for name in ("redactions.json", "eras.json"):
            shutil.copy(FIXTURE / name, cls.ws_path / "prepare" / name)
        cls.run_stage("prepare")
        cls.status_before_approval = cls.run_stage("status")
        for name in ("self-prediction.md", "ipip-120.csv"):
            shutil.copy(FIXTURE / name, cls.ws_path / "sealed" / name)
        cls.run_stage("seal")
        cls.unapproved_freeze = cls.run_stage("freeze")
        cls.generations_before_approval = len(store.read_manifest(store.Workspace(cls.ws_path)))
        spot = cls.ws_path / "prepare" / "spot-check.json"
        doc = json.loads(spot.read_text())
        doc["kept"][0]["wrong"] = True
        for item in doc["removed"][:2]:
            item["wrong"] = True
        spot.write_text(json.dumps({**doc, "approved": True}, indent=1))
        for stage in ("freeze", "extract", "unmask"):
            cls.run_stage(stage)
        cls.ws = store.Workspace(cls.ws_path)
        cls.results = store.read_json(cls.ws.path("report/results.json"))
        cls.report = cls.ws.path("report/report.md").read_text()
        cls.prompts = [json.loads(line) for line in (cls.log / "prompts.jsonl").read_text().splitlines()]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_stage(cls, stage: str, *extra: str):
        p = subprocess.run([sys.executable, str(RUN), stage, str(cls.ws_path), *extra], env=cls.env,
                           capture_output=True, text=True, timeout=600, check=False)
        cls.codes[stage] = p.returncode
        if p.returncode != 0:
            raise AssertionError(f"{stage} exited {p.returncode}: {p.stderr[-2000:]}")
        return p.stdout + p.stderr

    def test_every_stage_completed_and_status_is_done(self):
        self.assertTrue(all(code == 0 for code in self.codes.values()), self.codes)
        p = subprocess.run([sys.executable, str(RUN), "status", str(self.ws_path)], env=self.env,
                           capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(p.stdout)["next"], "done: read report/report.md")
        self.assertEqual([e["gen"] for e in store.verify_manifest(self.ws)], ["A", "B", "C", "D", "E"])

    def test_invalid_outputs_are_counted_from_frozen_incidents_only(self):
        n = self.results["invalid_outputs"]["by_stage"]
        # Replicate runs send the same prompt, and the fake fails each distinct prompt once: recode 2 lineages x 2
        # eras, the foil author 1 lineage x 2 eras.
        self.assertEqual((n["prepare"], n["freeze"], n["extract"], n["unmask"]), (0, 2, 4, 2))
        self.assertIn("Invalid outputs (failed their schema or validator; each call gets up to 3 attempts): 8 "
                      "(prepare 0, freeze 2, extract 4, unmask 2).", self.report)
        self.assertIn("Outputs over a schema cap, cut by script (discovery keeps the traits with the most separate "
                      "occurrences; the call record keeps the full output): 0 (prepare 0, freeze 0, extract 0, "
                      "unmask 0).", self.report)
        frozen ={path for e in store.verify_manifest(self.ws) for path in e["artifacts"]}
        incidents = {self.ws.rel(p) for p in self.ws.root.glob("run/incidents/**/*.json")}
        self.assertEqual(len(incidents), 8)
        self.assertLessEqual(incidents, frozen)
        victim = self.ws.path(sorted(incidents)[0])
        saved = victim.read_bytes()
        victim.unlink()
        try:
            self.assertTrue(recompute.problems(self.ws))
        finally:
            victim.write_bytes(saved)
        self.assertEqual(recompute.problems(self.ws), [])
        stray = self.ws.path("run/incidents/extract/recode/added-after-freezing-1.json")
        store.write_json(stray, {"identity": "x", "kind": "invalid_output", "detail": []})
        try:
            self.assertEqual(invoke.invalid_outputs(self.ws), n)
            self.assertEqual(recompute.problems(self.ws), [])
        finally:
            stray.unlink()

    def test_freeze_waits_for_the_spot_check_and_the_report_counts_the_marks(self):
        self.assertIn("spot-check.json", self.status_before_approval)
        self.assertIn("spot-check.json", self.unapproved_freeze)
        self.assertEqual(self.generations_before_approval, 0)
        self.assertEqual(self.results["spot_check"], {"kept": {"shown": 20, "marked": 1},
                                                      "removed": {"shown": 20, "marked": 2}})
        frozen = {path for e in store.verify_manifest(self.ws) for path in e["artifacts"]}
        self.assertIn("prepare/spot-check.json", frozen)

    def test_a_report_with_no_facet_compared_still_lists_every_facet(self):
        results = json.loads(json.dumps(self.results))
        fb = results["facet_bands"]
        for row in fb["facets"].values():
            row.update(observer=None, observer_band=None, same_band=None)
        fb.update(observer_missing=len(fb["facets"]), compared=0, same_band_rate=None)
        text = report.render(results)
        self.assertIn("Same band in n/a of 0 facets compared.", text)
        rows = [line for line in text.splitlines() if any(line.startswith(f"| {f} ") for f in fb["facets"])]
        self.assertEqual(len(rows), len(fb["facets"]))
        self.assertTrue(all(line.endswith("| n/a |") for line in rows))

    def test_report_first_section_is_non_clinical(self):
        headings = [line for line in self.report.splitlines() if line.startswith("## ")]
        self.assertEqual(headings[0], "## This is not a clinical assessment")

    def test_report_is_not_vacuous(self):
        p = self.results["predictions"]
        self.assertTrue(p["inductive_reportable"])
        self.assertGreater(p["supported_eligible"], 0)
        self.assertGreater(len(p["matched"]), 0)
        self.assertGreater(self.results["deductive_vs_ipip"]["compared"], 0)
        self.assertGreater(self.results["facet_bands"]["compared"], 0)
        self.assertIn("### Facets, descriptive", self.report)
        self.assertGreater(self.results["agreement"]["downgrades"], 0)

    def test_temporal_read_is_not_vacuous_and_observation_is_whole_record_support(self):
        rows = self.results["temporal"]["constructs"]
        self.assertTrue(any(r["fades"] for r in rows))
        self.assertTrue(any(r["emergences"] for r in rows))
        self.assertTrue(any(r["held"] for r in rows))
        observed = {r["construct_id"] for r in rows if "observed" in r["eras"].values()}
        supported = {c["id"] for c in self.results["constructs"] if "supported" in c["status"].values()}
        self.assertEqual(observed, supported)
        self.assertIn("## Over time", self.report)

    def test_every_report_figure_recomputes_from_frozen_files(self):
        self.assertEqual(recompute.problems(self.ws), [])

    def test_recomputation_catches_wrong_figures(self):
        exp = recompute.expected(self.ws)
        bad = json.loads(json.dumps(self.results))
        bad["agreement"]["inductive"]["raw_agreement"] = 0.123
        bad["agreement"]["deductive"]["missing_rate"] = 0.999
        bad["deductive_vs_ipip"]["compared"] += 1
        bad["predictions"]["coder_agreement"] = 0.5
        bad["corpus"]["e1"]["kept_ratio"] = 0.01
        bad["temporal"]["constructs"][0]["held"] = ["e9"]
        found = recompute.compare(bad, self.report, exp)
        for field in ("raw_agreement", "missing_rate", "deductive_vs_ipip.compared", "coder_agreement", "corpus",
                      "temporal"):
            self.assertTrue(any(field in f for f in found), (field, found))
        audit = self.results["agreement"]["integrity_audit"]
        if audit:
            bad_audit = json.loads(json.dumps(self.results))
            bad_audit["agreement"]["integrity_audit"]["events"]["counts"]["records"] = 999
            self.assertTrue(recompute.compare(bad_audit, self.report, exp))
            count = audit["events"]["counts"]["records"]
            tampered = self.report.replace(f"{count} call records", "999 call records")
            self.assertTrue(recompute.compare(self.results, tampered, exp))
        alpha = f"{self.results['agreement']['inductive']['alpha']:.3f}"
        self.assertTrue(recompute.compare(self.results, self.report.replace(alpha, "0.000"), exp))

    def test_sealed_content_reaches_crosswalk_prompts_only_and_ipip_none(self):
        predictions = [line.split(":", 1)[1].strip() for line in
                       (FIXTURE / "self-prediction.md").read_text().splitlines() if line.startswith("P")]
        answers = [line for line in (FIXTURE / "ipip-120.csv").read_text().splitlines()[1:] if line[-1].isdigit()]
        roles = [p["prompt"].splitlines()[0] for p in self.prompts]
        first_crosswalk = roles.index("# abx-role: crosswalk")
        self.assertTrue(all(r != "# abx-role: crosswalk" for r in roles[:first_crosswalk]))
        self.assertGreater(first_crosswalk, max(i for i, r in enumerate(roles) if r == "# abx-role: recode"))
        for i, p in enumerate(self.prompts):
            carries = any(s in p["prompt"] for s in predictions)
            self.assertEqual(carries, roles[i] == "# abx-role: crosswalk", roles[i])
            self.assertFalse(any(a in p["prompt"] for a in answers))
            self.assertNotIn("Collins", p["prompt"])
            self.assertNotIn("Keimer", p["prompt"])


if __name__ == "__main__":
    unittest.main()
