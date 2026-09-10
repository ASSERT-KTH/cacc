#!/usr/bin/env python3
"""Tinyest coding agent — no system prompt, one tool: shell(cmd), no description.

Like onetool_agent but stripped to the absolute minimum:
- no system prompt supplement at all
- a single tool named ``shell`` with an empty description and a single
  parameter ``cmd`` with no description either

Usage:
    tinyest_agent.py [task ...]   # one-shot task
    tinyest_agent.py               # interactive REPL
    echo "task" | tinyest_agent.py # piped task
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
        "shell",
        "",
        t_run,
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": ""}},
            "required": ["cmd"],
        },
        param_map={"cmd": "command"},
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
    p = argparse.ArgumentParser(description="Tinyest coding agent (shell(cmd), no system prompt).")
    p.add_argument(
        "args", nargs="*",
        help="Optional model (ID or run:// URI) followed by the task",
    )
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--session", metavar="SESSION_ID")
    p.add_argument("--non-interactive", action="store_true", dest="non_interactive")
    ns = p.parse_args()
    args = list(ns.args)
    # Only consume the first positional as the model when it actually looks
    # like one (an explicit run:// URI, or an org/model ID with no spaces) —
    # otherwise it's task text.
    first = args[0] if args else ""
    is_model = first.startswith("run:") or (" " not in first and "/" in first)
    if is_model:
        ns.model = args.pop(0)
    else:
        ns.model = "run:///home/martin/bin/best-effort-completions.py"
    ns.task = args
    return ns


def main() -> None:
    args = parse_args()
    schema = _build_schema(args.model, args.endpoint)
    opts = dict(
        session_id=args.session,
        non_interactive=args.non_interactive,
        strict_cache_proof=False,
    )

    if args.task:
        task = " ".join(args.task)
        # Interactive one-shot: run the task in the session UI (tool calls
        # are streamed to the console) instead of the silent run_task path.
        client, session, model, hist_file = agentknit._core._repl_setup(
            schema,
            non_interactive=args.non_interactive,
            session_id=args.session,
            strict_cache_proof=False,
        )
        try:
            result = agentknit._core.run_turn(client, model, session, task)
        finally:
            agentknit._core._repl_teardown(
                session, hist_file,
                agentknit._core._build_resume_cmd(model, session["session_id"], sys.argv[0]),
            )
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
