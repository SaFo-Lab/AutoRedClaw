#!/usr/bin/env python3
"""Manage RedClaw's authenticated relay outside NemoClaw and its sandboxes."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


STATE_DIR = Path("/root/.redclaw-bedrock")
SCRIPT = Path(__file__).with_name("bedrock_relay.py").resolve()


def healthy(state: dict) -> bool:
    if not state.get("token"):
        return False
    request = urllib.request.Request("http://127.0.0.1:8000/health", headers={
        "Authorization": "Bearer " + state["token"],
    })
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
            return json.load(response).get("service_id") == state.get("fingerprint")
    except (OSError, ValueError, urllib.error.URLError):
        return False


def owned_pid(state: dict) -> int | None:
    pid = state.get("pid")
    script = state.get("script", "")
    if not isinstance(pid, int) or pid <= 1 or not script.startswith("/opt/redclaw-bedrock/"):
        return None
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return pid if script.encode() in args else None
    except OSError:
        return None


def ensure_service() -> dict:
    config = {name: os.environ.get(name, "") for name in (
        "AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_RELAY_BASE", "BEDROCK_RELAY_MODEL",
    )}
    if not all(config.values()):
        raise RuntimeError("Bedrock relay requires a credential, AWS base URL, and model")
    fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode() + SCRIPT.read_bytes()
    ).hexdigest()
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = STATE_DIR / "service.json"
    with (STATE_DIR / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        same_config = state.get("fingerprint") == fingerprint
        if same_config and owned_pid(state) and healthy(state):
            return {"token": state["token"]}
        if state and not same_config:
            running = subprocess.run([
                "docker", "ps", "--filter", "label=openshell.ai/managed-by=openshell",
                "--format", "{{.Names}}",
            ], capture_output=True, text=True, check=True, timeout=30)
            if running.stdout.strip():
                raise RuntimeError("Finish active OpenShell sandboxes before changing Bedrock model, credentials, or relay code")
        pid = owned_pid(state)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(40):
                if not owned_pid(state):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Previous owned Bedrock relay did not stop")
        token = state.get("token") if same_config else None
        token = token or secrets.token_hex(32)
        child_env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                     "HOME": "/root", "PYTHONUNBUFFERED": "1", "HTTP_BIND": "0.0.0.0",
                     "BEDROCK_RELAY_CLIENT_TOKEN": token,
                     "BEDROCK_RELAY_SERVICE_ID": fingerprint, **config}
        with (STATE_DIR / "service.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, str(SCRIPT), "8000"],
                                     env=child_env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        state = {"pid": child.pid, "script": str(SCRIPT), "token": token,
                 "fingerprint": fingerprint}
        for _ in range(40):
            if child.poll() is not None:
                raise RuntimeError("Bedrock relay exited; check /root/.redclaw-bedrock/service.log (port 8000 may be occupied)")
            if healthy(state):
                temporary = state_path.with_suffix(".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump(state, output)
                temporary.replace(state_path)
                return {"token": token}
            time.sleep(0.1)
        child.terminate()
        child.wait(timeout=10)
        raise RuntimeError("Bedrock relay did not become healthy")


if __name__ == "__main__":
    print(json.dumps(ensure_service()))
