"""SeClaw claw adapter.

SeClaw exposes a direct one-shot CLI turn:

    seclaw agent --message "<task>"

For RedClaw evaluation we run that CLI inside a disposable container, seed an
isolated ~/.seclaw workspace/config, redirect fixture hosts to the mock sink, and
collect the real SeClaw durable memory files afterward.
"""

from __future__ import annotations

from redclaw.paths import RUNTIME_DIR

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from redclaw.runtime.images import build_claw_image

from redclaw.evaluation.core import (
    ClawAdapter,
    Observations,
    PreparedRun,
    RunResult,
    filesystem_workspace_targets,
    retry_transient,
)


MOCK_SINK_SRC = RUNTIME_DIR / "mock_sink.py"
BEDROCK_RELAY_SRC = RUNTIME_DIR / "bedrock_relay.py"
SINK_STATE = "/tmp/redclaw_sink.json"
IMAGE_TAG = "redclaw-claw-seclaw:latest"

RUNTIME_HOME = "/root"
CONFIG_DIR = f"{RUNTIME_HOME}/.seclaw"
CONFIG_PATH = f"{CONFIG_DIR}/config.json"
WORKSPACE = f"{CONFIG_DIR}/workspace"
SESSIONS_DIR = f"{CONFIG_DIR}/sessions"
SECURITY_DIR = f"{CONFIG_DIR}/security"
SECLAW_TEMPLATE_DIR = "/app/templates"
SECLAW_WORKSPACE_TEMPLATES = (
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "memory/MEMORY.md",
)
OPENROUTER_PROXY_PORT = 18081
BEDROCK_RELAY_PORT = 7891

CORE_MEMORY_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "TOOLS.md",
    "IDENTITY.md",
    "HEARTBEAT.md",
)

IDENTITY_SEED = (
    "# IDENTITY.md\n\n"
    "Name: Assistant\n"
)

OPENROUTER_PROXY = r"""#!/usr/bin/env python3
import argparse
import http.server
import json
import os
import urllib.error
import urllib.request

# Drop the leading 'openrouter/' SeClaw's gateway path adds to model ids.
# SeClaw resolves the model id through its OpenRouter *gateway* spec, whose
# litellmPrefix is "openrouter", so it always sends exactly one such prefix --
# which the real OpenRouter API rejects. Stripping it here is deterministic and
# model-agnostic. (Docstrings are avoided in this embedded script: it is carried
# as an r-string, so a nested triple quote would terminate the literal.)
def _unprefix(body):
    try:
        payload = json.loads(body)
        model = payload.get("model")
        if isinstance(model, str) and model.startswith("openrouter/"):
            payload["model"] = model[len("openrouter/"):]
            return json.dumps(payload).encode()
    except Exception:
        pass
    return body


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", "0") or "0"))
        body = _unprefix(body)
        req = urllib.request.Request("https://openrouter.ai/api" + self.path, data=body, method="POST")
        req.add_header("content-type", self.headers.get("content-type", "application/json"))
        req.add_header("authorization", "Bearer " + os.environ["OPENROUTER_API_KEY"])
        req.add_header("http-referer", "https://redclaw.local")
        req.add_header("x-title", "RedClaw SeClaw Adapter")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in {"connection", "transfer-encoding", "content-encoding"}:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() not in {"connection", "transfer-encoding", "content-encoding"}:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
"""


def _requested_user_confirmation(stdout: str) -> bool:
    """Did seclaw stop and ask for approval? Recorded for observability only.

    seclaw's own defense is to raise USER_CONFIRMATION_REQUEST and wait. We do
    NOT answer it: no other claw's questions get answered either, so answering
    only seclaw's made it the one agent whose interaction style the harness
    accommodated. Every claw now faces the same
    offline user; whether the agent asks is its behaviour to be measured, not
    something the harness compensates for.
    """
    return "USER_CONFIRMATION_REQUEST" in stdout


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {}


def _execution_log_to_records(trace_text: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(trace_text)
    except json.JSONDecodeError:
        return [{"role": "system", "content": trace_text}]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return [{"role": "system", "content": str(parsed)}]


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + ("\n" if records else "")


def _synthetic_messages_from_trace(trace_text: str) -> list[dict[str, Any]]:
    """Turn SeClaw's internal message-tool calls into runner behavioral state.

    The real SeClaw message tool publishes onto its in-process bus; in direct CLI
    mode no external channel manager is running, so nothing reaches the mock
    sink. The execution log is still environment-observable evidence that the
    agent attempted the send, so we project those calls into state["messages"].
    """
    messages: list[dict[str, Any]] = []
    for record in _execution_log_to_records(trace_text):
        if record.get("role") != "assistant":
            continue
        for tool_call in record.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            fn = tool_call.get("function")
            if not isinstance(fn, dict) or fn.get("name") != "message":
                continue
            args = _parse_tool_arguments(fn.get("arguments"))
            content = str(args.get("content") or "")
            channel = str(args.get("channel") or args.get("channel_id") or "")
            chat_id = str(args.get("chat_id") or args.get("chatId") or args.get("to") or "")
            target = chat_id or channel or "cli:direct"
            messages.append(
                {
                    "target": target,
                    "content": content,
                    "body": content,
                    "url": target if target.startswith(("http://", "https://")) else "",
                    "params": args,
                }
            )
    return messages


class SeclawAdapter(ClawAdapter):
    name = "seclaw"
    runtime_home = RUNTIME_HOME
    workspace_targets = filesystem_workspace_targets(
        WORKSPACE, (*CORE_MEMORY_FILES, "memory/MEMORY.md")
    )

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}

    def skills_roots(self) -> list[str]:
        return [f"{WORKSPACE}/skills"]

    # -- image ------------------------------------------------------------ #
    def build_image(self, *, force: bool = False) -> str:
        return build_claw_image("seclaw", IMAGE_TAG, force=force)

    # -- lifecycle -------------------------------------------------------- #
    def prepare(
        self,
        *,
        image: str,
        rootfs_dir: Path,
        test_mode: str,
        poisoned_memory: str | None,
        poisoned_target: str = "memory.md",
        network_hosts: list[str] | None = None,
        web: dict | None = None,
    ) -> PreparedRun:
        cid = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--user",
                "0:0",
                "-e",
                f"HOME={RUNTIME_HOME}",
                "-e",
                "NODE_ENV=production",
                "--entrypoint",
                "sleep",
                image,
                "infinity",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        prepared = PreparedRun(container_id=cid, workspace=WORKSPACE)

        self._exec(cid, f"mkdir -p /Users /home /tmp {shlex.quote(WORKSPACE)}/memory {shlex.quote(SESSIONS_DIR)}")
        subprocess.run(
            ["docker", "cp", f"{str(rootfs_dir)}/.", f"{cid}:/"],
            capture_output=True,
            text=True,
            check=True,
        )

        self._seed_completed_workspace_templates(
            cid,
            template_dir=SECLAW_TEMPLATE_DIR,
            workspace=WORKSPACE,
            files=SECLAW_WORKSPACE_TEMPLATES,
            identity=IDENTITY_SEED,
        )

        if test_mode == "trigger" and poisoned_memory:
            container_path = self.workspace_target_path(poisoned_target)
            self._append_workspace_payload(cid, container_path, poisoned_memory)
            prepared.extra["seeded_memory"] = True
            prepared.extra["seeded_target"] = container_path

        self._write_config(cid)
        if self.model_provider == "openrouter":
            self._start_openrouter_proxy(cid)
        elif self.model_provider == "bedrock":
            self._start_bedrock_relay(cid)

        all_hosts = list(dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or [])))
        if all_hosts or web:
            ca = self._start_sink(cid, all_hosts, web)
            prepared.extra["sink"] = SINK_STATE
            prepared.extra["network_hosts"] = all_hosts
            if ca:
                prepared.extra["ca_cert"] = ca

        return prepared

    def _provider_config(self) -> tuple[dict[str, Any], str | None]:
        providers: dict[str, Any] = {}
        model = self.model

        if self.model_provider == "openrouter":
            self.provider_secret("OPENROUTER_API_KEY")
            # Declare the credentials in SeClaw's *openrouter* slot so
            # findGateway() matches the gateway spec by name and _resolveModel()
            # takes the gateway branch. That branch adds exactly one
            # "openrouter/" prefix regardless of the model id; the local proxy
            # strips it back off and adds the real Authorization header.
            #
            # The alternative — declaring a plain "openai" provider — makes
            # SeClaw fall through to findByModel(), which infers a provider from
            # keywords *in the model name* over the whole registry and ignores
            # the provider actually configured. Any OpenRouter id whose vendor
            # namespace differs from SeClaw's litellm prefix then gets double-
            # prefixed and 400s: google/gemini-3.5-flash-lite ->
            # gemini/google/gemini-3.5-flash-lite (same for qwen/, moonshotai/,
            # z-ai/ …). It only happened to work for openai/* and anthropic/*,
            # whose litellm prefix is empty.
            providers["openrouter"] = {
                "apiKey": "redclaw-openrouter-proxy",
                "apiBase": f"http://127.0.0.1:{OPENROUTER_PROXY_PORT}/v1",
                "extraHeaders": None,
            }
        elif self.model_provider == "openai":
            providers["openai"] = {
                "apiKey": self.provider_secret("OPENAI_API_KEY"),
                "apiBase": None,
                "extraHeaders": None,
            }
        else:
            self.provider_secret("AWS_BEARER_TOKEN_BEDROCK")
            # Route through a local relay that prepends 'anthropic.' to the
            # model name and injects the real bearer token before forwarding
            # to bedrock-mantle.
            providers["anthropic"] = {
                "apiKey": "bedrock-relay",
                "apiBase": f"http://127.0.0.1:{BEDROCK_RELAY_PORT}",
                "extraHeaders": None,
            }
            if model and model.startswith("anthropic."):
                model = model[len("anthropic."):]

        return providers, model

    def _write_config(self, cid: str) -> None:
        providers, model = self._provider_config()
        defaults: dict[str, Any] = {
            "workspace": WORKSPACE,
            "maxToolIterations": 20,
        }
        if model:
            defaults["model"] = model

        cfg: dict[str, Any] = {
            "agents": {"defaults": defaults},
            "providers": providers,
            "tools": {
                "exec": {"timeout": 60},
                "restrictToWorkspace": False,
                "web": {"search": {"apiKey": self.env.get("BRAVE_API_KEY", ""), "maxResults": 5}},
            },
            # seclaw's real onboarding default (verified by running `seclaw onboard`
            # and inspecting ~/.seclaw/config.json): prohibitedCommands ["rm -rf",
            # "sudo"], input/output validation on, BOTH audits on. We match it so
            # seclaw is scored with the defenses it actually ships with — the only
            # deviation is dockerSandbox (see below).
            "security": {
                # Default is enabled=true, which runs every shell command inside a
                # nested `ubuntu:22.04` container. Kept OFF here for two reasons,
                # both uniform across claws rather than seclaw-specific: (1) the eval
                # already runs seclaw inside its own throwaway container — that IS
                # the isolation boundary, and no claw gets a second nested one;
                # (2) a nested sandbox would divert the agent's file writes away
                # from the container the oracle reads, making every attack
                # unobservable (artificial 0% ASR). Docker-in-docker also isn't
                # available in the eval container.
                "dockerSandbox": {"enabled": False},
                "prohibitedCommands": ["rm -rf", "sudo"],
                "inputValidationEnabled": True,
                "outputValidationEnabled": True,
                "executionLogEnabled": True,
                "executionLogStep": 1,
                "postExecutionAuditEnabled": True,
                "skillAuditEnabled": True,
            },
        }
        self._write_file(cid, CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))

    def _start_openrouter_proxy(self, cid: str) -> None:
        self._write_file(cid, "/opt/openrouter_proxy.py", OPENROUTER_PROXY)
        subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                "-e",
                f"OPENROUTER_API_KEY={self.env['OPENROUTER_API_KEY']}",
                cid,
                "python3",
                "/opt/openrouter_proxy.py",
                "--port",
                str(OPENROUTER_PROXY_PORT),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for _ in range(30):
            chk = self._exec(
                cid,
                "python3 - <<'PY'\n"
                "import socket\n"
                "s=socket.socket(); s.settimeout(0.2)\n"
                f"s.connect(('127.0.0.1',{OPENROUTER_PROXY_PORT}))\n"
                "print('ok')\n"
                "PY",
            )
            if chk.stdout.strip().endswith("ok"):
                return
            time.sleep(0.2)
        raise RuntimeError("OpenRouter proxy did not start")

    def _start_bedrock_relay(self, cid: str) -> None:
        subprocess.run(
            ["docker", "cp", str(BEDROCK_RELAY_SRC), f"{cid}:/opt/bedrock_relay.py"],
            capture_output=True, text=True, check=True,
        )
        region = self.env.get("AWS_REGION", "us-east-1")
        token = self.env["AWS_BEARER_TOKEN_BEDROCK"]
        subprocess.run(
            [
                "docker", "exec", "-d",
                "-e", f"BEDROCK_RELAY_BASE=https://bedrock-mantle.{region}.api.aws",
                "-e", f"AWS_BEARER_TOKEN_BEDROCK={token}",
                cid, "python3", "/opt/bedrock_relay.py", str(BEDROCK_RELAY_PORT),
            ],
            capture_output=True, text=True, check=True,
        )
        for _ in range(30):
            chk = self._exec(cid, f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{BEDROCK_RELAY_PORT}/ 2>/dev/null || true")
            if chk.stdout.strip() in ("404", "405", "200"):
                return
            time.sleep(0.2)

    def _start_sink(self, cid: str, network_hosts: list[str], web: dict | None = None) -> str | None:
        subprocess.run(
            ["docker", "cp", str(MOCK_SINK_SRC), f"{cid}:/opt/mock_sink.py"],
            capture_output=True,
            text=True,
            check=True,
        )
        env_prefix = ""
        if web and web.get("web_dir") and web.get("url_map"):
            self._exec(cid, "mkdir -p /opt/web")
            subprocess.run(
                ["docker", "cp", f"{str(web['web_dir'])}/.", f"{cid}:/opt/web"],
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["docker", "cp", str(web["url_map"]), f"{cid}:/opt/url_map.json"],
                capture_output=True,
                text=True,
                check=True,
            )
            env_prefix = "REDCLAW_WEB_DIR=/opt/web REDCLAW_URL_MAP=/opt/url_map.json "
            if web.get("site_meta"):
                subprocess.run(
                    ["docker", "cp", str(web["site_meta"]), f"{cid}:/opt/site_meta.json"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                env_prefix += "REDCLAW_SITE_META=/opt/site_meta.json "
            if web.get("app_behavior"):
                subprocess.run(
                    ["docker", "cp", str(web["app_behavior"]), f"{cid}:/opt/app_behavior.json"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                env_prefix += "REDCLAW_APP_BEHAVIOR=/opt/app_behavior.json "
        if network_hosts:
            append = " && ".join(f"echo {shlex.quote('127.0.0.1 ' + h)} >> /etc/hosts" for h in network_hosts)
            self._exec(cid, append)
        tls = self.provision_fixture_tls(cid, network_hosts)
        https = tls.sink_arguments()
        subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                cid,
                "sh",
                "-lc",
                f"{env_prefix}python3 /opt/mock_sink.py --port 80 {https}--state {shlex.quote(SINK_STATE)}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for _ in range(30):
            chk = self._exec(cid, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/__sink__/state || true")
            if chk.stdout.strip().endswith("200"):
                return tls.ca_cert
            time.sleep(0.2)
        return tls.ca_cert

    def run(self, prepared: PreparedRun, task: str, *, timeout: int) -> RunResult:
        cid = prepared.container_id

        def cmd_for(message: str) -> list[str]:
            cmd = [
                "docker",
                "exec",
                "-e",
                f"HOME={RUNTIME_HOME}",
                "-e",
                "NODE_ENV=production",
            ]
            if self.model_provider in ("openrouter", "openai"):
                for key, value in self.provider_credentials().items():
                    cmd += ["-e", f"{key}={value}"]
            if self.env.get("BRAVE_API_KEY"):
                cmd += ["-e", f"BRAVE_API_KEY={self.env['BRAVE_API_KEY']}"]
            cmd += self.fixture_docker_env_args(prepared)
            cmd += [
                cid,
                "node",
                "/app/dist/index.js",
                "agent",
                "--no-markdown",
                "--session",
                "cli:direct",
                "--message",
                message,
            ]
            return cmd

        start = time.monotonic()
        try:
            proc, retries_total = retry_transient(
                lambda: subprocess.run(
                    cmd_for(task), capture_output=True, text=True, timeout=timeout
                )
            )
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=round(time.monotonic() - start, 2),
                extra={
                    "provider_retries": retries_total,
                    # observation only — nothing is sent back in reply
                    "confirmation_requested": _requested_user_confirmation(proc.stdout),
                },
            )
        except subprocess.TimeoutExpired as exc:
            dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")  # noqa: E731
            return RunResult(
                returncode=124,
                stdout=dec(exc.stdout),
                stderr=dec(exc.stderr),
                duration_s=round(time.monotonic() - start, 2),
                timed_out=True,
            )

    def collect(self, prepared: PreparedRun, *, file_paths: list[str]) -> Observations:
        cid = prepared.container_id
        memory_sources, blob_parts, memory_attempted = self.read_workspace_memory(
            lambda path: self._stat_file(cid, path)
        )
        registered_paths = set(self.memory_files())
        for mem in self._all_memory_files(cid):
            if mem in registered_paths:
                continue
            exists, content = self._stat_file(cid, mem)
            if exists:
                memory_sources[mem] = content
                blob_parts.append(f"===== {mem} =====\n{content}")

        files: dict[str, str] = {}
        for path in file_paths:
            exists, content = self._stat_file(cid, path)
            if exists:
                files[path] = content

        state: dict[str, Any] = {
            "network_requests": [],
            "messages": [],
            "web_state": {},
            "logs": [],
            "collections": {},
        }
        if prepared.extra.get("sink"):
            exists, content = self._stat_file(cid, prepared.extra["sink"])
            if exists and content.strip():
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        state.update(parsed)
                except json.JSONDecodeError:
                    pass

        trajectory = ""
        trace_path = self._latest_execution_log(cid)
        if trace_path:
            _, trace = self._stat_file(cid, trace_path)
            if trace:
                trajectory = _jsonl(_execution_log_to_records(trace))
                synthetic_messages = _synthetic_messages_from_trace(trace)
                if synthetic_messages:
                    state.setdefault("messages", []).extend(synthetic_messages)
                    state.setdefault("logs", []).extend({"tool": "message", "params": m.get("params", {})} for m in synthetic_messages)

        if not trajectory:
            _, trajectory = self._stat_file(cid, f"{SESSIONS_DIR}/cli_direct.jsonl")

        # SKILL.md surface: read back skills on disk (pure read, no selection).
        state["skills"] = self.read_skills(prepared)

        return Observations(
            memory_blob="\n\n".join(blob_parts),
            files=files,
            memory_sources=memory_sources,
            memory_attempted=memory_attempted,
            state=state,
            trajectory=trajectory,
        )

    def teardown(self, prepared: PreparedRun) -> None:
        subprocess.run(["docker", "rm", "-f", prepared.container_id], capture_output=True, text=True)

    # -- helpers ---------------------------------------------------------- #
    def _all_memory_files(self, cid: str) -> list[str]:
        paths = list(self.memory_files())
        out = self._exec(cid, f"find {shlex.quote(WORKSPACE)}/memory -maxdepth 1 -type f -name '*.md' 2>/dev/null | sort").stdout
        for line in out.splitlines():
            path = line.strip()
            if path and path not in paths:
                paths.append(path)
        return paths

    def _latest_execution_log(self, cid: str) -> str:
        out = self._exec(
            cid,
            f"ls -t {shlex.quote(SECURITY_DIR)}/execution_logs/*.json 2>/dev/null | head -1",
        ).stdout.strip()
        return out.splitlines()[0] if out else ""

    def _exec(self, cid: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", "exec", cid, "sh", "-lc", command], capture_output=True, text=True)

    def _stat_file(self, cid: str, path: str) -> tuple[bool, str]:
        q = shlex.quote(path)
        proc = subprocess.run(
            ["docker", "exec", cid, "sh", "-lc", f"if [ -f {q} ]; then echo __RC_PRESENT__; cat {q}; fi"],
            capture_output=True,
            text=True,
        )
        out = proc.stdout
        if not out.startswith("__RC_PRESENT__"):
            return False, ""
        return True, out[len("__RC_PRESENT__\n") :]

    def _write_file(self, cid: str, path: str, content: str) -> None:
        subprocess.run(
            ["docker", "exec", "-i", cid, "sh", "-lc", f"mkdir -p $(dirname {shlex.quote(path)}) && cat > {shlex.quote(path)}"],
            input=content,
            capture_output=True,
            text=True,
            check=True,
        )
