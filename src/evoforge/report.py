from __future__ import annotations

import json
from pathlib import Path

from .archive import Archive
from .models import load_json
from .selection import pareto_front


def export_report(archive: Archive, output: Path = None) -> Path:
    state = archive.state()
    nodes = []
    for node in state["nodes"]:
        directory = archive.candidate_dir(node["id"])
        nodes.append({**node, "diff": (directory / "change.diff").read_text(encoding="utf-8"),
                      "mutation": load_json(directory / "mutation.json")})
    frontier = pareto_front([n for n in nodes if n["status"] in {"seed", "accepted", "stepping_stone"}])
    data = {"name": "EvoForge", "version": 1, "mode": state["config"]["mode"],
            "backend": state["config"]["backend"], "model": state["config"]["model"],
            "limits": state["config"]["limits"], "status": state["status"], "selected": state["selected"],
            "nodes": nodes, "frontier": frontier, "attempts": state["attempts"],
            "ledger": state["ledger"], "audit": archive.audit(), "suite_hash": state["suite_hash"],
            "test": None, "ablations": None}
    for key, rel in (("test", "final/test.json"), ("ablations", "ablations.json")):
        if (archive.root / rel).is_file():
            value = load_json(archive.root / rel)
            # Reports publish aggregate evidence, not private test inputs or source fixtures.
            if key == "test":
                data[key] = {k: v for k, v in value.items() if k != "evaluation"}
            else:
                data[key] = {**value, "results": {axis: {k: v for k, v in v.items() if k != "evaluation"}
                                                       for axis, v in value["results"].items()}}
    template = Path(__file__).with_name("assets") / "report.html"
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    html = template.read_text(encoding="utf-8").replace("__REPORT_DATA__", encoded)
    output = (output or archive.root / "report.html").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output
