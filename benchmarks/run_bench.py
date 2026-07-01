"""Benchmark runner for InsightAgent capability probes.

Runs each task in benchmarks/tasks.json by shelling out to the real
`insightagent.cli.run_task` (so it exercises the exact production path: config,
sessions, MCP, tools), supports multi-turn session resume, seeds files, and
collects metrics from the per-turn JSONL traces.

Usage:
  python3 benchmarks/run_bench.py --only 05_kvstore_longtraj,08_leapyear_logicbug
  python3 benchmarks/run_bench.py --list
  python3 benchmarks/run_bench.py            # run all
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS_FILE = REPO / "benchmarks" / "tasks.json"
RUNS_DIR = REPO / "benchmarks" / "runs"
TRACE_DIR = REPO / "reports" / "bench"
RESULTS_FILE = REPO / "benchmarks" / "results.json"

PROVIDER = "siliconflow"
MODEL = "Qwen/Qwen2.5-72B-Instruct"


def load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))


def setup_workspace(task: dict) -> Path:
    ws = RUNS_DIR / task["id"]
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    if task.get("copy_src"):
        shutil.copytree(REPO / "src" / "insightagent", ws / "src" / "insightagent")
    for filename, content in (task.get("seed") or {}).items():
        target = ws / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return ws


def run_turn(task: dict, ws: Path, turn: dict, turn_index: int, session_id: str | None) -> tuple[Path, int]:
    trace = TRACE_DIR / f"{task['id']}_t{turn_index}.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "insightagent.cli.run_task",
        "--provider", PROVIDER, "--model", MODEL,
        "--language", "中文",
        "--tool-profile", task["profile"],
        "--workspace", str(ws.relative_to(REPO)),
        "--permission-mode", task["permission"],
        "--no-trace",
        "--max-tool-iterations", str(task["max_iters"]),
        "--max-wall-seconds", str(task["max_wall"]),
        "--trace-jsonl", str(trace),
        "--task", turn["prompt"],
    ]
    if turn.get("allow_no_tool"):
        cmd.append("--allow-no-tool-final")
    if task.get("protect"):
        cmd += ["--protect-paths", ",".join(task["protect"])]
    if session_id:
        cmd += ["--session-id", session_id]
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=task["max_wall"] + 120)
    if proc.returncode != 0:
        err_file = TRACE_DIR / f"{task['id']}_t{turn_index}_stderr.txt"
        err_file.write_text((proc.stdout or "") + "\n----STDERR----\n" + (proc.stderr or ""), encoding="utf-8")
    return trace, proc.returncode


def parse_session_id(trace: Path) -> str | None:
    if not trace.is_file():
        return None
    session_id = None
    for line in trace.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "session_started":
            session_id = event.get("session_id")  # keep the last (current run)
    return session_id


def summarize_trace(trace: Path) -> dict:
    if not trace.is_file():
        return {"missing_trace": True}
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    model_calls = sum(1 for e in events if e.get("type") == "model_request")
    tool_calls = Counter(e.get("name") for e in events if e.get("type") == "tool_call")
    repairs = sum(1 for e in events if e.get("type") == "self_healing_repair")
    usage = [e for e in events if e.get("type") == "usage_recorded"]
    verifications = [e for e in events if e.get("type") == "tool_result" and e.get("name") == "run_verification"]
    verify_pass = any(not e.get("is_error", False) for e in verifications)
    budget_exceeded = any(e.get("type") == "time_budget_exceeded" for e in events)
    final_phase = None
    for e in events:
        if e.get("type") == "task_phase_changed":
            final_phase = e.get("phase")
    final = next((e for e in events if e.get("type") == "final_answer"), {})
    return {
        "model_calls": model_calls,
        "iterations": final.get("iterations"),
        "executed_tools": sum(tool_calls.values()) > 0,
        "tool_calls": dict(tool_calls),
        "repairs": repairs,
        "verify_passed": verify_pass,
        "budget_exceeded": budget_exceeded,
        "final_phase": final_phase,
        "total_tokens": sum(e.get("total_tokens_est", 0) for e in usage),
        "cost_usd": round(sum(e.get("cost_usd", 0.0) for e in usage), 6),
    }


def classify_outcome(metrics: dict, return_code: int) -> str:
    if metrics.get("missing_trace"):
        return "no_trace"
    if return_code not in (0,):
        return f"error(exit={return_code})"
    if not metrics["executed_tools"]:
        return "no_execution"
    if metrics["budget_exceeded"]:
        return "timeout_wall"
    phase = metrics["final_phase"]
    if phase == "done":
        return "done"
    if phase == "failed":
        return "failed"
    return phase or "unknown"


def run_task(task: dict) -> dict:
    ws = setup_workspace(task)
    session_id = None
    turn_metrics = []
    last_rc = 0
    for i, turn in enumerate(task["turns"], start=1):
        trace, rc = run_turn(task, ws, turn, i, session_id)
        last_rc = rc
        if session_id is None:
            session_id = parse_session_id(trace)
        turn_metrics.append(summarize_trace(trace))
    final = turn_metrics[-1]
    return {
        "id": task["id"],
        "category": task["category"],
        "turns": len(task["turns"]),
        "outcome": classify_outcome(final, last_rc),
        "executed_tools": final.get("executed_tools"),
        "verify_passed": final.get("verify_passed"),
        "model_calls": [m.get("model_calls") for m in turn_metrics],
        "tool_calls_final": final.get("tool_calls"),
        "repairs_final": final.get("repairs"),
        "total_tokens_final": final.get("total_tokens"),
        "cost_usd_final": final.get("cost_usd"),
        "eval_note": task.get("eval", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Comma-separated task ids to run.")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    tasks = load_tasks()
    if args.list:
        for task in tasks:
            print(f"{task['id']:32s} {task['category']:22s} profile={task['profile']}")
        return
    if args.only:
        wanted = {name.strip() for name in args.only.split(",")}
        tasks = [task for task in tasks if task["id"] in wanted]
    results = []
    for task in tasks:
        print(f">>> running {task['id']} ...", flush=True)
        result = run_task(task)
        results.append(result)
        print(f"    outcome={result['outcome']} verify={result['verify_passed']} "
              f"tools={result['tool_calls_final']}", flush=True)
    RESULTS_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n=== RESULTS ===")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
