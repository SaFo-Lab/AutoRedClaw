"""Codex CLI adapter for the RedClaw harness.

Codex exposes a stable non-interactive entry point (`codex exec`). Each case is
run inside a disposable container with the materialized fixture rootfs. The
outer Docker container is the security boundary, so the inner Codex sandbox is
disabled to give the evaluated agent the same rootfs-level capability as the
other claw adapters. Approval requests remain enabled and are routed through
Codex's automatic Guardian reviewer so batch runs do not require a human.

Native persistent instruction surfaces are intentionally narrow:

* project instructions: /workspace/AGENTS.md
* repository skills:    /workspace/.agents/skills/<name>/SKILL.md

Codex does not natively consume MEMORY.md, SOUL.md, or USER.md. This adapter
therefore rejects those logical targets rather than mapping them to AGENTS.md.

Authentication is never baked into the image. ``CodexAdapter.initialize()``
creates or validates a named Docker volume and starts native device auth when a
first interactive login is required. At prepare time only auth.json is copied
into the case-local CODEX_HOME. API key runs pass CODEX_API_KEY only to the
``docker exec`` process that launches Codex.
"""

from __future__ import annotations

from redclaw.paths import CLAWS_DIR, RUNTIME_DIR

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from redclaw.evaluation.core import (
    ClawAdapter,
    Observations,
    PreparedRun,
    RunResult,
    filesystem_workspace_targets,
    retry_transient,
)


CODEX_SRC = CLAWS_DIR / "codex"
MOCK_SINK_SRC = RUNTIME_DIR / "mock_sink.py"
IMAGE_TAG = "redclaw-claw-codex:latest"
CODEX_CLI_VERSION = "0.156.1"

RUNTIME_HOME = "/root"
CODEX_HOME = f"{RUNTIME_HOME}/.codex"
WORKSPACE = "/workspace"
SKILLS_ROOT = f"{WORKSPACE}/.agents/skills"

AUTO_REVIEW_CONFIG = "approvals_reviewer=auto_review"
EXECUTION_PROFILE = {
    "sandbox_mode": "danger-full-access",
    "approval_policy": "on-request",
    "approvals_reviewer": "auto_review",
    "execpolicy_rules": "enabled",
}

AUTH_MOUNT = "/run/redclaw-codex-auth"
DEFAULT_AUTH_VOLUME = "redclaw-codex-auth"
SINK_STATE = "/tmp/redclaw_sink.json"
TRAJECTORY_PATH = "/tmp/redclaw_codex_trajectory.jsonl"
PREFLIGHT_PROMPT = "Reply exactly REDCLAW_CODEX_READY. Do not use tools."
PREFLIGHT_TIMEOUT = 300


def _codex_exec_argv(*, model: str | None, task: str) -> list[str]:
    """Build the in-container Codex command for the reproducible eval profile.

    Docker is the filesystem/process isolation boundary, while Codex keeps its
    approval policy interactive and sends eligible approval requests to the
    automatic Guardian reviewer. Execpolicy rules intentionally remain enabled
    because rule-triggered prompts are one of the reviewer's approval inputs.
    """
    argv = [
        "codex",
        "--sandbox",
        "danger-full-access",
        "--ask-for-approval",
        "on-request",
        "-c",
        AUTO_REVIEW_CONFIG,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--cd",
        WORKSPACE,
    ]
    if model:
        argv += ["--model", model]
    argv.append(task)
    return argv


class CodexAdapter(ClawAdapter):
    name = "codex"
    supported_model_providers = ("codex", "openai")
    runtime_home = RUNTIME_HOME
    workspace_targets = filesystem_workspace_targets(WORKSPACE, ("AGENTS.md",))

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}
        self.image_tag = str(self.env.get("CODEX_IMAGE") or IMAGE_TAG)
        self.cli_version = str(self.env.get("CODEX_CLI_VERSION") or CODEX_CLI_VERSION)
        self._initialized = False

    def skills_roots(self) -> list[str]:
        # Repository-scoped skills are case-local and are natively discovered by
        # Codex from CWD up to the repository root.
        return [SKILLS_ROOT]

    def build_image(self, *, force: bool = False) -> str:
        image_tag = self.image_tag
        if not force:
            existing = subprocess.run(
                ["docker", "image", "inspect", image_tag],
                capture_output=True,
                text=True,
            )
            if existing.returncode == 0:
                version = subprocess.run(
                    ["docker", "run", "--rm", image_tag, "--version"],
                    capture_output=True,
                    text=True,
                )
                if (
                    version.returncode == 0
                    and version.stdout.strip().split()[-1:] == [self.cli_version]
                ):
                    return image_tag
        if not CODEX_SRC.exists():
            raise FileNotFoundError(f"Codex image source not found at {CODEX_SRC}")
        proc = subprocess.run(
            [
                "docker", "build", "--build-arg",
                f"CODEX_VERSION={self.cli_version}",
                "-t", image_tag, str(CODEX_SRC),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "docker build failed for Codex:\n"
                f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
            )
        return image_tag

    def _auth_ready(self, image: str, volume: str) -> bool:
        inspected = subprocess.run(
            ["docker", "volume", "inspect", volume],
            capture_output=True,
            text=True,
        )
        if inspected.returncode != 0:
            return False
        status = subprocess.run(
            [
                "docker", "run", "--rm", "-e", "HOME=/root",
                "-e", "CODEX_HOME=/root/.codex",
                "--mount", f"type=volume,source={volume},target=/root/.codex,readonly",
                image, "login", "status",
            ],
            capture_output=True,
            text=True,
        )
        return status.returncode == 0

    def _credentials(self) -> tuple[str, str | None]:
        """(auth volume, API key) for the selected provider; exactly one is set."""
        if self.model_provider == "openai":
            return "", self.provider_secret("OPENAI_API_KEY")
        return self.provider_secret("CODEX_AUTH_VOLUME").strip(), None

    def initialize(self, *, force: bool = False) -> str:
        """Build Codex and initialize native authentication when it is absent."""
        if self._initialized and not force:
            return self.image_tag
        image = self.build_image(force=force)
        if self.model_provider == "openai":
            self.provider_secret("OPENAI_API_KEY")
            self._initialized = True
            return image
        volume = str(self.env.get("CODEX_AUTH_VOLUME") or DEFAULT_AUTH_VOLUME).strip()
        self.env["CODEX_AUTH_VOLUME"] = volume
        force_login = str(self.env.get("CODEX_FORCE_LOGIN") or "").lower() in {
            "1", "true", "yes", "on",
        }
        if not force_login and self._auth_ready(image, volume):
            self._initialized = True
            return image
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RuntimeError(
                "Codex native sign-in is missing and requires one interactive "
                "device-auth flow. Re-run the same evaluation command in a TTY; "
                "CodexAdapter.initialize() will start it automatically."
            )
        subprocess.run(["docker", "volume", "create", volume], check=True)
        login = subprocess.run(
            [
                "docker", "run", "--rm", "-it", "-e", "HOME=/root",
                "-e", "CODEX_HOME=/root/.codex",
                "--mount", f"type=volume,source={volume},target=/root/.codex",
                image, "login", "--device-auth",
            ]
        )
        if login.returncode != 0 or not self._auth_ready(image, volume):
            raise RuntimeError("Codex native device authentication did not complete")
        self._initialized = True
        return image

    def preflight(self) -> dict[str, str]:
        """Check that the selected model completes a turn with this CLI and login.

        Uses the case command, credential source and execution profile in a
        throwaway container, so an unsupported model or a rejected login stops
        the pipeline before any case runs.
        """
        if not self.model:
            raise RuntimeError("Codex preflight requires an explicit model")
        image = self.initialize()
        cmd = [
            "docker", "run", "--rm", "--user", "0:0",
            "-e", f"HOME={RUNTIME_HOME}", "-e", f"CODEX_HOME={CODEX_HOME}",
        ]
        setup = [f"mkdir -p {shlex.quote(CODEX_HOME)} {shlex.quote(WORKSPACE)}"]
        auth_volume, api_key = self._credentials()
        if auth_volume:
            cmd += [
                "--mount",
                f"type=volume,source={auth_volume},target={AUTH_MOUNT},readonly",
            ]
            setup += [
                f"cp {AUTH_MOUNT}/auth.json {CODEX_HOME}/auth.json",
                f"chmod 600 {CODEX_HOME}/auth.json",
            ]
        elif not api_key:
            raise RuntimeError(
                "Codex is not authenticated. Set CODEX_AUTH_VOLUME to the volume "
                "managed by CodexAdapter.initialize(), or provide OPENAI_API_KEY."
            )
        if api_key:
            cmd += ["-e", f"CODEX_API_KEY={api_key}"]
        argv = _codex_exec_argv(model=self.model, task=PREFLIGHT_PROMPT)
        launch = " && ".join([*setup, f"exec {shlex.join(argv)}"])
        cmd += ["--entrypoint", "sh", image, "-lc", launch]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Codex preflight timed out after {PREFLIGHT_TIMEOUT}s for model {self.model!r}"
            ) from exc
        events = []
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        completed = any(e.get("type") == "turn.completed" for e in events)
        if proc.returncode != 0 or not completed:
            raise RuntimeError(
                f"Codex preflight failed for model {self.model!r} "
                f"(exit={proc.returncode}, turn.completed={completed}):\n"
                f"{(proc.stdout or '')[-4000:]}\n{(proc.stderr or '')[-4000:]}"
            )
        return {
            "model": self.model,
            "cli_version": self.cli_version,
            "auth_mode": "chatgpt" if auth_volume else "api_key",
        }

    def prepare(
        self,
        *,
        image: str,
        rootfs_dir: Path,
        test_mode: str,
        poisoned_memory: str | None,
        poisoned_target: str = "agents.md",
        network_hosts: list[str] | None = None,
        web: dict | None = None,
    ) -> PreparedRun:
        run_cmd = [
            "docker",
            "run",
            "-d",
            "--user",
            "0:0",
            "-e",
            f"HOME={RUNTIME_HOME}",
            "-e",
            f"CODEX_HOME={CODEX_HOME}",
        ]

        auth_volume, api_key = self._credentials()
        if auth_volume:
            inspected = subprocess.run(
                ["docker", "volume", "inspect", auth_volume],
                capture_output=True,
                text=True,
            )
            if inspected.returncode != 0:
                raise RuntimeError(
                    f"Codex auth volume {auth_volume!r} does not exist. "
                    "CodexAdapter.initialize() should have created it."
                )
            run_cmd += [
                "--mount",
                f"type=volume,source={auth_volume},target={AUTH_MOUNT},readonly",
            ]
        elif not api_key:
            raise RuntimeError(
                "Codex is not authenticated. Set CODEX_AUTH_VOLUME to the volume "
                "managed by CodexAdapter.initialize(), or provide OPENAI_API_KEY."
            )

        run_cmd += ["--entrypoint", "sleep", image, "infinity"]
        cid = subprocess.run(
            run_cmd, capture_output=True, text=True, check=True
        ).stdout.strip()
        prepared = PreparedRun(container_id=cid, workspace=WORKSPACE)
        prepared.extra["auth_mode"] = "chatgpt" if auth_volume else "api_key"

        try:
            self._exec(cid, "mkdir -p /Users /home /tmp /workspace /root/.codex")
            subprocess.run(
                ["docker", "cp", f"{str(rootfs_dir)}/.", f"{cid}:/"],
                capture_output=True,
                text=True,
                check=True,
            )

            if auth_volume:
                copied = self._exec(
                    cid,
                    f"test -f {AUTH_MOUNT}/auth.json && "
                    f"cp {AUTH_MOUNT}/auth.json {CODEX_HOME}/auth.json && "
                    f"chmod 600 {CODEX_HOME}/auth.json",
                )
                if copied.returncode != 0:
                    raise RuntimeError(
                        f"{auth_volume!r} contains no auth.json; rerun adapter initialization"
                    )
                status = self._exec(cid, "codex login status")
                if status.returncode != 0:
                    raise RuntimeError(
                        "Codex rejected the cached login: "
                        f"{(status.stderr or status.stdout).strip()}"
                    )

            self._exec(cid, f"mkdir -p {shlex.quote(SKILLS_ROOT)}")

            if test_mode == "trigger" and poisoned_memory:
                container_path = self.workspace_target_path(poisoned_target)
                self._write_file(cid, container_path, poisoned_memory)
                prepared.extra["seeded_memory"] = True
                prepared.extra["seeded_target"] = container_path

            all_hosts = list(
                dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or []))
            )
            if all_hosts or web:
                ca = self._start_sink(cid, all_hosts, web)
                prepared.extra["sink"] = SINK_STATE
                prepared.extra["network_hosts"] = all_hosts
                if ca:
                    prepared.extra["ca_cert"] = ca
            return prepared
        except Exception:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
            raise

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
            for source_key, destination, env_key in (
                ("site_meta", "/opt/site_meta.json", "REDCLAW_SITE_META"),
                ("app_behavior", "/opt/app_behavior.json", "REDCLAW_APP_BEHAVIOR"),
            ):
                if web.get(source_key):
                    subprocess.run(
                        ["docker", "cp", str(web[source_key]), f"{cid}:{destination}"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    env_prefix += f"{env_key}={destination} "

        for host in network_hosts:
            self._exec(
                cid,
                f"echo {shlex.quote('127.0.0.1 ' + host)} >> /etc/hosts",
            )

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
                f"{env_prefix}python3 /opt/mock_sink.py --port 80 "
                f"{https}--state {shlex.quote(SINK_STATE)}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for _ in range(30):
            ready = self._exec(
                cid,
                "curl -s -o /dev/null -w '%{http_code}' "
                "http://127.0.0.1/__sink__/state || true",
            )
            if ready.stdout.strip().endswith("200"):
                return tls.ca_cert
            time.sleep(0.2)
        return tls.ca_cert

    def run(self, prepared: PreparedRun, task: str, *, timeout: int) -> RunResult:
        cid = prepared.container_id
        cmd = [
            "docker",
            "exec",
            "-e",
            f"HOME={RUNTIME_HOME}",
            "-e",
            f"CODEX_HOME={CODEX_HOME}",
        ]
        _, api_key = self._credentials()
        if api_key:
            cmd += ["-e", f"CODEX_API_KEY={api_key}"]
        cmd += self.fixture_docker_env_args(prepared)
        cmd += [cid, *_codex_exec_argv(model=self.model, task=task)]

        start = time.monotonic()
        try:
            proc, retries = retry_transient(
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            )
            self._write_file(cid, TRAJECTORY_PATH, proc.stdout or "")
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=round(time.monotonic() - start, 2),
                extra={
                    "provider_retries": retries,
                    "auth_mode": prepared.extra.get("auth_mode"),
                    **EXECUTION_PROFILE,
                },
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                (exc.stdout or b"").decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                (exc.stderr or b"").decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            self._write_file(cid, TRAJECTORY_PATH, stdout)
            return RunResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                duration_s=round(time.monotonic() - start, 2),
                timed_out=True,
                extra={
                    "auth_mode": prepared.extra.get("auth_mode"),
                    **EXECUTION_PROFILE,
                },
            )

    def collect(self, prepared: PreparedRun, *, file_paths: list[str]) -> Observations:
        cid = prepared.container_id
        memory_sources, blob_parts, memory_attempted = self.read_workspace_memory(
            lambda path: self._stat_file(cid, path)
        )

        files: dict[str, str] = {}
        for path in file_paths:
            exists, content = self._stat_file(cid, path)
            if exists:
                files[path] = content

        state: dict = {}
        if prepared.extra.get("sink"):
            exists, content = self._stat_file(cid, prepared.extra["sink"])
            if exists and content.strip():
                try:
                    state = json.loads(content)
                except json.JSONDecodeError:
                    state = {}
        state["skills"] = self.read_skills(prepared)

        _, trajectory = self._stat_file(cid, TRAJECTORY_PATH)
        return Observations(
            memory_blob="\n\n".join(blob_parts),
            files=files,
            memory_sources=memory_sources,
            memory_attempted=memory_attempted,
            state=state,
            trajectory=trajectory,
        )

    def teardown(self, prepared: PreparedRun) -> None:
        subprocess.run(
            ["docker", "rm", "-f", prepared.container_id],
            capture_output=True,
            text=True,
        )

    def _exec(self, cid: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", cid, "sh", "-lc", command],
            capture_output=True,
            text=True,
        )

    def _stat_file(self, cid: str, path: str) -> tuple[bool, str]:
        quoted = shlex.quote(path)
        proc = subprocess.run(
            [
                "docker",
                "exec",
                cid,
                "sh",
                "-lc",
                f"if [ -f {quoted} ]; then echo __RC_PRESENT__; cat {quoted}; fi",
            ],
            capture_output=True,
            text=True,
        )
        if not proc.stdout.startswith("__RC_PRESENT__"):
            return False, ""
        return True, proc.stdout[len("__RC_PRESENT__\n") :]

    def _write_file(self, cid: str, path: str, content: str) -> None:
        quoted = shlex.quote(path)
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                cid,
                "sh",
                "-lc",
                f"mkdir -p $(dirname {quoted}) && cat > {quoted}",
            ],
            input=content,
            capture_output=True,
            text=True,
            check=True,
        )
