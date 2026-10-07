from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class Limits:
    steps: int = 14
    calls: int = 4
    tokens: int = 16000
    seconds: float = 120.0
    output_tokens: int = 2048


@dataclass
class Config:
    mode: str = "demo"
    backend: str = "local"
    rounds: int = 9
    repetitions: int = 1
    seed: int = 17
    model: str = "scripted-fixture-v1"
    base_url: str = "http://localhost:8000/v1"
    api_key_env: str = "EVOFORGE_API_KEY"
    docker_image: str = "evoforge-runtime:local"
    total_tokens: int = 400000
    total_calls: int = 300
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    mutation_tokens: int = 10000
    timeout_seconds: float = 45.0
    exploration: float = 0.35
    regression_tolerance: float = 0.0
    min_gain: float = 0.0
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> Config:
        value = dict(value)
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown configuration fields: " + ", ".join(sorted(unknown)))
        value["limits"] = Limits(**value.get("limits", {}))
        result = cls(**value)
        result.validate()
        return result

    def validate(self) -> None:
        if self.mode not in ("demo", "live") or self.backend not in ("local", "docker"):
            raise ValueError("mode must be demo/live; backend must be local/docker")
        if self.mode == "live" and self.backend != "docker":
            raise ValueError("Live mutations require the Docker backend")
        if not 0 <= self.rounds <= 9999 or self.repetitions < 1:
            raise ValueError("rounds must be in [0, 9999]; repetitions must be >= 1")
        for name in ("total_tokens", "total_calls", "mutation_tokens"):
            if getattr(self, name) <= 0:
                raise ValueError(name + " must be positive")
        for name, value in asdict(self.limits).items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("limits." + name + " must be finite and positive")
        for name in ("rounds", "repetitions", "seed", "total_tokens", "total_calls", "mutation_tokens"):
            if type(getattr(self, name)) is not int:
                raise ValueError(name + " must be an integer")
        for name in ("steps", "calls", "tokens", "output_tokens"):
            if type(getattr(self.limits, name)) is not int:
                raise ValueError("limits." + name + " must be an integer")
        for name in ("exploration", "regression_tolerance"):
            if not math.isfinite(getattr(self, name)) or not 0 <= getattr(self, name) <= 1:
                raise ValueError(name + " must be in [0, 1]")
        for name in ("input_per_million", "output_per_million", "min_gain", "timeout_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(name + " must be finite and non-negative")
        if self.timeout_seconds == 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class Case:
    args: List[Any]
    expected: Any
    kwargs: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    check_input_unchanged: bool = False


@dataclass(frozen=True)
class Task:
    id: str
    split: str
    family: str
    instruction: str
    files: Dict[str, str]
    target: str
    symbol: str
    public: List[Case]
    private: List[Case]

    def visible(self) -> Dict[str, Any]:
        return {"instruction": self.instruction, "files": sorted(self.files),
                "public_tests": [asdict(c) for c in self.public]}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.estimated |= other.estimated


@dataclass
class TaskResult:
    task_id: str
    family: str
    split: str
    repetition: int
    passed: bool
    public_passed: bool
    steps: int
    seconds: float
    usage: Usage
    reason: str
    trace: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Evaluation:
    split: str
    results: List[TaskResult]

    @property
    def score(self) -> float:
        return sum(r.passed for r in self.results) / max(1, len(self.results))

    @property
    def tokens(self) -> int:
        return sum(r.usage.tokens for r in self.results)

    @property
    def mean_tokens(self) -> float:
        return self.tokens / max(1, len(self.results))

    @property
    def mean_seconds(self) -> float:
        return sum(r.seconds for r in self.results) / max(1, len(self.results))

    def by_task(self) -> Dict[str, float]:
        buckets: Dict[str, List[bool]] = {}
        for r in self.results:
            buckets.setdefault(r.task_id, []).append(r.passed)
        return {key: sum(vals) / len(vals) for key, vals in buckets.items()}

    def descriptor(self) -> Dict[str, float]:
        buckets: Dict[str, List[bool]] = {}
        for r in self.results:
            buckets.setdefault(r.family, []).append(r.passed)
        return {key: sum(vals) / len(vals) for key, vals in buckets.items()}

    def summary(self) -> Dict[str, Any]:
        return {"score": self.score, "tokens": self.tokens, "mean_tokens": self.mean_tokens,
                "mean_seconds": self.mean_seconds, "by_task": self.by_task(),
                "descriptor": self.descriptor(), "count": len(self.results),
                "calls": sum(r.usage.calls for r in self.results)}
