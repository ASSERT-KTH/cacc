#!/usr/bin/env python3
"""Grammar-only agent — every tool call is a grammar-constrained custom tool.

All four tools are declared as `{"type": "custom", "format": {"type":
"grammar", "syntax": "lark", ...}}` (OpenAI custom tools, cf.
https://gist.github.com/monperrus/2a9f682f3fca18de5693f0975bf5b396): the
endpoint runs constrained decoding against the Lark grammar, so the model
structurally cannot emit a malformed tool call. No JSON Schema tool is ever
sent — hence "grammar based tool calls only".

Tool surface (one grammar each):
  apply_patch — the upstream codex `apply_patch` grammar, verbatim
                (*** Begin Patch / Add / Update+Move to / Delete / End Patch).
  exec        — the codex code-mode grammar: optional `// @exec:` pragma
                line, then arbitrary shell source.
  read_file   — `PATH [?lines=N-M]`.
  write_file  — first line is the path, the rest is the content.

Endpoint: `~/bin/copilot-gpt-5.6-luna.py` (GitHub Copilot Responses API,
gpt-5.6-luna) as a `run://` subprocess schema, on top of agentknit.

Two agentknit gaps are bridged locally (tracked upstream as issues
monperrus/agentknit#21, #22, #23): custom+grammar tools can't be declared in
a spec, custom tool calls aren't surfaced by openai_compat, and run_repl
takes no injected client. `GrammarCompletions` does the wire translation
both ways; `_FUNCTION_MIRROR` is the spec-side stand-in whose entries never
reach the endpoint. When those issues land, `GrammarCompletions` and the
mirror shrink to nothing.

On dispatch, the raw input string is re-parsed locally with the same Lark
grammar (lark is only a *validator* here — the endpoint already guarantees
well-formedness) and then executed.

Usage:
    agent-lark.py "<task>"        # one-shot
    agent-lark.py                 # interactive REPL
    echo "<task>" | agent-lark.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import agentknit
from agentknit._core import run_repl, run_turn, validate_schema
from agentknit.openai_compat import SubprocessOpenAI, _parse_response
from agentknit.tool_library import t_read, t_run, t_update, t_write

COMPLETIONS_SCRIPT = os.path.expanduser("~/bin/copilot-gpt-5.6-luna.py")
MODEL = f"run://{COMPLETIONS_SCRIPT}"


# ── grammars ─────────────────────────────────────────────────────────────
#
# apply_patch.lark, upstream verbatim (openai/codex,
# codex-rs/core/src/tools/handlers/apply_patch.lark).
APPLY_PATCH_GRAMMAR = """\
start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF

%import common.LF
"""

# exec.lark, upstream verbatim (codex-rs/core/src/tools/code_mode).
EXEC_GRAMMAR = """\
start: pragma_source | plain_source
pragma_source: PRAGMA_LINE NEWLINE SOURCE
plain_source: SOURCE

PRAGMA_LINE: /[ \\t]*\\/\\/ @exec:[^\\r\\n]*/
NEWLINE: /\\r?\\n/
SOURCE: /[\\s\\S]+/
"""

# Purpose-built for this concept car.
READ_GRAMMAR = """\
start: PATH OPT?
PATH: /\\/?[^\\n?]+/
OPT: /\\?lines=\\d+-\\d+/
"""

WRITE_GRAMMAR = """\
start: PATH_LINE BODY
PATH_LINE: /[^\\n]+\\n/
BODY: /[\\s\\S]+/
"""

# Python's lark (Earley) rejects the zero-width `/(.*)/` terminals of the
# grammar above (gist gotcha; lark-parser PR #1639). This line-oriented
# variant accepts the same language and is used for local validation only.
APPLY_PATCH_GRAMMAR_LOCAL = """\
start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename add_line+
delete_hunk: "*** Delete File: " filename
update_hunk: "*** Update File: " filename change_move? change?

filename: FILENAME
FILENAME: /[^\\n]+\\n/

add_line: "+" BODYLINE

change_move: "*** Move to: " filename
change: (change_context | change_line)+ eof_line?
change_context: "@@" LF | "@@ " ANCHOR
change_line: ("+" | "-" | " ") BODYLINE
eof_line: "*** End of File" LF

ANCHOR: /[^\\n]+\\n/
BODYLINE: /[^\\n]*\\n/

%import common.LF
"""


# ── tool implementations (raw-text in, plain text out) ────────────────────

def _fail(msg: str) -> tuple[str, dict]:
    return msg, {"result": msg}


_HUNK_AT = re.compile(r"^@@")


def t_apply_patch(input: str) -> tuple[str, dict]:
    """Grammar-constrained apply_patch executor (custom tool)."""
    if err := _guarded("apply_patch", input):
        return _fail(err)

    body = input.split("*** Begin Patch\n", 1)[1].split("*** End Patch", 1)[0]
    report: list[str] = []

    for block in re.split(r"(?=^\*\*\* (?:Add|Update|Delete) File: )", body,
                          flags=re.MULTILINE):
        if not block.startswith("*** "):
            continue
        first, _, rest = block.partition("\n")
        if first.startswith("*** Add File: "):
            path = first.removeprefix("*** Add File: ").strip()
            content = "".join(ln[1:] for ln in rest.splitlines(keepends=True))
            p = Path(os.path.expanduser(path))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            report.append(f"add {path} (+{content.count(chr(10)) + 1} lines)")
        elif first.startswith("*** Delete File: "):
            path = first.removeprefix("*** Delete File: ").strip()
            Path(os.path.expanduser(path)).unlink(missing_ok=True)
            report.append(f"delete {path}")
        elif first.startswith("*** Update File: "):
            src = first.removeprefix("*** Update File: ").strip()
            dest = src
            if rest.startswith("*** Move to: "):
                move, _, rest = rest.partition("\n")
                dest = move.removeprefix("*** Move to: ").strip()
            old_lines, new_lines, seen_at = [], [], False
            for ln in rest.splitlines():
                if ln.startswith("***"):
                    break
                if _HUNK_AT.match(ln):
                    seen_at = True
                elif seen_at and ln[:1] in ("+", "-", " "):
                    (new_lines if ln[0] == "+" else old_lines).append(ln[1:])
                    if ln[0] == " ":
                        new_lines.append(ln[1:])
            # A bare rename (no @@ hunk) is a valid update per the grammar.
            if dest != src:
                Path(os.path.expanduser(dest)).parent.mkdir(parents=True, exist_ok=True)
                os.replace(os.path.expanduser(src), os.path.expanduser(dest))
                report.append(f"move {src} -> {dest}")
                src = dest
            if not seen_at:
                continue
            if not old_lines and not new_lines:
                return _fail(f"ERROR: update hunk for {src} is empty")
            out, _ = t_update(path=src, old="\n".join(old_lines),
                              new="\n".join(new_lines))
            report.append(f"update {src}: {out}")

    msg = "OK: " + "; ".join(report) if report else "OK: nothing to do"
    return msg, {"result": msg}


def t_exec(input: str) -> tuple[str, dict]:
    """Grammar-constrained shell executor (custom tool)."""
    if err := _guarded("exec", input):
        return _fail(err)
    command = input
    if input.startswith("//"):  # // @exec: pragma line
        _pragma, nl, command = input.partition("\n")
        if not nl:
            return _fail("ERROR: exec pragma line not followed by a command")
    return t_run(command)


def t_read_raw(input: str) -> tuple[str, dict]:
    """Grammar-constrained file reader (custom tool)."""
    if err := _guarded("read_file", input):
        return _fail(err)
    spec = input.strip()
    if "?lines=" in spec:
        path, _, rng = spec.partition("?lines=")
        first, _, last = rng.partition("-")
        return t_read(path.strip(), offset=int(first), limit=int(last) - int(first) + 1)
    return t_read(spec)


def t_write_raw(input: str) -> tuple[str, dict]:
    """Grammar-constrained file writer (custom tool)."""
    if err := _guarded("write_file", input):
        return _fail(err)
    path, nl, content = input.partition("\n")
    if not nl:
        return _fail("ERROR: write_file input needs a path line followed by content")
    return t_write(path.strip(), content)


# Tool table: name → (wire grammar, local validator grammar, description, fn).
TOOLS: dict[str, tuple[str, str | None, str, object]] = {
    "apply_patch": (
        APPLY_PATCH_GRAMMAR,
        APPLY_PATCH_GRAMMAR_LOCAL,
        "Apply a patch that adds, updates, moves or deletes files. Format: "
        "'*** Begin Patch', one or more hunks (*** Add File: / *** Update "
        "File: [/ *** Move to:] / *** Delete File:), '*** End Patch'. Add "
        "lines start with +, remove lines with -, context lines with a "
        "single space; each hunk starts with @@ optionally followed by a "
        "context anchor.",
        t_apply_patch,
    ),
    "exec": (
        EXEC_GRAMMAR,
        EXEC_GRAMMAR,
        "Run a bash command. Optionally prefix a first line "
        "'// @exec: cwd=/path timeout=120' pragma; the rest is the command.",
        t_exec,
    ),
    "read_file": (
        READ_GRAMMAR,
        READ_GRAMMAR,
        "Read a file at PATH. Optionally append '?lines=N-M' to read a "
        "1-indexed inclusive line range.",
        t_read_raw,
    ),
    "write_file": (
        WRITE_GRAMMAR,
        WRITE_GRAMMAR,
        "Write (or overwrite) a file. First line: the path. "
        "Remaining lines: the full file content.",
        t_write_raw,
    ),
}

CUSTOM_TOOLS = [
    {
        "type": "custom",
        "custom": {
            "name": name,
            "description": description,
            "format": {"type": "grammar", "syntax": "lark", "definition": grammar},
        },
    }
    for name, (grammar, _local, description, _fn) in TOOLS.items()
]

# Local validator per tool: the wire grammar, except apply_patch whose
# upstream grammar Python lark cannot compile (zero-width regexps).
_parsers: dict[str, object] = {}
try:  # lark is optional: the endpoint guarantees well-formedness either way.
    from lark import Lark

    for _name, (_wire, _local, _desc, _fn) in TOOLS.items():
        _parsers[_name] = Lark(_local, parser="earley")
except ImportError:  # pragma: no cover
    _parsers = {}


def _grammar_ok(name: str, text: str) -> str | None:
    """Return None when *text* parses against tool *name*'s local grammar.

    Returns a short error string otherwise. With lark absent, validation is
    skipped: constrained decoding on the endpoint already made malformed
    input impossible.
    """
    parser = _parsers.get(name)
    if parser is None:
        return None
    try:
        parser.parse(text)
    except Exception as exc:  # lark raises several exception types
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    return None


def _guarded(name: str, input: str) -> str | None:
    """Shared precondition: return the error message if input is malformed."""
    if offending := _grammar_ok(name, input):
        return f"ERROR: grammar violation in {name} input ({offending})"
    return None


# ── spec ──────────────────────────────────────────────────────────────────
#
# Spec-side stand-in for the grammar tools: agentknit's spec/dispatch layer
# is JSON-Schema oriented (issue #21), so each tool is declared as a
# single-string-parameter function here. GrammarCompletions swaps the real
# custom+grammar declarations in on the wire, so the model never sees this.
_FUNCTION_MIRROR = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"input": {"type": "string",
                                         "description": "Raw tool input."}},
                "required": ["input"],
            },
        },
    }
    for name, (_wire, _local, description, _fn) in TOOLS.items()
]

SCHEMA: dict = {
    "model": MODEL,
    "endpoint": MODEL,
    "status": "default",
    "display_name": "agent-lark (grammar tools + gpt-5.6-luna)",
    "behaviour": {"call_delivery_mode": "structured_tool_calls"},
    "inferred_tool_schema": _FUNCTION_MIRROR,
    "tool_dispatch": {
        name: {"python_function": fn, "param_map": {}}
        for name, (_wire, _local, _desc, fn) in TOOLS.items()
    },
    # GPT-5.6-class models only cache prefixes ≥ 1024 tokens.
    "min_cacheable_tokens": 1024,
    # run:// binaries don't stream.
    "provider_api_support": {"streaming": {"supported": False}},
}

SYSTEM_SUPPLEMENT = (
    "Your tools are grammar-constrained: each tool input must match the Lark "
    "grammar the endpoint enforces. apply_patch takes the "
    "'*** Begin Patch'/'*** End Patch' format; exec takes raw bash (optionally "
    "a '// @exec:' pragma first line); read_file takes 'PATH' or "
    "'PATH?lines=N-M'; write_file takes a path line followed by content lines. "
    "Prefer apply_patch over write_file for edits to existing files."
)


# ── wire bridge (issues #21/#22) ──────────────────────────────────────────

_RETRY_AFTER = re.compile(r"^RETRY_AFTER:\s*(\S*)", re.MULTILINE)


class GrammarCompletions:
    """chat.completions shim that speaks custom grammar tools end to end.

    Outgoing: every tool whose name matches a grammar tool is replaced by its
    custom+grammar declaration, so only grammar-constrained tools reach the
    endpoint. Incoming: ``custom_tool_call`` items are rewritten into
    function calls whose arguments are ``{"input": "<raw text>"}``, which
    agentknit's loop dispatches like any other tool (the raw string lands in
    the ``input`` kwarg of the implementers above).
    """

    def __init__(self, binary: str) -> None:
        self._binary = binary

    def create(self, *, messages, temperature=0, model=None, tools=None,
               tool_choice=None, user=None, extra_body=None, max_tokens=None,
               **_ignored) -> object:
        payload: dict = {"messages": messages, "temperature": temperature}
        if model and not model.startswith("run://"):
            payload["model"] = model
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools is not None:
            payload["tools"] = self._grammar_tools(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if user is not None:
            payload["user"] = user
        if extra_body:
            payload.update(extra_body)

        for attempt in range(3):
            proc = subprocess.run(
                [self._binary], input=json.dumps(payload),
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode == 0:
                return _parse_response(self._translate_calls(json.loads(proc.stdout)))
            if match := _RETRY_AFTER.search(proc.stderr):
                delay = float(match.group(1) or 5)
                print(f"  [rate-limited] waiting {delay:.0f}s …", flush=True)
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Binary {self._binary!r} exited {proc.returncode}: {proc.stderr}"
            )
        raise RuntimeError(f"{self._binary!r} still rate-limited after 3 attempts")

    @staticmethod
    def _grammar_tools(tools: list) -> list:
        by_name = {t["custom"]["name"]: t for t in CUSTOM_TOOLS}
        return [
            by_name.get((t.get("function") or {}).get("name"), t)
            if t.get("type") == "function" else t
            for t in tools
        ]

    @staticmethod
    def _translate_calls(data: dict) -> dict:
        for choice in data.get("choices", []):
            for tc in choice.get("message", {}).get("tool_calls") or []:
                if tc.get("type") == "custom":
                    custom = tc.get("custom") or {}
                    call_id = tc.get("id") or custom.get("id") or custom.get("call_id")
                    tc.clear()
                    tc["id"] = call_id or f"call_{time.monotonic_ns():x}"
                    tc["type"] = "function"
                    tc["function"] = {
                        "name": custom.get("name", ""),
                        "arguments": json.dumps({"input": custom.get("input", "")}),
                    }
        return data


class GrammarOpenAI(SubprocessOpenAI):
    """SubprocessOpenAI with grammar tools on the wire (custom calls back)."""

    def __init__(self, binary_path: str) -> None:
        super().__init__(binary_path)
        self.chat.completions = GrammarCompletions(binary_path)


# ── entry point ───────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Grammar-only tool-calling agent (agentknit + gpt-5.6-luna).",
    )
    p.add_argument("task", nargs="*", help="Task to run (omit for REPL / pipe stdin)")
    p.add_argument("--session", metavar="SESSION_ID", help="Resume a session by ID")
    p.add_argument("--non-interactive", action="store_true",
                   dest="non_interactive", help="Disable interactive tools")
    return p.parse_args()


def _run_once(task: str, args: argparse.Namespace) -> None:
    client = GrammarOpenAI(COMPLETIONS_SCRIPT)
    session = agentknit.init_session(
        SCHEMA,
        non_interactive=args.non_interactive,
        resumed_from=args.session,
        system_prompt_supplement=SYSTEM_SUPPLEMENT,
    )
    try:
        result = run_turn(client, MODEL, session, task)
    finally:
        agentknit._save_messages_snapshot(session)
    if result.final_reply:
        print(result.final_reply)


def main() -> None:
    args = parse_args()
    validate_schema(SCHEMA)

    if args.task:
        _run_once(" ".join(args.task), args)
        return

    if not sys.stdin.isatty():
        task = sys.stdin.read().strip()
        if task:
            _run_once(task, args)
        return

    # REPL: run_repl builds its own client via create_client() and takes no
    # override (issue #23), so swap the factory for the duration of the call.
    import agentknit._core as core

    _orig = core.create_client
    core.create_client = lambda _schema: GrammarOpenAI(COMPLETIONS_SCRIPT)
    try:
        run_repl(
            SCHEMA,
            non_interactive=args.non_interactive,
            session_id=args.session,
            system_prompt_supplement=SYSTEM_SUPPLEMENT,
        )
    finally:
        core.create_client = _orig


if __name__ == "__main__":
    main()
