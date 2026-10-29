"""`init`: create a workspace, copy the text in read-only, write config and the sealed templates."""

import copy
import os
import shutil
import stat
from pathlib import Path

from . import ipip, lifecycle, store
from .config import CLAUDE_PROFILE_DEFAULT
from .errors import UsageError

__all__ = [
    "CLAUDE_LOCKED_READ",
    "CODEX_CONFIG",
    "CODEX_DISABLE",
    "CODEX_RETAINED",
    "STAGE",
    "codex_native",
    "default_config",
    "init",
]

SYNCED_MARKERS = ("/Library/CloudStorage/", "/Dropbox/", "/Library/Mobile Documents/", "/Google Drive/",
                  "/OneDrive")

CODEX_DISABLE = (
    "shell_tool", "unified_exec", "shell_snapshot", "code_mode_host", "apps", "plugins", "remote_plugin",
    "plugin_sharing", "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
    "computer_use", "hooks", "multi_agent", "image_generation", "view_image", "sleep_tool", "skill_search",
    "skill_mcp_dependency_install", "tool_suggest", "tool_call_mcp_elicitation", "goals", "memories",
    "enable_request_compression",
)
CODEX_CONFIG = ('web_search="disabled"', "agents.enabled=false", "mcp_servers={}", "apps._default.enabled=false",
                "plugins={}")
# Tools codex-cli 0.160.1 lists even under the full disable set, each with a schema-valid scripted call and
# the exact result that proves it inert (measured 2026-10-19 against a loopback provider).
CODEX_RETAINED = {
    "functions.exec": {"call": {"type": "custom_tool_call", "name": "exec",
                                "input": 'const r = await tools.exec_command({cmd: "cat {canary}"}); '
                                         "text(JSON.stringify(r));"},
                       "result": "code-mode host is disabled"},
    "functions.wait": {"call": {"type": "function_call", "name": "wait", "arguments": '{"cell_id": "1"}'},
                       "result": "code-mode host is disabled"},
    "functions.request_user_input": {
        "call": {"type": "function_call", "name": "request_user_input",
                 "arguments": '{"questions": [{"id": "q", "header": "h", "question": "?", '
                              '"options": [{"label": "a", "description": "b"}, {"label": "c", "description": "d"}]}]}'},
        "result": "request_user_input is unavailable in Default mode"},
    "functions.request_user_input_async": {
        "call": {"type": "function_call", "name": "request_user_input_async",
                 "arguments": '{"questions": [{"title": "?"}]}'},
        "result": '{"accepted":true}'},
}
CLAUDE_LOCKED_READ = ("<tool_use_error>Error: No such tool available: Read. Read is disabled for this session, "
                      "in subagents as well as here.</tool_use_error>")


def _refuse_location(ws: Path, allow_synced: bool) -> None:
    resolved = str(ws.resolve())
    for parent in [ws.resolve(), *ws.resolve().parents]:
        if (parent / ".git").exists() and not allow_synced:
            raise UsageError(f"{ws} is inside a git repository ({parent}); pick another folder or pass --allow-synced")
    if not allow_synced and any(m in resolved + "/" for m in SYNCED_MARKERS):
        raise UsageError(f"{ws} looks like a synced folder; pick a local folder or pass --allow-synced")


def codex_native(cli: str) -> tuple[str, str]:
    """(native binary, package root) behind an npm `codex` launcher, so no node runtime runs in the sandbox."""
    real = Path(os.path.realpath(cli))
    if real.suffix != ".js":
        return str(real), str(real.parent)
    package = real.parent.parent
    found = sorted(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    if not found:
        raise UsageError(f"cannot find the native codex binary under {package}")
    return str(found[0]), str(package)


def default_config(canaries: list[str]) -> dict:
    claude = shutil.which("claude")
    codex = shutil.which("codex")
    if not claude or not codex:
        raise UsageError("both `claude` and `codex` must be on PATH and logged in")
    codex_bin, codex_root = codex_native(codex)
    # The native installer's `claude` is a symlink its self-updater repoints; the versioned binary behind it keeps a
    # run of several hours on the CLI it qualified.
    return {
        "lineages": {
            "claude": {"cli": os.path.realpath(claude), "model": "claude-opus-5-5",
                       "input_budget_tokens": 180000, "reserve_output_tokens": 32000,
                       "locked_read_result": CLAUDE_LOCKED_READ, "profile_file": CLAUDE_PROFILE_DEFAULT},
            "codex": {"cli": codex_bin, "model": "gpt-6.1-sol",
                      "input_budget_tokens": 400000, "reserve_output_tokens": 32000,
                      "sandbox_exec": "/usr/bin/sandbox-exec", "sandbox_allow": [codex_root],
                      "auth_file": "~/.codex/auth.json", "disable": list(CODEX_DISABLE), "config": list(CODEX_CONFIG),
                      "retained_tools": copy.deepcopy(CODEX_RETAINED), "capture_forward": True},
        },
        "runs": 3,
        "roles": {"zoner": "claude", "codebook_author": "claude", "foil_author": "claude", "auditor": "codex",
                  "facet_mapper": "codex",
                  "crosswalk_coders": ["claude", "codex"]},
        "canaries": list(canaries),
        "env_passthrough": [],
        "timeout_s": 900,
        "not_names": [],
    }


def init(ws_path: Path, texts: list[Path], canaries: list[str], allow_synced: bool,
         config: dict | None = None) -> store.Workspace:
    if ws_path.exists() and any(ws_path.iterdir()):
        raise UsageError(f"{ws_path} already exists and is not empty")
    _refuse_location(ws_path, allow_synced)
    try:
        body = "\n\n".join(p.read_text(encoding="utf-8").strip() for p in texts) + "\n"
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read the text as UTF-8: {exc}") from exc
    if not body.strip():
        raise UsageError("the text is empty")
    cfg = config or default_config(canaries)
    ws = store.Workspace(ws_path)
    original = ws.path("text/original.txt")
    original.parent.mkdir(parents=True)
    original.write_text(body, encoding="utf-8")
    original.chmod(stat.S_IRUSR | stat.S_IRGRP)
    store.write_json(ws.path("config.json"), cfg)
    store.write_json(ws.path(".claude/settings.json"), {"permissions": {"deny": [
        "Read(./sealed/**)", "Edit(./sealed/**)", "Write(./sealed/**)", "Bash(*sealed/*)"]}})
    inst = ipip.load_instrument()
    store.write_text_atomic(ws.path("sealed/self-prediction.md"), SELF_PREDICTION_TEMPLATE)
    store.write_text_atomic(ws.path("sealed/ipip-120.csv"),
                            "item_id,response\n" + "".join(f"{i['id']},\n" for i in inst["items"]))
    store.write_text_atomic(ws.path("sealed/ipip-120-items.md"), "\n".join(
        [IPIP_HEADER, *(f"- {i['id']}: {i['text']}" for i in inst["items"])]) + "\n")
    ws.path("sealed").chmod(stat.S_IRWXU)
    return ws


SELF_PREDICTION_TEMPLATE = """# Self-prediction

Write, before the run, what you expect the blind extraction to find about you. One prediction per line,
each starting with an id (P1, P2, …) and a colon. Keep each to one claim. Do not show this file to the agent.

P1:
"""

IPIP_HEADER = """# IPIP-NEO-120 (Johnson, 2014; public domain)

Answer each statement in ipip-120.csv as you are now: 1 very inaccurate, 2 moderately inaccurate,
3 neither, 4 moderately accurate, 5 very accurate. Leave a response blank to skip it.
"""


def _run(ws_path: Path, args) -> lifecycle.CliResult:
    ws = init(ws_path, args.text, args.canary, args.allow_synced)
    return lifecycle.CliResult({"workspace": str(ws.root), "next": "review_config"})


STAGE = lifecycle.Stage(
    "init", 0, "create a workspace from the text", _run, creates_workspace=True, args=(
        lifecycle.Arg(("--text",), {"type": Path, "nargs": "+", "required": True}),
        lifecycle.Arg(("--canary",), {"nargs": "*", "default": [],
                                      "help": "terms only your own profile would contain"}),
        lifecycle.Arg(("--allow-synced",), {"action": "store_true"})))
