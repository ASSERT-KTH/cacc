#!/usr/bin/env python3
"""One-tool coding agent — a single exec_shell tool, nothing else.

No read_file/write_file/edit_file tools exist. All file inspection and
modification goes through shell commands (echo, cat, sed, tee, ...) run via
exec_shell — the model is told so in the system prompt.

Usage:
    onetool_agent.py [task ...]   # one-shot task
    onetool_agent.py               # interactive REPL
    echo "task" | onetool_agent.py # piped task
"""
from __future__ import annotations

import argparse
import sys

import agentknit
from agentknit import Tool, build_tool_spec, register_tools_in_library
from agentknit.tool_library import t_run
from agentknit._core import DEFAULT_ENDPOINT

_TOOLS = [
    Tool(
        "exec_shell",
        "Execute a shell command and return its stdout, stderr, and exit code.",
        t_run,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
            },
            "required": ["command"],
        },
    ),
]

register_tools_in_library(_TOOLS)
_TOOL_SCHEMA, _TOOL_DISPATCH = build_tool_spec(_TOOLS)

_SYSTEM_SUPPLEMENT = (
    "You are a coding agent with exactly one tool: exec_shell. There is no "
    "read_file, write_file, or edit_file tool. Inspect and modify files "
    "exclusively with standard unix tools invoked through exec_shell: "
    "cat to read a file, sed to edit one in place "
    "(e.g. `sed -i 's/old/new/' file`), and cat with a heredoc for "
    "writing content (e.g. `cat > file <<'EOF' ... EOF`)."
)


def _build_schema(model: str, endpoint: str) -> dict:
    return {
        "model": model,
        "endpoint": endpoint,
        "status": "default",
        "inferred_tool_schema": _TOOL_SCHEMA,
        "behaviour": {"call_delivery_mode": "structured_tool_calls"},
        "tool_dispatch": _TOOL_DISPATCH,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="One-tool coding agent (exec_shell only).")
    p.add_argument(
        "model", nargs="?",
        default="run:///home/martin/bin/best-effort-completions.py",
        help="Model ID or run:// URI",
    )
    p.add_argument("task", nargs="*", help="One-shot task (omit for REPL)")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--session", metavar="SESSION_ID")
    p.add_argument("--non-interactive", action="store_true", dest="non_interactive")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    schema = _build_schema(args.model, args.endpoint)
    opts = dict(
        session_id=args.session,
        system_prompt_supplement=_SYSTEM_SUPPLEMENT,
        non_interactive=args.non_interactive,
        strict_cache_proof=False,
    )

    if args.task:
        result = agentknit.run_task(schema, " ".join(args.task), **opts)
        if result.final_reply:
            print(result.final_reply)
        return

    if not sys.stdin.isatty():
        task = sys.stdin.read().strip()
        if task:
            result = agentknit.run_task(schema, task, **opts)
            if result.final_reply:
                print(result.final_reply)
        return

    agentknit.run_repl(schema, **opts)


if __name__ == "__main__":
    main()
