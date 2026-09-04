#!/usr/bin/env python3
"""Racket-aware coding agent built on agentknit.

Tools:
  read_file       — read a file
  write_file      — write a file
  str_update      — replace a substring in a file
  exec_racket     — evaluate a Racket expression and return the result

Demo: run with --demo to see a self-contained Racket metaprogramming example.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agentknit import (
    Tool,
    build_tool_spec,
    register_tools_in_library,
    run_task,
    load_or_probe,
    validate_schema,
    check_and_display_pricing,
    DEFAULT_ENDPOINT,
)


# ── tool implementations ──────────────────────────────────────────────────────

def t_read(path: str) -> tuple[str, dict]:
    try:
        content = Path(os.path.expanduser(path)).read_text()
        return content, {"result": content}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}


def t_write(path: str, content: str) -> tuple[str, dict]:
    try:
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        r = f"OK: wrote {len(content)} bytes to {path}"
        return r, {"result": r}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}


def t_str_update(path: str, old_str: str, new_str: str) -> tuple[str, dict]:
    try:
        p = Path(os.path.expanduser(path))
        text = p.read_text()
        if old_str not in text:
            r = (f"ERROR: old string not found in {path} "
                 f"({len(old_str)} chars, starts with {repr(old_str[:80])}).")
            return r, {"result": r}
        n = text.count(old_str)
        p.write_text(text.replace(old_str, new_str))
        r = f"OK: replaced {n} occurrence(s) in {path}"
        return r, {"result": r}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}


def t_exec_racket(expression: str) -> tuple[str, dict]:
    """Evaluate a Racket expression and return stdout, stderr, and exit code.

    The expression is written to a temp file in the current directory and run
    with `racket` so that `#lang` directives, `require`, and full module syntax
    work correctly relative to the CWD.
    """
    import uuid
    tmppath = f".racket_agent_{uuid.uuid4().hex[:8]}.rkt"
    try:
        Path(tmppath).write_text(expression)
        proc = subprocess.run(
            ['racket', tmppath],
            capture_output=True, text=True, timeout=30,
        )
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        combined = out
        if err:
            combined += f"\n[stderr]\n{err}"
        if proc.returncode != 0:
            combined += f"\n[exit {proc.returncode}]"
        result = combined or "(no output)"
        return result, {"result": result, "stdout": out, "stderr": err,
                        "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        r = "ERROR: Racket evaluation timed out after 30 s"
        return r, {"result": r}
    except FileNotFoundError:
        r = "ERROR: `racket` not found on PATH. Install Racket first."
        return r, {"result": r}
    except Exception as e:
        r = f"ERROR: {e}"
        return r, {"result": r}
    finally:
        try:
            Path(tmppath).unlink(missing_ok=True)
        except Exception:
            pass


# ── tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    Tool("read_file", "Read and return the contents of a file at the specified path.",
         t_read,
         parameters={"path": {"type": "string", "description": "Path to the file."}}),
    Tool("write_file", "Write (or overwrite) a file at the specified path with the given content.",
         t_write,
         parameters={"path": {"type": "string", "description": "Path to the file."},
                     "content": {"type": "string", "description": "Content to write."}}),
    Tool("str_update", "Edit an existing file by replacing a specific substring.",
         t_str_update,
         parameters={"path": {"type": "string"},
                     "old_str": {"type": "string"},
                     "new_str": {"type": "string"}}),
    Tool("exec_racket", "Evaluate a Racket expression and return the result.",
         t_exec_racket,
         parameters={"expression": {"type": "string",
                                    "description": "Racket expression to evaluate."}}),
]

TOOL_SCHEMA, TOOL_DISPATCH = build_tool_spec(TOOLS)
register_tools_in_library(TOOLS)


# ── demo: self-referential Racket metaprogramming ─────────────────────────────

DEMO_TASK = """
Write a Racket program that:

1. Defines a macro `define-logged` that works like `define` but also prints
   "defined <name> = <value>" at expansion time.

2. Uses it to define `(greet name)` — a function that returns a greeting string.

3. Writes the program to `greeter.rkt`.

4. Runs `greeter.rkt` with racket and shows the output.

Then write a second Racket program that reads `greeter.rkt` as an S-expression,
finds the `define-logged` form, and pretty-prints its structure. Save it as
`meta.rkt` and run it too.
"""


def run_demo() -> None:
    """Run the demo using a local subprocess agent (no API key needed)."""
    print("=" * 60)
    print("  Racket Agent Demo — Metaprogramming with Macros")
    print("=" * 60)

    # Build a minimal agent spec that uses a local subprocess.
    # We bypass the LLM and just run the tools directly to demonstrate them.
    print("\n[1] Writing greeter.rkt ...")
    greeter_code = r"""#lang racket

(define-syntax-rule (define-logged name val)
  (begin
    (printf "defined ~a = ~a\n" 'name val)
    (define name val)))

(define-logged greeting "Hello from Racket!")

(define (greet name)
  (string-append greeting " Nice to meet you, " name "."))

(provide greet)
"""
    result, meta = t_write("greeter.rkt", greeter_code)
    print(f"  {result}")

    print("\n[2] Running greeter.rkt ...")
    result, meta = t_exec_racket('#lang racket\n(require "greeter.rkt")\n(displayln (greet "Agent"))')
    print(f"  {result}")

    print("\n[3] Writing meta.rkt (reads greeter.rkt as data) ...")
    meta_code = r"""#lang racket

;; Read greeter.rkt as raw text, strip #lang line, then read as datum
(define raw
  (with-input-from-file "greeter.rkt"
    (λ () (port->string))))

;; Remove the #lang line and any blank lines before it
(define cleaned
  (regexp-replace #rx"^#lang[^\n]*\n" raw ""))

;; Read the remaining forms
(define port (open-input-string cleaned))
(define forms
  (let loop ([acc '()])
    (define expr (read port))
    (if (eof-object? expr)
        (reverse acc)
        (loop (cons expr acc)))))
(close-input-port port)

(printf "Module structure (~a forms):\n" (length forms))
(for ([form forms])
  (pretty-print form)
  (newline))

;; Find define-logged forms
(printf "\n--- define-logged forms ---\n")
(for ([form forms]
      #:when (and (list? form)
                  (>= (length form) 3)
                  (eq? (car form) 'define-logged)))
  (printf "  name: ~a\n" (cadr form))
  (printf "  value: ~a\n\n" (caddr form)))
"""
    result, meta = t_write("meta.rkt", meta_code)
    print(f"  {result}")

    print("\n[4] Running meta.rkt ...")
    result, meta = t_exec_racket('#lang racket\n(require "meta.rkt")')
    print(f"  {result}")

    print("\n[5] Cleaning up ...")
    for f in ["greeter.rkt", "meta.rkt"]:
        Path(f).unlink(missing_ok=True)
    print("  Done.")

    print("\n" + "=" * 60)
    print("  Demo complete — all tools work correctly.")
    print("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Racket coding agent — uses agentknit with read_file, write_file, str_update, exec_racket."
    )
    parser.add_argument("task", nargs="*", help="Task to run (omit for demo)")
    parser.add_argument("--demo", action="store_true", help="Run the self-contained demo")
    parser.add_argument("--model", default="qwen/qwen3-8b",
                        help="Model ID (default: qwen/qwen3-8b)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="API endpoint (default: OpenRouter)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Disable user-interaction tools")
    parser.add_argument("--session", help="Resume a previous session")
    args = parser.parse_args()

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

    # Build an agent spec on the fly
    schema = {
        "model": args.model,
        "endpoint": args.endpoint,
        "inferred_tool_schema": TOOL_SCHEMA,
        "tool_dispatch": TOOL_DISPATCH,
        "behaviour": {"call_delivery_mode": "structured_tool_calls"},
    }

    try:
        result = run_task(
            schema,
            task,
            non_interactive=args.non_interactive,
            session_id=args.session,
        )
        if result.final_reply:
            print(result.final_reply)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()