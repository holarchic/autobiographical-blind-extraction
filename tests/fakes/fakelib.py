"""Fake `claude` and `codex` CLIs for the offline suite.

They answer `--version`, read the prompt on stdin, and print each lineage's event stream
around an output generated from the prompt (`roles.respond`). Environment switches inject
failures:
  ABX_FAKE_LOG          directory: every prompt is logged there, with call counters
  ABX_FAKE_VERSION      version string to report
  ABX_FAKE_TOOL_EVENT   emit this tool-event kind (see TOOL_EVENTS)
  ABX_FAKE_FAIL         end with a CLI failure
  ABX_FAKE_LEAK         answer the leak probe with this text
  ABX_FAKE_FAIL_AFTER   fail every call after this many successful ones (interruption)
  ABX_FAKE_FAIL_ROLE    fail every call of this role (interruption at a role boundary)
  ABX_FAKE_RAW          print this string as the final message verbatim
  ABX_FAKE_SLEEP        after printing the stream, sleep this many seconds (timeouts)
  ABX_FAKE_STRAY_CITATION  the deductive scorer cites a sentence id no ledger holds
  ABX_FAKE_PRELUDE      emit this as an earlier assistant message before the final one
  ABX_FAKE_THINKING     (claude) emit this as a thinking block
  ABX_FAKE_NO_INIT      (claude) omit the init event
  ABX_FAKE_NO_MESSAGE   finish successfully without a final message
  ABX_FAKE_LENIENT_SAME the lenient scorer gives every rater the same scores
  ABX_FAKE_STRICT_MIRROR (codex) the strict scorer mirrors its scores around 4
  ABX_FAKE_CROSS_FACET  the deductive scorer scores a facet with no evidence, citing another facet's
  ABX_FAKE_ERROR_NOTICE (codex) emit a top-level error notice with this message
  ABX_FAKE_READ_INSTRUCTIONS  repeat any CLAUDE.md / AGENTS.md found in the working directory, as an
                        unlocked CLI would load it
  ABX_FAKE_STDERR       write this to stderr
  ABX_FAKE_PLANTED      the auditor flags a construct only in an era whose text holds this phrase, citing it
  ABX_FAKE_INVALID      "role:n,role:n": the first n answers to each prompt of that role are not JSON (resampling);
                        they leave the prompt's occurrence count alone, so the valid answer is the usual one
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakecapture
import roles

TOOL_EVENTS = {
    "claude": {
        "tool_use": lambda: {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
        "server_tool_use": lambda: {"type": "assistant", "message": {"content": [
            {"type": "server_tool_use", "id": "t1", "name": "web_search", "input": {}}]}},
        "tool_result": lambda: {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        "unknown": lambda: {"type": "stream_event", "event": {}},
    },
    "codex": {k: (lambda k=k: {"type": "item.completed", "item": {"id": "i9", "type": k}})
              for k in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list",
                        "collab_tool_call", "image_view")}
    | {"unknown": lambda: {"type": "session.configured"}},
}


def _state(log: Path | None, lineage: str, prompt: str) -> tuple[int, int]:
    """(successful calls so far for this lineage, occurrences of this exact prompt so far)."""
    if log is None:
        return 0, 0
    path = log / f"state-{lineage}.json"
    state = json.loads(path.read_text()) if path.exists() else {"calls": 0, "prompts": {}}
    return state["calls"], state["prompts"].get(hashlib.sha256(prompt.encode()).hexdigest(), 0)


def _bump(log: Path | None, lineage: str, prompt: str) -> None:
    if log is None:
        return
    path = log / f"state-{lineage}.json"
    state = json.loads(path.read_text()) if path.exists() else {"calls": 0, "prompts": {}}
    key = hashlib.sha256(prompt.encode()).hexdigest()
    state["calls"] += 1
    state["prompts"][key] = state["prompts"].get(key, 0) + 1
    path.write_text(json.dumps(state))


def _invalid_now(log: Path | None, lineage: str, prompt: str, env: str = "ABX_FAKE_INVALID") -> bool:
    """Whether this answer is one of the `role:times` ones `env` asks for (invalid outputs, or output-limit
    recoveries with ABX_FAKE_OUTPUT_LIMIT), counting it if so."""
    wanted = dict(item.split(":") for item in os.environ.get(env, "").split(",") if item)
    times = int(wanted.get(roles.role_of(prompt), 0))
    if log is None or not times:
        return False
    path = log / f"{env.lower()}-{lineage}.json"
    seen = json.loads(path.read_text()) if path.exists() else {}
    key = hashlib.sha256(prompt.encode()).hexdigest()
    if seen.get(key, 0) >= times:
        return False
    seen[key] = seen.get(key, 0) + 1
    path.write_text(json.dumps(seen))
    return True


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def main(lineage: str) -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(os.environ.get("ABX_FAKE_VERSION", f"{lineage}-fake 1.0.0"))
        return 0
    prompt = sys.stdin.read()
    model = _arg(argv, "--model") or _arg(argv, "-m") or "fake-model"
    endpoint = os.environ.get("ANTHROPIC_BASE_URL") if lineage == "claude" else fakecapture.codex_endpoint(argv)
    if endpoint:
        run = fakecapture.claude if lineage == "claude" else fakecapture.codex
        for ev in run(argv, prompt, model, endpoint):
            print(json.dumps(ev))
        return 0
    log = Path(os.environ["ABX_FAKE_LOG"]) if os.environ.get("ABX_FAKE_LOG") else None
    calls, occurrence = _state(log, lineage, prompt)
    if log is not None:
        log.mkdir(parents=True, exist_ok=True)
        with (log / "prompts.jsonl").open("a") as f:
            f.write(json.dumps({"lineage": lineage, "argv": argv, "prompt": prompt,
                                "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
    fail_after = os.environ.get("ABX_FAKE_FAIL_AFTER")
    failing = bool(os.environ.get("ABX_FAKE_FAIL")) or (fail_after is not None and calls >= int(fail_after)) or \
        os.environ.get("ABX_FAKE_FAIL_ROLE") == roles.role_of(prompt)
    if os.environ.get("ABX_FAKE_READ_INSTRUCTIONS") and "--setting-sources" not in argv and "--disable" not in argv:
        planted = [Path(n).read_text() for n in ("CLAUDE.md", "AGENTS.md") if Path(n).exists()]
        text = json.dumps({"answer": " ".join(planted) or "NONE"})
    elif "ABX_FAKE_RAW" in os.environ:
        text = os.environ["ABX_FAKE_RAW"]
    else:
        text = json.dumps(roles.respond(prompt, lineage, occurrence, os.environ.get("ABX_FAKE_LEAK")))
    invalid = _invalid_now(log, lineage, prompt)
    if invalid:
        text = "not json"
    recovered = lineage == "claude" and _invalid_now(log, lineage, prompt, "ABX_FAKE_OUTPUT_LIMIT")
    if lineage == "claude" and os.environ.get("ABX_FAKE_PROFILE_FETCH"):
        profile = Path(os.environ["HOME"]) / ".claude.json"
        profile.write_text(json.dumps({"oauthAccount": {"emailAddress": "someone@example.com"}}))
    events = []
    injected = os.environ.get("ABX_FAKE_TOOL_EVENT")
    if lineage == "claude":
        if not os.environ.get("ABX_FAKE_NO_INIT"):
            events.append({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": model})
        if os.environ.get("ABX_FAKE_THINKING"):
            events.append({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": os.environ["ABX_FAKE_THINKING"]}]}})
        if os.environ.get("ABX_FAKE_PRELUDE"):
            events.append({"type": "assistant", "message": {"content": [
                {"type": "text", "text": os.environ["ABX_FAKE_PRELUDE"]}]}})
        if injected:
            events.append(TOOL_EVENTS["claude"][injected]())
        events.append({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50})
        if recovered:  # Claude Code's own continuation prompt after the output-token limit, as observed
            events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text[:5]}]}})
            events.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "Output token limit hit. Resume directly, no apology, no recap."}]}})
        events.append({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        events.append({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}})
        events.append({"type": "result", "subtype": "error_during_execution" if failing else "success",
                       "is_error": failing, "result": "fake failure" if failing else text})
    else:
        events += [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
                   {"type": "item.completed", "item": {"id": "i0", "type": "error",
                                                       "message": "Code Mode is unavailable"}}]
        if os.environ.get("ABX_FAKE_ERROR_NOTICE"):
            events.append({"type": "error", "message": os.environ["ABX_FAKE_ERROR_NOTICE"]})
        if os.environ.get("ABX_FAKE_PRELUDE"):
            events.append({"type": "item.completed", "item": {"id": "ip", "type": "agent_message",
                                                              "text": os.environ["ABX_FAKE_PRELUDE"]}})
        if injected:
            events.append(TOOL_EVENTS["codex"][injected]())
        if failing:
            events.append({"type": "turn.failed", "error": {"message": "fake failure"}})
        else:
            if not os.environ.get("ABX_FAKE_NO_MESSAGE"):
                events.append({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}})
            events.append({"type": "turn.completed", "usage": {}})
    if os.environ.get("ABX_FAKE_STDERR"):
        print(os.environ["ABX_FAKE_STDERR"], file=sys.stderr, flush=True)
    for ev in events:
        print(json.dumps(ev), flush=True)
    if os.environ.get("ABX_FAKE_SLEEP"):
        time.sleep(float(os.environ["ABX_FAKE_SLEEP"]))
    if failing:
        return 1
    if not (invalid or recovered):
        _bump(log, lineage, prompt)
    return 0
