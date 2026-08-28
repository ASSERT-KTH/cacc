#!/usr/bin/env python3
"""Microsandbox-only coding agent built on agentknit.

The agent has the usual file tools (read_file, write_file, str_replace),
which act on the host workspace directory, but exactly one execution tool:

  sandbox_exec — run a shell command inside a persistent microsandbox
                 microVM (https://github.com/superradcompany/microsandbox)

There is no local shell, no local subprocess: the only way to run or
observe program behaviour is inside the microVM, reached through the `msb`
CLI. The host workspace directory is bind-mounted into the sandbox at
/workspace, so files written with write_file are visible to sandbox_exec
and vice versa.

Demo: run with --demo for a self-contained run (no API key needed).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from agentknit import (
    Tool,
    build_tool_spec,
    register_tools_in_library,
    run_task,
)

MSB = os.environ.get("MICROSANDBOX_AGENT_MSB") or shutil.which("msb") \
    or str(Path.home() / ".local/bin/msb")
IMAGE = os.environ.get("MICROSANDBOX_AGENT_IMAGE", "python")
WORKDIR = Path(os.environ.get("MICROSANDBOX_AGENT_WORKDIR", ".")).resolve()
CREATE_TIMEOUT_S = 120
EXEC_TIMEOUT_S = int(os.environ.get("MICROSANDBOX_AGENT_EXEC_TIMEOUT", "120"))

SANDBOX_NAME = os.environ.get("MICROSANDBOX_AGENT_NAME") or f"agent-{uuid.uuid4().hex[:8]}"
_sandbox_ready = False


# ── sandbox lifecycle ──────────────────────────────────────────────────────────

def _msb(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([MSB, *args], capture_output=True, text=True, timeout=timeout)


def _ensure_sandbox() -> str | None:
    """Lazily boot the persistent sandbox. Returns an error string, or None on success."""
    global _sandbox_ready
    if _sandbox_ready:
        return None
    try:
        proc = _msb(
            ["create", IMAGE, "--name", SANDBOX_NAME, "--replace",
             "--volume", f"{WORKDIR}:/workspace", "-w", "/workspace"],
            timeout=CREATE_TIMEOUT_S,
        )
    except FileNotFoundError:
        return f"ERROR: `{MSB}` not found. Install microsandbox (see https://get.microsandbox.dev)."
    except subprocess.TimeoutExpired:
        return f"ERROR: sandbox creation timed out after {CREATE_TIMEOUT_S}s"
    if proc.returncode != 0:
        return f"ERROR: failed to create sandbox: {proc.stderr.strip() or proc.stdout.strip()}"
    _sandbox_ready = True
    return None


def teardown_sandbox() -> None:
    if not _sandbox_ready:
        return
    try:
        _msb(["stop", SANDBOX_NAME], timeout=30)
        _msb(["rm", SANDBOX_NAME], timeout=30)
    except Exception:
        pass


# ── tool implementations ──────────────────────────────────────────────────────

def _resolve(path: str) -> Path:
    """Resolve a tool path against WORKDIR, matching the sandbox's /workspace mount."""
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else WORKDIR / p


def t_read(path: str) -> tuple[str, dict]:
    try:
        content = _resolve(path).read_text()
        if len(content) > 60_000:
            content = content[:60_000] + f"\n... [truncated, {len(content)} chars total]"
        return content, {"result": content}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}


def t_write(path: str, content: str) -> tuple[str, dict]:
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        r = f"OK: wrote {len(content)} bytes to {path}"
        return r, {"result": r}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}


def t_str_replace(path: str, old_str: str, new_str: str) -> tuple[str, dict]:
    try:
        p = _resolve(path)
        text = p.read_text()
        if old_str not in text:
            r = (f"ERROR: old_str not found in {path} "
                 f"({len(old_str)} chars, starts with {repr(old_str[:80])}).")
            return r, {"result": r}
        n = text.count(old_str)
        p.write_text(text.replace(old_str, new_str))
        r = f"OK: replaced {n} occurrence(s) in {path}"
        return r, {"result": r}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}


def t_sandbox_exec(command: str, timeout: int | None = None) -> tuple[str, dict]:
    """Run a shell command inside the persistent microsandbox microVM.

    The sandbox is booted from IMAGE (default: python) on first use, with
    the host workspace directory mounted at /workspace (also the working
    directory for the command). State (installed packages, running
    processes) persists across calls until sandbox_reset.
    """
    err = _ensure_sandbox()
    if err:
        return err, {"result": err}
    t = timeout or EXEC_TIMEOUT_S
    argv = ["exec", SANDBOX_NAME, "--timeout", f"{t}s", "--", "sh", "-c", command]
    try:
        proc = _msb(argv, timeout=t + 15)
    except subprocess.TimeoutExpired:
        r = f"ERROR: command timed out after {t}s"
        return r, {"result": r}
    out = proc.stdout
    err_out = proc.stderr.strip()
    result = out
    if err_out:
        result += f"\n[stderr]\n{err_out}"
    if proc.returncode != 0:
        result += f"\n[exit {proc.returncode}]"
    result = result or "(no output)"
    return result, {"result": result, "stdout": out, "stderr": err_out,
                    "returncode": proc.returncode}


def t_sandbox_reset() -> tuple[str, dict]:
    """Destroy and recreate the sandbox, discarding all in-VM state (installed
    packages, running processes). The workspace mount (host files) is unaffected."""
    global _sandbox_ready
    teardown_sandbox()
    _sandbox_ready = False
    err = _ensure_sandbox()
    if err:
        return err, {"result": err}
    r = f"OK: sandbox {SANDBOX_NAME} reset (fresh {IMAGE} microVM)"
    return r, {"result": r}


# ── tool definitions ────────────────────────────────────────────────────────────

TOOLS = [
    Tool("read_file", "Read and return the contents of a file at the specified path.",
         t_read,
         parameters={"type": "object",
                     "properties": {"path": {"type": "string", "description": "Path to the file."}},
                     "required": ["path"]}),
    Tool("write_file", "Write (or overwrite) a file at the specified path with the given content.",
         t_write,
         parameters={"type": "object",
                     "properties": {"path": {"type": "string", "description": "Path to the file."},
                                    "content": {"type": "string", "description": "Content to write."}},
                     "required": ["path", "content"]}),
    Tool("str_replace", "Edit an existing file by replacing a specific substring.",
         t_str_replace,
         parameters={"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "old_str": {"type": "string"},
                                    "new_str": {"type": "string"}},
                     "required": ["path", "old_str", "new_str"]}),
    Tool("sandbox_exec",
         f"Run a shell command inside a persistent microsandbox microVM (booted from "
         f"the '{IMAGE}' image on first use). The host workspace is mounted at "
         f"/workspace, which is also the command's working directory, so files "
         f"written with write_file are visible here and vice versa. This is the "
         f"only way to execute code or run programs — there is no local shell. "
         f"State (installed packages, background processes) persists across calls.",
         t_sandbox_exec,
         parameters={"type": "object",
                     "properties": {"command": {"type": "string",
                                                 "description": "Shell command to run (via sh -c)."},
                                    "timeout": {"type": "integer",
                                                "description": f"Timeout in seconds (default {EXEC_TIMEOUT_S})."}},
                     "required": ["command"]}),
    Tool("sandbox_reset",
         "Destroy and recreate the microVM sandbox from a clean image, discarding "
         "all in-VM state (installed packages, running processes). The host "
         "workspace files (and anything written via write_file) are unaffected.",
         t_sandbox_reset,
         parameters={"type": "object", "properties": {}, "required": []}),
]

TOOL_SCHEMA, TOOL_DISPATCH = build_tool_spec(TOOLS)
register_tools_in_library(TOOLS)


# ── demo (no LLM): exercise the tools directly ────────────────────────────────

DEMO_TASK = """Write a Python script `fizzbuzz.py` that prints FizzBuzz from 1 to 15, \
run it in the microsandbox, install the `cowsay` package there and use it to print a \
message, then reset the sandbox and show that cowsay is gone but the script file remains."""


def run_demo() -> None:
    print("=" * 64)
    print("  Microsandbox Agent Demo — msb microVM, no local shell")
    print("=" * 64)
    work = Path("microsandbox_agent_demo")
    work.mkdir(exist_ok=True)
    src = "fizzbuzz.py"  # relative to WORKDIR, matches the /workspace mount

    global WORKDIR
    WORKDIR = work.resolve()

    print(f"\n[0] sandbox: {SANDBOX_NAME}  image: {IMAGE}  workspace: {WORKDIR}")

    print("\n[1] write_file fizzbuzz.py")
    code = """\
for i in range(1, 16):
    if i % 15 == 0:
        print("FizzBuzz")
    elif i % 3 == 0:
        print("Fizz")
    elif i % 5 == 0:
        print("Buzz")
    else:
        print(i)
"""
    print("  " + t_write(str(src), code)[0])

    print("\n[2] sandbox_exec: run fizzbuzz.py in the microVM")
    print("  " + t_sandbox_exec("python3 fizzbuzz.py")[0].replace("\n", "\n  "))

    print("\n[3] sandbox_exec: break the file, rerun, see the traceback")
    t_str_replace(str(src), 'print(i)', 'print(i')
    print("  " + t_sandbox_exec("python3 fizzbuzz.py")[0].splitlines()[-1])
    t_str_replace(str(src), 'print(i', 'print(i)')

    print("\n[4] sandbox_exec: install cowsay in the microVM and use it")
    print("  " + t_sandbox_exec("pip install -q cowsay && python3 -c "
                                "\"import cowsay; cowsay.cow('hello from the microVM')\"")[0]
          .replace("\n", "\n  "))

    print("\n[5] sandbox_reset: fresh microVM, cowsay is gone, fizzbuzz.py (host file) remains")
    print("  " + t_sandbox_reset()[0])
    print("  " + t_sandbox_exec("python3 -c 'import cowsay' 2>&1 || echo 'cowsay: not installed'")[0]
          .replace("\n", "\n  "))
    print("  " + t_sandbox_exec("ls fizzbuzz.py && echo 'fizzbuzz.py: still here'")[0]
          .replace("\n", "\n  "))

    teardown_sandbox()
    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 64)
    print("  Demo complete — sandbox_exec + sandbox_reset work.")
    print("=" * 64)


# ── CLI ───────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_SUPPLEMENT = (
    "You are a coding agent whose only execution tool is sandbox_exec, which runs "
    "shell commands inside a persistent microsandbox microVM (a real, isolated "
    "Linux VM, booted from a container image). There is no local shell — to run "
    "or test anything, use sandbox_exec. The host workspace is mounted at "
    "/workspace inside the sandbox (also its working directory), so files "
    "written with write_file/str_replace are immediately visible to "
    "sandbox_exec, and files the sandbox creates under /workspace appear on "
    "the host. Installed packages and running processes persist across "
    "sandbox_exec calls until sandbox_reset, which boots a clean microVM "
    "(host workspace files are unaffected by a reset). "
    "Path convention: read_file/write_file/str_replace take HOST-relative "
    "paths (e.g. 'primes.py', not '/workspace/primes.py' — the host has no "
    "/workspace directory). sandbox_exec commands run with cwd /workspace "
    "inside the VM, so use the same bare relative filename there too "
    "(e.g. `python3 primes.py`, not `python3 /workspace/primes.py`)."
)


def main() -> None:
    global IMAGE
    parser = argparse.ArgumentParser(
        description="Microsandbox-only coding agent: read/write tools + sandbox_exec (msb microVM)."
    )
    parser.add_argument("task", nargs="*", help="Task to run (omit for demo)")
    parser.add_argument("--demo", action="store_true", help="Run the self-contained demo (no API key)")
    parser.add_argument("--model", default="glm-5.3", help="Model ID (default: glm-5.3)")
    parser.add_argument("--endpoint", default="https://api.z.ai/api/coding/paas/v4",
                        help="API endpoint (default: z.ai coding endpoint)")
    parser.add_argument("--image", default=IMAGE, help=f"Sandbox image (default: {IMAGE})")
    parser.add_argument("--session", help="Resume a previous session")
    args = parser.parse_args()
    IMAGE = args.image

    if args.demo:
        run_demo()
        return

    task = " ".join(args.task) if args.task else None
    if not task and sys.stdin.isatty():
        parser.print_help()
        print("\nProvide a task, pipe one via stdin, or use --demo.")
        sys.exit(1)
    if not task:
        task = sys.stdin.read().strip()

    schema = {
        "model": args.model,
        "endpoint": args.endpoint,
        "keyring_service": "z.ai",
        "keyring_username": "api_key",
        "inferred_tool_schema": TOOL_SCHEMA,
        "tool_dispatch": TOOL_DISPATCH,
        "behaviour": {"call_delivery_mode": "structured_tool_calls"},
    }
    try:
        result = run_task(
            schema,
            task,
            non_interactive=True,
            session_id=args.session,
            system_prompt_supplement=SYSTEM_PROMPT_SUPPLEMENT,
        )
        if result.final_reply:
            print(result.final_reply)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        teardown_sandbox()


if __name__ == "__main__":
    main()
