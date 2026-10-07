from __future__ import annotations

import json
import platform
import random
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List

from .archive import Archive, artifact_hashes, controller_hash, file_hash
from .benchmarks import builtin_suite, load_suite, suite_dict
from .budget import BudgetExceeded, Ledger
from .evaluation import ACTION_CONTRACT, Evaluator
from .genome import ALLOWED_IMPORTS, COMPONENTS, InvalidGenome, ablate, apply_edits, fingerprints, read_genome, write_genome
from .models import Config, Evaluation, atomic_json, digest
from .providers import ChatProvider, DemoProvider, ProviderFailure
from .sandbox import RuntimeFailure, Sandbox
from .selection import champion, novelty, pareto_front, regression_gate, select_parent


class Engine:
    def __init__(self, archive: Archive, progress: Callable[[str], None] = print):
        self.archive, self.state, self.progress = archive, archive.state(), progress
        self.config = Config.from_dict(self.state["config"])
        self.sandbox = Sandbox(self.config.backend, self.config.docker_image)
        self.sandbox.preflight()
        recorded_runtime = self.state["environment"].get("runtime")
        if recorded_runtime is not None and recorded_runtime != self.sandbox.identity:
            raise ValueError("Runtime identity changed; restore the recorded Python version or Docker image")
        if recorded_runtime is None:
            self.state["environment"]["runtime"] = self.sandbox.identity
            self.archive.commit(self.state, "runtime_registered", self.sandbox.identity)
        self.tasks = load_suite(archive.root / "suite.json")
        self.ledger = Ledger(self.config)
        self.ledger.restore(self.state["ledger"]["entries"])
        self.ledger.on_change = self._budget_checkpoint
        self.provider = (DemoProvider if self.config.mode == "demo" else ChatProvider)(self.config, self.ledger)
        self.evaluator = Evaluator(self.config, self.sandbox, self.provider)

    @classmethod
    def create(cls, root: Path, config: Config, suite: Path = None, seed_dir: Path = None,
               progress: Callable[[str], None] = print):
        config.validate()
        if config.backend == "local" and (suite or seed_dir):
            raise ValueError("Custom suites and genomes require Docker; local mode is for bundled fixtures only")
        tasks = load_suite(suite) if suite else builtin_suite(config.seed)
        seed_genome = read_genome(seed_dir or Path(__file__).with_name("seed"))
        archive = Archive(root, create=True)
        atomic_json(archive.root / "suite.json", suite_dict(tasks))
        state = {"version": 1, "status": "initializing", "config": asdict(config),
                 "controller_hash": controller_hash(), "suite_file_hash": file_hash(archive.root / "suite.json"),
                 "suite_hash": digest(suite_dict(tasks)), "seed_genome": seed_genome,
                 "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
                 "nodes": [], "attempts": [], "last_round": 0, "inflight": None,
                 "ledger": Ledger(config).summary(), "selected": None, "sealed_artifacts": {}}
        archive.commit(state, "created", {"mode": config.mode, "backend": config.backend})
        try:
            return cls(archive, progress)
        except Exception:
            archive.close()
            raise

    def _budget_checkpoint(self) -> None:
        self.state["ledger"] = self.ledger.summary()
        self.archive.commit(self.state, "budget", {"calls": self.ledger.usage.calls,
                                                   "tokens": self.ledger.usage.tokens})

    def _checkpoint(self, kind: str, details: Any = None) -> None:
        self.state["ledger"] = self.ledger.summary()
        self.archive.commit(self.state, kind, details)

    def _new_directory(self, candidate_id: str) -> Path:
        path = self.archive.candidate_dir(candidate_id)
        if path.exists():
            if any(n["id"] == candidate_id for n in self.state["nodes"]):
                raise ValueError("Candidate ID already committed")
            abandoned = self.archive.root / "abandoned"
            abandoned.mkdir(exist_ok=True)
            path.rename(abandoned / (candidate_id + "-" + str(time.time_ns())))
        path.mkdir(parents=True)
        return path

    def _evaluate_node(self, candidate_id: str, genome: Dict[str, str], parent: dict = None,
                       mutation: dict = None, diff: str = "", donor: dict = None) -> dict:
        directory = self._new_directory(candidate_id)
        write_genome(directory / "genome", genome)
        atomic_json(directory / "mutation.json", mutation or {"summary": "Initial agent", "component": "seed"})
        (directory / "change.diff").write_text(diff, encoding="utf-8")
        self.progress(candidate_id + " evaluating training tasks")
        train = self.evaluator.evaluate(directory / "genome", self.tasks, "train", candidate_id)
        self.progress(candidate_id + " evaluating validation tasks")
        validation = self.evaluator.evaluate(directory / "genome", self.tasks, "validation", candidate_id)
        for split, evaluation in (("train", train), ("validation", validation)):
            atomic_json(directory / (split + ".json"), asdict(evaluation))
        node = {"id": candidate_id, "parent": parent["id"] if parent else None,
                "donor": donor["id"] if donor else None, "genome_hash": digest(genome),
                "fingerprints": fingerprints(genome), "train": train.summary(), "validation": validation.summary(),
                "summary": (mutation or {}).get("summary", "Initial agent"),
                "component": (mutation or {}).get("component", "seed"), "status": "seed",
                "offspring": 0, "novelty": 1.0, "gate": None,
                "artifacts": artifact_hashes(directory)}
        if parent:
            node["gate"] = regression_gate(parent["validation"], node["validation"],
                                           self.config.regression_tolerance, self.config.min_gain)
            node["novelty"] = novelty(node, self.state["nodes"])
            if node["gate"]["accepted"]:
                node["status"] = "accepted"
            elif node["novelty"] >= 0.1:
                node["status"] = "stepping_stone"
            else:
                node["status"] = "rejected"
        self.state["nodes"].append(node)
        self._checkpoint("candidate", {"id": candidate_id, "status": node["status"],
                                       "genome_hash": node["genome_hash"]})
        self.progress(candidate_id + "  validation={:.1%}  tokens/task={:.0f}  {}".format(
            node["validation"]["score"], node["validation"]["mean_tokens"], node["status"]))
        return node

    def _component(self, parent: dict, round_number: int) -> str:
        order = list(COMPONENTS)
        if round_number <= 3:
            return order[(round_number - 1) % len(order)]
        seed_fingerprints = fingerprints(self.state["seed_genome"])
        missing = [name for name in order if parent["fingerprints"][name] == seed_fingerprints[name]]
        return missing[0] if missing else order[(round_number - 1) % len(order)]

    def _proposal(self, parent: dict, component: str, candidate_id: str) -> dict:
        from .models import load_json
        train = load_json(self.archive.candidate_dir(parent["id"]) / "train.json")
        evidence = [{"family": r["family"], "passed": r["passed"], "reason": r["reason"],
                     "trace": r["trace"][-8:]} for r in train["results"]]
        # Bound context deterministically. Neither validation traces nor final tests enter it.
        while len(json.dumps(evidence).encode()) > 50000 and evidence:
            evidence.pop()
        context = {"component": component, "allowed_files": list(COMPONENTS[component]),
                   "allowed_imports": sorted(ALLOWED_IMPORTS), "genome": self.archive.genome(parent),
                   "training_evidence": evidence, "action_contract": ACTION_CONTRACT}
        decision = self.sandbox.invoke("propose", self.archive.candidate_dir(parent["id"]) / "genome", context)
        if not isinstance(decision, dict) or set(decision) not in ({"request"}, {"mutation"}):
            raise InvalidGenome("propose must return exactly request or mutation")
        if "request" in decision:
            proposal = self.provider.complete(decision["request"], self.config.mutation_tokens,
                                              candidate_id + "/mutation").value
        else:
            proposal = decision["mutation"]
        if (not isinstance(proposal, dict) or set(proposal) != {"summary", "rationale", "edits"}
                or not isinstance(proposal["summary"], str) or not isinstance(proposal["rationale"], str)
                or len(proposal["summary"]) > 300 or len(proposal["rationale"]) > 12000):
            raise InvalidGenome("Mutation needs bounded summary, rationale and edits")
        return {**proposal, "component": component, "method": "self_proposal"}

    def evolve(self) -> None:
        if self.state["status"] in {"sealed", "sealing", "exhausted"}:
            raise ValueError("This run is closed to evolution: " + self.state["status"])
        audit = self.archive.audit()
        if not audit["ok"]:
            raise ValueError("Integrity check failed: " + "; ".join(audit["errors"]))
        try:
            if not self.state["nodes"]:
                self._evaluate_node("c0000", self.state["seed_genome"])
            self.state["status"] = "running"
            self._checkpoint("started")
            for round_number in range(self.state["last_round"] + 1, self.config.rounds + 1):
                candidate_id = "c" + str(round_number).zfill(4)
                if len(candidate_id) != 5:
                    raise ValueError("At most 9999 rounds are supported")
                # A completed candidate can precede its round checkpoint after interruption.
                if any(n["id"] == candidate_id for n in self.state["nodes"]):
                    self.state["last_round"] = round_number
                    self.state["inflight"] = None
                    self._checkpoint("round_recovered", {"round": round_number})
                    continue
                rng = random.Random(self.config.seed + round_number * 1009)
                if self.state.get("inflight") and self.state["inflight"]["round"] == round_number:
                    parent = next(n for n in self.state["nodes"] if n["id"] == self.state["inflight"]["parent"])
                else:
                    parent = select_parent(self.state["nodes"], rng, round_number, self.config.exploration)
                    parent["offspring"] += 1
                component = self._component(parent, round_number)
                self.state["inflight"] = {"round": round_number, "parent": parent["id"], "component": component}
                self._checkpoint("round_started", self.state["inflight"])
                self.progress("Round {} / {}: {} → {} [{}]".format(
                    round_number, self.config.rounds, parent["id"], candidate_id, component))
                try:
                    parent_genome = self.archive.genome(parent)
                    donor = None
                    if round_number % 5 == 0:
                        donors = [n for n in self.state["nodes"] if n["id"] != parent["id"]
                                  and n["status"] in {"accepted", "stepping_stone"}
                                  and n["fingerprints"][component] != parent["fingerprints"][component]]
                        if donors:
                            donor = max(donors, key=lambda n: n["validation"]["score"])
                    if donor:
                        donor_genome = self.archive.genome(donor)
                        proposal = {"summary": "Recombine " + component + " from " + donor["id"],
                                    "rationale": "Transfer a component from an archived branch; re-evaluate the full child.",
                                    "component": component, "method": "crossover",
                                    "edits": [{"path": n, "content": donor_genome[n]} for n in COMPONENTS[component]]}
                    else:
                        proposal = self._proposal(parent, component, candidate_id)
                    child, diff = apply_edits(parent_genome, proposal["edits"], component)
                    if any(n["genome_hash"] == digest(child) for n in self.state["nodes"]):
                        raise InvalidGenome("This program variant already exists in the archive")
                    self._evaluate_node(candidate_id, child, parent, proposal, diff, donor)
                except (InvalidGenome, RuntimeFailure, ProviderFailure, ValueError) as exc:
                    attempt = {"id": candidate_id, "round": round_number, "parent": parent["id"],
                               "component": component, "error": str(exc)[:2000]}
                    self.state["attempts"].append(attempt)
                    atomic_json(self.archive.root / "attempts" / (candidate_id + ".json"), attempt)
                    self.progress(candidate_id + " rejected before completion: " + attempt["error"])
                self.state["last_round"], self.state["inflight"] = round_number, None
                self._checkpoint("round_finished", {"round": round_number})
            self.state["status"] = "completed"
            self.state["selected"] = champion(self.state["nodes"])["id"]
            self._checkpoint("completed", {"selected": self.state["selected"]})
        except BudgetExceeded as exc:
            self.state["status"] = "exhausted"
            self._checkpoint("exhausted", {"reason": str(exc)})
            self.progress(str(exc))
        except BaseException:
            self.state["status"] = "interrupted"
            self._checkpoint("interrupted")
            raise

    def seal(self) -> dict:
        audit = self.archive.audit()
        if not audit["ok"]:
            raise ValueError("Integrity check failed before sealing")
        if self.state["status"] == "sealed":
            from .models import load_json
            return load_json(self.archive.root / "final" / "test.json")
        if self.state["status"] not in {"completed", "sealing"}:
            raise ValueError("Only a completed run can be sealed")
        selected = next(n for n in self.state["nodes"] if n["id"] == self.state["selected"])
        self.archive.genome(selected)
        self.state["status"] = "sealing"
        self._checkpoint("seal_started", {"selected": selected["id"], "genome_hash": selected["genome_hash"]})
        self.progress("Final test: fixed candidate " + selected["id"])
        test = self.evaluator.evaluate(self.archive.candidate_dir(selected["id"]) / "genome",
                                       self.tasks, "test", "final")
        result = {"candidate": selected["id"], "genome_hash": selected["genome_hash"],
                  "summary": test.summary(), "evaluation": asdict(test),
                  "note": "Final tests do not feed back into selection or mutation."}
        atomic_json(self.archive.root / "final" / "test.json", result)
        self.state["sealed_artifacts"]["final/test.json"] = file_hash(self.archive.root / "final" / "test.json")
        self.state["status"] = "sealed"
        self._checkpoint("sealed", {"candidate": selected["id"], "score": test.score})
        return result

    def ablations(self) -> dict:
        if self.state["status"] not in {"completed", "sealed"}:
            raise ValueError("Ablations require a completed run")
        audit = self.archive.audit()
        if not audit["ok"]:
            raise ValueError("Integrity check failed before ablations")
        selected = next(n for n in self.state["nodes"] if n["id"] == self.state["selected"])
        genome, seed = self.archive.genome(selected), self.state["seed_genome"]
        results = {}
        for component in COMPONENTS:
            directory = self.archive.root / "ablations" / component
            result_path = directory / "result.json"
            if str(result_path.relative_to(self.archive.root)) in self.state["sealed_artifacts"]:
                from .models import load_json
                results[component] = load_json(result_path)
                continue
            if directory.exists():
                abandoned = self.archive.root / "abandoned"
                abandoned.mkdir(exist_ok=True)
                directory.rename(abandoned / ("ablation-" + component + "-" + str(time.time_ns())))
            write_genome(directory / "genome", ablate(genome, seed, component))
            self.progress("Ablation: restore seed " + component)
            evaluation = self.evaluator.evaluate(directory / "genome", self.tasks, "validation", "ablate/" + component)
            results[component] = {"summary": evaluation.summary(),
                                  "delta": evaluation.score - selected["validation"]["score"],
                                  "evaluation": asdict(evaluation)}
            atomic_json(result_path, results[component])
            for relative, fingerprint in artifact_hashes(directory).items():
                self.state["sealed_artifacts"]["ablations/" + component + "/" + relative] = fingerprint
            self._checkpoint("ablation_completed", {"component": component})
        result = {"candidate": selected["id"], "split": "validation", "baseline": selected["validation"],
                  "results": results, "note": "Component reversion measures interactions, not independent causal effects."}
        path = self.archive.root / "ablations.json"
        atomic_json(path, result)
        self.state["sealed_artifacts"]["ablations.json"] = file_hash(path)
        self._checkpoint("ablations_completed", {"candidate": selected["id"]})
        return result
