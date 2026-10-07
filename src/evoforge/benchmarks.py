from __future__ import annotations

import random
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from .models import Case, Task, digest, load_json


def safe_path(name: str) -> bool:
    if not isinstance(name, str):
        return False
    p = PurePosixPath(name)
    return (bool(name) and bool(p.parts) and not p.is_absolute()
            and ".." not in p.parts and "\\" not in name and str(p) == name
            and not any(ord(c) < 32 for c in name)
            and all(not x.startswith(".") for x in p.parts))


def validate_suite(tasks: List[Task]) -> List[Task]:
    if not tasks or len(tasks) > 10000:
        raise ValueError("A suite must contain 1–10000 tasks")
    ids, signatures = set(), set()
    for task in tasks:
        if task.id in ids or task.split not in {"train", "validation", "test"}:
            raise ValueError("Duplicate task ID or invalid split")
        ids.add(task.id)
        if (not task.id or len(task.id) > 120 or not task.family or not task.instruction
                or not task.symbol.isidentifier() or task.symbol.startswith("_")):
            raise ValueError("Task metadata is incomplete")
        if task.target not in task.files or not task.target.endswith(".py"):
            raise ValueError("Target must be an existing Python source file")
        if not all(safe_path(n) and isinstance(v, str) for n, v in task.files.items()):
            raise ValueError("Task file paths must be normalized relative paths")
        if len(task.files) > 100 or sum(len(v.encode()) for v in task.files.values()) > 200000:
            raise ValueError("Task exceeds fixture limits")
        if not task.public or not task.private or len(task.public) + len(task.private) > 200:
            raise ValueError("Each task needs public and private cases (max 200 total)")
        for case in task.public + task.private:
            if (not isinstance(case.args, list) or not isinstance(case.kwargs, dict)
                    or not isinstance(case.error, str) or type(case.check_input_unchanged) is not bool):
                raise ValueError("Cases require args=list, kwargs=object, error=text and a boolean input check")
        signature = digest({"instruction": task.instruction, "files": task.files,
                            "public": [asdict(c) for c in task.public],
                            "private": [asdict(c) for c in task.private]})
        if signature in signatures:
            raise ValueError("Exact duplicate task across the suite")
        signatures.add(signature)
    if {t.split for t in tasks} != {"train", "validation", "test"}:
        raise ValueError("The suite must provide train, validation and test splits")
    return tasks


def load_suite(path: Path) -> List[Task]:
    raw = load_json(path)
    if not isinstance(raw, dict) or set(raw) != {"version", "tasks"} or raw["version"] != 1:
        raise ValueError("Suite needs version=1 and a tasks array")
    tasks = []
    for record in raw["tasks"]:
        item = dict(record)
        item["public"] = [Case(**c) for c in item["public"]]
        item["private"] = [Case(**c) for c in item["private"]]
        tasks.append(Task(**item))
    return validate_suite(tasks)


CONTRACTS = {
    "intervals": ("merge_intervals", "intervals", "Merge overlapping OR touching [start, end] intervals. "
                  "Sort by start, do not mutate the input, and return an empty list for no intervals. "
                  "Assume each start <= end."),
    "chunks": ("chunked", "items, size", "Split a list into consecutive chunks of at most size items. "
               "Keep the remainder and preserve order. Raise ValueError for size <= 0, even for empty items."),
    "slug": ("slugify", "text", "Lowercase ASCII letters and digits; replace each run of other "
             "characters with a single hyphen and strip leading/trailing hyphens. Empty input returns ''."),
    "averages": ("moving_average", "values, window", "Return averages of every complete sliding window. "
                 "Raise ValueError for window <= 0, even with no values; return [] if window exceeds length. "
                 "Do not mutate the input."),
}


def builtin_suite(seed: int = 17) -> List[Task]:
    rng = random.Random(seed)
    tasks = []
    for split_index, split in enumerate(("train", "validation", "test")):
        for family_index, (family, (symbol, arguments, contract)) in enumerate(CONTRACTS.items()):
            for variant in range(2):
                nested = (family_index + variant) % 2 == 1
                padded = (family_index + split_index) % 2 == 1
                target = "src/solution.py" if nested else "solution.py"
                prefix = "# Implementation notes: preserve the documented boundary behavior.\n" * (24 if padded else 1)
                source = prefix + "def " + symbol + "(" + arguments + "):\n    return None\n"
                files = {target: source}
                if nested:
                    files["docs/walkthrough.py"] = "# A usage example; the implementation lives in src.\nTITLE = 'Walkthrough'\n"
                public, private = _cases(family, rng, split_index * 2 + variant)
                tasks.append(Task(id=split + "-" + family + "-" + str(variant), split=split,
                                  family=family, instruction="Repair the existing " + symbol + " function. " + contract,
                                  files=files, target=target, symbol=symbol, public=public, private=private))
    return validate_suite(tasks)


def _cases(family: str, rng: random.Random, offset: int):
    if family == "intervals":
        public = [Case([[[1, 3], [2, 5]]], [[1, 5]])]
        private = [Case([[]], []), Case([[[3, 4], [1, 3], [7, 8]]], [[1, 4], [7, 8]]),
                   Case([[[2, 2], [2, 2]]], [[2, 2]])]
        for _ in range(5):
            intervals = [[a, a + rng.randint(0, 6)] for a in [rng.randint(-8, 12) + offset for _ in range(7)]]
            expected = []
            for a, b in sorted(intervals):
                if expected and a <= expected[-1][1]:
                    expected[-1][1] = max(b, expected[-1][1])
                else:
                    expected.append([a, b])
            private.append(Case([intervals], expected))
    elif family == "chunks":
        public = [Case([[1, 2, 3, 4, 5], 2], [[1, 2], [3, 4], [5]])]
        private = [Case([[], 3], []), Case([[], 0], None, error="ValueError"),
                   Case([[1, 2], -1], None, error="ValueError")]
        for _ in range(5):
            items, size = [rng.randint(-20, 20) + offset for _ in range(rng.randint(1, 13))], rng.randint(1, 8)
            private.append(Case([items, size], [items[i:i + size] for i in range(0, len(items), size)]))
    elif family == "slug":
        public = [Case(["Hello, World!"], "hello-world")]
        private = [Case([""], ""), Case(["---"], ""), Case(["A__B   C"], "a-b-c"),
                   Case(["Café & Tea"], "caf-tea"), Case(["ABC123"], "abc123")]
        for _ in range(3):
            n = rng.randint(1, 999) + offset
            private.append(Case(["  Release__" + str(n) + "!!! "], "release-" + str(n)))
    else:
        public = [Case([[1, 2, 3, 4], 2], [1.5, 2.5, 3.5])]
        private = [Case([[], 1], []), Case([[], 0], None, error="ValueError"),
                   Case([[1, 2], 3], []), Case([[0, -3, 6], 1], [0.0, -3.0, 6.0])]
        for _ in range(4):
            values, window = [rng.randint(-9, 9) + offset for _ in range(9)], rng.randint(1, 5)
            private.append(Case([values, window], [sum(values[i:i + window]) / window
                                                  for i in range(len(values) - window + 1)]))
    return ([replace(c, check_input_unchanged=True) for c in public],
            [replace(c, check_input_unchanged=True) for c in private])


def suite_dict(tasks: List[Task]) -> Dict[str, Any]:
    return {"version": 1, "tasks": [asdict(t) for t in tasks]}
