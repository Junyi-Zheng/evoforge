from __future__ import annotations

import math
import random
from typing import Dict, List, Tuple


def dominates(a: dict, b: dict) -> bool:
    # Wall time remains descriptive: process startup noise should not drive selection.
    return (a["score"] >= b["score"] and a["mean_tokens"] <= b["mean_tokens"]
            and (a["score"] > b["score"] or a["mean_tokens"] < b["mean_tokens"]))


def pareto_front(nodes: List[dict]) -> List[str]:
    return [a["id"] for a in nodes if not any(
        a["id"] != b["id"] and dominates(b["validation"], a["validation"]) for b in nodes)]


def novelty(candidate: dict, nodes: List[dict]) -> float:
    if not nodes:
        return 1.0
    scores = []
    for node in nodes:
        descriptor_a = candidate["validation"]["descriptor"]
        descriptor_b = node["validation"]["descriptor"]
        keys = set(descriptor_a) | set(descriptor_b)
        behavior = sum(abs(descriptor_a.get(k, 0) - descriptor_b.get(k, 0)) for k in keys) / max(1, len(keys))
        structure = sum(candidate["fingerprints"][k] != node["fingerprints"][k]
                        for k in candidate["fingerprints"]) / len(candidate["fingerprints"])
        scores.append(0.8 * behavior + 0.2 * structure)
    return min(scores)


def paired_interval(parent: Dict[str, float], child: Dict[str, float], seed: int = 17) -> Tuple[float, float]:
    if parent.keys() != child.keys():
        raise ValueError("Paired comparisons require identical tasks")
    differences = [child[k] - parent[k] for k in sorted(parent)]
    if not differences:
        return (0.0, 0.0)
    rng = random.Random(seed)
    samples = sorted(sum(rng.choice(differences) for _ in differences) / len(differences)
                     for _ in range(2000))
    return (samples[49], samples[1949])


def regression_gate(parent: dict, child: dict, tolerance: float, min_gain: float) -> dict:
    p, c = parent["by_task"], child["by_task"]
    if p.keys() != c.keys():
        raise ValueError("Regression gate requires the same evaluation tasks")
    lost = [key for key in p if c[key] < p[key]]
    gain = child["score"] - parent["score"]
    accepted = len(lost) / max(1, len(p)) <= tolerance and gain >= min_gain
    return {"accepted": accepted, "regressed_tasks": sorted(lost), "gain": gain,
            "paired_95_interval": list(paired_interval(p, c)),
            "interval_unit": "task", "criterion": "non-regression and configured minimum mean gain"}


def select_parent(nodes: List[dict], rng: random.Random, round_number: int, exploration: float) -> dict:
    eligible = [n for n in nodes if n["status"] in {"seed", "accepted", "stepping_stone"}]
    if not eligible:
        raise ValueError("Archive has no eligible parent")
    if round_number <= 3:
        return eligible[0]
    if rng.random() < exploration:
        weights = [1 / (1 + n["offspring"]) for n in eligible]
        return rng.choices(eligible, weights=weights, k=1)[0]
    frontier = set(pareto_front(eligible))
    finalists = [n for n in eligible if n["id"] in frontier]
    weights = [(1 + n.get("novelty", 0)) / (1 + n["offspring"]) for n in finalists]
    return rng.choices(finalists, weights=weights, k=1)[0]


def champion(nodes: List[dict]) -> dict:
    eligible = [n for n in nodes if n["status"] in {"seed", "accepted"}]
    if not eligible:
        raise ValueError("No promotable candidate")
    return min(eligible, key=lambda n: (-n["validation"]["score"], n["validation"]["mean_tokens"], n["id"]))
