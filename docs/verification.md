# Verification record

Validated locally on 2026-10-07 with Python 3.9.6 on macOS and Python 3.12.15 in the Docker runtime.

| Check | Result |
| --- | --- |
| Controller, genome, accounting, provider, and lifecycle tests | 25 passed |
| Explicit Docker runtime integration tests | 2 passed |
| Editable installation and CLI entry point | Passed |
| Wheel build and bundled runtime/seed/report assets | Passed |
| Complete Docker fixture experiment | 9 mutation rounds, 7 archived candidates, 3 duplicate candidates rejected |
| Component reversions | Tools, memory, workflow, and skills evaluated |
| Final evaluation | Selected candidate frozen and run sealed |
| Archive integrity | Event history and artifact checks passed |
| Report interaction | Candidate selection, rationale tab, frontier filter, and narrow viewport overflow checked |

The final local evidence is in `runs/verified/` (excluded from version control). The shareable snapshot is [examples/offline-report.html](../examples/offline-report.html). Reproduce the experiment with:

```bash
PYTHONPATH=src python3 -m evoforge demo --backend docker --output runs/reproduction --rounds 9 --ablations --seal
```

This verification uses the scripted fixture provider. No external model capability experiment was run. The fixture's score improvements validate selection, component interactions, and evidence collection, and are not a claim of autonomous improvement on unseen real-world repositories.
