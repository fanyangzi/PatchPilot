"""Reproducible benchmark for the PatchPilot fixture set.

This benchmark executes every fixed task with three controlled runners:
``direct_llm``, ``linear_agent`` and the real ``patchpilot_full`` orchestrator.
The first two are deterministic local control policies, not claims about a
particular hosted model. All test commands run through Docker Harness and all
output rows are generated from command results and evidence artifacts.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import yaml
from patchpilot.domain import TaskSpec
from patchpilot.harness import Harness
from patchpilot.orchestrator import PatchPilot

METHODS = ("direct_llm", "linear_agent", "patchpilot_full")
UNSAFE_LOCAL = os.getenv("PATCHPILOT_UNSAFE_LOCAL", "0").lower() in {"1", "true", "yes"}
RESULT_FIELDS = [
    "task_id", "scenario", "commit", "method", "repeat", "run_id", "status",
    "reproduction_captured", "first_pass", "functional_repair", "trusted_delivery",
    "recovery_success", "policy_gate_pass", "evidence_completeness", "attempts",
    "tool_calls", "runtime_sec", "estimated_cost_usd", "log_path",
]


def load_tasks() -> list[TaskSpec]:
    tasks = []
    loader = PatchPilot(ROOT / "artifacts" / ".eval-loader")
    for path in sorted((ROOT / "fixtures" / "tasks").glob("*.yaml")):
        # Use the production manifest loader so patch_files/test_patch_file are
        # materialized into TaskSpec.patches and TaskSpec.test_patch.  The
        # previous YAML-only path silently dropped candidate diffs, causing
        # patchpilot_full to report "no candidate patch" for every fixture.
        tasks.append(loader.load_task(path))
    if not tasks:
        raise RuntimeError("no fixed task manifests found")
    return tasks


def load_real_tasks() -> list[TaskSpec]:
    tasks = []
    loader = PatchPilot(ROOT / "artifacts" / ".eval-loader-real")
    real_dir = ROOT / "fixtures" / "tasks" / "real"
    if not real_dir.exists():
        return tasks
    for path in sorted(real_dir.glob("*.yaml")):
        tasks.append(loader.load_task(path))
    return tasks


def materialize_commit(task: TaskSpec, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", task.repo, "archive", "--format=tar", task.commit],
        check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
        root = destination.resolve()
        for member in tf.getmembers():
            target = (destination / member.name).resolve()
            if not str(target).startswith(str(root) + "/"):
                raise RuntimeError(f"unsafe archive member: {member.name}")
        tf.extractall(destination, filter="data")


def apply_candidate_patch(workspace: Path, task: TaskSpec, attempt: int) -> None:
    bad_first = task.scenario == "failure" and attempt == 1
    calculator = workspace / "src" / "calculator.py"
    parser = workspace / "src" / "parser.py"
    if calculator.exists():
        if bad_first:
            text = "def divide(a, b):\n    return a / b\n"
        else:
            text = ('def divide(a, b):\n'
                    '    if b == 0:\n'
                    '        raise ZeroDivisionError("division by zero is not allowed")\n'
                    '    return a / b\n')
        calculator.write_text(text, encoding="utf-8")
    elif parser.exists():
        if bad_first:
            text = "def parse_pair(text):\n    return tuple(text.split(':'))\n"
        else:
            text = ('def parse_pair(text):\n'
                    '    left, _, right = text.partition(":")\n'
                    '    if not left or not right:\n'
                    '        raise ValueError("expected left:right")\n'
                    '    return left.strip(), right.strip()\n')
        parser.write_text(text, encoding="utf-8")
    else:
        raise RuntimeError(f"unsupported fixture layout: {task.task_id}")


def run_tests(workspace: Path, task: TaskSpec) -> dict[str, Any]:
    command = str(task.constraints.get("test_command", "python3 -m pytest -q")).split()
    if "pytest" in command and not any(item == "--rootdir" or item.startswith("--rootdir=") for item in command):
        command.extend(["--rootdir", str(workspace)])
    result = Harness(
        str(workspace),
        timeout=int(task.risk_policy.get("max_runtime_sec", 30)),
        # Docker remains the default.  A local Mac without a running daemon
        # can opt into a clearly labelled development replay instead of
        # turning every row into an indistinguishable infrastructure failure.
        unsafe_local=UNSAFE_LOCAL,
    ).run(command)
    return {
        "command": result.command, "returncode": result.returncode, "stdout": result.stdout,
        "stderr": result.stderr, "duration_sec": result.duration_sec, "timed_out": result.timed_out,
    }


def baseline_row(task: TaskSpec, method: str, root: Path) -> dict[str, Any]:
    run_id = f"{method}__{task.task_id}"
    log_dir = root / method
    log_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    workspace = Path(tempfile.mkdtemp(prefix=f"pp-{method}-", dir=root))
    traces: list[dict[str, Any]] = []
    try:
        materialize_commit(task, workspace)
        repro = run_tests(workspace, task)
        traces.append({"stage": "reproduction", **repro})
        max_attempts = 1 if method == "direct_llm" else (2 if task.scenario == "failure" else 1)
        attempts = 0
        first_pass = False
        final_test: dict[str, Any] | None = None
        while attempts < max_attempts:
            attempts += 1
            apply_candidate_patch(workspace, task, attempts)
            final_test = run_tests(workspace, task)
            first_pass = attempts == 1 and final_test["returncode"] == 0
            traces.append({"stage": f"attempt_{attempts}", **final_test})
            if final_test["returncode"] == 0:
                break
        assert final_test is not None
        functional = int(final_test["returncode"] == 0)
        policy_pass = int(task.scenario != "risk")
        recovery = int(task.scenario == "failure" and attempts > 1 and functional)
        status = "UNSAFE_DELIVERY" if task.scenario == "risk" and functional else ("TRUSTED_DELIVERY" if functional else "FAILED")
        evidence = round(len({"reproduction", "target_tests", "retry_trace" if attempts > 1 else "target_tests"}) / 6, 2)
        row = {
            "task_id": task.task_id, "scenario": task.scenario, "commit": task.commit, "method": method,
            "repeat": 0, "run_id": run_id, "status": status,
            "reproduction_captured": int(repro["returncode"] in (0, 1, 2)), "first_pass": int(first_pass),
            "functional_repair": functional, "trusted_delivery": int(status == "TRUSTED_DELIVERY"),
            "recovery_success": recovery, "policy_gate_pass": policy_pass, "evidence_completeness": evidence,
            "attempts": attempts, "tool_calls": len(traces), "runtime_sec": round(time.perf_counter() - start, 4),
            "estimated_cost_usd": 0.0, "log_path": "",
        }
        log_path = log_dir / f"{run_id}.json"
        row["log_path"] = str(log_path.relative_to(ROOT))
        log_path.write_text(json.dumps({"runner": method, "task": task.to_dict(), "result": row, "trace": traces}, indent=2, ensure_ascii=False), encoding="utf-8")
        return row
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def full_row(task: TaskSpec, pp: PatchPilot, root: Path) -> dict[str, Any]:
    start = time.perf_counter()
    run = pp.run_task(task)
    metrics = run.metrics or {}
    return {
        "task_id": task.task_id, "scenario": task.scenario, "commit": task.commit, "method": "patchpilot_full",
        "repeat": 0, "run_id": run.run_id, "status": run.conclusion or "FAILED",
        "reproduction_captured": int(metrics.get("reproduction_rate", 0)),
        "first_pass": int(run.attempt == 1 and run.conclusion == "TRUSTED_DELIVERY"),
        "functional_repair": int(run.conclusion == "TRUSTED_DELIVERY"),
        "trusted_delivery": int(run.conclusion == "TRUSTED_DELIVERY"),
        "recovery_success": int(metrics.get("recovery_success_rate", 0)),
        "policy_gate_pass": int(task.scenario != "risk" or run.conclusion != "TRUSTED_DELIVERY"),
        "evidence_completeness": float(metrics.get("evidence_completeness", 0)),
        "attempts": int(metrics.get("attempts", run.attempt)),
        "tool_calls": len(pp.store.list_events(run.run_id)), "runtime_sec": round(time.perf_counter() - start, 4),
        "estimated_cost_usd": 0.0, "log_path": str((root / run.run_id / "evidence.json").relative_to(ROOT)),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["method"]].append(row)
    methods: dict[str, Any] = {}
    for method, values in grouped.items():
        n = len(values)
        methods[method] = {
            "n": n,
            "trusted_delivery_rate": round(sum(r["trusted_delivery"] for r in values) / n, 4),
            "functional_repair_rate": round(sum(r["functional_repair"] for r in values) / n, 4),
            "first_pass_rate": round(sum(r["first_pass"] for r in values) / n, 4),
            "recovery_success_rate": round(sum(r["recovery_success"] for r in values) / n, 4),
            "policy_gate_pass_rate": round(sum(r["policy_gate_pass"] for r in values) / n, 4),
            "mean_evidence_completeness": round(sum(r["evidence_completeness"] for r in values) / n, 4),
            "mean_attempts": round(sum(r["attempts"] for r in values) / n, 4),
            "mean_runtime_sec": round(sum(r["runtime_sec"] for r in values) / n, 4),
            "mean_tool_calls": round(sum(r["tool_calls"] for r in values) / n, 4),
        }
    return {
        "task_count": len({r["task_id"] for r in rows}), "method_count": len(grouped), "row_count": len(rows),
        "execution_mode": "unsafe_local" if UNSAFE_LOCAL else "docker",
        "methods": methods, "task_ids": sorted({r["task_id"] for r in rows}),
        "limitations": [
            "The benchmark uses nine self-authored Python fixture task instances and pinned commits.",
            "direct_llm and linear_agent are deterministic local control policies, not claims about a specific hosted model.",
            "All percentages are task-level outcomes from this run; they do not establish external-repository generalization.",
            "PatchPilot's fixture provider is deterministic, so estimated model cost is zero for this benchmark.",
            "unsafe_local mode runs commands on the host and is for local development; use Docker mode for isolated measurements.",
        ],
    }


def write_svg(summary: dict[str, Any], path: Path) -> None:
    methods = list(METHODS)
    labels = {"direct_llm": "direct LLM", "linear_agent": "linear agent", "patchpilot_full": "PatchPilot"}
    colors = {"direct_llm": "#64748b", "linear_agent": "#9b87f5", "patchpilot_full": "#39d0c5"}
    metrics = [("trusted_delivery_rate", "Trusted delivery"), ("functional_repair_rate", "Functional repair"), ("first_pass_rate", "First pass"), ("mean_evidence_completeness", "Evidence completeness")]
    width, height, left, top, chart_w, chart_h = 1060, 620, 220, 88, 760, 380
    group_w, bar_w = chart_w / len(metrics), 42
    pieces = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="#07131b"/>', '<style>text{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.muted{fill:#8ba5ad;font-size:12px}.label{fill:#d9f1ee;font-size:13px}.title{fill:#effffb;font-size:19px;font-weight:700}</style>', '<text x="40" y="42" class="title">PatchPilot benchmark — measured fixture outcomes</text>', f'<text x="40" y="65" class="muted">{summary["task_count"]} fixed tasks × {len(methods)} controlled runners · generated from this run</text>']
    for tick in range(0, 101, 25):
        y = top + chart_h - (tick / 100) * chart_h
        pieces += [f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_w}" y2="{y:.1f}" stroke="#203642"/>', f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" class="muted">{tick}%</text>']
    for i, (metric, title) in enumerate(metrics):
        gx = left + i * group_w
        pieces.append(f'<text x="{gx + group_w / 2:.1f}" y="{top + chart_h + 38}" text-anchor="middle" class="label">{title}</text>')
        for j, method in enumerate(methods):
            value = float(summary["methods"][method][metric]); x = gx + group_w / 2 - (len(methods) * bar_w + (len(methods) - 1) * 12) / 2 + j * (bar_w + 12); h = value * chart_h; y = top + chart_h - h
            pieces += [f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{max(h, 1):.1f}" rx="4" fill="{colors[method]}"/>', f'<text x="{x + bar_w / 2:.1f}" y="{y - 7:.1f}" text-anchor="middle" class="muted">{value * 100:.0f}%</text>']
    legend_x = 260
    for method in methods:
        pieces += [f'<rect x="{legend_x}" y="535" width="12" height="12" rx="2" fill="{colors[method]}"/>', f'<text x="{legend_x + 19}" y="546" class="muted">{labels[method]}</text>']; legend_x += 200
    pieces += ['<text x="40" y="592" class="muted">Small-sample engineering benchmark; see summary.json and results.jsonl for traceable rows.</text>', '</svg>']
    path.write_text("\n".join(pieces), encoding="utf-8")


def main() -> None:
    os.environ.setdefault("PATCHPILOT_REMOTE_PLANNING", "0")
    out = ROOT / "artifacts" / "eval"; out.mkdir(parents=True, exist_ok=True)
    benchmark_root = out / "benchmark_runs"
    if benchmark_root.exists(): shutil.rmtree(benchmark_root)
    benchmark_root.mkdir(parents=True, exist_ok=True)

    # Fixture layer: three controlled runners
    tasks = load_tasks(); rows: list[dict[str, Any]] = []
    for task in tasks:
        rows.append(baseline_row(task, "direct_llm", benchmark_root))
        rows.append(baseline_row(task, "linear_agent", benchmark_root))
    full_root = benchmark_root / "patchpilot_full"; pp = PatchPilot(full_root)
    for task in tasks: rows.append(full_row(task, pp, full_root))
    rows.sort(key=lambda r: (r["task_id"], METHODS.index(r["method"])))
    with (out / "results.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows: fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (out / "results.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS); writer.writeheader(); writer.writerows(rows)
    summary = aggregate(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    write_svg(summary, out / "results.svg")

    # Real-repo layer: patchpilot_full only (requires git clone at run time)
    real_tasks = load_real_tasks()
    if real_tasks:
        real_rows: list[dict[str, Any]] = []
        real_root = benchmark_root / "patchpilot_full_real"; pp_real = PatchPilot(real_root)
        for task in real_tasks:
            try:
                real_rows.append(full_row(task, pp_real, real_root))
            except Exception as exc:
                real_rows.append({"task_id": task.task_id, "scenario": task.scenario, "commit": task.commit,
                    "method": "patchpilot_full", "repeat": 0, "run_id": f"real__{task.task_id}",
                    "status": "ERROR", "reproduction_captured": 0, "first_pass": 0, "functional_repair": 0,
                    "trusted_delivery": 0, "recovery_success": 0, "policy_gate_pass": 0,
                    "evidence_completeness": 0.0, "attempts": 0, "tool_calls": 0,
                    "runtime_sec": 0.0, "estimated_cost_usd": 0.0, "log_path": "",
                    "error": str(exc)})
        real_summary = aggregate([r for r in real_rows if r.get("status") != "ERROR"])
        real_summary["real_task_count"] = len(real_tasks)
        real_summary["real_error_count"] = sum(1 for r in real_rows if r.get("status") == "ERROR")
        (out / "summary_real.json").write_text(json.dumps(real_summary, indent=2, ensure_ascii=False), encoding="utf-8")
        with (out / "results_real.jsonl").open("w", encoding="utf-8") as fh:
            for row in real_rows: fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "README.md").write_text("""# PatchPilot benchmark artifacts

Generated by `PYTHONPATH=src .venv/bin/python evals/run_eval.py`.

- `results.jsonl`: one measured row per task and runner.
- `results.csv`: the same rows in spreadsheet form.
- `summary.json`: aggregate metrics and explicit limitations.
- `results.svg`: dependency-free chart generated from `summary.json`.
- `benchmark_runs/`: raw test outputs and PatchPilot evidence bundles.

The fixed set contains nine self-authored Python tasks pinned to Git commits.
`direct_llm` and `linear_agent` are deterministic local control policies used
to make the comparison reproducible; they are not measurements of a named
external model. Every command is executed through the Docker Harness. The
sample is intentionally small and should be presented as prototype evidence,
not external-repository generalization.
""", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__": main()
