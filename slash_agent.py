#!/usr/bin/env python3
"""
Agent that exposes slash commands as LLM-callable tools.

The LLM can invoke slash commands (clear, model, usage, help) directly as
structured tool calls, in addition to standard coding tools.
"""
from __future__ import annotations

import argparse

from agentknit import Tool, build_tool_spec, register_tools_in_library
from agentknit import SLASH_COMMAND_TOOL, slash_tool_ctx
from agentknit.tool_library import t_read, t_write, t_run, t_update
from agentknit._core import DEFAULT_ENDPOINT

# ── tool registry ──────────────────────────────────────────────────────────────

_TOOLS = [
    SLASH_COMMAND_TOOL,
    # standard coding tools
    Tool(
        "read_file",
        "Read and return the contents of a file.",
        t_read,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
            },
            "required": ["path"],
        },
    ),
    Tool(
        "write_file",
        "Write content to a file, creating parent directories as needed.",
        t_write,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "Content to write."},
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        "run_shell",
        "Run a shell command and return its stdout, stderr, and exit code.",
        t_run,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
            },
            "required": ["command"],
        },
    ),
    Tool(
        "edit_file",
        "Replace an exact substring in a file with new text.",
        t_update,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "old": {"type": "string", "description": "Exact text to replace."},
                "new": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old", "new"],
        },
    ),
]

_TOOL_SCHEMA, _TOOL_DISPATCH = build_tool_spec(_TOOLS)
register_tools_in_library(_TOOLS)

_SYSTEM_SUPPLEMENT = (
    "You are a coding agent with access to slash command tools "
    "(slash_clear, slash_model, slash_usage, slash_help) that control your own "
    "session, in addition to standard file and shell tools. "
    "Use slash_usage to report token consumption, slash_clear to reset context, "
    "slash_model to inspect or switch models, and slash_help to enumerate commands."
)


def _build_schema(model: str, endpoint: str) -> dict:
    schema: dict = {
        "model": model,
        "endpoint": endpoint,
        "status": "default",
        "inferred_tool_schema": _TOOL_SCHEMA,
        "behaviour": {"call_delivery_mode": "structured_tool_calls"},
        "tool_dispatch": _TOOL_DISPATCH,
    }
    if "opencode.ai/zen" in endpoint:
        schema["auth"] = "opencode-github-copilot"
    return schema


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Agent that exposes slash commands as LLM-callable tools."
    )
    p.add_argument("model", nargs="?", default="run:///home/martin/bin/best-effort-completions.py", help="Model ID or run:// URI")
    p.add_argument("task", nargs="*", help="One-shot task (omit for REPL)")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--session", metavar="SESSION_ID")
    return p.parse_args()


def main() -> None:
    from agentknit import create_client, init_session

    args = parse_args()
    schema = _build_schema(args.model, args.endpoint)
    opts = dict(resumed_from=args.session, system_prompt_supplement=_SYSTEM_SUPPLEMENT)

    # Populate shared context so slash tool functions can access session/client.
    client = create_client(schema)
    session = init_session(schema, **opts)
    slash_tool_ctx["session"] = session
    slash_tool_ctx["client"] = client
    slash_tool_ctx["model"] = args.model

    if args.task:
        from agentknit import run_turn
        from agentknit._core import _save_messages_snapshot, _log, _build_resume_cmd
        try:
            run_turn(client, args.model, session, " ".join(args.task))
        finally:
            _save_messages_snapshot(session)
            _log(session, {"type": "session_end", "session_id": session["session_id"],
                           "reason": "one_shot_task"})
    else:
        # Delegate to run_repl but pass our already-created client + session.
        # run_repl creates its own client/session, so we run the REPL loop
        # inline here to reuse our pre-populated slash_tool_ctx.
        from agentknit._core import (
            _save_messages_snapshot, _log, _build_resume_cmd,
            run_turn, read_repl_input, print_session_history,
            _InputCollector, _emit,
            BOLD, DIM, RED, RESET, RL_BOLD, RL_RESET,
        )
        from agentknit.slash_commands import REGISTRY as _slash_registry
        import readline
        import hashlib as _hashlib
        import os
        from pathlib import Path

        model = args.model

        if args.session:
            print_session_history(session)

        _hist_dir = Path.home() / ".local" / "share" / "agent_probe" / "repl_history"
        _hist_dir.mkdir(parents=True, exist_ok=True)
        _cwd_tag = _hashlib.md5(os.getcwd().encode()).hexdigest()[:12]
        _hist_file = _hist_dir / f"{_cwd_tag}.hist"
        try:
            readline.read_history_file(_hist_file)
        except FileNotFoundError:
            pass
        readline.set_history_length(500)

        resume_cmd = _build_resume_cmd(model, session["session_id"])
        print(f"{BOLD}slash-agent {model}{RESET}  (type 'exit' to quit)\n")
        try:
            while True:
                try:
                    t = read_repl_input(f"{RL_BOLD}>{RL_RESET} ")
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue
                cmd = t.strip()
                if cmd.lower() in ("exit", "quit", "q"):
                    break
                if cmd:
                    current_model = session.get("model", model)
                    slash_tool_ctx["model"] = current_model
                    if _slash_registry.dispatch(cmd, session, client, current_model):
                        _save_messages_snapshot(session)
                        continue
                    _collector = _InputCollector()
                    _pending = [t]
                    while _pending:
                        _task = _pending.pop(0)
                        _collector.start()
                        _interrupted = False
                        try:
                            run_turn(client, current_model, session, _task)
                        except KeyboardInterrupt:
                            print(f"\n{DIM}[interrupted]{RESET}")
                            _interrupted = True
                        except Exception as exc:
                            _emit(session, "error", text=str(exc),
                                  fmt=f"\n{RED}Error: {exc}{RESET}")
                            _log(session, {"type": "error", "error": str(exc),
                                           "ts": __import__("datetime").datetime.now()
                                           .isoformat(timespec="seconds")})
                        finally:
                            _collector.stop()
                        _save_messages_snapshot(session)
                        if _interrupted:
                            break
                        for _qi in _collector.drain():
                            _qs = _qi.strip()
                            if not _qs or _qs.lower() in ("exit", "quit", "q"):
                                continue
                            if not _slash_registry.dispatch(_qs, session, client, current_model):
                                _pending.append(_qi)
                            else:
                                _save_messages_snapshot(session)
        finally:
            try:
                readline.write_history_file(_hist_file)
            except Exception:
                pass
            _save_messages_snapshot(session)
            _log(session, {"type": "session_end", "session_id": session["session_id"],
                           "reason": "repl_exit"})
            print(f"\n{DIM}Resume: {resume_cmd}{RESET}")


if __name__ == "__main__":
    main()
