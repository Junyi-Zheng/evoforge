from dataclasses import dataclass, field
from typing import Any, Dict

from .models import Config, Usage


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Ledger:
    config: Config
    usage: Usage = field(default_factory=Usage)
    entries: list = field(default_factory=list)
    on_change: Any = None
    pending_index: Any = None

    def reserve(self, input_estimate: int, output_limit: int) -> None:
        if self.pending_index is not None:
            raise RuntimeError("A model request is already in flight")
        if input_estimate < 0 or output_limit <= 0:
            raise ValueError("Invalid model reservation")
        if self.usage.calls + 1 > self.config.total_calls:
            raise BudgetExceeded("Run call budget exhausted")
        if self.usage.tokens + input_estimate + output_limit > self.config.total_tokens:
            raise BudgetExceeded("Run token budget cannot cover the next request")
        self.pending_index = len(self.entries)
        self.entries.append({"label": "inflight_reservation", "calls": 1,
                             "input_tokens": input_estimate, "output_tokens": output_limit,
                             "estimated": True, "pending": True})
        self._recompute()
        if self.on_change:
            self.on_change()

    def record(self, label: str, usage: Usage) -> None:
        entry = {"label": label, "calls": usage.calls, "input_tokens": usage.input_tokens,
                 "output_tokens": usage.output_tokens, "estimated": usage.estimated, "pending": False}
        if self.pending_index is not None:
            self.entries[self.pending_index] = entry
            self.pending_index = None
        else:
            self.entries.append(entry)
        self._recompute()
        if self.on_change:
            self.on_change()
        if self.usage.tokens > self.config.total_tokens:
            raise BudgetExceeded("Provider usage exceeded the reserved run budget")

    def _recompute(self) -> None:
        self.usage = Usage()
        for e in self.entries:
            self.usage.add(Usage(e["calls"], e["input_tokens"], e["output_tokens"], e["estimated"]))

    def restore(self, entries: list) -> None:
        self.entries = list(entries)
        self.pending_index = None
        self._recompute()

    def summary(self) -> Dict[str, Any]:
        cost = (self.usage.input_tokens * self.config.input_per_million
                + self.usage.output_tokens * self.config.output_per_million) / 1_000_000
        return {"calls": self.usage.calls, "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens, "tokens": self.usage.tokens,
                "estimated": self.usage.estimated, "estimated_cost": cost,
                "pricing_configured": bool(self.config.input_per_million or self.config.output_per_million),
                "entries": self.entries}
