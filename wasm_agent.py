#!/usr/bin/env python3
"""Wasm-only coding agent built on agentknit.

The agent has the usual file tools (read_file, write_file, str_replace) but
exactly two execution tools:

  compile_rust_to_wasm — compile a Rust source file to WebAssembly
                         (wasm32-wasip1, rustc)
  wasmtime_exec        — run a .wasm module with the wasmtime CLI

There is no shell, no python, no bash: the only way to observe program
behaviour is to compile Rust to wasm and execute it in wasmtime.

Demo: run with --demo for a self-contained run (no API key needed).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agentknit import (
    Tool,
    build_tool_spec,
    register_tools_in_library,
    run_task,
)

RUSTC = os.environ.get("WASM_AGENT_RUSTC", "rustc")
WASMTIME = os.environ.get("WASM_AGENT_WASMTIME") or shutil.which("wasmtime") \
    or str(Path.home() / ".wasmtime/bin/wasmtime")
COMPILE_TARGET = os.environ.get("WASM_AGENT_TARGET", "wasm32-wasip1")
COMPILE_TIMEOUT_S = 300
# Wasmtime fuel: a hard cap on executed wasm instructions. The module traps
# ("all fuel consumed by WebAssembly") when the budget runs out — a
# deterministic, load-independent bound on guest work, unlike a wall-clock
# timeout.
EXEC_FUEL = int(os.environ.get("WASM_AGENT_FUEL", "20_000_000_000"))


# ── tool implementations ──────────────────────────────────────────────────────

def t_read(path: str) -> tuple[str, dict]:
    try:
        content = Path(os.path.expanduser(path)).read_text()
        if len(content) > 60_000:
            content = content[:60_000] + f"\n... [truncated, {len(content)} chars total]"
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


def t_str_replace(path: str, old_str: str, new_str: str) -> tuple[str, dict]:
    try:
        p = Path(os.path.expanduser(path))
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


def t_compile_rust_to_wasm(path: str, out: str | None = None,
                           deps: str | None = None) -> tuple[str, dict]:
    """Compile a Rust source file to WebAssembly with rustc.

    Uses the wasm32-wasip1 target, so the resulting module has full std
    (println!, fs, argv/env) and runs under wasmtime.

    ``deps`` adds vendored crate source dirs (as produced by cargo_install)
    to rustc's include search path, so `#[path]`/`mod` references and crate
    sources under deps/<crate>/src resolve.
    """
    src = Path(os.path.expanduser(path))
    wasm = Path(os.path.expanduser(out)) if out else src.with_suffix(".wasm")
    if not src.exists():
        r = f"ERROR: {path} does not exist"
        return r, {"result": r}
    argv = [RUSTC, "--target", COMPILE_TARGET, "-O"]
    for d in (deps or "").split():
        dep_dir = Path(os.path.expanduser(d))
        if not dep_dir.is_dir():
            r = f"ERROR: dep dir {d} does not exist (install it with cargo_install)"
            return r, {"result": r}
        argv += ["-L", f"dependency={dep_dir}"]
    argv += ["-o", str(wasm), str(src)]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
        )
    except FileNotFoundError:
        r = (f"ERROR: `{RUSTC}` not found. Install a Rust toolchain with the "
             f"{COMPILE_TARGET} target (`rustup target add {COMPILE_TARGET}`).")
        return r, {"result": r}
    except subprocess.TimeoutExpired:
        r = f"ERROR: compilation timed out after {COMPILE_TIMEOUT_S} s"
        return r, {"result": r}
    if proc.returncode != 0:
        err = proc.stderr.strip() or "(no diagnostics)"
        r = f"COMPILATION FAILED (exit {proc.returncode}):\n{err}"
        return r, {"result": r, "returncode": proc.returncode, "stderr": err}
    size = wasm.stat().st_size if wasm.exists() else -1
    r = f"OK: compiled {path} -> {wasm} ({size} bytes, target {COMPILE_TARGET})"
    return r, {"result": r, "wasm_path": str(wasm), "size_bytes": size}


def t_wasmtime_exec(path: str, args: str | None = None,
                    dirs: str | None = None) -> tuple[str, dict]:
    """Run a WebAssembly module with the wasmtime CLI.

    ``dirs`` grants WASI filesystem access, one entry per space-separated
    host directory. Each entry is either ``HOST`` (preopened at the same
    guest path) or ``HOST::GUEST`` (preopened as ``/GUEST``), e.g.
    ``"data /tmp/wasmtest::/work"``. Without ``dirs`` the module runs with
    no filesystem capability at all.
    """
    wasm = Path(os.path.expanduser(path))
    if not wasm.exists():
        r = f"ERROR: {path} does not exist (compile it first with compile_rust_to_wasm)"
        return r, {"result": r}
    argv = [WASMTIME, "run", "-W", f"fuel={EXEC_FUEL}"]
    for entry in (dirs or "").split():
        host, _, guest = entry.partition("::")
        argv += ["--dir", entry if guest else f"{host}::{host.strip('/')}"]
    argv.append(str(wasm))
    if args:
        argv += args.split()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
        )
    except FileNotFoundError:
        r = f"ERROR: `{WASMTIME}` not found. Install the wasmtime CLI."
        return r, {"result": r}
    out = proc.stdout
    err = proc.stderr.strip()
    result = out
    if err:
        result += f"\n[stderr]\n{err}"
    if proc.returncode != 0:
        result += f"\n[exit {proc.returncode}]"
    result = result or "(no output)"
    return result, {"result": result, "stdout": out, "stderr": err,
                    "returncode": proc.returncode}


# ── crates.io: search + vendor (no cargo, no global cache) ────────────────────

CRATES_IO_API = "https://crates.io/api/v1/crates"
CRATES_USER_AGENT = "wasm_agent.py (concept car; martin.monperrus@gnieh.org)"
CARGO_SEARCH_TIMEOUT_S = 30


def t_cargo_search(query: str, limit: int = 5) -> tuple[str, dict]:
    """Search crates.io and return the top crates for a query.

    Pure HTTPS GET against the crates.io index API — no cargo involved, no
    code downloaded, nothing executed. Read-only by construction.
    """
    try:
        import requests
    except ImportError:
        r = "ERROR: the `requests` package is required for cargo_search"
        return r, {"result": r}
    try:
        resp = requests.get(
            CRATES_IO_API, params={"q": query, "per_page": max(1, min(limit, 25))},
            headers={"User-Agent": CRATES_USER_AGENT}, timeout=CARGO_SEARCH_TIMEOUT_S,
        )
        resp.raise_for_status()
        crates = resp.json().get("crates", [])
    except Exception as e:
        r = f"ERROR: crates.io search failed: {e}"
        return r, {"result": r}
    if not crates:
        r = f"No crates found for {query!r}"
        return r, {"result": r}
    lines = []
    for c in crates:
        lines.append(f"{c['name']} {c.get('max_version', '?')} — "
                     f"{(c.get('description') or '').strip()[:100]}")
    result = "\n".join(lines)
    return result, {"result": result, "crates": crates[:max(1, min(limit, 25))]}


def t_cargo_install(crate: str, version: str | None = None) -> tuple[str, dict]:
    """Download a crate from crates.io and vendor it into ./deps/<crate>/.

    Fetches the .crate tarball (a plain gzip'd tar over HTTPS) and extracts
    only its source — no build, no code execution, no ~/.cargo cache, no
    global state. Everything lands inside the current working directory, so
    the install is visible to read_file and bounded by the same journal as
    every other side effect.
    """
    try:
        import requests
    except ImportError:
        r = "ERROR: the `requests` package is required for cargo_install"
        return r, {"result": r}
    try:
        info = requests.get(f"{CRATES_IO_API}/{crate}",
                            headers={"User-Agent": CRATES_USER_AGENT},
                            timeout=CARGO_SEARCH_TIMEOUT_S)
        info.raise_for_status()
        versions = info.json().get("versions", [])
        if not versions:
            r = f"ERROR: no versions found for crate {crate!r}"
            return r, {"result": r}
        ver = next((v for v in versions if v.get("num") == version), versions[0])
        dl_url = ver["dl_path"] if ver.get("dl_path", "").startswith("http") \
            else f"https://crates.io{ver['dl_path']}"
        tarball = requests.get(dl_url, headers={"User-Agent": CRATES_USER_AGENT},
                               timeout=CARGO_SEARCH_TIMEOUT_S)
        tarball.raise_for_status()
    except Exception as e:
        r = f"ERROR: fetching {crate} from crates.io failed: {e}"
        return r, {"result": r}

    dest = Path.cwd() / "deps" / crate
    try:
        dest.mkdir(parents=True, exist_ok=True)
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(tarball.content), mode="r:gz") as tf:
            # The tarball has a single top-level dir <crate>-<version>/.
            for member in tf.getmembers():
                if not member.isfile() or "/src/" not in f"/{member.name}":
                    continue
                rel = member.name.split("/", 1)[1]           # strip top-level dir
                if not rel.startswith("src/") and "Cargo.toml" not in rel:
                    continue
                # Refuse path escapes while extracting.
                target = (dest / rel).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tf.extractfile(member).read())
    except Exception as e:
        r = f"ERROR: extracting {crate} failed: {e}"
        return r, {"result": r}

    files = sorted(str(p.relative_to(Path.cwd())) for p in dest.rglob("*") if p.is_file())
    result = (f"OK: vendored {crate} {ver['num']} into {dest.relative_to(Path.cwd())}/ "
              f"({len(files)} files)")
    return result, {"result": result, "crate": crate, "version": ver["num"],
                    "files": files}


# ── tool definitions ──────────────────────────────────────────────────────────

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
    Tool("compile_rust_to_wasm",
         f"Compile a Rust source file to a WebAssembly module (target {COMPILE_TARGET}, "
         f"so the module has std and prints/args support). Returns compiler diagnostics on failure. "
         f"Pass deps= (space-separated vendored crate dirs, e.g. 'deps/serde') to make "
         f"cargo_install'ed crate sources available to the build.",
         t_compile_rust_to_wasm,
         parameters={"type": "object",
                     "properties": {"path": {"type": "string", "description": "Rust source file (.rs)."},
                                    "out": {"type": "string",
                                            "description": "Optional output .wasm path "
                                                           "(default: same name with .wasm suffix)."},
                                    "deps": {"type": "string",
                                             "description": "Optional space-separated vendored "
                                                            "crate dirs (from cargo_install), "
                                                            "e.g. 'deps/serde'."}},
                     "required": ["path"]}),
    Tool("wasmtime_exec",
         "Run a WebAssembly module with wasmtime. Returns stdout, stderr and exit code. "
         "Execution is bounded by a deterministic fuel budget (a hard cap on executed "
         "wasm instructions; the module traps when it runs out). "
         "Pass `dirs` to grant the module WASI filesystem access (read/write), e.g. "
         "\"data\" preopens the host dir `data` at guest path `/data`, "
         "\"/tmp/w::/work\" preopens /tmp/w as /work. Without dirs the module gets no filesystem.",
         t_wasmtime_exec,
         parameters={"type": "object",
                     "properties": {"path": {"type": "string", "description": "Wasm module (.wasm) to run."},
                                    "args": {"type": "string",
                                             "description": "Optional space-separated CLI arguments "
                                                            "passed to the module."},
                                    "dirs": {"type": "string",
                                             "description": "Optional space-separated host dirs to "
                                                            "preopen for WASI, each HOST or "
                                                            "HOST::GUEST (e.g. \"data::/data\")."}},
                     "required": ["path"]}),
    Tool("cargo_search",
         "Search crates.io for Rust packages. Read-only HTTPS query against the "
         "crates.io index: returns name, latest version and description for the "
         "top matches. Use it to discover crates before cargo_install.",
         t_cargo_search,
         parameters={"type": "object",
                     "properties": {"query": {"type": "string", "description": "Search terms."},
                                    "limit": {"type": "integer", "description": "Max results (1-25, default 5)."}},
                     "required": ["query"]}),
    Tool("cargo_install",
         "Download a crate's source from crates.io and vendor it into ./deps/<crate>/ "
         "in the current working directory. Pure download + extract: no build, no code "
         "execution, no ~/.cargo cache. After installing, compile against it with "
         "compile_rust_to_wasm by adding deps/<crate>/src to the search path "
         "(rustc --extern is not used; the crate source is compiled as an include).",
         t_cargo_install,
         parameters={"type": "object",
                     "properties": {"crate": {"type": "string", "description": "Crate name, e.g. 'serde'."},
                                    "version": {"type": "string", "description": "Optional exact version (default: latest)."}},
                     "required": ["crate"]}),
]

TOOL_SCHEMA, TOOL_DISPATCH = build_tool_spec(TOOLS)
register_tools_in_library(TOOLS)


# ── demo (no LLM): exercise the tools directly ────────────────────────────────

DEMO_TASK = """Write a Rust program `fizzbuzz.rs` that prints FizzBuzz from 1 to 15, \
compile it to WebAssembly, and run it under wasmtime. Then modify it to also print \
the program's command-line arguments and re-run everything."""


def run_demo() -> None:
    print("=" * 64)
    print("  Wasm Agent Demo — rustc + wasmtime, no shell")
    print("=" * 64)
    work = Path("wasm_agent_demo")
    work.mkdir(exist_ok=True)
    src = work / "fizzbuzz.rs"

    print("\n[1] write_file fizzbuzz.rs")
    code = """\
fn main() {
    for i in 1..=15 {
        let s = match (i % 3, i % 5) {
            (0, 0) => "FizzBuzz".to_string(),
            (0, _) => "Fizz".to_string(),
            (_, 0) => "Buzz".to_string(),
            (_, _) => i.to_string(),
        };
        println!("{s}");
    }
}
"""
    print("  " + t_write(str(src), code)[0])

    print("\n[2] compile_rust_to_wasm")
    print("  " + t_compile_rust_to_wasm(str(src))[0])

    print("\n[3] wasmtime_exec")
    print("  " + t_wasmtime_exec(str(src.with_suffix(".wasm")))[0])

    print("\n[4] break the file, recompile, see diagnostics")
    t_str_replace(str(src), "i.to_string()", "i.to_string(}")
    print("  " + t_compile_rust_to_wasm(str(src))[0].splitlines()[0])

    print("\n[5] fix + add args echo, recompile, run with args")
    t_str_replace(str(src), "i.to_string(}", "i.to_string()")
    t_str_replace(str(src), "fn main() {", "fn main() {\n    for a in std::env::args().skip(1) { println!(\"arg: {a}\"); }")
    print("  " + t_compile_rust_to_wasm(str(src))[0])
    print("  " + t_wasmtime_exec(str(src.with_suffix(".wasm")), args="hello wasm world")[0])

    print("\n[6] WASI file I/O — preopen the folder, read and write files")
    io_src = work / "io.rs"
    io_code = """\
fn main() {
    let who = std::fs::read_to_string("/demo/in.txt")
        .unwrap_or_else(|_| "wasm".into());
    println!("hello, {who}!");
    std::fs::write("/demo/out.txt", format!("goodbye, {who}.")).unwrap();
    println!("wrote /demo/out.txt");
}
"""
    t_write(str(io_src), io_code)
    (work / "in.txt").write_text("wasi")
    print("  " + t_compile_rust_to_wasm(str(io_src))[0])
    print("  " + t_wasmtime_exec(str(io_src.with_suffix(".wasm")),
                                dirs=f"{work}::/demo")[0])
    print("  /demo/out.txt now reads: " + (work / "out.txt").read_text().strip())

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 64)
    print("  Demo complete — compile_rust_to_wasm + wasmtime_exec work.")
    print("=" * 64)


# ── CLI ───────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_SUPPLEMENT = (
    "You are a Rust/Wasm coding agent. You have file tools plus exactly two "
    "execution tools: compile_rust_to_wasm (rustc, wasm32-wasip1) and "
    "wasmtime_exec. There is no shell — to run or test anything, write Rust, "
    "compile it to wasm, and execute it with wasmtime. Write programs as "
    "self-contained single .rs files with a main() that prints its results. "
    "The module has no filesystem by default: pass wasmtime_exec dirs= to "
    "preopen host directories (e.g. \"somedir::/work\" preopens somedir at "
    "guest path /work) when the program needs to read or write files. "
    "For external crates: cargo_search to find one, cargo_install to vendor "
    "its source into deps/<crate>/, then pull the modules into your program "
    "with #[path = \"deps/<crate>/src/<file>.rs\"] mod ...; (rustc compiles "
    "them together; no cargo, no build scripts — prefer small dependency-free "
    "crates)."
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wasm-only coding agent: read/write tools + compile_rust_to_wasm + wasmtime_exec."
    )
    parser.add_argument("task", nargs="*", help="Task to run (omit for demo)")
    parser.add_argument("--demo", action="store_true", help="Run the self-contained demo (no API key)")
    parser.add_argument("--model", default="glm-5.3", help="Model ID (default: glm-5.3)")
    parser.add_argument("--endpoint", default="https://api.z.ai/api/coding/paas/v4",
                        help="API endpoint (default: z.ai coding endpoint)")
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


if __name__ == "__main__":
    main()
