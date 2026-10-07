from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

from .models import canonical


class RuntimeFailure(RuntimeError):
    pass


class Sandbox:
    def __init__(self, backend: str, image: str, timeout: float = 15.0):
        if backend not in {"local", "docker"}:
            raise ValueError("Unknown runtime backend")
        self.backend, self.image, self.timeout = backend, image, timeout
        self.worker = Path(__file__).with_name("worker.py").resolve()

    def preflight(self) -> None:
        if self.backend == "local":
            self.identity = {"backend": "local", "python": sys.version.split()[0]}
            return
        try:
            result = subprocess.run(["docker", "image", "inspect", self.image, "--format", "{{.Id}}"],
                                    capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeFailure("Docker runtime is unavailable") from exc
        if result.returncode:
            raise RuntimeFailure("Start Docker and build the runtime image: docker build -t "
                                 + self.image + " -f runtime/Dockerfile runtime")
        image_id = result.stdout.decode().strip()
        if not image_id.startswith("sha256:"):
            raise RuntimeFailure("Docker did not return an immutable image ID")
        self.image = image_id
        self.identity = {"backend": "docker", "image_id": image_id}

    def invoke(self, mode: str, root: Path, context: Dict[str, Any], timeout=None) -> Any:
        root = root.resolve()
        container_name = "evoforge-" + uuid.uuid4().hex
        if self.backend == "docker":
            cmd = ["docker", "run", "--rm", "-i", "--name", container_name,
                   "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--pids-limit", "32",
                   "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
                   "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                   "--workdir", "/tmp", self.image, "python", "-I", "-B", "-c",
                   self.worker.read_text(encoding="utf-8")]
            payload_root = "/tmp/evoforge-payload"
        else:
            cmd = [sys.executable, "-I", "-B", str(self.worker)]
            payload_root = str(root)
        request = {"mode": mode, "root": payload_root, "context": context}
        if self.backend == "docker":
            files = {}
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise RuntimeFailure("Runtime sources cannot contain symlinks")
                if path.is_file():
                    files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
            if len(files) > 100 or sum(len(v.encode()) for v in files.values()) > 200000:
                raise RuntimeFailure("Runtime source snapshot exceeds its limit")
            request["files"] = files
        if mode == "solution":
            request.update(context)
            request["path"] = payload_root + "/" + context["relative_path"]
            del request["context"]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "PYTHONHASHSEED": "0"}
        # Docker's CLI may need the host's context path. It is never passed into the container.
        if self.backend == "docker" and "HOME" in os.environ:
            env["HOME"] = os.environ["HOME"]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=root, env=env, start_new_session=True)
            out, err = proc.communicate(canonical(request).encode(), timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired as exc:
            if proc:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise RuntimeFailure("Runtime deadline exceeded") from exc
        except OSError as exc:
            raise RuntimeFailure("Could not start runtime") from exc
        finally:
            if self.backend == "docker":
                try:
                    subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if len(out) > 1_000_000:
            raise RuntimeFailure("Runtime output exceeds 1 MB")
        try:
            response = json.loads(out)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeFailure("Runtime returned an invalid protocol response") from exc
        if not isinstance(response, dict) or not response.get("ok") or proc.returncode:
            message = response.get("error", "Runtime failed") if isinstance(response, dict) else "Runtime failed"
            raise RuntimeFailure(str(message)[:2000])
        return response["result"]
