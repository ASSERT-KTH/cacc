#!/usr/bin/env python3
"""
epistemic_agent.py — a self-documenting coding agent (agentknit concept car).

The idea: an agent cannot run a shell command without saying *what kind of
act* it is and *why*.  Every `bash` call carries two mandatory fields:

  type    — one of a small closed vocabulary of epistemic/effectful acts
            (collect-info, test-hypothesis, change-state, verify,
             setup, revert, cleanup)
  reason  — free text: the intent behind this specific command

Both are validated in-process: an ill-typed or hand-wavy call never reaches
the shell, it comes back as a tool error the model must fix.  Every accepted
call is appended to a journal (`.epistemic/journal.jsonl`) and the journal is
re-rendered into `RATIONALE.md` after *every* tool call, so the narrative of
the session exists on disk even if the agent is killed mid-run.

The taxonomy is the interesting part.  The three obvious types
(change-state / test-hypothesis / collect-info) do not cover what a coding
agent actually does, so four more are needed to keep the buckets honest:

  verify   — re-running a known check to confirm finished work.  Not a
             hypothesis test: nothing is in doubt, we are producing evidence.
  setup    — preparing the environment (installs, scaffolding, fixtures).
             It mutates state, but it is a *means*, not the deliverable.
  revert   — undoing a previous change-state that proved wrong.  Separating
             it from change-state makes dead ends visible in the report.
  cleanup  — removing the agent's own temporary artifacts.

Exposed tools:
  bash              — shell execution; requires `type` and `reason`
  read_file         — read a file; requires `reason` (type: collect-info)
  write_file        — write a file; requires `reason` (type: change-state)
  str_replace_edit  — substring edit; requires `reason` (type: change-state)

The file tools take `reason` but not `type`: their act type is a property of
the tool, not of the situation, so asking for it would only invite lying.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from agentknit import Tool, build_tool_spec, register_tools_in_library, run_task, run_repl
from agentknit.tool_library import t_read, t_write, t_update, t_run
from agentknit._core import DEFAULT_ENDPOINT

# ── the taxonomy ─────────────────────────────────────────────────────────────
# name -> (one-line meaning shown to the model, does it mutate the world?)
ACT_TYPES: dict[str, tuple[str, bool]] = {
    "collect-info": (
        "Read-only observation to build understanding. Nothing is being "
        "tested, nothing changes. (ls, cat, grep, git log, --help)",
        False,
    ),
    "test-hypothesis": (
        "An experiment run to confirm or refute a specific belief you hold "
        "right now. The reason MUST state the belief and what observation "
        "would refute it.",
        False,
    ),
    "change-state": (
        "Mutates the world in a way meant to persist: it is part of the "
        "deliverable. (editing files via shell, git commit, mv, rm)",
        True,
    ),
    "verify": (
        "Re-runs a known check to confirm work you already did is correct. "
        "Not a hypothesis test — you are producing evidence, not learning.",
        False,
    ),
    "setup": (
        "Prepares the environment so other work becomes possible (installs, "
        "scaffolding, fixtures). Mutates state, but is a means, not the "
        "deliverable.",
        True,
    ),
    "revert": (
        "Undoes a previous change-state that proved wrong (git checkout --, "
        "restoring a backup). Marks a dead end explicitly.",
        True,
    ),
    "cleanup": (
        "Removes temporary artifacts you created yourself. Never touches "
        "anything the user cares about.",
        True,
    ),
}

MIN_REASON_CHARS = 20

# Words that indicate a reason actually articulates a hypothesis rather than
# just restating the command.
_HYPOTHESIS_MARKERS = (
    "if ", "expect", "should", "hypoth", "because", "whether", "suspect",
    "assume", "believe", "confirm", "refute", "check ", "unsure", "maybe",
    "probably", "would ",
)

JOURNAL_DIR = Path(".epistemic")
JOURNAL_FILE = JOURNAL_DIR / "journal.jsonl"
REPORT_FILE = Path("RATIONALE.md")

_t0 = time.time()
_seq = 0


# ── journal ──────────────────────────────────────────────────────────────────
def _record(entry: dict[str, object]) -> None:
    """Append one entry to the journal and re-render the report."""
    global _seq
    _seq += 1
    entry = {
        "seq": _seq,
        "t": round(time.time() - _t0, 2),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **entry,
    }
    JOURNAL_DIR.mkdir(exist_ok=True)
    with JOURNAL_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _render_report()


def _load_journal() -> list[dict[str, object]]:
    if not JOURNAL_FILE.exists():
        return []
    out: list[dict[str, object]] = []
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _render_report() -> None:
    """Rewrite RATIONALE.md from the journal — the self-documentation."""
    entries = _load_journal()
    if not entries:
        return

    declared = [e for e in entries if e.get("reason")]
    counts: dict[str, int] = {}
    for e in entries:
        if e.get("act") in ACT_TYPES:
            counts[str(e["act"])] = counts.get(str(e["act"]), 0) + 1

    lines: list[str] = [
        "# Session rationale",
        "",
        f"_Generated by epistemic_agent from `{JOURNAL_FILE}` — "
        f"{len(entries)} tool call(s), of which {len(declared)} shell act(s) "
        f"with a declared intent._",
        "",
        "## Shape of the session",
        "",
        "| act type | calls | share |",
        "|---|---:|---:|",
    ]
    total = len(entries) or 1
    for name in ACT_TYPES:
        n = counts.get(name, 0)
        if n:
            lines.append(f"| `{name}` | {n} | {100 * n / total:.0f}% |")
    lines += ["", "## Narrative", ""]

    for e in entries:
        act = str(e.get("act", "?"))
        cmd = str(e.get("command", ""))
        ok = e.get("ok")
        mark = {True: "ok", False: "FAILED", None: ""}.get(ok, "")
        head = f"### {e['seq']}. `{act}`" + (f" — {mark}" if mark else "")
        lines.append(head)
        lines.append("")
        reason = str(e.get("reason") or "_(untyped tool — reason not required)_")
        lines.append(f"**Why:** {reason}")
        lines.append("")
        lines.append("```console")
        lines.append(f"$ {cmd}")
        lines.append("```")
        outcome = str(e.get("outcome") or "").strip()
        if outcome:
            lines.append("")
            lines.append(f"**Outcome:** {outcome}")
        lines.append("")

    # Dead ends are the thing post-hoc readers most want and never get.
    reverts = [e for e in entries if e.get("act") == "revert"]
    if reverts:
        lines += ["## Dead ends", ""]
        for e in reverts:
            lines.append(f"- (call {e['seq']}) {e.get('reason')}")
        lines.append("")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


# ── validation ───────────────────────────────────────────────────────────────
def _reject(message: str) -> tuple[str, dict[str, object]]:
    r = "TOOL CALL REJECTED (not executed): " + message
    return r, {"result": r, "rejected": True}


def _type_menu() -> str:
    return "\n".join(f"  - {k}: {v[0]}" for k, v in ACT_TYPES.items())


def t_bash(command: str = "", type: str = "", reason: str = "") -> tuple[str, dict[str, object]]:
    """Run a shell command, but only if it is typed and justified."""
    command = (command or "").strip()
    act = (type or "").strip().lower()
    why = (reason or "").strip()

    if not command:
        return _reject("`command` is empty.")
    if not act:
        return _reject(
            "`type` is mandatory. Re-issue the call with one of:\n" + _type_menu()
        )
    if act not in ACT_TYPES:
        return _reject(
            f"`type` must be one of {', '.join(ACT_TYPES)} — got {act!r}.\n"
            + _type_menu()
        )
    if not why:
        return _reject(
            "`reason` is mandatory: state the intent behind THIS command, "
            "not what the command does."
        )
    if len(why) < MIN_REASON_CHARS:
        return _reject(
            f"`reason` is too terse ({len(why)} chars, need >= {MIN_REASON_CHARS}). "
            "Explain the intent, not the syntax."
        )
    if act == "test-hypothesis" and not any(m in why.lower() for m in _HYPOTHESIS_MARKERS):
        return _reject(
            "type=test-hypothesis requires the `reason` to state the belief "
            "under test and what would refute it (e.g. \"I believe the total "
            "is wrong because quantity is ignored; if so the sum will differ "
            "from 42\"). If you are not testing a belief, use collect-info "
            "or verify instead."
        )

    result, meta = t_run(command)
    rc = meta.get("returncode")
    ok = rc == 0
    stdout = str(meta.get("stdout", ""))
    stderr = str(meta.get("stderr", ""))
    outcome = (stdout.strip() or stderr.strip() or "(no output)")
    outcome = " ".join(outcome.split())[:300]
    _record({
        "act": act,
        "mutating": ACT_TYPES[act][1],
        "command": command,
        "reason": why,
        "ok": ok,
        "returncode": rc,
        "outcome": f"rc={rc} — {outcome}",
    })
    return result, meta


def _journaled(fn, act: str, describe):
    """Wrap a file tool: `type` is derivable from the tool, `reason` is not,
    so the reason stays mandatory and the journal stays gap-free."""
    def wrapper(reason: str = "", **kwargs):
        why = (reason or "").strip()
        if len(why) < MIN_REASON_CHARS:
            return _reject(
                f"`reason` is mandatory and must be at least {MIN_REASON_CHARS} "
                f"characters (got {len(why)}). Say what this file access is for."
            )
        result, meta = fn(**kwargs)
        r = str(meta.get("result", result))
        _record({
            "act": act,
            "mutating": ACT_TYPES[act][1],
            "command": describe(kwargs),
            "reason": why,
            "ok": not r.startswith("ERROR"),
            "outcome": " ".join(r.split())[:200],
        })
        return result, meta
    wrapper.__name__ = f"t_journaled_{fn.__name__}"
    wrapper.__doc__ = fn.__doc__
    return wrapper


t_read_j = _journaled(t_read, "collect-info", lambda kw: f"read_file {kw.get('path')}")
t_write_j = _journaled(t_write, "change-state", lambda kw: f"write_file {kw.get('path')}")
t_update_j = _journaled(t_update, "change-state", lambda kw: f"str_replace_edit {kw.get('path')}")


# ── tools ────────────────────────────────────────────────────────────────────
_REASON_PROP = {
    "type": "string",
    "description": "Free text, at least "
                   f"{MIN_REASON_CHARS} characters: the intent behind this specific "
                   "call — what you are trying to learn, establish or achieve. "
                   "Do NOT paraphrase what the call does.",
}

_TOOLS = [
    Tool(
        "bash",
        "Execute a shell command. **Every call MUST carry `type` and `reason`** — "
        "the call is rejected without executing anything if they are missing, "
        "if `type` is outside the vocabulary, or if `reason` is under "
        f"{MIN_REASON_CHARS} characters. Valid `type` values:\n" + _type_menu(),
        t_bash,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "type": {
                    "type": "string",
                    "enum": list(ACT_TYPES),
                    "description": "What kind of act this command is. "
                                   + "; ".join(f"{k}: {v[0]}" for k, v in ACT_TYPES.items()),
                },
                "reason": _REASON_PROP,
            },
            "required": ["command", "type", "reason"],
        },
    ),
    Tool(
        "read_file",
        "Read the contents of a local file. `reason` is mandatory (the act type "
        "is always collect-info, so it is not asked for).",
        t_read_j,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "reason": _REASON_PROP,
            },
            "required": ["path", "reason"],
        },
    ),
    Tool(
        "write_file",
        "Write content to a local file, creating parent directories as needed. "
        "`reason` is mandatory (the act type is always change-state).",
        t_write_j,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "Content to write."},
                "reason": _REASON_PROP,
            },
            "required": ["path", "content", "reason"],
        },
    ),
    Tool(
        "str_replace_edit",
        "Edit an existing file by replacing a specific substring. Supply enough "
        "context in `old` to identify the target uniquely. `reason` is mandatory "
        "(the act type is always change-state).",
        t_update_j,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "old": {"type": "string", "description": "Exact text to replace."},
                "new": {"type": "string", "description": "Replacement text."},
                "reason": _REASON_PROP,
            },
            "required": ["path", "old", "new", "reason"],
        },
    ),
]

_TOOL_SCHEMA, _TOOL_DISPATCH = build_tool_spec(_TOOLS)
register_tools_in_library(_TOOLS)

_SYSTEM_SUPPLEMENT = (
    "You are a self-documenting coding agent. You work through `bash`, and "
    "every single `bash` call must declare what kind of act it is (`type`) and "
    "why you are making it (`reason`, free text, at least "
    f"{MIN_REASON_CHARS} characters). The file tools take a mandatory `reason` "
    "too; their type is fixed by the tool itself.\n\n"
    "The act vocabulary is closed:\n" + _type_menu() + "\n\n"
    "Rules that matter:\n"
    "- Pick the type by *your epistemic situation*, not by the command. "
    "`pytest` is test-hypothesis when you expect a particular failure, "
    "`verify` when you are confirming finished work.\n"
    "- `reason` explains intent, never syntax. \"list the files\" is a bad "
    "reason for `ls`; \"find out whether the oracle script is present before "
    "I trust check.py\" is a good one.\n"
    "- For test-hypothesis, name the belief and what observation would refute "
    "it.\n"
    "- Use `revert` when you undo something you did: dead ends are valuable "
    "documentation, do not hide them.\n"
    "- Rejected calls do not run. Fix the metadata and re-issue.\n\n"
    "Your reasons are rendered into RATIONALE.md as the session's narrative. "
    "Write them for a colleague reading the diff next week. Never edit "
    "RATIONALE.md or .epistemic/ yourself."
)


def _build_schema(model: str, endpoint: str) -> dict[str, object]:
    return {
        "model": model,
        "endpoint": endpoint,
        "status": "default",
        "inferred_tool_schema": _TOOL_SCHEMA,
        "behaviour": {"call_delivery_mode": "structured_tool_calls"},
        "tool_dispatch": _TOOL_DISPATCH,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Self-documenting coding agent: every shell call is typed "
                    "and justified, and the session narrates itself into "
                    "RATIONALE.md."
    )
    p.add_argument("model", nargs="?",
                   default="run:///home/martin/bin/opencode-free-deepseek-v4-flash-completions.py",
                   help="Model ID or run:// URI")
    p.add_argument("task", nargs="*", help="One-shot task (omit for REPL)")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--session", metavar="SESSION_ID")
    p.add_argument("--types", action="store_true", help="Print the act vocabulary and exit")
    args = p.parse_args()
    # `model` is optional-positional, so a bare task would otherwise swallow its
    # first word. If it does not look like a model id, treat it as task text.
    if args.model and not _looks_like_model(args.model):
        args.task = [args.model, *args.task]
        args.model = p.get_default("model")
    return args


def _looks_like_model(s: str) -> bool:
    return s.startswith("run://") or bool(re.fullmatch(r"[\w.:@+-]+(/[\w.:@+-]+)*", s))


def main() -> None:
    args = parse_args()
    if args.types:
        print(_type_menu())
        return
    schema = _build_schema(args.model, args.endpoint)
    opts = dict(session_id=args.session, system_prompt_supplement=_SYSTEM_SUPPLEMENT,
                strict_cache_proof=False)
    try:
        if args.task:
            run_task(schema, " ".join(args.task), **opts)
        else:
            run_repl(schema, **opts)
    finally:
        _render_report()
        if JOURNAL_FILE.exists():
            print(f"\nrationale: {os.path.abspath(REPORT_FILE)}")


if __name__ == "__main__":
    main()
