"""The only module that starts a model CLI, and the single boundary every model call passes.

`call` takes a `CallRequest` (artifact ids, never text), assembles and audits the prompt,
checks fingerprint and budget, runs one isolated process (`execute`), audits its event
stream, validates the output and records it, or reuses a record whose every input still
matches. Each process starts in an empty temp directory (also its cwd: Codex reads its
launch directory); Codex runs with a temp CODEX_HOME inside an OS read sandbox.
"""

import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType

from . import artifacts, prompts, store, withheld
from . import schema as jsonschema
from .config import Config, Lineage
from .errors import CallFailed, IntegrityError, IsolationError, UsageError
from .roles import role as get_role
from .text import effective_label

__all__ = ["FINGERPRINT_KEYS", "FIRST_ERA_MARKER", "ISOLATION_POLICY", "MAX_ATTEMPTS", "METHOD_VERSION",
           "QUALIFICATION", "QUALIFICATION_A", "CallRequest", "Event", "ProbeOutcome", "Result", "audit_events",
           "call", "check_fingerprint", "check_method", "claude_argv", "claude_home", "codex_argv", "decode_events",
           "estimate_tokens", "execute", "execute_unlocked", "identity_restored", "invalid_outputs",
           "live_fingerprint", "outcome", "parse_claude", "parse_codex", "probe", "qualified", "record_path",
           "sandbox_profile", "trimmed_outputs"]

SYSTEM_READ_ROOTS = (
    "/System", "/usr/lib", "/usr/libexec", "/usr/bin", "/usr/sbin", "/usr/share", "/bin", "/sbin",
    "/Library/Apple", "/private/etc", "/private/var/db/dyld", "/dev", "/Library/Preferences/SystemConfiguration",
)
BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "TMPDIR", "USER", "LOGNAME")
CLAUDE_TEXT_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})
CODEX_ITEMS = frozenset({"agent_message", "reasoning", "error"})
CODEX_LIFECYCLE = frozenset({"thread.started", "turn.started"})
CODEX_PLAIN_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+\s|Reading additional input from stdin)")
QUALIFICATION = "run/qualification.json"          # the current qualification, until generation A
QUALIFICATION_A = "prereg/qualification-A.json"   # the frozen baseline every later call is held to
FIRST_ERA_MARKER = "(none: this is the first era)"


def sandbox_profile(allow: list[str]) -> str:
    """Deny every file read except system runtime paths and the given (canonicalised) roots.

    The root directory itself must be readable or dyld aborts; metadata reads stay allowed so
    paths resolve. `/usr/local` is deliberately not a runtime root.
    """
    def subpaths(paths):
        return " ".join(f'(subpath "{os.path.realpath(p)}")' for p in paths)

    return (
        "(version 1)\n(allow default)\n(deny file-read*)\n(allow file-read-metadata)\n"
        f'(allow file-read* (literal "/") {subpaths(SYSTEM_READ_ROOTS)})\n'
        f"(allow file-read* {subpaths(allow)})\n"
    )


@dataclass(frozen=True)
class Event:
    """One event from a CLI stream, reduced to what the audit needs."""

    type: str
    detail: str = ""        # Claude: subtype or block types; Codex: the item type
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    registry_present: bool = True


@dataclass
class Result:
    status: str = "no_terminal"     # "success" | "failed" | "no_terminal"
    text: str | None = None         # the final message, the only one parsed as output
    model: str | None = None
    detail: str = ""
    events: list[Event] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)   # every assistant text seen, for leak scans only


# Event kinds named in diagnostics. Anything a CLI emits outside these is reported as "other", so no
# CLI-supplied string reaches an incident, a record's audit or an error message.
_CLAUDE_KNOWN_BLOCKS = frozenset({"tool_use", "server_tool_use", "web_search_tool_result", "mcp_tool_use",
                                  "mcp_tool_result", "image", "document"})
_CLAUDE_KNOWN_EVENTS = frozenset({"user", "stream_event", "system", "rate_limit_event", "identity_restored",
                                  "unparseable"})
# Status events a real Claude stream carries on a tool-free call, as (type, detail): the thinking-token
# progress counter and the account's usage-window status (which has no subtype). Anything else is a tool event.
CLAUDE_TELEMETRY = frozenset({("system", "thinking_tokens"), ("rate_limit_event", "")})
_CODEX_KNOWN_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list",
                                "collab_tool_call", "image_view"})
_CODEX_KNOWN_EVENTS = frozenset({"session.configured", "unparseable"})


_BLOCK_NAME = re.compile(r"[a-z_]+")


def _code(prefix: str, value: str, known: frozenset[str]) -> str:
    return f"{prefix}:{value if value in known else 'other'}"


def audit_events(lineage: str, events: list[Event], allowed: frozenset[str] = frozenset()) -> list[str]:
    """Tool events in a stream, as reason codes: every event kind outside the lineage's allowlist,
    except `allowed` (qualification's scripted calls)."""
    found = []
    for ev in events:
        if f"{ev.type}:{ev.detail}" in allowed or (lineage == "claude" and (ev.type, ev.detail) in CLAUDE_TELEMETRY):
            continue
        if lineage == "claude":
            if ev.type == "system" and ev.detail == "init":
                if not ev.registry_present or ev.tools or ev.mcp_servers:
                    found.append("init_registry_not_empty")
            elif ev.type == "assistant":
                found += [_code("assistant_block", b, _CLAUDE_KNOWN_BLOCKS) for b in ev.detail.split(",")
                          if b and b not in CLAUDE_TEXT_BLOCKS]
            elif ev.type != "result":
                found.append(_code("event", ev.type, _CLAUDE_KNOWN_EVENTS))
        elif ev.type in CODEX_LIFECYCLE or ev.type in {"error", "turn.completed", "turn.failed"}:
            continue
        elif ev.type in {"item.started", "item.updated", "item.completed"}:
            if ev.detail not in CODEX_ITEMS:
                found.append(_code("item", ev.detail, _CODEX_KNOWN_ITEMS))
        else:
            found.append(_code("event", ev.type, _CODEX_KNOWN_EVENTS))
    return found


def _registry_seen(lineage: str, events: list[Event]) -> bool:
    """Claude's init event is its registry evidence; a stream without it proves nothing."""
    return lineage != "claude" or any(e.type == "system" and e.detail == "init" for e in events)


def _terminal_ok(lineage: str, events: list[Event]) -> bool:
    if lineage == "claude":
        results = [e for e in events if e.type == "result"]
        return bool(results) and all(e.detail == "success" for e in results)
    return any(e.type == "turn.completed" for e in events) and not any(e.type == "turn.failed" for e in events)


def outcome(lineage: str, code: int | None, events: list[Event], allowed: frozenset[str] = frozenset()) -> str:
    """The one success predicate for fresh calls, probes, resume and the integrity audit.

    `tool_event` (exit 3) outranks everything; then `timeout` and `failed` (the CLI did not finish:
    exit 5 for calls, 2 for probes); then `no_registry` (finished without registry evidence: exit 3);
    else `success`.
    """
    if audit_events(lineage, events, allowed):
        return "tool_event"
    if code is None:
        return "timeout"
    if code != 0 or not _terminal_ok(lineage, events):
        return "failed"
    if not _registry_seen(lineage, events):
        return "no_registry"
    return "success"


def decode_events(stored) -> list[Event] | None:
    """Events as a record stores them, with exact field types; None for anything malformed, never coerced."""
    def strings(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    if not isinstance(stored, list):
        return None
    events = []
    for e in stored:
        if not (isinstance(e, dict) and isinstance(e.get("type"), str) and isinstance(e.get("detail"), str)
                and strings(e.get("tools")) and strings(e.get("mcp_servers"))
                and isinstance(e.get("registry_present"), bool)):
            return None
        events.append(Event(e["type"], e["detail"], tuple(e["tools"]), tuple(e["mcp_servers"]), e["registry_present"]))
    return events


def _strings(value) -> tuple[str, ...] | None:
    """A registry list as the CLI sent it; anything else (not a list, or a non-string entry) is no registry evidence,
    never coerced into some."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return None


def _block_type(block: dict) -> str:
    """A block's type for the allowlist; one that is not a plain name (a comma would split it into allowed parts
    when the event is audited) is `other`."""
    kind = block.get("type")
    return kind if isinstance(kind, str) and _BLOCK_NAME.fullmatch(kind) else "other"


def parse_claude(lines: list[str]) -> Result:
    out = Result()
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = store.loads_strict(line)  # a repeated key must not let a later value hide an earlier one
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            out.events.append(Event("unparseable"))
            continue
        etype, sub = str(ev.get("type")), ev.get("subtype")
        if etype == "system" and sub == "init":
            out.model = ev.get("model") if isinstance(ev.get("model"), str) else None
            tools, servers = _strings(ev.get("tools")), _strings(ev.get("mcp_servers"))
            out.events.append(Event("system", "init", tools or (), servers or (),
                                    registry_present=tools is not None and servers is not None))
        elif etype == "assistant":
            message = ev.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
                out.events.append(Event("unparseable"))
                continue
            out.events.append(Event("assistant", ",".join(_block_type(b) for b in blocks)))
            out.messages += [b[k] for b in blocks for k in ("text", "thinking") if isinstance(b.get(k), str)]
        elif etype == "result":
            ok = sub == "success" and ev.get("is_error") is False and isinstance(ev.get("result"), str)
            out.events.append(Event("result", "success" if ok else "failed"))
            if isinstance(ev.get("result"), str):
                out.messages.append(ev["result"])
            out.status = "success" if ok and out.status != "failed" else "failed"
            out.text = ev["result"] if ok else None
            out.detail = "" if ok else "result_failed"
        else:
            # Telemetry matches only an exact string subtype, or no subtype field at all; any other value is "other".
            detail = "" if "subtype" not in ev else sub if isinstance(sub, str) and sub else None
            telemetry = (etype, detail) in CLAUDE_TELEMETRY
            out.events.append(Event(etype if etype in _CLAUDE_KNOWN_EVENTS else "other",
                                    detail if telemetry else ("other" if "subtype" in ev else "")))
    return out


def _texts(value) -> list[str]:
    """Every string inside a nested field, for leak scans."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    return []


def parse_codex(lines: list[str]) -> Result:
    out = Result()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if CODEX_PLAIN_LINE.match(stripped):
            out.messages.append(stripped)  # a diagnostic line: not an event, but still scanned for canaries
            continue
        try:
            ev = store.loads_strict(stripped)
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            out.events.append(Event("unparseable"))
            continue
        etype = str(ev.get("type"))
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        out.events.append(Event(etype, str(item.get("type", ""))))
        # Everything a message, reasoning or diagnostic carries is scanned for canaries, top-level errors too.
        out.messages += [t for k in ("text", "summary", "content", "message") for t in _texts(item.get(k))]
        out.messages += _texts(ev.get("message")) + _texts(ev.get("error"))
        text = item.get("text")
        if etype == "item.completed" and item.get("type") == "agent_message":
            out.text = text if isinstance(text, str) else None
        elif etype == "turn.completed" and out.status != "failed":
            out.status = "success"
        elif etype == "turn.failed":
            out.status = "failed"
            out.detail = "turn_failed"
    return out


def claude_argv(lin: Lineage, tools: str = "", extra: tuple[str, ...] = ()) -> list[str]:
    return [lin.cli, "-p", "--setting-sources", "", "--strict-mcp-config", "--tools", tools, *extra,
            "--no-session-persistence", "--output-format", "stream-json", "--verbose", "--model", lin.model]


def codex_argv(lin: Lineage, locked: bool = True) -> list[str]:
    """The pinned Codex argv; `locked=False` (qualification's positive controls only) drops sandbox and disables."""
    core = [lin.cli, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-s", "read-only",
            "--skip-git-repo-check", "--cd", "<cwd>", "-m", lin.model]
    if not locked:
        return [*core, "-"]
    flags = [x for f in lin.disable for x in ("--disable", f)] + [x for c in lin.config for x in ("-c", c)]
    return [lin.sandbox_exec, "-f", "<profile>", *core, "--output-schema", "<schema>", *flags, "-"]


def _pinned_argv(lin: Lineage) -> list[str]:
    return claude_argv(lin) if lin.name == "claude" else codex_argv(lin)


def cli_version(cli: str) -> str:
    try:
        p = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageError(f"cannot run {cli} --version: {exc}") from exc
    if p.returncode != 0:
        raise UsageError(f"{cli} --version exited {p.returncode}")
    return p.stdout.strip()


# Bumped whenever how a locked call is isolated, its profile guarded or its stream audited changes outside its argv
# and config. It is part of the fingerprint, so a qualification or call record made under an older policy is never
# held to, or reused under, the current one.
ISOLATION_POLICY = 3
# Bumped whenever how verdicts are coded, resampled or counted, or what preparation must pass, changes, so a workspace
# prepared under older rules can never be finished under these.
METHOD_VERSION = 11
# What a requalification compares against the baseline: any change means the lineage moved.
FINGERPRINT_KEYS = ("cli", "cli_version", "invocation_hash", "config_hash", "isolation_policy", "method_version",
                    "resolved_model")


def live_fingerprint(cfg: Config, lineage: str) -> dict:
    lin = cfg.lineage(lineage)
    return {
        "cli": lin.cli,
        "cli_version": cli_version(lin.cli),
        "invocation_hash": store.sha256_text(store.canonical(_pinned_argv(lin))),
        "config_hash": store.sha256_text(lin.raw_json),
        "isolation_policy": ISOLATION_POLICY,
        "method_version": METHOD_VERSION,
    }


def check_method(ws: store.Workspace) -> None:
    """A workspace prepared under an older isolation policy or method version is never continued: its earlier calls
    and records were made under other rules. Stages that write artifacts call this before writing any."""
    current = {"isolation_policy": ISOLATION_POLICY, "method_version": METHOD_VERSION}
    if any(q.get(k) != v for q in qualified(ws).values() for k, v in current.items()):
        raise IntegrityError("this workspace was prepared under an older isolation policy or method version; "
                             "start a new workspace")


def qualified(ws: store.Workspace) -> dict:
    """The qualification calls are held to: the frozen baseline once generation A exists."""
    for rel in (QUALIFICATION_A, QUALIFICATION):
        if ws.path(rel).exists():
            return store.read_json(ws.path(rel))
    raise UsageError("no qualification on record; run `prepare` first")


def check_fingerprint(cfg: Config, lineage: str, qualified_fp: dict) -> dict:
    live = live_fingerprint(cfg, lineage)
    changed = [k for k, v in live.items() if qualified_fp.get(k) != v]
    if changed:
        raise IntegrityError(f"{lineage} fingerprint changed since qualification: {', '.join(changed)}")
    return {**live, "resolved_model": qualified_fp.get("resolved_model")}


def _env(cfg: Config, extra: dict[str, str]) -> dict[str, str]:
    keys = (*BASE_ENV_KEYS, *cfg.env_passthrough)
    return {**{k: os.environ[k] for k in keys if k in os.environ}, **extra}


# Claude Code puts the logged-in account's email into the model's context (from `oauthAccount` in its profile
# file), whatever `--setting-sources` says. Locked Claude runs therefore get a HOME holding only the login: the
# keychain, and the profile with these fields blank and marked fresh, so it is not fetched again. Essential-traffic
# mode skips the startup bootstrap that would merge the email back, and env overrides that relocate the profile or
# supply an identity are dropped. This suppresses the identity by construction; the post-call check below detects a
# profile written back, not what the CLI held in memory, and the leak probe is the end-to-end check.
CLAUDE_IDENTITY = ("emailAddress", "fullName", "displayName", "organizationName")
CLAUDE_CACHE_FIELDS = ("billingType", "accountCreatedAt", "subscriptionCreatedAt", "ccOnboardingFlags")
# The only account fields copied into the locked profile: login and plan metadata, never identity.
CLAUDE_ACCOUNT_FIELDS = (*CLAUDE_CACHE_FIELDS, "accountUuid", "organizationUuid", "organizationType", "seatTier",
                         "organizationRateLimitTier", "userRateLimitTier", "hasExtraUsageEnabled",
                         "claudeCodeTrialEndsAt", "claudeCodeTrialDurationDays")
CLAUDE_LOCKED_ENV = MappingProxyType({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
CLAUDE_PROFILE_ENV = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ACCOUNT_UUID",
                                "CLAUDE_CODE_ORGANIZATION_UUID", "ANTHROPIC_UNIX_SOCKET"})


def claude_home(tmp: Path, lin: Lineage) -> Path:
    """The locked HOME. A profile Claude Code would fetch again (unreadable, or missing the cache fields) is a
    preflight failure, before any process starts."""
    source = os.path.expanduser(lin.profile_file)
    try:
        account = store.loads_strict(Path(source).read_text()).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        account = None
    if not (isinstance(account, dict) and all(account.get(k) is not None for k in CLAUDE_CACHE_FIELDS)):
        raise UsageError(f"Claude Code's cached account profile at {lin.profile_file} is missing or incomplete; "
                         "log in and run `claude` once, then retry")
    home = tmp / "home"
    (home / "Library").mkdir(parents=True)
    keychains = Path(os.path.expanduser("~/Library/Keychains"))
    if keychains.is_dir():
        (home / "Library" / "Keychains").symlink_to(keychains)
    store.write_json(home / ".claude.json", {"oauthAccount": {
        **{k: account[k] for k in CLAUDE_ACCOUNT_FIELDS if k in account}, **dict.fromkeys(CLAUDE_IDENTITY, ""),
        "profileFetchedAt": int(time.time() * 1000)}})
    return home


def identity_restored(home: Path) -> bool:
    """True unless the profile is still there with every identity field exactly "". Anything else (an identity
    written back, a removed or malformed profile) means the CLI rewrote it during the run, and fails closed."""
    try:
        account = store.loads_strict((home / ".claude.json").read_text()).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return True
    return not isinstance(account, dict) or any(account.get(k, None) != "" for k in CLAUDE_IDENTITY)


def _text(value) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


def _run(argv: list[str], prompt: str, cwd: Path, env: dict[str, str],
         timeout: int) -> tuple[int | None, list[str], list[str]]:
    """(exit code, stdout lines, stderr lines); exit code None on a timeout, with whatever arrived."""
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout,
                           check=False)
    except subprocess.TimeoutExpired as exc:
        return None, _text(exc.stdout).splitlines(), _text(exc.stderr).splitlines()
    except OSError as exc:
        raise CallFailed(f"CLI could not start: {exc}") from exc
    return p.returncode, p.stdout.splitlines(), p.stderr.splitlines()


def _parsed(lineage: str, code: int | None, out: list[str], err: list[str]) -> tuple[int | None, Result]:
    """The stdout stream parsed; stderr is never an event, but every line of it is scanned for canaries."""
    result = parse_claude(out) if lineage == "claude" else parse_codex(out)
    result.messages += err
    return code, result


def execute(cfg: Config, lineage: str, prompt_text: str, schema: str | None, extra_args: list[str] | None = None,
            env_extra: dict[str, str] | None = None) -> tuple[int | None, Result]:
    """One locked CLI process on the pinned argv in an empty directory; returns its exit code (None on timeout)
    and parsed stream. Capture runs pass provider overrides: `extra_args`, inserted before Codex's trailing stdin
    marker, and `env_extra`. A locked Claude profile rewritten during the run is reported as an
    `identity_restored` event, so every caller's audit and records see it."""
    return _execute(cfg, lineage, prompt_text, schema, None, extra_args, env_extra, None)


def execute_unlocked(cfg: Config, lineage: str, prompt_text: str, schema: str | None, argv: list[str],
                     extra_args: list[str] | None = None, env_extra: dict[str, str] | None = None,
                     cwd_files: dict[str, str] | None = None) -> tuple[int | None, Result]:
    """A positive control only: `argv` as an ordinary user runs the CLI, without the locked Claude HOME, and
    optionally with instruction files planted in its directory."""
    return _execute(cfg, lineage, prompt_text, schema, list(argv), extra_args, env_extra, cwd_files)


def _execute(cfg: Config, lineage: str, prompt_text: str, schema: str | None, unlocked_argv: list[str] | None,
             extra_args: list[str] | None, env_extra: dict[str, str] | None,
             cwd_files: dict[str, str] | None) -> tuple[int | None, Result]:
    lin = cfg.lineage(lineage)
    argv = unlocked_argv if unlocked_argv is not None else _pinned_argv(lin)
    if extra_args:
        argv = [*argv[:-1], *extra_args, argv[-1]] if argv[-1] == "-" else [*argv, *extra_args]
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="abx-")))
    try:
        cwd = tmp / "cwd"
        cwd.mkdir()
        for name, body in (cwd_files or {}).items():
            (cwd / name).write_text(body)
        env = _env(cfg, env_extra or {})
        if lineage == "claude" and unlocked_argv is not None:
            return _parsed(lineage, *_run(argv, prompt_text, cwd, env, cfg.timeout_s))
        if lineage == "claude":
            home = claude_home(tmp, lin)
            locked_env = {**{k: v for k, v in env.items() if k not in CLAUDE_PROFILE_ENV}, **CLAUDE_LOCKED_ENV,
                          "HOME": str(home)}
            code, result = _parsed(lineage, *_run(argv, prompt_text, cwd, locked_env, cfg.timeout_s))
            if identity_restored(home):
                result.events.append(Event("identity_restored"))
            return code, result
        home = tmp / "home"
        home.mkdir()
        auth = Path(os.path.expanduser(lin.auth_file))
        if not auth.is_file():
            raise UsageError(f"Codex login not found at {lin.auth_file}; run `codex login` first")
        shutil.copy(auth, home / "auth.json")
        (tmp / "profile.sb").write_text(sandbox_profile([*lin.sandbox_allow, str(tmp)]))
        (tmp / "schema.json").write_text(schema or "{}")
        argv = [{"<profile>": str(tmp / "profile.sb"), "<cwd>": str(cwd),
                 "<schema>": str(tmp / "schema.json")}.get(a, a) for a in argv]
        return _parsed(lineage, *_run(argv, prompt_text, cwd, {**env, "CODEX_HOME": str(home)}, cfg.timeout_s))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4 * 1.2)


@dataclass(frozen=True)
class CallRequest:
    """One model call, named by artifact ids. `call` turns it into a prompt; nothing else may."""

    stage: str
    role: str
    lineage: str
    run: int | None
    era: str | None
    generation: str
    slots: Mapping[str, str]
    validate: Callable[[object], list[str]] | None = None
    part: int | None = None  # a zoning chunk's or calibration excerpt's number within its era
    trim: Callable[[object], object] | None = None  # applied to a parsed output before its schema is checked

    def __post_init__(self):
        object.__setattr__(self, "slots", MappingProxyType(dict(self.slots)))

    @property
    def identity(self) -> str:
        # "." cannot occur in an era id, so a chunk's identity never collides with another era's.
        part = f".{self.part}" if self.part is not None else ""
        return f"{self.stage}/{self.role}/{self.lineage}-r{self.run or 0}-{self.era or 'all'}{part}"


def record_path(ws: store.Workspace, req: CallRequest) -> Path:
    return ws.path(f"run/calls/{req.identity}.json")


def _eras(ws: store.Workspace) -> list[dict]:
    path = ws.path("prepare/eras.json")
    return store.read_json(path)["eras"] if path.exists() else []


def _labels(ws: store.Workspace, req: CallRequest) -> dict[str, str]:
    """The labels a role's template takes, derived from the workspace, never from the caller."""
    policy = get_role(req.role)
    eras = _eras(ws)
    labels = {}
    if "era_label" in policy.params:
        era = next((e for e in eras if e["id"] == req.era), None)
        if era is None:
            raise UsageError(f"{req.role} call names an unknown era")
        labels["era_label"] = effective_label(era)
    if "hypotheses" in policy.params and "hypotheses" not in req.slots:
        if not eras or eras[0]["id"] != req.era:
            raise IsolationError(f"{req.role} for a later era must carry its own earlier hypotheses")
        labels["hypotheses"] = FIRST_ERA_MARKER
    return labels


def load_output(text: str | None) -> tuple[object, list[str]]:
    if text is None:
        return None, ["no final message"]
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    try:
        return store.loads_strict(body), []
    except ValueError:
        return None, ["output is not JSON"]


def parse_output(text: str | None, schema: dict, validate, trim=None) -> tuple[object, list[str]]:
    out, problems = load_output(text)
    if problems:
        return None, problems
    if trim:
        # the whole output, whatever the trim will cut, passes the validator first
        problems = validate(out) if validate else []
        if problems:
            return out, problems
        out = trim(out)
    problems = jsonschema.errors(out, schema)
    if not problems and validate:
        problems = validate(out)
    return out, problems


# An output failing its schema or validator is resampled with the same prompt up to this many attempts in all.
MAX_ATTEMPTS = 3
RESAMPLED_STAGES = ("prepare", "freeze", "extract", "unmask")


def _incident(ws: store.Workspace, req: CallRequest, kind: str, detail: list[str]) -> None:
    """One file per attempt, numbered, so a resample or a rerun never overwrites earlier evidence."""
    n = 1
    while ws.path(f"run/incidents/{req.identity}-{n}.json").exists():
        n += 1
    store.write_json(ws.path(f"run/incidents/{req.identity}-{n}.json"),
                     {"identity": req.identity, "kind": kind, "detail": detail})


def invalid_outputs(ws: store.Workspace) -> dict[str, int]:
    """Invalid outputs per stage, each resampled or the last of its call's attempts, counted only from the incidents
    the manifest froze (generation C for prepare, freeze and extract, D for unmask): an incident added later counts
    nothing."""
    frozen = [rel for e in store.read_manifest(ws) for rel in e["artifacts"] if rel.startswith("run/incidents/")]
    return {stage: sum(1 for rel in frozen if rel.startswith(f"run/incidents/{stage}/")
                       and store.read_json(ws.path(rel))["kind"] == "invalid_output") for stage in RESAMPLED_STAGES}


def trimmed_outputs(ws: store.Workspace) -> dict[str, int]:
    """Outputs a request's trim cut, per stage, counted only from the call records the manifest froze."""
    frozen = [rel for e in store.read_manifest(ws) for rel in e["artifacts"] if rel.startswith("run/calls/")]
    return {stage: sum(1 for rel in frozen if rel.startswith(f"run/calls/{stage}/")
                       and "untrimmed_output" in store.read_json(ws.path(rel))) for stage in RESAMPLED_STAGES}


def _reusable(ws: store.Workspace, req: CallRequest, expect: dict, schema: dict) -> object:
    """A record is reused only when it is this call's, matches every input, and its recorded stream
    re-derives as a success under the same predicate a fresh call must pass."""
    path = record_path(ws, req)
    if not path.exists():
        return None
    rec = store.read_json(path)
    identity = {"identity": req.identity, "stage": req.stage, "role": req.role, "lineage": req.lineage,
                "run": req.run, "era": req.era}
    if not isinstance(rec, dict) or "output" not in rec or \
            any(rec.get(k) != v for k, v in {**identity, **expect}.items()):
        return None
    events = decode_events(rec.get("events"))
    if events is None:
        return None
    code = rec.get("exit")
    if not isinstance(code, int) or isinstance(code, bool):
        return None
    state = outcome(req.lineage, code, events)
    if state == "tool_event":
        raise IsolationError(f"{req.identity}: its record holds a tool event; it cannot be reused")
    if state != "success":
        return None
    # A recorded output is already trimmed: one the trim would still change was never this call's.
    out, problems = parse_output(store.canonical(rec["output"]), schema, req.validate, req.trim)
    return None if problems or out != rec["output"] else rec["output"]


def call(ws: store.Workspace, cfg: Config, req: CallRequest) -> object:
    """Assemble, audit, run (or reuse) one model call and return its validated output."""
    rater = (req.lineage, req.run) if req.run is not None else None
    slots = {**req.slots, "schema": f"schema:{get_role(req.role).schema}"}
    revealed = "C" in store.frozen_generations(ws)
    prompt = prompts.assemble(req.role, slots, artifacts.resolver(ws), _labels(ws, req), rater=rater, era=req.era,
                              era_order=[e["id"] for e in _eras(ws)], revealed=revealed)
    prompts.audit(prompt, withheld.load(ws), after_blind_results=revealed)
    fingerprint = check_fingerprint(cfg, req.lineage, qualified(ws)[req.lineage])
    lin = cfg.lineage(req.lineage)
    need = estimate_tokens(prompt.text) + lin.reserve_output_tokens
    if need > lin.input_budget_tokens:
        raise UsageError(f"{req.role} input for {req.lineage} needs ~{need} tokens, over the "
                         f"{lin.input_budget_tokens} budget")
    s_text = artifacts.resolver(ws)(slots["schema"])
    s_obj = store.loads_strict(s_text)
    expect = {"generation": req.generation, "fingerprint": fingerprint, "input_artifacts": dict(prompt.artifacts),
              "schema_hash": store.sha256_text(s_text), "prompt_hash": store.sha256_text(prompt.text)}
    reused = _reusable(ws, req, expect, s_obj)
    if reused is not None:
        return reused
    for _ in range(MAX_ATTEMPTS):
        code, result = _attempt(ws, cfg, req, prompt.text, s_text, fingerprint)
        output, problems = parse_output(result.text, s_obj, req.validate, req.trim)
        if not problems:
            break
        _incident(ws, req, "invalid_output", problems[:10])
    else:
        raise CallFailed(f"{req.identity}: invalid output on all {MAX_ATTEMPTS} attempts ({problems[0]})")
    untrimmed, _ = load_output(result.text)
    store.write_json(record_path(ws, req), {
        "attempt_id": store.sha256_text(store.canonical(expect))[:16], "identity": req.identity,
        "stage": req.stage, "role": req.role, "lineage": req.lineage, "run": req.run, "era": req.era,
        "exit": code, **expect, "output": output, "events": [asdict(e) for e in result.events],
        # what the model returned, kept as evidence whenever the trim cut it
        **({"untrimmed_output": untrimmed} if untrimmed != output else {}),
    })
    return output


def _attempt(ws: store.Workspace, cfg: Config, req: CallRequest, prompt_text: str, s_text: str,
             fingerprint: dict) -> tuple[int | None, Result]:
    """One execution, audited before its output is looked at: a tool event, a CLI failure or a timeout ends the call
    here and is never resampled."""
    code, result = execute(cfg, req.lineage, prompt_text, s_text)
    state = outcome(req.lineage, code, result.events)
    if state == "tool_event":
        _incident(ws, req, "tool_event", audit_events(req.lineage, result.events))
        raise IsolationError(f"{req.identity}: tool event; output discarded")
    if state in {"timeout", "failed"}:
        _incident(ws, req, "call_failed", [state])
        raise CallFailed(f"{req.identity}: the CLI {'timed out' if state == 'timeout' else 'failed'}")
    if state == "no_registry":
        _incident(ws, req, "tool_event", ["no_registry_evidence"])
        raise IsolationError(f"{req.identity}: the stream carried no registry evidence")
    if req.lineage == "claude" and (not result.model or result.model != fingerprint["resolved_model"]):
        raise IntegrityError(f"{req.identity}: the model is not the qualified {fingerprint['resolved_model']}")
    return code, result


PROBE_MODES = ("leak", "control")


@dataclass(frozen=True)
class ProbeOutcome:
    """A probe's verdict: `clean`, `leak` (exit 3), `isolation` (exit 3) or `error` (exit 2, never a pass);
    for the control, `leak` is the pass."""

    verdict: str
    reason: str
    model: str | None
    record: Mapping


def _control_argv(lin: Lineage) -> list[str]:
    """The probe's positive control: the CLI as an ordinary user runs it, so it reads instruction files."""
    if lin.name == "claude":
        return [lin.cli, "-p", "--tools", "", "--no-session-persistence", "--output-format", "stream-json", "--verbose",
                "--model", lin.model]
    return [lin.cli, "exec", "--json", "--ephemeral", "-s", "read-only", "--skip-git-repo-check", "--cd", "<cwd>",
            "-m", lin.model, "-"]


def probe(ws: store.Workspace, cfg: Config, lineage: str, canaries: tuple[str, ...],
          mode: str = "leak") -> ProbeOutcome:
    """The one probe boundary. `leak` runs the pinned invocation in an empty directory and passes only on
    exactly NONE with no canary anywhere in the stream. `control` plants an instruction file holding a fresh
    canary and runs the CLI unlocked; it passes only if that run succeeds and reports that canary. Every
    probe is recorded, without reply content, at run/probes/<mode>-<lineage>.json."""
    if mode not in PROBE_MODES:
        raise UsageError(f"unknown probe mode {mode!r}")
    prompt = prompts.assemble("probe", {"schema": "schema:probe"}, artifacts.resolver(ws))
    prompts.audit(prompt, withheld.load(ws))
    schema_text = artifacts.resolver(ws)("schema:probe")
    lin = cfg.lineage(lineage)
    planted = f"ABXLEAKCONTROL{secrets.token_hex(4).upper()}"
    # Shaped like the leak it stands in for (a user profile in an instruction file), with the canary as the
    # person's name: a bare codename reads as no information, and a model may rightly answer NONE.
    note = (f"# About the user\n\nThe user you are working for is {planted}, an accountant in Leeds. On the "
            f"IPIP-NEO-120 they score high on Conscientiousness and low on Extraversion.\n")
    try:
        if mode == "leak":
            code, result = execute(cfg, lineage, prompt.text, schema_text)
        else:
            code, result = execute_unlocked(cfg, lineage, prompt.text, schema_text, _control_argv(lin),
                                            cwd_files={"CLAUDE.md": note, "AGENTS.md": note})
    except CallFailed:
        code, result = -1, Result(status="failed")
    seen = "\n".join([*result.messages, result.text or ""]).lower()
    if mode == "control":
        finished = code == 0 and _terminal_ok(lineage, result.events)
        verdict, reason = (("leak", "planted_canary_reported") if finished and planted.lower() in seen
                           else ("error", "control_failed" if not finished else "planted_canary_not_reported"))
    else:
        verdict, reason = _leak_verdict(lineage, code, result, schema_text,
                                        any(c.lower() in seen for c in canaries))
    record = {"mode": mode, "lineage": lineage, "verdict": verdict, "reason": reason,
              "prompt_hash": store.sha256_text(prompt.text), "exit": code, "events": len(result.events),
              "tool_events": tuple(audit_events(lineage, result.events))}
    store.write_json(ws.path(f"run/probes/{mode}-{lineage}.json"), record)
    return ProbeOutcome(verdict, reason, result.model, MappingProxyType(record))


def _leak_verdict(lineage: str, code: int | None, result: Result, schema_text: str,
                  canary_seen: bool) -> tuple[str, str]:
    state = outcome(lineage, code, result.events)
    if state == "tool_event":
        return "isolation", "tool_event"
    if canary_seen:
        return "leak", "canary"
    if state in {"timeout", "failed"}:
        return "error", state
    if state == "no_registry":
        return "isolation", "no_registry"
    if lineage == "claude" and not result.model:
        return "error", "no_model"
    if result.text is None:
        return "error", "no_final_message"
    try:
        reply = store.loads_strict(result.text.strip())
    except ValueError:
        return "error", "not_json"
    if jsonschema.errors(reply, store.loads_strict(schema_text)):
        return "error", "schema"
    return ("clean", "none") if reply["answer"].strip() == "NONE" else ("leak", "not_none")
