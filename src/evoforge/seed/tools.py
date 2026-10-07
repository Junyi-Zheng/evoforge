def choose_file(files):
    candidates = [name for name in files if name.endswith('.py')]
    return sorted(candidates)[0] if candidates else ''


def build_request(task, context, skills):
    return {
        "system": "Repair the requested function. Return exactly one JSON action. "
                  "Allowed actions: read_file(path), search(query), write_file(path, content), "
                  "run_tests, finish. Preserve the function signature and unrelated code.",
        "payload": {"instruction": task["instruction"], "context": context},
    }
