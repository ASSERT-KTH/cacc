#!/usr/bin/env python3
"""Rollout agent — one step of RL post-training, minus the gradient.

N copies of the blind agent (read/write, no execution) attempt the same task
in parallel, each in its own copy of the project. The agents never run
anything; the *harness* then executes the tests on every attempt and turns
the outcome into a reward: 1 if the tests pass, 0 otherwise.

That reward vector is exactly what RL post-training (RLVR) feeds back into
the weights. Here it is only printed.

Usage:
    rollout_agent.py PROJECT_DIR [-n N] [--model M] [--task T] [--test CMD]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOLD, DIM, GREEN, RED, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def rollout(i: int, project: Path, model: str, task: str, test: str) -> dict:
    """Run one blind-agent attempt in a fresh copy, then score it by execution."""
    work = Path(tempfile.mkdtemp(prefix=f"rollout-{i}-"))
    shutil.copytree(project, work, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    before = {p: p.read_text() for p in work.glob("*.py")}
    agent = subprocess.run(
        [sys.executable, str(HERE / "blind_agent.py"), model, task],
        cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    tests = subprocess.run(test, shell=True, cwd=work, capture_output=True, text=True)
    changed = [
        line.strip()
        for p, old in before.items()
        for line in p.read_text().splitlines()
        if line not in old.splitlines() and line.strip()
    ]
    return {"i": i, "work": work, "reward": int(tests.returncode == 0),
            "changed": changed, "agent_rc": agent.returncode}


def main() -> None:
    p = argparse.ArgumentParser(description="Rollout agent: N blind attempts, rewarded by execution.")
    p.add_argument("project", type=Path)
    p.add_argument("-n", type=int, default=4, help="number of rollouts (default 4)")
    p.add_argument("--model", default="run:///home/martin/bin/best-effort-completions.py")
    p.add_argument("--task", default="the test fails, fix the bug")
    p.add_argument("--test", default="python3 -m pytest -q -p no:cacheprovider")
    args = p.parse_args()

    print(f"{BOLD}{args.n} rollouts in parallel, no execution allowed to the agents...{OFF}")
    with ThreadPoolExecutor(max_workers=args.n) as pool:
        results = list(pool.map(
            lambda i: rollout(i, args.project.resolve(), args.model, args.task, args.test),
            range(1, args.n + 1)))

    print(f"\n{BOLD}the harness executes the tests: reward = tests pass{OFF}")
    for r in results:
        colour = GREEN if r["reward"] else RED
        edit = r["changed"][0] if r["changed"] else "(no change)"
        print(f"  rollout {r['i']}  {colour}reward {r['reward']}{OFF}  {DIM}{edit}{OFF}")
    rewards = [r["reward"] for r in results]
    print(f"\n{BOLD}rewards {rewards}  mean {sum(rewards) / len(rewards):.2f}{OFF}")
    if len(set(rewards)) == 1:
        print("all rewards equal: advantage = 0, nothing to learn from this task.")
        print("RL post-training needs tasks the model solves only sometimes.")
    else:
        print("in RL post-training, this vector updates the weights.")


if __name__ == "__main__":
    main()
