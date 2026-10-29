"""Shared fixtures: a config that points at the fake CLIs, and a scratch workspace."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from abx import config, init, invoke, store

from . import REPO

FAKES = REPO / "tests" / "fakes"
FAKE_ENV = ["ABX_FAKE_LOG", "ABX_FAKE_VERSION", "ABX_FAKE_TOOL_EVENT", "ABX_FAKE_FAIL", "ABX_FAKE_LEAK",
            "ABX_FAKE_FAIL_AFTER", "ABX_FAKE_RAW", "ABX_FAKE_CAPTURE_EXTRA_TOOL", "ABX_FAKE_CODEX_CAPTURE_RESULT",
            "ABX_FAKE_CLAUDE_CAPTURE_RESULT", "ABX_FAKE_SLEEP", "ABX_FAKE_STRAY_CITATION", "ABX_FAKE_PRELUDE",
            "ABX_FAKE_THINKING", "ABX_FAKE_NO_INIT", "ABX_FAKE_NO_MESSAGE", "ABX_FAKE_CROSS_FACET",
            "ABX_FAKE_ERROR_NOTICE", "ABX_FAKE_READ_INSTRUCTIONS", "ABX_FAKE_STDERR", "ABX_FAKE_PROFILE_FETCH",
            "ABX_FAKE_INVALID", "ABX_FAKE_PLANTED", "ABX_FAKE_JUDGE", "ABX_FAKE_JUDGE_FLIP",
            "ABX_FAKE_FAIL_ROLE", "ABX_FAKE_LENIENT_SAME", "ABX_FAKE_STRICT_MIRROR", "ABX_FAKE_OUTPUT_LIMIT",
            "ABX_FAKE_KINDS"]


class Logins(NamedTuple):
    codex_auth: Path
    claude_profile: Path


def claude_profile() -> dict:
    """A cached Claude account profile in the shape Claude Code 2.1.303 keeps: identity plus the fields whose
    presence stops it fetching the profile again. Synthetic, and a fresh object each time."""
    return {"oauthAccount": {"emailAddress": "someone@example.com", "fullName": "Some One", "displayName": "Some",
                             "organizationName": "Some org", "billingType": "stripe_subscription",
                             "accountCreatedAt": "2026-01-01", "subscriptionCreatedAt": "2026-01-01",
                             "ccOnboardingFlags": {}}}


def write_logins(root: Path) -> Logins:
    """A Codex auth file and a Claude account profile under `root`."""
    logins = Logins(root / "auth.json", root / "claude.json")
    logins.codex_auth.write_text("{}")
    logins.claude_profile.write_text(json.dumps(claude_profile()))
    return logins


def fake_config(logins: Logins) -> dict:
    return {
        "lineages": {
            "claude": {"cli": str(FAKES / "claude"), "model": "fake-claude-1",
                       "input_budget_tokens": 200000, "reserve_output_tokens": 8000,
                       "locked_read_result": init.CLAUDE_LOCKED_READ, "profile_file": str(logins.claude_profile)},
            "codex": {"cli": str(FAKES / "codex"), "model": "fake-codex-1",
                      "input_budget_tokens": 200000, "reserve_output_tokens": 8000,
                      "sandbox_exec": str(FAKES / "sandbox-exec"), "sandbox_allow": [str(FAKES)],
                      "auth_file": str(logins.codex_auth), "disable": list(init.CODEX_DISABLE),
                      "config": list(init.CODEX_CONFIG),
                      "retained_tools": init.CODEX_RETAINED, "capture_forward": False},
        },
        "runs": 3,
        "roles": {"zoner": "claude", "codebook_author": "claude", "foil_author": "claude", "auditor": "codex",
                  "facet_mapper": "codex",
                  "crosswalk_coders": ["claude", "codex"]},
        "canaries": ["ABXPROFILECANARY"],
        "env_passthrough": FAKE_ENV,
        "timeout_s": 60,
    }


class FakeWorkspace:
    """A temp workspace with the repo's prompts and schemas under prereg/, plus a fake log dir.

    `cfg` is the raw config written by `init`; `config` the parsed one the harness uses.
    """

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="abx-test-")
        root = Path(os.path.realpath(self._tmp.name))
        self.ws = store.Workspace(root / "ws")
        for name in ("schemas", "prompts"):
            shutil.copytree(REPO / name, self.ws.path(f"prereg/{name}"))
        self.log = root / "fakelog"
        self.cfg = fake_config(write_logins(root))
        self.config = config.parse(self.cfg)
        self._env = mock.patch.dict(os.environ, {"ABX_FAKE_LOG": str(self.log)})
        self._env.start()

    def qualify_stub(self) -> dict:
        """Record the live fingerprints as qualified, without running qualification."""
        q = {lin: {**invoke.live_fingerprint(self.config, lin), "resolved_model": self.config.lineage(lin).model}
             for lin in self.config.lineages}
        store.write_json(self.ws.path(invoke.QUALIFICATION), q)
        return q

    def prompts_logged(self) -> list[str]:
        path = self.log / "prompts.jsonl"
        return path.read_text().splitlines() if path.exists() else []

    def close(self):
        self._env.stop()
        self._tmp.cleanup()


FIXTURE = REPO / "fixtures" / "franklin"


def approve_spot_check(ws: store.Workspace) -> None:
    """The fixture's spot check, approved with no marks, as a reader satisfied with the zoning would."""
    path = ws.path("prepare/spot-check.json")
    store.write_json(path, {**store.read_json(path), "approved": True})


def build(fw: FakeWorkspace, until: str = "extract") -> store.Workspace:
    """Run the pipeline on the Franklin fixture with fakes, up to and including `until`."""
    from abx import extract, freeze, prepare, seal

    root = fw.ws.root.parent / "w"
    ws = init.init(root, [FIXTURE / "autobiography.txt"], [], False, config=fw.cfg)
    for name in ("redactions.json", "eras.json"):
        store.write_json(ws.path(f"prepare/{name}"), store.read_json(FIXTURE / name))
    prepare.prepare(ws)
    if until == "prepare":
        return ws
    approve_spot_check(ws)
    for name in ("self-prediction.md", "ipip-120.csv"):
        ws.path(f"sealed/{name}").write_text((FIXTURE / name).read_text())
    seal.seal(ws)
    freeze.freeze(ws)
    if until == "freeze":
        return ws
    extract.extract(ws)
    return ws
