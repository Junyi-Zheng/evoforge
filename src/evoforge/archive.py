from __future__ import annotations

import hashlib
import fcntl
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

from .genome import read_genome
from .models import canonical, digest, load_json


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_hashes(path: Path) -> Dict[str, str]:
    return {str(p.relative_to(path)): file_hash(p) for p in sorted(path.rglob("*")) if p.is_file()}


def controller_hash() -> str:
    root = Path(__file__).parent
    return digest({p.name: file_hash(p) for p in sorted(root.glob("*.py"))})


class Archive:
    def __init__(self, root: Path, create: bool = False):
        self.root = root.resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=False)
        if not self.root.is_dir():
            raise ValueError("Run directory does not exist")
        db_path = self.root / "archive.sqlite3"
        if not create and not db_path.is_file():
            raise ValueError("Run has no archive database")
        self.lock = (self.root / ".lock").open("a+")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise ValueError("Run is in use by another process") from exc
        try:
            self.db = sqlite3.connect(str(db_path), timeout=30)
        except Exception:
            self.lock.close()
            raise
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        if create:
            self.db.executescript("""
                CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL);
                CREATE TABLE events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                                     previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
            """)
            self.db.commit()

    def close(self) -> None:
        self.db.close()
        self.lock.close()

    def state(self) -> Dict[str, Any]:
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Archive has no state")
        return json.loads(row[0])

    def commit(self, state: Dict[str, Any], kind: str, details: Any = None) -> None:
        row = self.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (row[0] + 1, row[1]) if row else (1, "0" * 64)
        payload = canonical({"kind": kind, "details": details, "state_hash": digest(state),
                             "timestamp": time.time()})
        event_hash = digest({"seq": seq, "previous_hash": previous, "payload": payload})
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (canonical(state),))
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (seq, payload, previous, event_hash))

    def events(self) -> List[Dict[str, Any]]:
        return [{"seq": seq, "hash": event_hash, "previous_hash": previous, **json.loads(payload)}
                for seq, payload, previous, event_hash in self.db.execute("SELECT * FROM events ORDER BY seq")]

    def candidate_dir(self, candidate_id: str) -> Path:
        if (not isinstance(candidate_id, str) or not candidate_id.startswith("c")
                or len(candidate_id) != 5 or not candidate_id[1:].isdigit()):
            raise ValueError("Invalid candidate ID")
        return self.root / "candidates" / candidate_id

    def genome(self, node: dict) -> Dict[str, str]:
        genome = read_genome(self.candidate_dir(node["id"]) / "genome")
        if digest(genome) != node["genome_hash"]:
            raise ValueError("Candidate genome changed: " + node["id"])
        return genome

    def audit(self, check_controller: bool = True) -> Dict[str, Any]:
        state, errors = self.state(), []
        previous, last_state, expected_seq = "0" * 64, None, 1
        for seq, payload, prev, event_hash in self.db.execute("SELECT * FROM events ORDER BY seq"):
            expected_hash = digest({"seq": seq, "previous_hash": prev, "payload": payload})
            if seq != expected_seq or prev != previous or expected_hash != event_hash:
                errors.append("Event chain mismatch at " + str(seq))
            previous, expected_seq = event_hash, seq + 1
            last_state = json.loads(payload)["state_hash"]
        if last_state != digest(state):
            errors.append("State does not match event head")
        if check_controller and state["controller_hash"] != controller_hash():
            errors.append("Controller source differs from the recorded run")
        suite_path = self.root / "suite.json"
        if not suite_path.is_file() or file_hash(suite_path) != state["suite_file_hash"]:
            errors.append("Suite fingerprint changed")
        ids = {n["id"] for n in state["nodes"]}
        for node in state["nodes"]:
            path = self.candidate_dir(node["id"])
            try:
                self.genome(node)
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
            if artifact_hashes(path) != node["artifacts"]:
                errors.append("Candidate artifact fingerprint mismatch: " + node["id"])
            if node["parent"] is not None and node["parent"] not in ids:
                errors.append("Missing parent: " + node["id"])
            for split in ("train", "validation"):
                try:
                    result = load_json(path / (split + ".json"))
                    records = result["results"]
                    score = sum(r["passed"] for r in records) / len(records)
                    mean_tokens = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in records) / len(records)
                    if score != node[split]["score"] or mean_tokens != node[split]["mean_tokens"]:
                        errors.append("Evaluation summary mismatch: " + node["id"] + "/" + split)
                except (OSError, ValueError, KeyError, ZeroDivisionError):
                    errors.append("Missing/invalid evaluation: " + node["id"] + "/" + split)
        for rel, expected in state.get("sealed_artifacts", {}).items():
            path = self.root / rel
            if not path.is_file() or file_hash(path) != expected:
                errors.append("Sealed artifact mismatch: " + rel)
        return {"ok": not errors, "errors": errors, "nodes": len(state["nodes"]),
                "events": expected_seq - 1, "head": previous, "status": state["status"],
                "scope": "Local integrity checks; the chain is not an external signature"}
