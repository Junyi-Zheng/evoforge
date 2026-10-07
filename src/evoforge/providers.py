from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict

from .budget import Ledger
from .models import Config, Usage, canonical


class ProviderFailure(RuntimeError):
    pass


def parse_object(text: str) -> Dict[str, Any]:
    if not isinstance(text, str):
        raise ProviderFailure("Provider content must be text")
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ProviderFailure("Provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderFailure("Provider response must be a JSON object")
    return value


@dataclass
class Reply:
    value: Dict[str, Any]
    usage: Usage


class ChatProvider:
    def __init__(self, config: Config, ledger: Ledger):
        self.config, self.ledger = config, ledger
        parsed = urllib.parse.urlparse(config.base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an HTTP endpoint without credentials or query parameters")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote model endpoints must use HTTPS")
        self.key = os.environ.get(config.api_key_env, "")
        if not self.key and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Missing model credential in " + config.api_key_env)

    def complete(self, request: Dict[str, Any], output_limit: int, label: str, timeout=None) -> Reply:
        _validate_request(request)
        messages = [{"role": "system", "content": request["system"]},
                    {"role": "user", "content": canonical(request["payload"])}]
        input_bound = len(canonical(messages).encode()) + 512
        self.ledger.reserve(input_bound, output_limit)
        body = canonical({"model": self.config.model, "messages": messages,
                          "temperature": 0, "max_tokens": output_limit,
                          "response_format": {"type": "json_object"}}).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.config.base_url.rstrip("/") + "/chat/completions",
                                     data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=min(self.config.timeout_seconds, timeout or self.config.timeout_seconds)) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ProviderFailure("Provider response exceeds 2 MB")
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            usage_data = data.get("usage", {})
            usage = Usage(calls=1, input_tokens=usage_data.get("prompt_tokens", input_bound),
                          output_tokens=usage_data.get("completion_tokens", output_limit),
                          estimated=not {"prompt_tokens", "completion_tokens"} <= set(usage_data))
            if (type(usage.input_tokens) is not int or type(usage.output_tokens) is not int
                    or usage.input_tokens < 0 or usage.output_tokens < 0):
                raise ProviderFailure("Invalid provider usage record")
        except urllib.error.HTTPError as exc:
            # Failed requests may consume quota; conservatively charge the reservation.
            self.ledger.record(label + ":http_error", Usage(1, input_bound, output_limit, True))
            raise ProviderFailure("Model endpoint returned HTTP " + str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            self.ledger.record(label + ":uncertain", Usage(1, input_bound, output_limit, True))
            raise ProviderFailure("Model request failed or returned an invalid envelope") from exc
        except ProviderFailure:
            self.ledger.record(label + ":invalid", Usage(1, input_bound, output_limit, True))
            raise
        self.ledger.record(label, usage)
        return Reply(parse_object(content), usage)


def _validate_request(request: Dict[str, Any]) -> None:
    if (not isinstance(request, dict) or set(request) != {"system", "payload"}
            or not isinstance(request["system"], str) or not isinstance(request["payload"], dict)):
        raise ProviderFailure("Model request must contain system text and a payload object")
    if len(canonical(request).encode()) > 160000:
        raise ProviderFailure("Model request exceeds 160 KB")


class DemoProvider:
    """Deterministic fixture responses for exercising the controller without a model."""
    def __init__(self, config: Config, ledger: Ledger):
        self.config, self.ledger = config, ledger

    def complete(self, request: Dict[str, Any], output_limit: int, label: str, timeout=None) -> Reply:
        _validate_request(request)
        input_bound = len(canonical(request).encode()) + 512
        self.ledger.reserve(input_bound, output_limit)
        payload = request["payload"]
        if "genome" in payload:
            value = self._mutate(payload)
        else:
            value = self._solve(payload)
        output = canonical(value)
        usage = Usage(1, max(1, len(canonical(request).encode()) // 4),
                      max(1, len(output.encode()) // 4), True)
        self.ledger.record(label, usage)
        return Reply(value, usage)

    def _solve(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        context = payload.get("context", [])
        for entry in reversed(context):
            if entry.get("action", {}).get("tool") != "read_file":
                continue
            result = entry.get("result", {})
            text = result.get("content", "") if isinstance(result, dict) else str(result)
            found = re.search(r"def (merge_intervals|chunked|slugify|moving_average)\(", text)
            if found:
                return {"tool": "write_file", "path": entry["action"]["path"],
                        "content": DEMO_SOLUTIONS[found.group(1)]}
        return {"tool": "finish", "reason": "The visible context does not contain a supported function"}

    def _mutate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        component, genome = payload["component"], payload["genome"]
        if component == "tools":
            path, content = "tools.py", EVOLVED_TOOLS
            summary = "Prioritize implementation paths over documentation examples"
        elif component == "memory":
            path, content = "memory.py", EVOLVED_MEMORY
            summary = "Preserve source structure and recent compiler feedback"
        elif component == "workflow":
            path = "policy.json"
            policy = json.loads(genome[path])
            policy["verify_before_finish"] = True
            content = json.dumps(policy, indent=2) + "\n"
            summary = "Verify public behavior before finishing a repair"
        else:
            path = "skills.json"
            content = json.dumps([{"id": "boundary-validation", "tags": ["window", "size", "empty"],
                                   "lesson": "Validate non-positive sizes before handling empty inputs."}], indent=2) + "\n"
            summary = "Record a reusable boundary-validation lesson"
        if content == genome[path]:
            if path.endswith(".py"):
                content += "\n# Prefer bounded observations and explicit tool results.\n"
            elif path == "skills.json":
                entries = json.loads(content)
                entries[0]["lesson"] += " Keep validation order explicit."
                content = json.dumps(entries, indent=2) + "\n"
            else:
                policy = json.loads(content)
                policy["max_context_chars"] = min(50000, policy.get("max_context_chars", 12000) + 1000)
                content = json.dumps(policy, indent=2) + "\n"
        return {"summary": summary, "rationale": "Scripted structural mutation for the offline fixture suite.",
                "edits": [{"path": path, "content": content}]}


EVOLVED_TOOLS = '''def choose_file(files):
    candidates = [name for name in files if name.endswith('.py')]
    return min(candidates, key=lambda name: (
        name.startswith(('docs/', 'examples/', 'tests/')),
        not name.startswith('src/'), len(name), name)) if candidates else ''


def build_request(task, context, skills):
    relevant = [s['lesson'] for s in skills
                if any(tag in task['instruction'].lower() for tag in s.get('tags', []))]
    return {
        "system": "Repair the requested function. Return exactly one JSON action. "
                  "Allowed actions: read_file(path), search(query), write_file(path, content), "
                  "run_tests, finish. Preserve signatures; handle boundary cases.",
        "payload": {"instruction": task["instruction"], "context": context, "lessons": relevant},
    }
'''

EVOLVED_MEMORY = '''def render(trace, max_chars=12000):
    selected = []
    for entry in trace[-5:]:
        result = dict(entry['result']) if isinstance(entry['result'], dict) else {'message': str(entry['result'])}
        if 'content' in result:
            result['content'] = result['content'][-min(8000, max_chars // 5):]
        selected.append({'action': entry['action'], 'result': result})
    return selected
'''

DEMO_SOLUTIONS = {
    "merge_intervals": '''def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
''',
    "chunked": '''def chunked(items, size):
    if size <= 0:
        raise ValueError('size must be positive')
    return [items[i:i + size] for i in range(0, len(items), size)]
''',
    "slugify": '''import re


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
''',
    "moving_average": '''def moving_average(values, window):
    if window <= 0:
        raise ValueError('window must be positive')
    return [sum(values[i:i + window]) / window for i in range(len(values) - window + 1)]
''',
}
