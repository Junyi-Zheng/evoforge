# Extension contracts

## Add tasks

Export the bundled suite to inspect a complete example:

```bash
PYTHONPATH=src python3 -m evoforge export-suite examples/suite.json
```

The file is `{"version": 1, "tasks": [...]}`. Provide all three splits, unique IDs, and at least one public and private case per task. Each task has this shape:

```json
{
  "id": "train-normalize-1",
  "split": "train",
  "family": "text",
  "instruction": "Repair normalize(text): collapse whitespace and trim both ends.",
  "files": {"src/normalize.py": "def normalize(text):\n    return text\n"},
  "target": "src/normalize.py",
  "symbol": "normalize",
  "public": [{"args": [" hello  there "], "expected": "hello there"}],
  "private": [{"args": ["\t a\n b  "], "expected": "a b", "check_input_unchanged": true}]
}
```

Cases accept `args` (array), `kwargs` (object, optional), `expected`, `error` (exception class name, optional), and `check_input_unchanged` (boolean, default false). For expected exceptions use `expected: null`. Outcomes must be JSON-serializable. Floats use a relative and absolute tolerance of `1e-9`; booleans are not accepted as numeric results. Each task can have up to 100 source files and 200 total cases.

The worker loads the target as a module and calls its exported function. This adapter does not install task dependencies or resolve arbitrary package layouts. Prefer standard-library, self-contained target modules. To support multi-file repository patching, implement a separate task adapter and grading protocol rather than treating `files` as a shell workspace.

```bash
PYTHONPATH=src python3 -m evoforge run --config configs/experiment.json --suite examples/suite.json --output runs/custom
```

## Customize the seed

Copy the five files under `src/evoforge/seed/` to a directory and pass `--seed-genome DIRECTORY` with `run`. That directory must contain exactly those regular files, with no subdirectories or symlinks. Live execution requires Docker.

`step(observation)` returns `{"action": {...}, "state": {...}}`. The observation contains:

- `task`: instruction, file names, public examples.
- `state`: the previous state dictionary, initially empty.
- `last`: the last action's result.
- `trace`: prior visible actions/results for this episode.
- `pending_action`: the latest model JSON reply, if any.
- `policy` and `skills`: the current genome's JSON data.
- `remaining`: remaining action and model-call allowances.
- `repetition`: the repeat index.

Actions:

```json
{"tool": "read_file", "path": "src/normalize.py"}
{"tool": "search", "query": "def normalize"}
{"tool": "write_file", "path": "src/normalize.py", "content": "..."}
{"tool": "ask_model", "request": {"system": "Return one JSON action.", "payload": {}}}
{"tool": "run_tests"}
{"tool": "finish"}
```

`list_files` has no arguments. `run_tests` exposes only pass counts for public examples. Private cases are evaluated after the agent finishes; they are never available as an action. A model reply is delivered to the next `step` as `pending_action`; the agent can choose whether to execute it.

## Proposal protocol

`propose(context)` receives the parent genome, the permitted component/files/imports, bounded training evidence, and the fixed action contract. Return either `{"request": {"system": "...", "payload": {...}}}` or `{"mutation": {...}}`.

A mutation contains exactly:

```json
{
  "summary": "Preserve compiler feedback in context",
  "rationale": "Source observations were lost before the repair request.",
  "edits": [{"path": "memory.py", "content": "def render(trace, max_chars=12000):\n    return trace[-3:]\n"}]
}
```

Edits replace whole files within the chosen component. Cross-component edits, path traversal, syntax errors, manifest changes, no-op mutations, and duplicate genomes are rejected. Rejections and their model cost remain in the archive. Requests and code should express general mechanisms; the framework does not automatically prove that a candidate has avoided all task-specific memorization.

## Add a model provider

Implement `complete(request, output_limit, label, timeout=None)` returning `Reply(value, usage)`, and expose the shared `ledger`. Reserve the request before dispatch, record actual or conservatively estimated usage on every outcome, then parse the reply. Do not retry invisibly: retries consume budget and must be separately recorded. Credentials remain in the controller environment.

The built-in HTTP adapter posts `/chat/completions` with `model`, `messages`, `temperature: 0`, `max_tokens`, and JSON response format. APIs with different parameter or authentication conventions need a provider adapter.
