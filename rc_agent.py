#!/usr/bin/env python3
"""RC agent — remote-controls a machine via SSH using agentknit + Zen endpoint.

Tools exposed to the model:
  read_file       — cat a remote file via SSH
  write_file      — write content to a remote path via SSH
  execute_shell   — run an arbitrary shell command on the remote host

Usage:
    rc_agent.py [--ssh-server HOST] [task ...]   # one-shot task
    rc_agent.py [--ssh-server HOST]               # interactive REPL
    echo "task" | rc_agent.py [--ssh-server HOST] # piped task

Default SSH server: sos-small02
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

import agentknit
from agentknit import Tool, build_tool_spec, register_tools_in_library

MODEL    = ""
ENDPOINT = "run:///home/martin/bin/best-effort-completions.py"
DEFAULT_SSH_SERVER = "sos-small02"

# Mutable single-element list so the closures below can be reassigned
# without needing `global`.
_host: list[str] = [DEFAULT_SSH_SERVER]


# ── SSH tool implementations ──────────────────────────────────────────────────

def ssh_read(path: str) -> tuple[str, dict]:
    """Read a file on the remote machine and return its contents."""
    try:
        r = subprocess.run(
            ["ssh", _host[0], "cat", "--", path],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if r.returncode != 0:
            msg = f"ERROR: {r.stderr.strip() or f'ssh exit {r.returncode}'}"
            return msg, {"result": msg}
        return r.stdout, {"result": r.stdout}
    except subprocess.TimeoutExpired:
        msg = "ERROR: SSH read timed out after 30 s"
        return msg, {"result": msg}
    except Exception as e:
        msg = f"ERROR: {e}"
        return msg, {"result": msg}


def ssh_write(path: str, content: str) -> tuple[str, dict]:
    """Write content to a file on the remote machine (creates parent dirs)."""
    try:
        parent = shlex.quote(str(Path(path).parent))
        dest   = shlex.quote(path)
        r = subprocess.run(
            ["ssh", _host[0], f"mkdir -p -- {parent} && cat > {dest}"],
            input=content, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if r.returncode != 0:
            msg = f"ERROR: {r.stderr.strip() or f'ssh exit {r.returncode}'}"
            return msg, {"result": msg}
        msg = f"OK: wrote {len(content)} bytes to {path} on {_host[0]}"
        return msg, {"result": msg}
    except subprocess.TimeoutExpired:
        msg = "ERROR: SSH write timed out after 30 s"
        return msg, {"result": msg}
    except Exception as e:
        msg = f"ERROR: {e}"
        return msg, {"result": msg}


def ssh_execute_shell(command: str) -> tuple[str, dict]:
    """Execute a shell command on the remote machine and return combined output."""
    try:
        r = subprocess.run(
            ["ssh", _host[0], "bash", "-c", command],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        out = r.stdout or ""
        err = r.stderr or ""
        combined = out
        if err:
            combined += ("\n" if combined else "") + err
        if r.returncode != 0:
            combined += f"\n[exit {r.returncode}]"
        return combined or "(no output)", {
            "stdout": out, "stderr": err, "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        msg = "ERROR: SSH command timed out after 60 s"
        return msg, {"result": msg}
    except Exception as e:
        msg = f"ERROR: {e}"
        return msg, {"result": msg}


# ── Tool spec ─────────────────────────────────────────────────────────────────

_TOOLS = [
    Tool(
        name="read_file",
        description="Read the contents of a file on the remote machine.",
        fn=ssh_read,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="write_file",
        description="Write (or overwrite) a file on the remote machine.",
        fn=ssh_write,
        parameters={
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Absolute path to the file."},
                "content": {"type": "string", "description": "Content to write."},
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="execute_shell",
        description="Execute a shell command on the remote machine and return stdout+stderr.",
        fn=ssh_execute_shell,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
            },
            "required": ["command"],
        },
    ),
]

register_tools_in_library(_TOOLS)
_tool_schema, _tool_dispatch = build_tool_spec(_TOOLS)

SCHEMA: dict = {
    "model":                MODEL,
    "endpoint":             ENDPOINT,
    "status":               "ok",
    "auth":                 "opencode-github-copilot",
    "behaviour":            {"call_delivery_mode": "structured_tool_calls"},
    "inferred_tool_schema": _tool_schema,
    "tool_dispatch":        _tool_dispatch,
}


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SSH remote-control agent (agentknit + Zen endpoint).",
    )
    p.add_argument(
        "--ssh-server", "-H", default=DEFAULT_SSH_SERVER, metavar="HOST",
        dest="ssh_server",
        help=f"SSH destination host (default: {DEFAULT_SSH_SERVER})",
    )
    p.add_argument(
        "--session", metavar="SESSION_ID",
        help="Resume a previous session by ID",
    )
    p.add_argument(
        "--non-interactive", action="store_true", dest="non_interactive",
        help="Disable interactive user-question tools",
    )
    p.add_argument(
        "task", nargs="*",
        help="Task to run (omit for REPL / pipe stdin)",
    )
    return p.parse_args()


def main() -> None:
    args   = parse_args()
    _host[0] = args.ssh_server

    agentknit.validate_schema(SCHEMA)

    sys_supplement = (
        f"You are an agent that controls a remote Linux machine via SSH. "
        f"The target host is {_host[0]}. "
        f"Use read_file, write_file, and execute_shell to accomplish the task. "
        f"When done, provide a concise plain-text summary."
    )

    if args.task:
        result = agentknit.run_task(
            SCHEMA,
            " ".join(args.task),
            non_interactive=args.non_interactive,
            session_id=args.session,
            system_prompt_supplement=sys_supplement,
        )
        if result.final_reply:
            print(result.final_reply)
        return

    if not sys.stdin.isatty():
        task = sys.stdin.read().strip()
        if task:
            result = agentknit.run_task(
                SCHEMA,
                task,
                non_interactive=args.non_interactive,
                session_id=args.session,
                system_prompt_supplement=sys_supplement,
            )
            if result.final_reply:
                print(result.final_reply)
        return

    agentknit.run_repl(
        SCHEMA,
        non_interactive=args.non_interactive,
        session_id=args.session,
        system_prompt_supplement=sys_supplement,
    )


if __name__ == "__main__":
    main()
