"""`config.json`, parsed once at the boundary into typed records. Malformed config is a usage error."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import NamedTuple

from . import store
from .errors import UsageError

__all__ = ["CLAUDE_PROFILE_DEFAULT", "LINEAGES", "Config", "Lineage", "RetainedSpec", "RetainedTool", "load", "parse",
           "retained"]

LINEAGES = ("claude", "codex")


class RetainedTool(NamedTuple):
    """A tool Codex lists even when locked: the scripted call that proves it inert, and its pinned result."""

    tool: str
    call_json: str
    result: str


CLAUDE_PROFILE_DEFAULT = "~/.claude.json"


@dataclass(frozen=True)
class Lineage:
    name: str
    cli: str
    model: str
    input_budget_tokens: int
    reserve_output_tokens: int
    raw_json: str = field(compare=False, repr=False)  # the lineage's config, canonical; the fingerprint hashes it
    locked_read_result: str = ""                      # claude: the pinned refusal of a scripted Read
    profile_file: str = ""                            # claude: the cached account profile (~/.claude.json)
    sandbox_exec: str = ""                            # codex only, from here down
    sandbox_allow: tuple[str, ...] = ()
    auth_file: str = ""
    disable: tuple[str, ...] = ()
    config: tuple[str, ...] = ()
    retained_tools: tuple[RetainedTool, ...] = ()
    capture_forward: bool = False


@dataclass(frozen=True)
class Config:
    lineages: Mapping[str, Lineage]
    runs: int
    zoner: str
    codebook_author: str
    foil_author: str
    auditor: str
    facet_mapper: str
    crosswalk_coders: tuple[str, ...]
    canaries: tuple[str, ...]
    env_passthrough: tuple[str, ...]
    timeout_s: int
    not_names: tuple[str, ...] = ()  # the user's additions to the name scan's non-names

    def lineage(self, name: str) -> Lineage:
        return self.lineages[name]


class RetainedSpec(NamedTuple):
    call: dict
    result: str


def retained(lin: Lineage) -> dict[str, RetainedSpec]:
    """Codex's retained tools by name, each with its scripted call (a fresh object) and pinned result."""
    return {t.tool: RetainedSpec(json.loads(t.call_json), t.result) for t in lin.retained_tools}


def _get(obj: dict, key: str, kind, where: str):
    if not isinstance(obj, dict) or key not in obj:
        raise UsageError(f"config.json: {where} is missing {key!r}")
    value = obj[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise UsageError(f"config.json: {where}.{key} must be {getattr(kind, '__name__', kind)}")
    return value


def _strings(obj: dict, key: str, where: str) -> tuple[str, ...]:
    values = _get(obj, key, list, where)
    if not all(isinstance(v, str) for v in values):
        raise UsageError(f"config.json: {where}.{key} must be a list of strings")
    return tuple(values)


def _lineage(name: str, obj: dict) -> Lineage:
    where = f"lineages.{name}"
    common = {"name": name, "raw_json": store.canonical(obj), "cli": _get(obj, "cli", str, where),
              "model": _get(obj, "model", str, where),
              "input_budget_tokens": _get(obj, "input_budget_tokens", int, where),
              "reserve_output_tokens": _get(obj, "reserve_output_tokens", int, where)}
    if name == "claude":
        # Workspaces made before `profile_file` existed are frozen without it; they used Claude Code's default.
        profile = _get(obj, "profile_file", str, where) if "profile_file" in obj else CLAUDE_PROFILE_DEFAULT
        return Lineage(**common, locked_read_result=_get(obj, "locked_read_result", str, where), profile_file=profile)
    retained = _get(obj, "retained_tools", dict, where)
    for tool, spec in retained.items():
        well_formed = (isinstance(spec, dict) and isinstance(spec.get("call"), dict)
                       and isinstance(spec.get("result"), str))
        if not well_formed:
            raise UsageError(f"config.json: {where}.retained_tools.{tool} needs a call object and a result string")
    return Lineage(**common, sandbox_exec=_get(obj, "sandbox_exec", str, where),
                   sandbox_allow=_strings(obj, "sandbox_allow", where), auth_file=_get(obj, "auth_file", str, where),
                   disable=_strings(obj, "disable", where), config=_strings(obj, "config", where),
                   retained_tools=tuple(RetainedTool(t, store.canonical(spec["call"]), spec["result"])
                                        for t, spec in retained.items()),
                   capture_forward=_get(obj, "capture_forward", bool, where))


def parse(obj: dict) -> Config:
    lineages = _get(obj, "lineages", dict, "config")
    if sorted(lineages) != sorted(LINEAGES):
        raise UsageError(f"config.json: lineages must be exactly {list(LINEAGES)}")
    roles = _get(obj, "roles", dict, "config")
    cfg = Config(
        lineages=MappingProxyType({n: _lineage(n, lineages[n]) for n in LINEAGES}),
        runs=_get(obj, "runs", int, "config"),
        zoner=_get(roles, "zoner", str, "roles"), codebook_author=_get(roles, "codebook_author", str, "roles"),
        foil_author=_get(roles, "foil_author", str, "roles"), auditor=_get(roles, "auditor", str, "roles"),
        facet_mapper=_get(roles, "facet_mapper", str, "roles"),
        crosswalk_coders=_strings(roles, "crosswalk_coders", "roles"), canaries=_strings(obj, "canaries", "config"),
        env_passthrough=_strings(obj, "env_passthrough", "config"), timeout_s=_get(obj, "timeout_s", int, "config"),
        # Workspaces made before `not_names` existed have none.
        not_names=_strings(obj, "not_names", "config") if "not_names" in obj else ())
    for who in (cfg.zoner, cfg.codebook_author, cfg.foil_author, cfg.auditor, cfg.facet_mapper):
        if who not in LINEAGES:
            raise UsageError("config.json: roles name an unknown lineage")
    if cfg.auditor == cfg.foil_author:
        raise UsageError("config.json: roles.auditor must differ from roles.foil_author, so another lineage audits "
                         "the foils")
    if sorted(cfg.crosswalk_coders) != sorted(LINEAGES):
        raise UsageError(f"config.json: roles.crosswalk_coders must be exactly {list(LINEAGES)}, one coder per lineage")
    if not 2 <= cfg.runs <= 9:
        raise UsageError("config.json: runs must be between 2 and 9")
    return cfg


def load(ws: store.Workspace) -> Config:
    return parse(store.read_json(ws.path("config.json")))
