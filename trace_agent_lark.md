# agent-lark — Grammar Tools in Flight

> **A real, live run** · 2026-08-17 · `gpt-5.6-luna` through the GitHub Copilot Responses API
>
> **Task:** Inspect `agent-lark.py` and explain how a custom grammar tool call travels from the model to local execution. Inspect the grammar definitions and `GrammarCompletions`; confirm whether local Lark validation is enabled; do not edit files.

| What happened | Count |
| --- | ---: |
| Model turns | 5 tool-use turns + final answer |
| Constrained tool calls | 5 |
| Successful reads | 2 |
| Shell checks | 2 |
| One useful recovery | 1 malformed path, then a corrected read |
| Session tokens | 28,139 prompt · 21,187 cached · 795 completion |

---

## 1. The model starts with the grammar definitions

### 🧠 Model → `read_file`

```text
agent-lark.py?lines=1-240
```

### ⚙️ Tool result

```text
#!/usr/bin/env python3
"""Grammar-only agent — every tool call is a grammar-constrained custom tool.

All four tools are declared as {"type": "custom", "format": {
"type": "grammar", "syntax": "lark", ...}} …

Tool surface (one grammar each):
  apply_patch — upstream Codex patch grammar
  exec        — Codex code-mode grammar
  read_file   — PATH [?lines=N-M]
  write_file  — path line followed by content
```

The first 240 lines establish the key premise: the model is given raw-text custom tools whose inputs are constrained by Lark grammars.

---

## 2. A visible recovery

### 🧠 Model → `read_file`

```text
{"input":"agent-lark.py?lines=241-520…"}
```

### ⚙️ Tool result

```text
ERROR: [Errno 2] No such file or directory: '{"input":"agent-lark.py'
```

The model briefly wrapped the raw custom-tool input as JSON. The local reader treated that as a filename and returned an ordinary tool error. The next turn fixes the input format.

### 🧠 Model → `read_file`

```text
agent-lark.py?lines=241-520
```

### ⚙️ Tool result

```text
# Tool table: name → (wire grammar, local validator grammar, description, fn).
TOOLS = { … }

CUSTOM_TOOLS = [
  {"type": "custom", "custom": {"name": name, …
    "format": {"type": "grammar", "syntax": "lark", …}}}
]

… _FUNCTION_MIRROR …
… GrammarCompletions …
… _translate_calls …
```

---

## 3. The model verifies the local guard

### 🧠 Model → `exec`

```bash
grep -nE 'Lark|_parsers|_grammar_ok|_guarded|local validation|lark' agent-lark.py
python - <<'PY'
from pathlib import Path
text = Path("agent-lark.py").read_text()
print("lark_import_present:", "from lark import Lark" in text)
print("local_validator_guard_present:", "def _guarded" in text)
print("guard_calls_present:", text.count("_guarded("))
PY
```

### ⚙️ Tool result

```text
293:    from lark import Lark
302:def _grammar_ok(name: str, text: str) -> str | None:
319:def _guarded(name: str, input: str) -> str | None:
… each executor calls _guarded(…)

lark_import_present: True
local_validator_guard_present: True
guard_calls_present: True
```

### 🧠 Model → `exec`

```bash
python - <<'PY'
try:
    import lark
    print("lark_installed: yes")
except ImportError:
    print("lark_installed: no")
PY
```

### ⚙️ Tool result

```text
lark_installed: yes
```

---

## 4. Model answer

<div style="border:1px solid #238636;border-left:4px solid #3fb950;border-radius:6px;padding:16px;background:#0d2218;margin:16px 0;color:#e6edf3">

### 🧠 The model’s final explanation

- `TOOLS` defines four raw-text tools and their Lark grammars. `CUSTOM_TOOLS` converts them to OpenAI `custom` tools with `format: {type: "grammar", syntax: "lark", definition: ...}`.
- Agentknit’s schema uses `_FUNCTION_MIRROR` only as a dispatch-compatible stand-in: each tool appears as a single-string function parameter named `input`. It is not sent to the endpoint.
- `GrammarOpenAI` replaces `chat.completions` with `GrammarCompletions`. On `create()`, `_grammar_tools()` swaps matching mirror function declarations for the real custom+grammar declarations, then serializes the request and runs `~/bin/copilot-gpt-5.6-luna.py` as a subprocess.
- The remote model performs constrained decoding against the supplied grammar, so emitted tool input is structurally valid. The subprocess response’s `custom` tool calls are translated by `_translate_calls()` into agentknit-compatible function calls with arguments `{"input": "<raw custom input>"}`.
- Agentknit dispatches the translated call to the implementation in `SCHEMA["tool_dispatch"]`. Each implementation calls `_guarded()`, which parses the raw input with a local Lark parser before executing filesystem or shell operations.
- Local Lark validation is enabled in this environment: `lark` is installed, `_parsers` are constructed with Earley parsers for all tools, and every executor invokes `_guarded()`. `apply_patch` uses its alternate line-oriented local grammar because the upstream grammar contains zero-width regex terminals that Python Lark cannot compile.

</div>

---

## The path, at a glance

```text
model
  │ constrained decoding against the supplied Lark grammar
  ▼
custom_tool_call { name, input: raw text }
  │ GrammarCompletions._translate_calls()
  ▼
agentknit-compatible function call { input: raw text }
  │ SCHEMA["tool_dispatch"]
  ▼
tool implementation → _guarded() → local Lark parse → filesystem / shell
```

> The JSON-wrapped read in step 2 is instructive: it shows the boundary between the model’s raw custom-tool input and agentknit’s internal `{ "input": … }` mirror. The agent recovered immediately and completed the investigation without modifying the repository.
