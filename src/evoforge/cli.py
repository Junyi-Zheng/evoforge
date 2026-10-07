from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .archive import Archive
from .benchmarks import builtin_suite, suite_dict
from .engine import Engine
from .models import Config, atomic_json, load_json
from .report import export_report


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evoforge", description="Evolve agent programs and inspect the evidence.")
    sub = p.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run deterministic offline fixtures")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--rounds", type=int, default=9)
    demo.add_argument("--seed", type=int, default=17)
    demo.add_argument("--backend", choices=("local", "docker"), default="local")
    demo.add_argument("--suite", type=Path)
    demo.add_argument("--seal", action="store_true")
    demo.add_argument("--ablations", action="store_true")
    run = sub.add_parser("run", help="Run a model-backed evolution experiment")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--suite", type=Path)
    run.add_argument("--seed-genome", type=Path)
    for command in ("resume", "seal", "ablate", "audit", "inspect", "report"):
        cmd = sub.add_parser(command)
        cmd.add_argument("run", type=Path)
        if command == "inspect":
            cmd.add_argument("candidate")
        if command == "report":
            cmd.add_argument("--output", type=Path)
    suite = sub.add_parser("export-suite", help="Export the built-in task schema and cases")
    suite.add_argument("output", type=Path)
    suite.add_argument("--seed", type=int, default=17)
    compare = sub.add_parser("compare", help="Compare runs only when experiment controls match")
    compare.add_argument("runs", type=Path, nargs="+", help="At least two run directories")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    archive = None
    try:
        if args.command == "export-suite":
            atomic_json(args.output, suite_dict(builtin_suite(args.seed)))
            print(args.output.resolve())
            return 0
        if args.command == "compare":
            return compare_runs(args.runs)
        if args.command in {"demo", "run"}:
            if args.command == "demo":
                config = Config(mode="demo", backend=args.backend, rounds=args.rounds, seed=args.seed)
                engine = Engine.create(args.output, config, suite=args.suite)
            else:
                config = Config.from_dict(load_json(args.config))
                if config.mode != "live":
                    raise ValueError("The run command requires mode=live; use demo for scripted fixtures")
                engine = Engine.create(args.output, config, suite=args.suite, seed_dir=args.seed_genome)
            archive = engine.archive
            engine.evolve()
            if args.command == "demo" and engine.state["status"] == "completed":
                if args.ablations:
                    engine.ablations()
                if args.seal:
                    engine.seal()
            output = export_report(archive)
            print("Report: " + str(output))
            return 0 if engine.state["status"] in {"completed", "sealed"} else 2
        archive = Archive(args.run)
        if args.command == "audit":
            result = archive.audit()
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.command == "inspect":
            state = archive.state()
            node = next((n for n in state["nodes"] if n["id"] == args.candidate), None)
            if node is None:
                raise ValueError("Unknown candidate")
            archive.genome(node)
            print(json.dumps(node, indent=2))
            print((archive.candidate_dir(node["id"]) / "change.diff").read_text(encoding="utf-8"))
            return 0
        if args.command == "report":
            print(export_report(archive, args.output))
            return 0
        engine = Engine(archive)
        if args.command == "resume":
            engine.evolve()
        elif args.command == "seal":
            result = engine.seal()
            print("Final test score: {:.1%}".format(result["summary"]["score"]))
        elif args.command == "ablate":
            engine.ablations()
        print("Report: " + str(export_report(archive)))
        return 2 if engine.state["status"] == "exhausted" else 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print("evoforge: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Resume an evolution run with: evoforge resume RUN", file=sys.stderr)
        return 130
    finally:
        if archive:
            archive.close()


def compare_runs(paths) -> int:
    if len(paths) < 2:
        raise ValueError("Provide at least two runs")
    states = []
    for path in paths:
        archive = Archive(path)
        try:
            audit = archive.audit()
            if not audit["ok"]:
                raise ValueError("Invalid run: " + str(path))
            states.append(archive.state())
        finally:
            archive.close()
    controls = ["mode", "model", "base_url", "backend", "docker_image", "limits", "repetitions", "rounds",
                "total_calls", "total_tokens", "mutation_tokens", "exploration", "regression_tolerance", "min_gain"]
    for state in states[1:]:
        if state["suite_hash"] != states[0]["suite_hash"]:
            raise ValueError("Runs use different task suites")
        if any(state["config"][key] != states[0]["config"][key] for key in controls):
            raise ValueError("Runs have incompatible model, runtime or budget controls")
        if state["controller_hash"] != states[0]["controller_hash"] or state["seed_genome"] != states[0]["seed_genome"]:
            raise ValueError("Runs have different controller source or seed genomes")
        if state["environment"].get("runtime") != states[0]["environment"].get("runtime"):
            raise ValueError("Runs use different runtime identities")
    rows = []
    for path, state in zip(paths, states):
        selected = next((n for n in state["nodes"] if n["id"] == state["selected"]), None)
        if not selected:
            raise ValueError("Run has no selected candidate")
        rows.append({"run": str(path), "candidate": selected["id"],
                     "validation": selected["validation"]["score"],
                     "mean_tokens": selected["validation"]["mean_tokens"],
                     "total_tokens": state["ledger"]["tokens"], "status": state["status"]})
    print(json.dumps(rows, indent=2))
    return 0
