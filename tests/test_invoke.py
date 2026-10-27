import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from abx import artifacts, config, invoke, prompts, store
from abx.errors import CallFailed, IntegrityError, IsolationError, UsageError
from abx.withheld import Withheld

from .fakes import fakelib
from .helpers import FakeWorkspace


def _req(run=1, lineage="claude"):
    return invoke.CallRequest(stage="test", role="probe", lineage=lineage, run=run, era=None, generation="A", slots={})


def _with(cfg: config.Config, **changes) -> config.Config:
    return config.Config(**{**cfg.__dict__, **changes})


class CallTest(unittest.TestCase):
    def setUp(self):
        self.fw = FakeWorkspace()
        self.ws, self.cfg = self.fw.ws, self.fw.config
        self.fw.qualify_stub()

    def tearDown(self):
        self.fw.close()

    def call(self, req, cfg=None):
        return invoke.call(self.ws, cfg or self.cfg, req)

    def test_success_records_inputs_and_events(self):
        for lineage in ("claude", "codex"):
            self.assertEqual(self.call(_req(lineage=lineage)), {"answer": "NONE"})
            rec = store.read_json(invoke.record_path(self.ws, _req(lineage=lineage)))
            self.assertEqual(set(rec["input_artifacts"]), {"template:probe", "schema:probe"})
            self.assertEqual(rec["fingerprint"]["resolved_model"], self.cfg.lineage(lineage).model)
            self.assertTrue(rec["events"])

    def test_every_tool_event_kind_fails_closed(self):
        for lineage, kinds in fakelib.TOOL_EVENTS.items():
            for n, kind in enumerate(kinds, start=1):
                with self.subTest(lineage=lineage, kind=kind), \
                        mock.patch.dict(os.environ, {"ABX_FAKE_TOOL_EVENT": kind}):
                    req = _req(lineage=lineage, run=n)
                    with self.assertRaises(IsolationError) as cm:
                        self.call(req)
                    self.assertEqual(cm.exception.exit_code, 3)
                    self.assertFalse(invoke.record_path(self.ws, req).exists())
                    incident = store.read_json(self.ws.path(f"run/incidents/{req.identity}-1.json"))
                    self.assertEqual(incident["kind"], "tool_event")

    def test_a_config_frozen_before_profile_file_defaults_it(self):
        legacy = json.loads(json.dumps(self.fw.cfg))
        del legacy["lineages"]["claude"]["profile_file"]
        lin = config.parse(legacy).lineage("claude")
        self.assertEqual(lin.profile_file, config.CLAUDE_PROFILE_DEFAULT)
        self.assertEqual(lin.raw_json, store.canonical(legacy["lineages"]["claude"]))

    def test_locked_claude_home_carries_the_login_without_identity(self):
        source = Path(self.cfg.lineage("claude").profile_file)
        profile = json.loads(source.read_text())
        profile["oauthAccount"]["somethingNew"] = "unvetted"
        source.write_text(json.dumps(profile))
        with tempfile.TemporaryDirectory() as tmp:
            home = invoke.claude_home(Path(tmp), self.cfg.lineage("claude"))
            seeded = json.loads((home / ".claude.json").read_text())
            self.assertEqual(set(seeded), {"oauthAccount"})
            self.assertNotIn("somethingNew", seeded["oauthAccount"])
            self.assertEqual({k: seeded["oauthAccount"][k] for k in invoke.CLAUDE_IDENTITY},
                             dict.fromkeys(invoke.CLAUDE_IDENTITY, ""))
            self.assertEqual(seeded["oauthAccount"]["billingType"], "stripe_subscription")
            self.assertGreater(seeded["oauthAccount"]["profileFetchedAt"], 1)
            self.assertFalse(invoke.identity_restored(home))
            for rewritten in ({"oauthAccount": {"emailAddress": "x@example.com"}}, {"oauthAccount": None},
                              {"oauthAccount": []}, {}, {"oauthAccount": {}}, {"oauthAccount": {
                                  **dict.fromkeys(invoke.CLAUDE_IDENTITY, ""), "emailAddress": None}}):
                with self.subTest(rewritten=rewritten):
                    (home / ".claude.json").write_text(json.dumps(rewritten))
                    self.assertTrue(invoke.identity_restored(home))
            # A repeated key must not let a blank account hide an identity written back before it.
            blank = json.dumps(dict.fromkeys(invoke.CLAUDE_IDENTITY, ""))
            (home / ".claude.json").write_text(f'{{"oauthAccount": {{"emailAddress": "x@example.com"}}, '
                                               f'"oauthAccount": {blank}}}')
            self.assertTrue(invoke.identity_restored(home))

    def test_an_incomplete_or_missing_claude_profile_is_a_preflight_failure(self):
        lin = self.cfg.lineage("claude")
        source = Path(os.path.expanduser(lin.profile_file))
        for body in ("not json", json.dumps({}), json.dumps({"oauthAccount": {"emailAddress": "x@example.com"}})):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                source.write_text(body)
                with self.assertRaises(UsageError) as cm:
                    invoke.claude_home(Path(tmp), lin)
                self.assertEqual(cm.exception.exit_code, 2)
        source.unlink()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(UsageError):
            invoke.claude_home(Path(tmp), lin)

    def test_profile_rewritten_during_a_locked_claude_call_is_an_audited_tool_event(self):
        req = _req()
        with mock.patch.dict(os.environ, {"ABX_FAKE_PROFILE_FETCH": "1"}), self.assertRaises(IsolationError) as cm:
            self.call(req)
        self.assertEqual(cm.exception.exit_code, 3)
        incident = store.read_json(self.ws.path(f"run/incidents/{req.identity}-1.json"))
        self.assertEqual(incident["kind"], "tool_event")
        self.assertNotIn("example.com", json.dumps(incident))

    def test_profile_location_overrides_never_reach_a_locked_claude_call(self):
        cfg = _with(self.cfg, env_passthrough=(*self.cfg.env_passthrough, "CLAUDE_CONFIG_DIR"))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/elsewhere"}):
            self.call(_req(), cfg)
        entries = [json.loads(line) for line in (self.fw.log / "prompts.jsonl").read_text().splitlines()]
        self.assertEqual([e["config_dir"] for e in entries if e["lineage"] == "claude"], [None])

    def test_tool_event_outranks_cli_failure_and_timeout(self):
        for env in ({"ABX_FAKE_FAIL": "1"}, {"ABX_FAKE_SLEEP": "5"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, {"ABX_FAKE_TOOL_EVENT": "mcp_tool_call", **env}):
                with self.assertRaises(IsolationError):
                    self.call(_req(lineage="codex"), _with(self.cfg, timeout_s=2))

    def test_cli_failure_timeout_and_invalid_output_exit_5(self):
        """A CLI failure or timeout is one execution; an invalid output is resampled to the attempt limit."""
        cases = (({"ABX_FAKE_FAIL": "1"}, 1), ({"ABX_FAKE_RAW": "not json"}, invoke.MAX_ATTEMPTS),
                 ({"ABX_FAKE_RAW": '{"other": 1}'}, invoke.MAX_ATTEMPTS), ({"ABX_FAKE_SLEEP": "5"}, 1))
        for n, (env, executions) in enumerate(cases, start=1):
            for lineage in ("claude", "codex"):
                with self.subTest(env=env, lineage=lineage), mock.patch.dict(os.environ, env):
                    before = len(self.fw.prompts_logged())
                    req = _req(lineage=lineage, run=n)
                    with self.assertRaises(CallFailed) as cm:
                        self.call(req, _with(self.cfg, timeout_s=2))
                    self.assertEqual(cm.exception.exit_code, 5)
                    self.assertEqual(len(self.fw.prompts_logged()) - before, executions)
                    self.assertEqual(len(list(self.ws.root.glob(f"run/incidents/{req.identity}-*.json"))), executions)
                    self.assertFalse(invoke.record_path(self.ws, req).exists())

    def test_an_invalid_output_is_resampled_and_recorded(self):
        req = _req()
        with mock.patch.dict(os.environ, {"ABX_FAKE_INVALID": "probe:1"}):
            self.assertEqual(self.call(req), {"answer": "NONE"})
        prompts_sent = self.fw.prompts_logged()
        self.assertEqual(len(prompts_sent), 2)
        self.assertEqual(json.loads(prompts_sent[0])["prompt"], json.loads(prompts_sent[1])["prompt"])
        incident = store.read_json(self.ws.path(f"run/incidents/{req.identity}-1.json"))
        self.assertEqual((incident["kind"], incident["detail"]), ("invalid_output", ["output is not JSON"]))
        self.assertTrue(invoke.record_path(self.ws, req).exists())
        self.call(req)  # resume reuses the resampled success
        self.assertEqual(len(self.fw.prompts_logged()), 2)

    def test_a_trim_runs_before_the_schema_and_the_untrimmed_output_is_kept(self):
        req = invoke.CallRequest(stage="test", role="probe", lineage="claude", run=1, era=None, generation="A",
                                 slots={}, trim=lambda o: {"answer": o["answer"]})
        with mock.patch.dict(os.environ, {"ABX_FAKE_RAW": '{"answer": "NONE", "extra": 1}'}):
            self.assertEqual(self.call(req), {"answer": "NONE"})
        self.assertEqual(len(self.fw.prompts_logged()), 1)
        self.assertEqual(store.read_json(invoke.record_path(self.ws, req))["untrimmed_output"],
                         {"answer": "NONE", "extra": 1})
        self.call(req)  # resume reuses the trimmed record
        self.assertEqual(len(self.fw.prompts_logged()), 1)

    def test_a_record_the_trim_would_still_cut_is_not_reused(self):
        req = _req()
        with mock.patch.dict(os.environ, {"ABX_FAKE_RAW": '{"answer": "NONE"}'}):
            self.call(req)
        cut = invoke.CallRequest(stage="test", role="probe", lineage="claude", run=1, era=None, generation="A",
                                 slots={}, trim=lambda o: {"answer": "CUT"})
        with mock.patch.dict(os.environ, {"ABX_FAKE_RAW": '{"answer": "NONE"}'}):
            self.assertEqual(self.call(cut), {"answer": "CUT"})
        self.assertEqual(len(self.fw.prompts_logged()), 2)

    def test_an_untouched_output_records_no_untrimmed_copy(self):
        req = invoke.CallRequest(stage="test", role="probe", lineage="claude", run=1, era=None, generation="A",
                                 slots={}, trim=lambda o: o)
        self.call(req)
        self.assertNotIn("untrimmed_output", store.read_json(invoke.record_path(self.ws, req)))

    def test_a_tool_event_is_never_resampled(self):
        req = _req()
        with mock.patch.dict(os.environ, {"ABX_FAKE_INVALID": "probe:1", "ABX_FAKE_TOOL_EVENT": "tool_use"}), \
                self.assertRaises(IsolationError):
            self.call(req)
        self.assertEqual(len(self.fw.prompts_logged()), 1)
        self.assertEqual([store.read_json(p)["kind"] for p in self.ws.root.glob("run/incidents/**/*.json")],
                         ["tool_event"])

    def test_replicates_run_separately_and_resume_reuses_each(self):
        for run in (1, 2, 3):
            self.call(_req(run=run))
        self.assertEqual(len(self.fw.prompts_logged()), 3)
        for run in (1, 2, 3):
            self.call(_req(run=run))
        self.assertEqual(len(self.fw.prompts_logged()), 3)

    def test_changed_input_reruns(self):
        self.call(_req())
        path = self.ws.path("prereg/prompts/probe.md")
        path.write_text(path.read_text() + "\nAlso answer briefly.\n")
        self.call(_req())
        self.assertEqual(len(self.fw.prompts_logged()), 2)

    def test_record_with_a_tool_event_is_never_reused(self):
        self.call(_req())
        path = invoke.record_path(self.ws, _req())
        rec = store.read_json(path)
        rec["events"].append({"type": "assistant", "detail": "tool_use", "tools": [], "mcp_servers": [],
                              "registry_present": True})
        store.write_json(path, rec)
        with self.assertRaises(IsolationError):
            self.call(_req())

    def test_a_record_with_a_non_integer_exit_is_not_reused(self):
        self.call(_req())
        path = invoke.record_path(self.ws, _req())
        rec = store.read_json(path)
        rec["exit"] = False
        store.write_json(path, rec)
        self.call(_req())
        self.assertEqual(len(self.fw.prompts_logged()), 2)

    def test_a_record_without_output_is_not_reused(self):
        self.call(_req())
        path = invoke.record_path(self.ws, _req())
        rec = store.read_json(path)
        del rec["output"]
        store.write_json(path, rec)
        self.call(_req())
        self.assertEqual(len(self.fw.prompts_logged()), 2)

    def test_claude_failure_before_init_is_exit_5_with_no_tool_incident(self):
        with mock.patch.dict(os.environ, {"ABX_FAKE_NO_INIT": "1", "ABX_FAKE_FAIL": "1"}), \
                self.assertRaises(CallFailed):
            self.call(_req())
        self.assertEqual(store.read_json(self.ws.path(f"run/incidents/{_req().identity}-1.json"))["kind"],
                         "call_failed")

    def test_a_record_copied_to_another_replicate_is_not_reused(self):
        self.call(_req(run=1))
        src, dst = invoke.record_path(self.ws, _req(run=1)), invoke.record_path(self.ws, _req(run=2))
        dst.write_text(src.read_text())
        self.call(_req(run=2))
        self.assertEqual(len(self.fw.prompts_logged()), 2)
        self.assertEqual(store.read_json(dst)["run"], 2)

    def test_a_qualification_from_an_older_isolation_policy_is_never_held_to(self):
        q = store.read_json(self.ws.path(invoke.QUALIFICATION))
        for lin in q.values():
            del lin["isolation_policy"]
        store.write_json(self.ws.path(invoke.QUALIFICATION), q)
        with self.assertRaises(IntegrityError) as cm:
            self.call(_req())
        self.assertEqual(cm.exception.exit_code, 4)
        self.assertEqual(self.fw.prompts_logged(), [])

    def test_artifact_reached_through_a_symlink_is_refused(self):
        era = self.ws.path("prepare/substrate/e1.md")
        era.parent.mkdir(parents=True)
        secret = self.ws.path("sealed/self-prediction.md")
        secret.parent.mkdir(parents=True)
        secret.write_text("P1: secret")
        era.symlink_to(secret)
        with self.assertRaises(UsageError):
            artifacts.path_of(self.ws, "era:e1")

    def test_fingerprint_change_is_integrity_failure(self):
        with mock.patch.dict(os.environ, {"ABX_FAKE_VERSION": "claude-fake 9.9.9"}):
            with self.assertRaises(IntegrityError) as cm:
                self.call(_req())
        self.assertIn("cli_version", str(cm.exception))
        self.assertEqual(self.fw.prompts_logged(), [])

    def test_audited_fingerprint_keys_are_every_live_field_plus_the_model(self):
        for lineage in self.cfg.lineages:
            live = invoke.live_fingerprint(self.cfg, lineage)
            self.assertEqual(set(invoke.FINGERPRINT_KEYS), {*live, "resolved_model"})

    def test_unexpected_claude_model_is_integrity_failure(self):
        q = store.read_json(self.ws.path(invoke.QUALIFICATION))
        q["claude"]["resolved_model"] = "some-other-model"
        store.write_json(self.ws.path(invoke.QUALIFICATION), q)
        with self.assertRaises(IntegrityError):
            self.call(_req())

    def test_over_budget_is_refused_before_transmission(self):
        lin = self.cfg.lineage("claude")
        small = _with(self.cfg, lineages={**self.cfg.lineages,
                                          "claude": config.Lineage(**{**lin.__dict__, "input_budget_tokens": 100})})
        with self.assertRaises(UsageError):
            self.call(_req(), small)
        self.assertEqual(self.fw.prompts_logged(), [])


class AllowlistTest(unittest.TestCase):
    ORDER = ("e1", "e2", "e3")

    def test_discovery_gets_only_own_earlier_hypotheses(self):
        prompts.check_allowlist("discovery", ["era:e2", "hypotheses:claude:1:e1"], rater=("claude", 1),
                                era="e2", era_order=list(self.ORDER))
        for bad in ("hypotheses:codex:1:e1", "hypotheses:claude:2:e1", "hypotheses:claude:1:e2",
                    "hypotheses:claude:1:e3", "era:e3"):
            with self.subTest(bad=bad), self.assertRaises(IsolationError):
                prompts.check_allowlist("discovery", ["era:e2", bad], rater=("claude", 1), era="e2",
                                        era_order=list(self.ORDER))

    def test_deductive_never_gets_inductive_artifacts(self):
        # Each call is otherwise valid (its era named, an allowed input present), so the refusal is the bad input's.
        allowed = {"deductive_ledger": ["era:e1", "facets:all"], "deductive_scorer": ["ledgers:claude:1", "facets:all"],
                   "recode": ["card:B", "era:e1"]}
        for role, ids in allowed.items():
            prompts.check_allowlist(role, ids, rater=("claude", 1), era="e1", era_order=list(self.ORDER))
        for role, bad in (("deductive_ledger", "discovery:claude:1:e1"), ("deductive_ledger", "card:B"),
                          ("deductive_scorer", "ledger:codex:1:e1"), ("recode", "self_prediction:sealed"),
                          ("recode", "template:discovery")):
            with self.subTest(role=role, bad=bad), self.assertRaises(IsolationError) as cm:
                prompts.check_allowlist(role, [*allowed[role], bad], rater=("claude", 1), era="e1",
                                        era_order=list(self.ORDER))
            self.assertNotIn("must name the era", str(cm.exception))

    def test_artifact_ids_cannot_escape_their_directory(self):
        for bad in ("era:../../sealed/self-prediction", "era:e1:extra", "discovery:claude:1", "card:A",
                    "ledger:claude:x:e1", "template:../x", "nokind:e1", "era:e1\n", "era:all", "run:1"):
            with self.subTest(bad=bad), self.assertRaises(UsageError):
                artifacts.parse(bad)
        with self.assertRaises(UsageError):
            prompts.check_allowlist("discovery", ["era:../../sealed/self-prediction"], era="e1")

    def test_labels_are_declared_short_and_single_line(self):
        texts = {"template:zoner": "# abx-role: zoner\n{{chunk}} {{era_label}}", "chunk:e1:1": "[s00001] x"}
        p = prompts.assemble("zoner", {"chunk": "chunk:e1:1"}, texts.__getitem__, {"era_label": "Boston"}, era="e1")
        self.assertEqual(p.text, "# abx-role: zoner\n[s00001] x Boston")
        for params in ({"era_label": "x\ny"}, {"era_label": "x" * 300}, {"secret": "x"}):
            with self.subTest(params=params), self.assertRaises(UsageError):
                prompts.assemble("zoner", {"chunk": "chunk:e1:1"}, texts.__getitem__, params)


class SubstringAuditTest(unittest.TestCase):
    WITHHELD = Withheld(
        redacted_names=["Collins"], removed_sentences=["Looking back, I can see this was my pattern all along."],
        self_prediction=["P1: I will be found conscientious."], ipip=["i001,4"])

    def _p(self, role, text):
        return prompts.Prompt(role, text, {}, {})

    def test_names_blocked_everywhere_including_zoning(self):
        with self.assertRaises(IsolationError):
            prompts.audit(self._p("zoner", "met Collins"), self.WITHHELD)

    def test_removed_text_blocked_only_for_zoned_consumers(self):
        text = "x " + self.WITHHELD.removed_sentences[0]
        prompts.audit(self._p("zoner", text), self.WITHHELD)
        with self.assertRaises(IsolationError):
            prompts.audit(self._p("recode", text), self.WITHHELD)

    def test_self_prediction_only_in_crosswalk_after_c(self):
        text = "P1: I will be found conscientious."
        prompts.audit(self._p("crosswalk", text), self.WITHHELD, after_blind_results=True)
        with self.assertRaises(IsolationError):
            prompts.audit(self._p("crosswalk", text), self.WITHHELD, after_blind_results=False)
        with self.assertRaises(IsolationError):
            prompts.audit(self._p("discovery", text), self.WITHHELD, after_blind_results=True)

    def test_ipip_blocked_everywhere(self):
        with self.assertRaises(IsolationError):
            prompts.audit(self._p("crosswalk", "i001,4"), self.WITHHELD, after_blind_results=True)


@unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "macOS sandbox-exec required")
class SandboxProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="abx-sb-")))
        self.allowed = self.tmp / "cwd" / "ok.txt"
        self.allowed.parent.mkdir()
        self.allowed.write_text("readable")
        self.profile = self.tmp / "p.sb"
        self.profile.write_text(invoke.sandbox_profile([str(self.tmp)]))
        self.canaries: list[Path] = []

    def tearDown(self):
        for c in self.canaries:
            c.unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cat(self, path) -> subprocess.CompletedProcess:
        return subprocess.run(["/usr/bin/sandbox-exec", "-f", str(self.profile), "/bin/cat", str(path)],
                              capture_output=True, text=True, timeout=30, check=False)

    def _canary(self, directory: Path) -> Path | None:
        try:
            fd, name = tempfile.mkstemp(prefix="abx-canary-", dir=directory)
        except OSError:
            return None
        os.write(fd, b"CANARY")
        os.close(fd)
        self.canaries.append(Path(name))
        return Path(name)

    def test_allowed_temp_dir_is_readable_under_denied_root(self):
        self.assertTrue(str(self.tmp).startswith(("/private/var/folders", "/private/tmp")))
        self.assertEqual(self.cat(self.allowed).stdout, "readable")

    def test_reads_outside_the_allowlist_are_denied(self):
        exercised = []
        for directory in (Path.home(), Path("/Users/Shared"), Path("/opt/homebrew"), Path("/usr/local")):
            canary = self._canary(directory)
            if canary is None:
                continue
            exercised.append(directory)
            with self.subTest(directory=directory):
                res = self.cat(canary)
                self.assertNotEqual(res.returncode, 0)
                self.assertNotIn("CANARY", res.stdout)
        self.assertGreaterEqual(len(exercised), 2, f"only {exercised} could hold a canary")

    def test_usr_local_is_not_a_runtime_root(self):
        text = self.profile.read_text()
        self.assertNotIn('(subpath "/usr")', text)
        self.assertNotIn("/usr/local", text)

    def test_symlink_into_denied_path_is_denied(self):
        canary = self._canary(Path.home())
        link = Path(tempfile.gettempdir()) / f"abx-link-{os.getpid()}"
        link.symlink_to(canary)
        try:
            self.assertNotIn("CANARY", self.cat(link).stdout)
        finally:
            link.unlink()


class OutputParsingTest(unittest.TestCase):
    def test_codex_plain_log_lines_are_ignored_and_last_message_wins(self):
        lines = ["2026-10-19T10:32:44.599315Z WARN something", json.dumps({"type": "turn.started"}),
                 json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
                 json.dumps({"type": "error", "message": "Reconnecting... 1/5"}),
                 json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second"}}),
                 json.dumps({"type": "turn.completed"})]
        out = invoke.parse_codex(lines)
        self.assertEqual((out.status, out.text, invoke.audit_events("codex", out.events)), ("success", "second", []))

    def test_terminal_failure_is_never_overwritten(self):
        claude = invoke.parse_claude([
            json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": "m"}),
            json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "x"})])
        self.assertEqual(invoke.outcome("claude", 0, claude.events), "failed")
        codex = invoke.parse_codex([json.dumps({"type": "turn.failed"}), json.dumps({"type": "turn.completed"})])
        self.assertEqual(codex.status, "failed")
        self.assertEqual(invoke.outcome("codex", 0, codex.events), "failed")

    def test_failure_before_init_is_a_failure_and_success_without_init_is_isolation(self):
        self.assertEqual(invoke.outcome("claude", 1, []), "failed")
        ok = invoke.parse_claude([json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                              "result": "{}"})])
        self.assertEqual(invoke.outcome("claude", 0, ok.events), "no_registry")

    def test_a_registry_with_a_non_string_entry_is_no_registry_evidence(self):
        out = invoke.parse_claude([
            json.dumps({"type": "system", "subtype": "init", "tools": [{"name": "Read"}], "mcp_servers": [],
                        "model": "m"}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}"})])
        init = out.events[0]
        self.assertEqual((init.registry_present, init.tools), (False, ()))
        self.assertEqual(invoke.audit_events("claude", out.events), ["init_registry_not_empty"])

    def test_a_block_type_that_is_not_a_plain_name_is_audited_as_other(self):
        for kind in ("text,thinking", "tool use", 7):
            with self.subTest(kind=kind):
                out = invoke.parse_claude([json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "hi"}, {"type": kind}]}})])
                self.assertEqual(out.events[0].detail, "text,other")
                self.assertEqual(invoke.audit_events("claude", out.events), ["assistant_block:other"])

    def test_a_repeated_key_makes_a_line_unparseable_not_its_last_value(self):
        line = '{"type": "assistant", "message": {"content": [{"type": "tool_use", "type": "text", "text": "hi"}]}}'
        claude = invoke.parse_claude([line])
        self.assertEqual(claude.events[0].type, "unparseable")
        self.assertTrue(invoke.audit_events("claude", claude.events))
        codex = invoke.parse_codex(['{"type": "item.completed", "item": {"type": "command_execution", '
                                    '"type": "agent_message", "text": "x"}}'])
        self.assertEqual(codex.events[0].type, "unparseable")
        self.assertTrue(invoke.audit_events("codex", codex.events))

    def test_timestamped_diagnostic_lines_are_scanned(self):
        out = invoke.parse_codex(["2026-10-19T10:32:44Z WARN about ABXPROFILECANARY",
                                  json.dumps({"type": "turn.completed"})])
        self.assertIn("ABXPROFILECANARY", " ".join(out.messages))

    def test_stored_events_are_never_coerced(self):
        good = {"type": "system", "detail": "init", "tools": [], "mcp_servers": [], "registry_present": True}
        self.assertEqual(len(invoke.decode_events([good])), 1)
        for bad in ({**good, "tools": ""}, {**good, "registry_present": "false"}, {**good, "type": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(invoke.decode_events([bad]))

    def test_unknown_event_kinds_are_reported_as_codes_not_content(self):
        events = invoke.parse_codex([json.dumps({"type": "item.completed", "item": {"type": "secret-name-xyz"}})])
        self.assertEqual(invoke.audit_events("codex", events.events), ["item:other"])

    def test_malformed_json_events_fail_closed(self):
        self.assertTrue(invoke.audit_events("codex", invoke.parse_codex(['{"type": "item.started", ']).events))
        self.assertTrue(invoke.audit_events("codex", invoke.parse_codex(['["not", "an", "event"]']).events))
        self.assertTrue(invoke.audit_events("claude", invoke.parse_claude(["not json"]).events))

    def test_claude_telemetry_events_pass_and_other_system_subtypes_do_not(self):
        init = json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": "m"})
        telemetry = [json.dumps({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50}),
                     json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})]
        result = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}"})
        out = invoke.parse_claude([init, *telemetry, result])
        self.assertEqual(invoke.outcome("claude", 0, out.events), "success")
        for extra in ({"type": "system", "subtype": "hook_started"}, {"type": "rate_limit_event", "subtype": "x"}):
            with self.subTest(extra=extra):
                out = invoke.parse_claude([init, json.dumps(extra), result])
                self.assertEqual(invoke.audit_events("claude", out.events), ["event:system"]
                                 if extra["type"] == "system" else ["event:rate_limit_event"])

    def test_claude_telemetry_with_a_malformed_subtype_is_a_tool_event(self):
        for extra in ({"type": "rate_limit_event", "subtype": 1}, {"type": "rate_limit_event", "subtype": {}},
                      {"type": "rate_limit_event", "subtype": ""}, {"type": "system", "subtype": None}):
            with self.subTest(extra=extra):
                out = invoke.parse_claude([json.dumps(extra)])
                self.assertTrue(invoke.audit_events("claude", out.events))

    def test_claude_init_registry_must_be_present_and_empty(self):
        for init in ({"tools": ["Read"], "mcp_servers": []}, {"tools": [], "mcp_servers": ["x"]}, {}):
            with self.subTest(init=init):
                out = invoke.parse_claude([json.dumps({"type": "system", "subtype": "init", **init})])
                self.assertTrue(invoke.audit_events("claude", out.events))


if __name__ == "__main__":
    unittest.main()
