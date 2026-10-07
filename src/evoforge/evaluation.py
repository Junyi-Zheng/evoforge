from __future__ import annotations

import math
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from .benchmarks import safe_path
from .budget import BudgetExceeded
from .models import Case, Config, Evaluation, Task, TaskResult, Usage, canonical
from .providers import ProviderFailure
from .sandbox import RuntimeFailure, Sandbox

ACTION_CONTRACT = {
    "list_files": "List task source paths.",
    "read_file": "path: normalized relative task path.",
    "search": "query: literal substring, max 200 characters. Returns matching lines.",
    "write_file": "path and content: replace the editable implementation file, max 48 KB.",
    "run_tests": "Run public examples; no private tests are exposed.",
    "ask_model": "request: {system: text, payload: object}; response is the next pending_action.",
    "finish": "Stop the episode; external evaluation follows.",
}


def equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or expected is None:
        return type(actual) is type(expected) and actual == expected
    if isinstance(expected, (int, float)):
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(equal(a, e) for a, e in zip(actual, expected)))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(equal(actual[k], expected[k]) for k in expected))
    return type(actual) is type(expected) and actual == expected


class Evaluator:
    def __init__(self, config: Config, sandbox: Sandbox, provider):
        self.config, self.sandbox, self.provider = config, sandbox, provider

    def grade(self, workspace: Path, task: Task, cases: List[Case], timeout=None) -> Dict[str, Any]:
        # Only arguments cross the boundary. Expected values remain in this process.
        outputs = self.sandbox.invoke("solution", workspace, {
            "relative_path": task.target, "symbol": task.symbol,
            "cases": [{"args": c.args, "kwargs": c.kwargs} for c in cases]}, timeout=timeout)
        if not isinstance(outputs, list) or len(outputs) != len(cases):
            raise RuntimeFailure("Submission returned an invalid result vector")
        checks = []
        for case, output in zip(cases, outputs):
            valid = isinstance(output, dict) and set(output) == {"value", "error", "args_after", "kwargs_after"}
            unchanged = valid and (not case.check_input_unchanged or (
                canonical(output["args_after"]) == canonical(case.args)
                and canonical(output["kwargs_after"]) == canonical(case.kwargs)))
            checks.append(unchanged and (output["error"] == case.error
                                     if case.error else not output["error"] and equal(output["value"], case.expected)))
        return {"all_passed": all(checks), "passed": sum(checks), "total": len(checks)}

    def evaluate(self, genome_dir: Path, tasks: List[Task], split: str, label: str) -> Evaluation:
        results = []
        for task in tasks:
            if task.split != split:
                continue
            for repetition in range(self.config.repetitions):
                results.append(self.episode(genome_dir, task, repetition, label))
        if not results:
            raise ValueError("No tasks in evaluation split: " + split)
        return Evaluation(split, results)

    def episode(self, genome_dir: Path, task: Task, repetition: int, label: str) -> TaskResult:
        from .models import load_json
        usage, trace, state = Usage(), [], {}
        started, steps, reason = time.monotonic(), 0, "step_budget"
        last, pending = {}, None
        policy = load_json(genome_dir / "policy.json")
        skills = load_json(genome_dir / "skills.json")
        public_passed, passed = False, False
        with tempfile.TemporaryDirectory(prefix="evoforge-task-") as directory:
            workspace = Path(directory)
            for name, text in task.files.items():
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            try:
                for steps in range(1, self.config.limits.steps + 1):
                    remaining = self.config.limits.seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        reason = "time_budget"
                        break
                    observation = {"task": task.visible(), "state": state, "trace": trace,
                                   "last": last, "pending_action": pending, "policy": policy,
                                   "skills": skills, "repetition": repetition,
                                   "remaining": {"steps": self.config.limits.steps - steps,
                                                 "calls": self.config.limits.calls - usage.calls}}
                    decision = self.sandbox.invoke("step", genome_dir, observation,
                                                   timeout=min(15.0, remaining))
                    if (not isinstance(decision, dict) or set(decision) != {"action", "state"}
                            or not isinstance(decision["state"], dict)
                            or len(canonical(decision["state"]).encode()) > 20000):
                        raise RuntimeFailure("Invalid agent decision/state")
                    state, action, pending = decision["state"], decision["action"], None
                    if not isinstance(action, dict) or action.get("tool") not in ACTION_CONTRACT:
                        raise RuntimeFailure("Unknown action")
                    tool = action["tool"]
                    if tool == "finish":
                        reason = "finished"
                        trace.append({"step": steps, "action": action, "result": {"finished": True}})
                        break
                    if tool == "ask_model":
                        request = action.get("request")
                        if usage.calls >= self.config.limits.calls:
                            reason = "call_budget"
                            break
                        input_bound = len(canonical(request).encode()) + 1024
                        if usage.tokens + input_bound + self.config.limits.output_tokens > self.config.limits.tokens:
                            reason = "token_budget"
                            break
                        before = self.provider.ledger.usage.tokens
                        try:
                            remaining = self.config.limits.seconds - (time.monotonic() - started)
                            if remaining <= 0:
                                reason = "time_budget"
                                break
                            reply = self.provider.complete(request, self.config.limits.output_tokens,
                                                           label + "/" + task.id + "/" + str(repetition),
                                                           timeout=remaining)
                        except (ProviderFailure, BudgetExceeded):
                            # Account for malformed/failed responses as episode cost as well.
                            entries = self.provider.ledger.entries
                            if entries and self.provider.ledger.usage.tokens > before:
                                entry = entries[-1]
                                usage.add(Usage(entry["calls"], entry["input_tokens"],
                                                entry["output_tokens"], entry["estimated"]))
                            raise
                        usage.add(reply.usage)
                        pending = reply.value
                        last = {"model_response": reply.value}
                        if usage.tokens > self.config.limits.tokens:
                            reason = "token_budget"
                            break
                    else:
                        remaining = self.config.limits.seconds - (time.monotonic() - started)
                        if remaining <= 0:
                            reason = "time_budget"
                            break
                        last = self.tool(action, workspace, task, min(15.0, remaining))
                    trace.append({"step": steps, "action": action, "result": last})
                    if len(canonical(trace).encode()) > 100000:
                        reason = "trace_budget"
                        break
                if time.monotonic() - started > self.config.limits.seconds:
                    reason = "time_budget"
                # External grading has its own runtime deadline and cannot be edited by the agent.
                public = self.grade(workspace, task, task.public)
                public_passed = public["all_passed"]
                private = self.grade(workspace, task, task.private)
                passed = public_passed and private["all_passed"]
                if reason in {"time_budget", "call_budget", "token_budget", "trace_budget"}:
                    passed = False
                if not passed and reason == "finished":
                    reason = "behavior_failed"
            except (RuntimeFailure, ProviderFailure, ValueError, KeyError, TypeError) as exc:
                reason = type(exc).__name__ + ": " + str(exc)[:1000]
            # Run-level exhaustion must stop the controller, rather than scoring missing tasks as zero.
            except BudgetExceeded:
                raise
        return TaskResult(task.id, task.family, task.split, repetition, passed, public_passed,
                          steps, time.monotonic() - started, usage, reason,
                          trace if task.split == "train" else [])

    def tool(self, action: Dict[str, Any], workspace: Path, task: Task, timeout: float) -> Dict[str, Any]:
        tool = action["tool"]
        if tool == "list_files":
            return {"files": sorted(task.files)}
        if tool in {"read_file", "write_file"}:
            name = action.get("path", "")
            if not safe_path(name) or name not in task.files:
                raise RuntimeFailure("Unknown task source path")
            path = workspace / name
            if tool == "read_file":
                return {"path": name, "content": path.read_text(encoding="utf-8")}
            content = action.get("content")
            if name != task.target or not isinstance(content, str) or len(content.encode()) > 48000:
                raise RuntimeFailure("Only the implementation target can be edited (max 48 KB)")
            path.write_text(content, encoding="utf-8")
            return {"written": name}
        if tool == "search":
            query = action.get("query")
            if not isinstance(query, str) or not 1 <= len(query) <= 200:
                raise RuntimeFailure("Search requires a literal query of 1–200 characters")
            matches = []
            for name in sorted(task.files):
                for line, text in enumerate((workspace / name).read_text(encoding="utf-8").splitlines(), 1):
                    if query in text:
                        matches.append({"path": name, "line": line, "text": text[:400]})
            return {"matches": matches[:50]}
        if tool == "run_tests":
            return self.grade(workspace, task, task.public, timeout=timeout)
        raise RuntimeFailure("Unsupported host tool")
