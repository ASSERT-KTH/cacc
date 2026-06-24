#!/usr/bin/env python3
"""
Seed Agent Demo
===============
An agent that starts with ONLY the `create_tool` tool and uses it to
generate its own additional tools at runtime.

Instead of a fixed toolset, the agent begins with a single meta-tool
(`create_tool`) that lets it define new Python functions, register them
in agentknit's TOOL_LIBRARY, and inject the corresponding schema +
dispatch entry into the live session — all on the fly.

The agent then immediately uses the newly minted tool on the next turn.

This demonstrates the full meta‑tooling / seeding cycle.
"""

import json
import textwrap

# ──────────────────────────────────────────────────────────────────────────────
# 1.  Register `create_tool` in agentknit's TOOL_LIBRARY
# ──────────────────────────────────────────────────────────────────────────────
from agentknit.tool_library import TOOL_LIBRARY, _register_generated
import agentknit._core as _core_mod


def t_create_tool(
    name: str = "",
    description: str = "",
    parameters: str = "{}",
    source_code: str = "",
) -> tuple[str, dict]:
    """Register a new tool at runtime and make it available immediately.

    Parameters
    ----------
    name : str
        Tool name (e.g. 'search_files').
    description : str
        What the tool does.
    parameters : str
        JSON string of the OpenAI-compatible parameter schema:
        {"type": "object", "properties": {...}, "required": [...]}
    source_code : str
        Complete Python source for a function named t_<name> that
        accepts the declared parameters as keyword args and returns
        tuple[str, dict].  Use only the standard library.

    Returns
    -------
    str
        Confirmation message, or an error description.
    """
    if not name or not source_code:
        return "ERROR: 'name' and 'source_code' are required.", {"result": "error"}

    fn_name = f"t_{name}"

    # 1. Compile & register the function in TOOL_LIBRARY
    if not _register_generated(fn_name, source_code):
        return (
            f"ERROR: Could not compile/register function '{fn_name}'. "
            f"Check the source_code for syntax errors.",
            {"result": "error"},
        )

    # 2. Build the OpenAI-compatible tool schema entry.
    params = json.loads(parameters) if isinstance(parameters, str) else parameters
    tool_schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }
    dispatch_entry = {"python_function": fn_name, "param_map": {}}

    # 3. Inject into the *active session* so the next API call includes
    #    this tool.  The global _ACTIVE_SESSION is set by our wrapper
    #    around run_turn (see below).
    session = _core_mod._ACTIVE_SESSION  # type: ignore[attr-defined]
    if session is None:
        return (
            "ERROR: No active session. create_tool can only be called "
            "during an agent turn.",
            {"result": "error"},
        )

    existing_names = {(t.get("function") or t).get("name") for t in session["tools"]}
    if name not in existing_names:
        session["tools"].append(tool_schema)
        session["tool_dispatch"][name] = dispatch_entry
        summary = (
            f"OK: created tool '{name}' (Python function '{fn_name}').\n"
            f"Description: {description}\n"
            f"It is now available — call it in your next turn."
        )
    else:
        summary = f"OK: tool '{name}' was already registered."

    return summary, {
        "result": "success",
        "tool_name": name,
        "function_name": fn_name,
    }


# Register the bootstrap tool.
TOOL_LIBRARY["t_create_tool"] = t_create_tool

# ──────────────────────────────────────────────────────────────────────────────
# 2.  Monkey-patch agentknit so create_tool can find the active session
# ──────────────────────────────────────────────────────────────────────────────
_core_mod._ACTIVE_SESSION = None  # type: ignore[attr-defined]

_original_run_turn = _core_mod._run_turn


def _patched_run_turn(client, model, session, task, cancel=None):
    _core_mod._ACTIVE_SESSION = session  # type: ignore[attr-defined]
    return _original_run_turn(client, model, session, task, cancel=cancel)


_core_mod._run_turn = _patched_run_turn

# ──────────────────────────────────────────────────────────────────────────────
# 3.  Build the seed agent spec — only `create_tool` at the start
# ──────────────────────────────────────────────────────────────────────────────

SEED_SPEC = {
    "model": "run:///home/martin/bin/opencode-free-deepseek-v4-flash-completions.py",
    "endpoint": "run:///home/martin/bin/opencode-free-deepseek-v4-flash-completions.py",
    "status": "ok",
    "behaviour": {"call_delivery_mode": "structured_tool_calls"},
    "auth": "opencode-github-copilot",
    "inferred_tool_schema": [
        {
            "type": "function",
            "function": {
                "name": "create_tool",
                "description": (
                    "Create a new tool at runtime.  Provide a name, "
                    "description, JSON parameter schema, and complete "
                    "Python source code.  The function in source_code "
                    "MUST be named 't_' + name and return (str, dict). "
                    "After calling this the new tool is available "
                    "for use in subsequent turns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Tool name (e.g. 'calculate_sum')",
                        },
                        "description": {
                            "type": "string",
                            "description": "Short description of the tool",
                        },
                        "parameters": {
                            "type": "string",
                            "description": (
                                "JSON schema object: "
                                '{"type":"object","properties":{...},'
                                '"required":[...]}'
                            ),
                        },
                        "source_code": {
                            "type": "string",
                            "description": (
                                "Complete Python source.  The function "
                                "must be named t_<name> and accept the "
                                "declared parameters as keyword arguments. "
                                "It must return tuple[str, dict]. "
                                "Standard library only."
                            ),
                        },
                    },
                    "required": ["name", "description", "parameters",
                                 "source_code"],
                },
            },
        },
    ],
    "tool_dispatch": {
        "create_tool": {
            "python_function": "t_create_tool",
            "param_map": {},
        },
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# 4.  Create a client from the schema
# ──────────────────────────────────────────────────────────────────────────────
def create_zen_client():
    from agentknit._core import create_client
    return create_client(SEED_SPEC)


# ──────────────────────────────────────────────────────────────────────────────
# 5.  Demonstration
# ──────────────────────────────────────────────────────────────────────────────

def main():
    import sys
    echo = lambda *a, **kw: print(*a, **kw, file=sys.stderr, flush=True)

    echo("=" * 72)
    echo("  SEED AGENT  (agentknit + Zen free endpoint)")
    echo("  The agent starts with ONE tool:  create_tool")
    echo("  It will generate its own tools at runtime, then use them.")
    echo("=" * 72)
    echo()

    schema = SEED_SPEC
    echo("  Model   :", schema["model"])
    echo("  Endpoint:", schema["endpoint"])
    echo("  Tools   :", [t["function"]["name"] for t in schema["inferred_tool_schema"]])
    echo()

    # ── Validate schema ──────────────────────────────────────────────
    from agentknit import validate_schema
    validate_schema(schema)

    # ── Create client ────────────────────────────────────────────────
    client = create_zen_client()
    echo("  Client created for Zen endpoint")
    echo()

    # ── Initialise session ───────────────────────────────────────────
    from agentknit._core import init_session

    session = init_session(
        schema,
        non_interactive=False,
        system_prompt_supplement=(
            "You are a seed agent.  You start with ONLY the "
            "'create_tool' tool.\n\n"
            "YOUR TASK: create whatever tools you need, then use them.\n\n"
            "RULES:\n"
            "*  Call create_tool with a name, description, JSON parameter "
            "schema, and complete Python source code.\n"
            "*  The function in source_code MUST be named t_<name> and "
            "return (str, dict).\n"
            "*  Use only the Python standard library.\n"
            "*  After calling create_tool, the new tool is registered and "
            "you can use it on your next turn.\n"
            "*  Create one tool at a time, then use it.\n"
            "*  When you have all the information you need, provide a "
            "concise plain-text summary."
        ),
    )

    echo("  Session ID:", session["session_id"])
    echo("  Tools available:", [t["function"]["name"] for t in session["tools"]])
    echo()

    # ── Task ─────────────────────────────────────────────────────────
    task = textwrap.dedent("""\
        Explore the agentknit package and answer:

        1. How many Python files are in the agentknit package directory?
        2. What is the total line count of all .py files combined?
        3. List the names of all public functions (no underscore prefix)
           exported by the agentknit package.

        You have only 'create_tool'.  Build whatever tools you need.
    """)

    echo("---" * 24)
    echo("  TASK")
    echo("---" * 24)
    for line in task.strip().splitlines():
        echo("   ", line)
    echo()

    # ── Run the agent ────────────────────────────────────────────────
    from agentknit._core import run_turn as core_run_turn

    try:
        result = core_run_turn(client, schema["model"], session, task)

        echo()
        echo("=" * 72)
        echo("  FINAL ANSWER")
        echo("=" * 72)
        if result.final_reply:
            echo(textwrap.indent(result.final_reply.strip(), "    "))
        else:
            echo("  (no final answer)")

        echo()
        usage = result.usage
        echo(f"  Token usage:  prompt {usage['prompt']:,}  "
             f"completion {usage['completion']:,}  "
             f"total {usage['total']:,}")
        echo(f"  Messages:     {len(result.messages)}  "
             f"Tools at end: {len(session['tools'])}")

    except KeyboardInterrupt:
        echo("\n  [interrupted]")
    except Exception as e:
        echo(f"\n  [ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    # ── Save state ───────────────────────────────────────────────────
    from agentknit._core import _save_messages_snapshot
    _save_messages_snapshot(session)
    echo(f"\n  Log: {session['log_path']}")


if __name__ == "__main__":
    main()
