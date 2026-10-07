"""Small runtime protocol receiving a genome or a submitted solution snapshot."""
import contextlib
import importlib.util
import io
import json
import os
import resource
import sys


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


def execute(request):
    mode = request["mode"]
    if mode in ("step", "propose"):
        root = request["root"]
        sys.path.insert(0, root)
        agent = module("candidate", os.path.join(root, "agent.py"))
        fn = agent.step if mode == "step" else agent.propose
        return fn(request["context"])
    if mode == "solution":
        solution = module("submission", request["path"])
        fn = getattr(solution, request["symbol"])
        outputs = []
        for case in request["cases"]:
            try:
                value = fn(*case["args"], **case.get("kwargs", {}))
                error = ""
            except Exception as exc:
                value, error = None, type(exc).__name__
            outputs.append({"value": value, "error": error, "args_after": case["args"],
                            "kwargs_after": case.get("kwargs", {})})
        return outputs
    raise ValueError("Unknown runtime mode")


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    raw = sys.stdin.buffer.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Request exceeds runtime limit")
    request = json.loads(raw)
    if "files" in request:
        root = "/tmp/evoforge-payload"
        if request["root"] != root:
            raise ValueError("Invalid payload root")
        os.mkdir(root, 0o755)
        for relative, source in request.pop("files").items():
            if (not relative or relative.startswith("/") or "\\" in relative
                    or any(part in ("", ".", "..") for part in relative.split("/"))):
                raise ValueError("Invalid payload path")
            path = os.path.join(root, relative)
            os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(source)
            os.chmod(path, 0o444)
    # Candidate stdout has no authority over the protocol result.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = execute(request)
    encoded = json.dumps({"ok": True, "result": result}, allow_nan=False)
    if len(encoded.encode()) > 1000000:
        raise ValueError("Result exceeds runtime limit")
    sys.stdout.write(encoded)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:2000]}))
        sys.exit(1)
