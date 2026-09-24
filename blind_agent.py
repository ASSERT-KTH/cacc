#!/usr/bin/env python3
"""Blind coding agent — it can read and write code, but never run it.

The SequenceR setting, as a coding agent: the model sees the buggy source and
the tests, and must produce the fix from reading alone. There is no shell, no
python, no test runner: three pure-Python tools, list_dir, read_file and
write_file. Whether the fix is right is only known after the agent stops.

Usage:
    blind_agent.py [model] [task ...]   # one-shot task
    blind_agent.py                      # interactive REPL
"""
from __future__ import annotations

import argparse
import os
import sys

import agentknit
from agentknit import Tool, build_tool_spec, register_tools_in_library
from agentknit.tool_library import t_read, t_write
from agentknit._core import DEFAULT_ENDPOINT

_SYSTEM_SUPPLEMENT = (
    "You are blind to execution: you cannot run any code, tests, compiler or "
    "shell command. You can only list directories, read files and write files. "
    "Reason about the code to find and fix bugs."
)


def t_list_dir(path: str = ".") -> tuple[str, dict[str, object]]:
    """List the entries of a directory, one per line, directories with a trailing /."""
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as e:
        return f"ERROR: {e}", {"result": "error"}
    lines = [e.name + ("/" if e.is_dir() else "") for e in entries if not e.name.startswith(".")]
    return "\n".join(lines), {"result": "ok"}


_TOOLS = [
    Tool(
        "list_dir",
        "List the entries of a directory.",
        t_list_dir,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path."}},
            "required": [],
        },
    ),
    Tool(
        "read_file",
        "Read and return the contents of a file.",
        t_read,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the file."}},
            "required": ["path"],
        },
    ),
    Tool(
        "write_file",
        "Write (overwrite) a file with the given content.",
        t_write,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "Full new content."},
            },
            "required": ["path", "content"],
        },
    ),
]

register_tools_in_library(_TOOLS)
_TOOL_SCHEMA, _TOOL_DISPATCH = build_tool_spec(_TOOLS)


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
    p = argparse.ArgumentParser(description="Blind coding agent (read/write, no execution).")
    p.add_argument("model", nargs="?",
                   default="run:///home/martin/bin/best-effort-completions.py",
                   help="Model ID or run:// URI")
    p.add_argument("task", nargs="*", help="One-shot task (omit for REPL)")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--session", metavar="SESSION_ID")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    schema = _build_schema(args.model, args.endpoint)
    opts = dict(session_id=args.session, system_prompt_supplement=_SYSTEM_SUPPLEMENT,
                strict_cache_proof=False)
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
