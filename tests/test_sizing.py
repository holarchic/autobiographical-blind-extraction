import dataclasses
import unittest
from unittest import mock

from abx import calibration, config, init, invoke, prepare, sizing, store, text
from abx.errors import UsageError
from abx.roles import ROLES

from .helpers import FIXTURE, FakeWorkspace

# The per-era roles sized on a whole era; the chunked ones (zoner, calibration judge) are sized per part.
ERA_ROLES = tuple(r for r, p in ROLES.items() if p.per_era and not p.chunked)
PARAGRAPH = " ".join(f"He walked to market number {i} and bought bread." for i in range(40))


def _budget(cfg: config.Config, tokens: int, roomy: str | None = None) -> config.Config:
    """Every lineage gets `tokens` of input room, except `roomy`, which gets ten times as much."""
    lineages = {name: dataclasses.replace(lin, input_budget_tokens=tokens * (10 if name == roomy else 1)
                                          + lin.reserve_output_tokens)
                for name, lin in cfg.lineages.items()}
    return config.Config(**{**cfg.__dict__, "lineages": lineages})


class EraSizeTest(unittest.TestCase):
    """The size check is an upper bound on what each per-era call will send."""

    def setUp(self):
        self.fw = FakeWorkspace()
        self.ws, self.cfg = self.fw.ws, self.fw.config
        prepare.stage_method(self.ws)

    def tearDown(self):
        self.fw.close()

    def corpus(self, n_eras: int):
        original = "\n\n".join([PARAGRAPH] * (2 * n_eras))
        segs = text.segment(original)
        eras = [{"id": f"e{k + 1}", "label": "", "segment_ids": [s.id for s in segs[2 * k:2 * k + 2]]}
                for k in range(n_eras)]
        return original, eras, text.sentences(original, segs)

    def need(self, role: str, position: int, body: str, card: bool = True, n_eras: int = 1) -> int:
        era_ids = [f"e{k + 1}" for k in range(n_eras)]
        prompt = sizing.prompt_for(self.ws, role, era_ids, position, era_ids[position], body)
        if not card:
            prompt = prompt.replace(sizing.largest_card(self.ws, n_eras), "")
        return invoke.estimate_tokens(prompt)

    def era_texts(self, original, era, sents):
        mine = [s for s in sents if s.segment_id in set(era["segment_ids"])]
        raw = "\n\n".join(original[s.start:s.end] for s in text.segment(original) if s.id in era["segment_ids"])
        return raw, text.render_zoned(original, era["segment_ids"], mine, {s.id for s in mine}), mine

    def test_the_all_kept_rendering_bounds_any_zoned_rendering(self):
        original, eras, sents = self.corpus(1)
        _, full, mine = self.era_texts(original, eras[0], sents)
        for kept in ({s.id for s in mine[::2]}, {mine[0].id}, set()):
            self.assertGreaterEqual(len(full), len(text.render_zoned(original, eras[0]["segment_ids"], mine, kept)))

    def test_rendered_era_over_budget_fails_where_raw_text_would_fit(self):
        original, eras, sents = self.corpus(1)
        raw, full, _ = self.era_texts(original, eras[0], sents)
        roles = ERA_ROLES
        budget = max(self.need(r, 0, raw) for r in roles)
        self.assertGreater(max(self.need(r, 0, full) for r in roles), budget)
        with self.assertRaises(UsageError) as cm:
            sizing.check_eras(self.ws, _budget(self.cfg, budget), eras, sents, original)
        self.assertIn("era e1 has", str(cm.exception))
        sizing.check_eras(self.ws, _budget(self.cfg, max(self.need(r, 0, full) for r in roles)), eras, sents,
                          original)

    def test_the_judges_context_lines_alone_tip_it_over(self):
        heading = "THE FIRST PART OF A RECORD WITH A HEADING LONG ENOUGH TO MATTER"
        original = "\n\n".join([heading] + [f"In 1801, 1802 and 1803 I went out, time {i}." for i in range(40)])
        segs = text.segment(original)
        eras = [{"id": "e1", "label": "", "segment_ids": [s.id for s in segs]}]
        sents = text.sentences(original, segs)
        context = calibration.paragraph_context(original, segs)
        zoner = self.need("zoner", 0, sizing.longest_part(original, sents))
        judge = self.need("calibration_judge", 0, sizing.longest_part(original, sents, context))
        self.assertGreater(judge, zoner)
        self.assertGreater(judge, self.need("calibration_judge", 0, sizing.longest_part(original, sents)))
        chunked = {name: policy for name, policy in ROLES.items() if policy.chunked}  # the card roles need more
        with mock.patch.object(sizing, "ROLES", chunked):
            with self.assertRaises(UsageError) as cm:
                sizing.check_eras(self.ws, _budget(self.cfg, zoner), eras, sents, original)
            self.assertIn("calibration_judge", str(cm.exception))
            sizing.check_eras(self.ws, _budget(self.cfg, judge), eras, sents, original)

    def test_the_largest_card_alone_tips_recode_over(self):
        original, eras, sents = self.corpus(1)
        _, full, _ = self.era_texts(original, eras[0], sents)
        others = max(self.need(r, 0, full) for r in ERA_ROLES if r not in {"recode", "auditor"})
        budget = max(others, self.need("recode", 0, full, card=False))
        self.assertGreater(self.need("recode", 0, full), budget)
        # The auditor, which also carries the largest codebook, runs on its own lineage; give that one room, so only
        # recode (which runs on every lineage) can fail.
        roomy = _budget(self.cfg, budget, roomy=self.cfg.auditor)
        with self.assertRaises(UsageError) as cm:
            sizing.check_eras(self.ws, roomy, eras, sents, original)
        self.assertIn(f"recode on {next(n for n in self.cfg.lineages if n != self.cfg.auditor)}", str(cm.exception))

    def test_identical_text_fits_an_early_era_but_not_a_later_discovery(self):
        original, eras, sents = self.corpus(8)
        _, full, _ = self.era_texts(original, eras[0], sents)
        budget = max(self.need("recode", 0, full, n_eras=8), self.need("discovery", 6, full, n_eras=8))
        self.assertGreater(self.need("discovery", 7, full, n_eras=8), budget)
        with self.assertRaises(UsageError) as cm:
            sizing.check_eras(self.ws, _budget(self.cfg, budget), eras, sents, original)
        self.assertIn("era e8 has", str(cm.exception))
        self.assertIn("discovery", str(cm.exception))

    def test_a_placeholder_used_twice_is_counted_twice(self):
        original, eras, sents = self.corpus(1)
        _, full, _ = self.era_texts(original, eras[0], sents)
        budget = max(self.need(r, 0, full) for r in ERA_ROLES)
        sizing.check_eras(self.ws, _budget(self.cfg, budget), eras, sents, original)
        template = self.ws.path("prereg/prompts/recode.md")
        template.write_text(template.read_text() + "\nThe era again:\n{{era}}\n")
        self.assertEqual(sizing.prompt_for(self.ws, "recode", ["e1"], 0, "e1", full).count(full), 2)
        with self.assertRaises(UsageError) as cm:
            sizing.check_eras(self.ws, _budget(self.cfg, budget), eras, sents, original)
        self.assertIn("recode", str(cm.exception))
        lin = _budget(self.cfg, budget).lineage("claude")
        once = sizing.words_that_fit(self.ws, "deductive_ledger", ["e1"], 0, "e1", lin, 6.0)
        twice = sizing.words_that_fit(self.ws, "recode", ["e1"], 0, "e1", lin, 6.0)
        template.write_text(template.read_text().replace("\nThe era again:\n{{era}}\n", ""))
        # Within the few words the added template line itself takes.
        self.assertAlmostEqual(twice, sizing.words_that_fit(self.ws, "recode", ["e1"], 0, "e1", lin, 6.0) // 2,
                               delta=2)
        self.assertGreater(once, twice)

    def test_an_oversize_era_stops_prepare_before_any_model_call(self):
        ws = init.init(self.fw.ws.root.parent / "w", [FIXTURE / "autobiography.txt"], [], False,
                       config={**self.fw.cfg, "lineages": {name: {**lin, "input_budget_tokens": 30000,
                                                                   "reserve_output_tokens": 8000}
                                                            for name, lin in self.fw.cfg["lineages"].items()}})
        for name in ("redactions.json", "eras.json"):
            store.write_json(ws.path(f"prepare/{name}"), store.read_json(FIXTURE / name))
        with self.assertRaises(UsageError) as cm:
            prepare.prepare(ws)
        self.assertEqual(cm.exception.exit_code, 2)
        # Recode and the auditor both carry the largest codebook, so either can be the first to fail.
        self.assertRegex(str(cm.exception), r"era e1 has \d+ words; (recode|auditor) on \w+ fits about \d+ words")
        self.assertEqual(self.fw.prompts_logged(), [])
        self.assertFalse(ws.path(invoke.QUALIFICATION).exists())

    def test_the_eras_template_states_the_rule_and_an_estimated_maximum(self):
        ws = init.init(self.fw.ws.root.parent / "w", [FIXTURE / "autobiography.txt"], [], False, config=self.fw.cfg)
        prepare.prepare(ws)
        note = store.read_json(ws.path("prepare/eras.json"))["note"]
        self.assertIn("at least 5, each a period of years with at least 15k words", note)
        self.assertRegex(note, r"about \d+ words for the sixth era")
        self.assertRegex(ws.path("prepare/segments-preview.txt").read_text().splitlines()[0],
                         r"^p\d+ \(\d+ words, (heading|dated|paragraph)\): ")


if __name__ == "__main__":
    unittest.main()
