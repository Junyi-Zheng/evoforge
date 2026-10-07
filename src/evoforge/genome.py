from __future__ import annotations

import ast
import difflib
import json
from importlib import resources
from pathlib import Path
from typing import Dict, List, Tuple

from .models import canonical, digest

FILES = ("agent.py", "tools.py", "memory.py", "policy.json", "skills.json")
COMPONENTS = {"tools": ("tools.py",), "memory": ("memory.py",),
              "workflow": ("agent.py", "policy.json"), "skills": ("skills.json",)}
ALLOWED_IMPORTS = {"json", "re", "math", "collections", "statistics", "itertools", "tools", "memory"}
BLOCKED_NAMES = {"open", "exec", "eval", "compile", "__import__", "getattr", "setattr",
                 "delattr", "globals", "locals", "vars", "input", "breakpoint"}


class InvalidGenome(ValueError):
    pass


def read_genome(path: Path) -> Dict[str, str]:
    actual = {p.name for p in path.iterdir() if p.is_file()}
    if actual != set(FILES) or any(p.is_symlink() or p.is_dir() for p in path.iterdir()):
        raise InvalidGenome("A genome must contain exactly the five regular genome files")
    genome = {name: (path / name).read_text(encoding="utf-8") for name in FILES}
    validate(genome)
    return genome


def read_seed() -> Dict[str, str]:
    # Installers may compile package data into __pycache__; load only the seed manifest.
    root = resources.files("evoforge").joinpath("seed")
    genome = {name: root.joinpath(name).read_text(encoding="utf-8") for name in FILES}
    validate(genome)
    return genome


def validate(genome: Dict[str, str]) -> None:
    if set(genome) != set(FILES):
        raise InvalidGenome("Genome file manifest mismatch")
    if not all(isinstance(v, str) for v in genome.values()):
        raise InvalidGenome("Genome contents must be UTF-8 text")
    if sum(len(v.encode()) for v in genome.values()) > 120000:
        raise InvalidGenome("Genome exceeds 120 KB")
    for name, source in genome.items():
        if not isinstance(source, str) or len(source.encode()) > 48000:
            raise InvalidGenome("Invalid file content: " + name)
        try:
            if name.endswith(".py"):
                tree = ast.parse(source, filename=name)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imports = [node.module or ""]
                        if node.level:
                            raise InvalidGenome("Relative imports are outside the genome contract")
                    else:
                        imports = []
                    if any(module not in ALLOWED_IMPORTS for module in imports):
                        raise InvalidGenome("Unsupported import in " + name)
                    if isinstance(node, ast.Name) and (node.id in BLOCKED_NAMES or node.id.startswith("__")):
                        raise InvalidGenome("Unsupported name in " + name + ": " + node.id)
                    if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                        raise InvalidGenome("Dunder access is outside the genome contract")
                if name == "agent.py":
                    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
                    if not {"step", "propose"} <= defined:
                        raise InvalidGenome("agent.py must define step(observation) and propose(context)")
            else:
                value = json.loads(source)
                if name == "policy.json" and not isinstance(value, dict):
                    raise InvalidGenome("policy.json must be an object")
                if name == "policy.json" and (type(value.get("max_context_chars", 12000)) is not int
                        or not 1000 <= value.get("max_context_chars", 12000) <= 50000):
                    raise InvalidGenome("max_context_chars must be an integer between 1000 and 50000")
                if name == "skills.json":
                    if not isinstance(value, list) or len(value) > 32:
                        raise InvalidGenome("skills.json must contain at most 32 skills")
                    ids = set()
                    for skill in value:
                        if (not isinstance(skill, dict) or not isinstance(skill.get("id"), str)
                                or not isinstance(skill.get("lesson"), str)
                                or not isinstance(skill.get("tags", []), list)
                                or not all(isinstance(t, str) for t in skill.get("tags", []))):
                            raise InvalidGenome("Each skill needs id, lesson and string tags")
                        if skill["id"] in ids:
                            raise InvalidGenome("Duplicate skill ID")
                        ids.add(skill["id"])
        except (SyntaxError, json.JSONDecodeError) as exc:
            raise InvalidGenome(str(exc)) from exc


def apply_edits(parent: Dict[str, str], edits: List[dict], component: str) -> Tuple[Dict[str, str], str]:
    if component not in COMPONENTS:
        raise InvalidGenome("Unknown mutation component")
    if not isinstance(edits, list) or not 1 <= len(edits) <= len(COMPONENTS[component]):
        raise InvalidGenome("Invalid edit count")
    child = dict(parent)
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "content"}:
            raise InvalidGenome("Edits must have exactly path and content")
        name = edit["path"]
        if name not in COMPONENTS[component] or name in seen:
            raise InvalidGenome("Mutation crossed its component boundary")
        seen.add(name)
        child[name] = edit["content"]
    validate(child)
    if digest(child) == digest(parent):
        raise InvalidGenome("Mutation produced no change")
    diff = "".join("".join(difflib.unified_diff(parent[n].splitlines(True), child[n].splitlines(True),
                                              fromfile="parent/" + n, tofile="child/" + n))
                   for n in FILES if parent[n] != child[n])
    return child, diff


def write_genome(path: Path, genome: Dict[str, str]) -> None:
    validate(genome)
    path.mkdir(parents=True, exist_ok=False)
    for name, text in genome.items():
        (path / name).write_text(text, encoding="utf-8")


def fingerprints(genome: Dict[str, str]) -> Dict[str, str]:
    return {axis: digest({n: genome[n] for n in names}) for axis, names in COMPONENTS.items()}


def ablate(genome: Dict[str, str], seed: Dict[str, str], component: str) -> Dict[str, str]:
    result = dict(genome)
    for name in COMPONENTS[component]:
        result[name] = seed[name]
    validate(result)
    return result
