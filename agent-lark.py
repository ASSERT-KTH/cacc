#!/usr/bin/env python3
"""Grammar-only agent — every tool call is a grammar-constrained custom tool.

All four tools are declared as `{"type": "custom", "format": {"type":
"grammar", "syntax": "lark", ...}}` (OpenAI custom tools, cf.
https://gist.github.com/monperrus/2a9f682f3fca18de5693f0975bf5b396): the
endpoint runs constrained decoding against the Lark grammar, so the model
structurally cannot emit a malformed tool call. No JSON Schema tool is
declared, hence "grammar based tool calls only".

Tool surface (one grammar each):
  apply_patch — the upstream codex `apply_patch` grammar, verbatim
                (*** Begin Patch / Add / Update+Move to / Delete / End Patch).
  exec        — the codex code-mode grammar: optional `// @exec:` pragma
                line, then arbitrary shell source.
  read_file   — `PATH [?lines=N-M]`.
  write_file  — first line is the path, the rest is the content.

Local validation: agentknit's openai_compat only surfaces `function`-shaped
tool calls, so GrammarOpenAI — a drop-in client that forwards the grammar
tools verbatim and re-emits custom tool calls as function calls carrying
`{"input": ...}` — bridges the two. On dispatch, the raw input string is
first parsed with the same Lark grammar (lark is only a *validator* here —
the endpoint already guarantees well-formedness), then executed.

Endpoint: `~/bin/copilot-gpt-5.6-luna.py` (GitHub Copilot Responses API,
gpt-5.6-luna) via a `run://` subprocess schema, i.e. agentknit on top of
agentknit's subprocess client.

Usage:
    agent-lark.py "<task>"        # one-shot
    agent-lark.py                 # interactive REPL
    echo "<task>" | agent-lark.py
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import agentknit
from agentknit._core import run_repl, run_turn, validate_schema
from agentknit.openai_compat import SubprocessOpenAI
from agentknit.tool_library import t_read, t_run, t_update, t_write

COMPLETIONS_SCRIPT = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "copilot-gpt-5.6-luna.py"
)
# The concept car lives next to the other concept cars; the endpoint binary
# is not part of this repo, so fall back to ~/bin.
if not Path(COMPLETIONS_SCRIPT).exists():
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

# read_file / write_file: purpose-built for this concept car.
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

# Python's lark (Earley) rejects zero-width regexps, so the verbatim
# apply_patch grammar above — which uses `/(.*)/` — cannot be compiled
# locally (documented in the gist; lark-parser PR #1639). The variant below
# is line-oriented instead (`BODYLINE: /[^\\n]*\\n/`) and accepts exactly the
# same language; it is used only for local double-checking of what the
# endpoint's constrained decoding already guarantees.
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


# ── tool declarations (custom + grammar, no JSON Schema anywhere) ─────────

CUSTOM_TOOLS: list[dict] = [
    {
        "type": "custom",
        "custom": {
            "name": "apply_patch",
            "description": (
                "Apply a patch that adds, updates, moves or deletes files. "
                "Format: '*** Begin Patch', one or more hunks "
                "(*** Add File: / *** Update File: [/ *** Move to:] / "
                "*** Delete File:), '*** End Patch'. Add lines start with +, "
                "remove lines with -, context lines with a single space; each "
                "hunk starts with @@ optionally followed by a context anchor."
            ),
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": APPLY_PATCH_GRAMMAR,
            },
        },
    },
    {
        "type": "custom",
        "custom": {
            "name": "exec",
            "description": (
                "Run a bash command. Optionally prefix a first line "
                "'// @exec: cwd=/path timeout=120' pragma; the rest is the "
                "command itself."
            ),
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": EXEC_GRAMMAR,
            },
        },
    },
    {
        "type": "custom",
        "custom": {
            "name": "read_file",
            "description": (
                "Read a file at PATH. Optionally append '?lines=N-M' to read "
                "a 1-indexed inclusive line range."
            ),
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": READ_GRAMMAR,
            },
        },
    },
    {
        "type": "custom",
        "custom": {
            "name": "write_file",
            "description": (
                "Write (or overwrite) a file. First line: the path. "
                "Remaining lines: the full file content."
            ),
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": WRITE_GRAMMAR,
            },
        },
    },
]

# Server-side grammar → local Lark validator (same language, local dialect).
LOCAL_GRAMMARS = {
    "apply_patch": APPLY_PATCH_GRAMMAR_LOCAL,
    "exec": EXEC_GRAMMAR,
    "read_file": READ_GRAMMAR,
    "write_file": WRITE_GRAMMAR,
}

_parsers: dict[str, object] = {}
try:  # lark is optional: the endpoint guarantees well-formedness either way.
    from lark import Lark

    for _name, _g in LOCAL_GRAMMARS.items():
        _parsers[_name] = Lark(_g, parser="earley")
except ImportError:  # pragma: no cover
    _parsers = {}


def grammar_ok(name: str, text: str) -> str | None:
    """Return None when *text* parses against tool *name*'s grammar.

    Returns a short error string otherwise. With lark absent, validation is
    skipped (returns None unconditionally): constrained decoding on the
    endpoint already made malformed input impossible.
    """
    parser = _parsers.get(name)
    if parser is None:
        return None
    try:
        parser.parse(text)  # type: ignore[attr-defined]
    except Exception as exc:  # lark raises several exception types
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    return None


# ── tool implementations (raw-text in, plain text out) ────────────────────

_UPDATE_HEADER = re.compile(r"^\*\*\* Update File:\s*(.+?)\s*$", re.MULTILINE)
_HUNK_AT = re.compile(r"^@@")


def _fail(msg: str) -> tuple[str, dict]:
    return msg, {"result": msg}


def t_apply_patch(input: str) -> tuple[str, dict]:
    """Grammar-constrained apply_patch executor (custom tool)."""
    offending = grammar_ok("apply_patch", input)
    if offending:
        return _fail(f"ERROR: grammar violation in apply_patch input ({offending})")

    # Split off the envelope; hunks keep their trailing newline structure.
    body = input.split("*** Begin Patch\n", 1)[1]
    body = body.split("*** End Patch", 1)[0]
    blocks = re.split(r"(?=^\*\*\* )", body, flags=re.MULTILINE)
    report: list[str] = []

    for block in blocks:
        if not block.startswith("*** "):
            continue
        first, _, rest = block.partition("\n")
        if first.startswith("*** Add File: "):
            path = first[len("*** Add File: "):].strip()
            content = "".join(ln[1:] for ln in rest.splitlines(keepends=True))
            p = Path(os.path.expanduser(path))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            report.append(f"add {path} (+{content.count(chr(10)) + 1} lines)")
        elif first.startswith("*** Delete File: "):
            path = first[len("*** Delete File: "):].strip()
            p = Path(os.path.expanduser(path))
            p.unlink(missing_ok=True)
            report.append(f"delete {path}")
        elif first.startswith("*** Update File: "):
            src = first[len("*** Update File: "):].strip()
            dest = src
            if rest.startswith("*** Move to: "):
                move, _, rest = rest.partition("\n")
                dest = move[len("*** Move to: "):].strip()
            old_lines, new_lines = [], []
            seen_at = False
            for ln in rest.splitlines():
                if ln.startswith("***"):
                    break
                if _HUNK_AT.match(ln):
                    seen_at = True
                    continue
                if not seen_at:
                    continue
                if ln.startswith("+"):
                    new_lines.append(ln[1:])
                elif ln.startswith("-"):
                    old_lines.append(ln[1:])
                elif ln.startswith(" "):
                    old_lines.append(ln[1:])
                    new_lines.append(ln[1:])
            if not seen_at:
                return _fail(f"ERROR: update hunk for {src} has no @@ marker")
            if dest != src:
                Path(os.path.expanduser(dest)).parent.mkdir(parents=True, exist_ok=True)
                os.replace(os.path.expanduser(src), os.path.expanduser(dest))
                report.append(f"move {src} -> {dest}")
                src = dest
            if not old_lines and not new_lines:
                return _fail(f"ERROR: update hunk for {src} is empty")
            old, new = "\n".join(old_lines), "\n".join(new_lines)
            out, _meta = t_update(path=src, old=old, new=new)
            report.append(f"update {src}: {out}")

    msg = "OK: " + "; ".join(report) if report else "OK: nothing to do"
    return msg, {"result": msg}


def t_exec(input: str) -> tuple[str, dict]:
    """Grammar-constrained shell executor (custom tool)."""
    offending = grammar_ok("exec", input)
    if offending:
        return _fail(f"ERROR: grammar violation in exec input ({offending})")
    command = input
    if input.startswith("//"):
        _pragma, nl, command = input.partition("\n")
        if not nl:
            return _fail("ERROR: exec pragma line not followed by a command")
    return t_run(command)


def t_read_raw(input: str) -> tuple[str, dict]:
    """Grammar-constrained file reader (custom tool)."""
    offending = grammar_ok("read_file", input)
    if offending:
        return _fail(f"ERROR: grammar violation in read_file input ({offending})")
    spec = input.strip()
    if "?lines=" in spec:
        path, _, rng = spec.partition("?lines=")
        first, _, last = rng.partition("-")
        return t_read(path.strip(), offset=int(first), limit=int(last) - int(first) + 1)
    return t_read(spec)


def t_write_raw(input: str) -> tuple[str, dict]:
    """Grammar-constrained file writer (custom tool)."""
    offending = grammar_ok("write_file", input)
    if offending:
        return _fail(f"ERROR: grammar violation in write_file input ({offending})")
    path, nl, content = input.partition("\n")
    if not nl:
        return _fail("ERROR: write_file input needs a path line followed by content")
    return t_write(path.strip(), content)


# ── client bridge: forward grammar tools, translate custom tool calls ─────

class GrammarOpenAI(SubprocessOpenAI):
    """SubprocessOpenAI that speaks custom grammar tools end to end.

    agentknit sends OpenAI *function* tools and expects function tool calls
    back. The grammar endpoint wants custom tools and answers with custom
    tool calls. This bridge:

    * rewrites the outgoing ``tools`` list — every function entry whose name
      matches a grammar tool is replaced by its custom+grammar declaration,
      so only grammar-constrained tools ever reach the endpoint;
    * translates incoming custom tool calls into function tool calls whose
      ``arguments`` are ``{"input": "<raw text>"}``, which agentknit's loop
      then dispatches like any other tool (the raw string lands in the
      ``input`` kwarg of the implementers above).

    The endpoint binary itself already translates chat/completions ⇄
    Responses, including the nested ``grammar`` format and
    ``custom_tool_call`` items, so this class only handles the agentknit
    side of the contract.
    """

    def _rewrite_tools(self, tools: list | None) -> list | None:
        if tools is None:
            return None
        by_name = {t["custom"]["name"]: t for t in CUSTOM_TOOLS}
        out = []
        for tool in tools:
            if tool.get("type") == "function":
                name = (tool.get("function") or {}).get("name")
                if name in by_name:
                    out.append(by_name[name])
                    continue
            out.append(tool)
        return out

    def create(self, *, messages, tools=None, tool_choice=None, extra_body=None,
               model=None, temperature=0, max_tokens=None, **kwargs):
        payload = dict(kwargs)
        payload["messages"] = messages
        payload["temperature"] = temperature
        if model is not None:
            payload["model"] = model
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        tools = self._rewrite_tools(tools)
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if extra_body:
            payload.update(extra_body)
        import json as _json

        proc = subprocess.run(
            [self._binary_path],
            input=_json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.stderr:
            print(f"  [subprocess stderr] {proc.stderr}", flush=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Binary {self._binary_path!r} exited {proc.returncode}: {proc.stderr}"
            )
        data = _json.loads(proc.stdout)

        # custom_tool_call → function tool call with {"input": ...}
        for choice in data.get("choices", []):
            message = choice.get("message", {})
            for tc in message.get("tool_calls") or []:
                if tc.get("type") == "custom":
                    custom = tc.get("custom") or {}
                    call_id = custom.get("id") or custom.get("call_id") or ""
                    tc.clear()
                    tc["id"] = call_id
                    tc["type"] = "function"
                    tc["function"] = {
                        "name": custom.get("name", ""),
                        "arguments": _json.dumps({"input": custom.get("input", "")}),
                    }
        return data


# ── spec ──────────────────────────────────────────────────────────────────

# Mirror of CUSTOM_TOOLS in function shape: agentknit's spec/dispatch layer
# is JSON-Schema oriented, so the grammar tools are declared as
# single-string-parameter functions here and GrammarOpenAI swaps in the real
# grammar declarations on the wire. The model never sees a JSON Schema.
_FUNCTION_MIRROR = [
    {
        "type": "function",
        "function": {
            "name": tool["custom"]["name"],
            "description": tool["custom"]["description"],
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Raw tool input, grammar-constrained.",
                    }
                },
                "required": ["input"],
            },
        },
    }
    for tool in CUSTOM_TOOLS
]

_IMPLEMENTERS = {
    "apply_patch": t_apply_patch,
    "exec": t_exec,
    "read_file": t_read_raw,
    "write_file": t_write_raw,
}

TOOL_DISPATCH = {
    name: {"python_function": fn, "param_map": {}}
    for name, fn in _IMPLEMENTERS.items()
}

SCHEMA: dict = {
    "model": MODEL,
    "endpoint": MODEL,
    "status": "default",
    "display_name": "agent-lark (grammar tools + gpt-5.6-luna)",
    "behaviour": {"call_delivery_mode": "structured_tool_calls"},
    "inferred_tool_schema": _FUNCTION_MIRROR,
    "tool_dispatch": TOOL_DISPATCH,
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


def _client() -> GrammarOpenAI:
    return GrammarOpenAI(COMPLETIONS_SCRIPT)


def main() -> None:
    args = parse_args()
    validate_schema(SCHEMA)

    if args.task:
        client = _client()
        session = agentknit.init_session(
            SCHEMA,
            non_interactive=args.non_interactive,
            resumed_from=args.session,
            system_prompt_supplement=SYSTEM_SUPPLEMENT,
        )
        try:
            result = run_turn(client, MODEL, session, " ".join(args.task))
        finally:
            agentknit._save_messages_snapshot(session)
        if result.final_reply:
            print(result.final_reply)
        return

    if not sys.stdin.isatty():
        task = sys.stdin.read().strip()
        if task:
            client = _client()
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
        return

    # REPL: run_repl creates its own client via create_client(), which
    # returns a plain SubprocessOpenAI — patch the session's tools through
    # the same bridge by injecting ours. agentknit exposes no client
    # override for run_repl, so we monkey-patch create_client for the call.
    import agentknit._core as core

    _orig = core.create_client

    def _patched(schema: dict):
        return _client()

    core.create_client = _patched
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
