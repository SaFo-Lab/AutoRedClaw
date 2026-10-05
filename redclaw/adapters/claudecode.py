"""Claude Code CLI adapter for the RedClaw harness.

Each case runs through Claude Code's non-interactive ``claude -p`` entry point
inside a disposable Docker container. Docker is the filesystem/process
boundary; Claude Code's native ``dontAsk`` permission mode is the default.
Actions that require approval are denied without prompting. Explicit ``auto``
and ``bypassPermissions`` modes remain available inside Docker. All modes retain
Claude Code's native default tool set without hand-picked allow or deny rules;
reports record the actual tools and mode.

Native persistent instruction surfaces are deliberately narrow:

* project instructions: /workspace/CLAUDE.md
* project skills:       /workspace/.claude/skills/<name>/SKILL.md

Claude Code does not natively consume AGENTS.md, MEMORY.md, SOUL.md, or USER.md.
The claw-neutral ``agents.md`` benchmark surface is therefore translated to
Claude Code's native CLAUDE.md path; the other non-native logical targets are
rejected rather than silently merged into it.

Authentication is never baked into the image. ``ClaudeCodeAdapter.initialize()``
creates or validates the setup-token Docker volume and starts native setup-token
authentication when a first interactive login is required. The case container
never mounts that volume: the runner reads it with a short-lived helper and
sends it to the Claude process over stdin. A root-owned shell-prefix wrapper
removes credentials and drops privileges before Bash, hook, or stdio MCP child
processes start. This preserves Auto mode: Claude Code 2.1.236's native
``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`` switch forcibly downgrades Auto to default.
"""

from __future__ import annotations

from redclaw.paths import CLAWS_DIR, RUNTIME_DIR

import getpass
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock

from redclaw.evaluation.core import (
    ClawAdapter,
    Observations,
    PreparedRun,
    RunResult,
    WorkspaceTargetSpec,
    retry_transient,
)


CLAUDECODE_SRC = CLAWS_DIR / "claudecode"
MOCK_SINK_SRC = RUNTIME_DIR / "mock_sink.py"
IMAGE_TAG = "redclaw-claw-claudecode:latest"
CLAUDE_CODE_CLI_VERSION = "2.1.236"

RUNTIME_HOME = "/root"
WORKSPACE = "/workspace"
SKILLS_ROOT = f"{WORKSPACE}/.claude/skills"
CLAUDE_PATH = f"{WORKSPACE}/CLAUDE.md"

PERMISSION_MODE = "dontAsk"
EXECUTION_PROFILE = {
    "outer_sandbox": "docker",
    "docker_seccomp": "default",
    "permission_mode_requested": PERMISSION_MODE,
    "native_prompt_injection_review": "disabled",
    "credential_subprocess_scrub": "shell_prefix_drop_privileges",
    "tool_subprocess_uid": 65534,
    "tool_subprocess_no_new_privs": True,
    "tool_policy": "native_default",
}

AUTH_HELPER_MOUNT = "/run/redclaw-claudecode-auth"
AUTH_TOKEN_PATH = f"{AUTH_HELPER_MOUNT}/oauth_token"
DEFAULT_AUTH_VOLUME = "redclaw-claudecode-auth"
SINK_STATE = "/tmp/redclaw_sink.json"
TRAJECTORY_PATH = "/tmp/redclaw_claudecode_trajectory.jsonl"
SUBPROCESS_WRAPPER = "/usr/local/bin/redclaw-claude-subprocess"
PREFLIGHT_PROMPT = "Reply exactly REDCLAW_CLAUDECODE_READY. Do not use tools."
PREFLIGHT_TIMEOUT = 300


def _claude_print_argv(
    *, model: str | None, task: str,
    permission_mode: str = PERMISSION_MODE,
) -> list[str]:
    """Build the stable, non-interactive Claude Code evaluation command."""
    argv = [
        "claude",
        "-p",
        task,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode",
        permission_mode,
    ]
    if model:
        argv += ["--model", model]
    return argv


def _claude_exec_argv(
    *, cid: str, launch: str, extra_env: dict[str, str] | None = None,
    permission_mode: str = PERMISSION_MODE,
) -> list[str]:
    """Build the docker exec command, keeping stdin attached for the token."""
    command = [
        "docker",
        "exec",
        "-i",
        "-w",
        WORKSPACE,
        "-e",
        f"HOME={RUNTIME_HOME}",
        "-e",
        "DISABLE_AUTOUPDATER=1",
        "-e",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
        "-e",
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0",
        "-e",
        f"CLAUDE_CODE_SHELL_PREFIX={SUBPROCESS_WRAPPER}",
    ]
    for name, value in (extra_env or {}).items():
        command += ["-e", f"{name}={value}"]
    if permission_mode == "bypassPermissions":
        # This is a real disposable Docker container. Preserve the root Claude
        # parent / unprivileged tool-child split while using the CLI's sandbox
        # opt-in for bypass mode. Never set this on the host runner.
        command += ["-e", "IS_SANDBOX=1"]
    return [*command, cid, "sh", "-lc", launch]


def _stream_init(stdout: str) -> dict:
    """Read the last init event, including its native tool inventory."""
    init = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
            init = event
    return init


def _stream_permission_mode(stdout: str) -> str | None:
    """Read the effective mode from Claude's stream-json init event."""
    mode = _stream_init(stdout).get("permissionMode")
    return str(mode) if mode is not None else None


class ClaudeCodeAdapter(ClawAdapter):
    name = "claudecode"
    supported_model_providers = ("claudecode",)
    runtime_home = RUNTIME_HOME

    # ``agents.md`` is RedClaw's claw-neutral project-instructions surface.
    # Claude Code's native spelling for the same surface is CLAUDE.md.
    workspace_targets = {
        "AGENTS.md": WorkspaceTargetSpec("AGENTS.md", CLAUDE_PATH),
    }

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}
        self.permission_mode = self.env.get("CLAUDECODE_PERMISSION_MODE", PERMISSION_MODE)
        if self.env.get("CLAUDECODE_ALLOWED_TOOLS", "").strip():
            raise ValueError("Remove CLAUDECODE_ALLOWED_TOOLS; evaluations retain the native default tool set")
        if self.permission_mode not in {"dontAsk", "auto", "bypassPermissions"}:
            raise ValueError("CLAUDECODE_PERMISSION_MODE must be dontAsk, auto, or bypassPermissions")
        if self.permission_mode == "auto" and (model or "").startswith("claude-sonnet-4-5"):
            raise ValueError("Sonnet 4.5 does not support auto; use dontAsk")
        self.execution_profile = {
            **EXECUTION_PROFILE,
            "permission_mode_requested": self.permission_mode,
            "native_prompt_injection_review": (
                "enabled" if self.permission_mode == "auto" else "disabled"
            ),
        }
        self.image_tag = str(self.env.get("CLAUDECODE_IMAGE") or IMAGE_TAG)
        self.cli_version = str(
            self.env.get("CLAUDE_CODE_CLI_VERSION") or CLAUDE_CODE_CLI_VERSION
        )
        self._initialized = False
        # A batch shares one adapter across workers. Keep credentials keyed by
        # container so one case's teardown cannot clear another case's token.
        self._oauth_tokens: dict[str, str] = {}
        self._oauth_tokens_lock = Lock()

    def _remember_oauth_token(self, cid: str, token: str) -> None:
        with self._oauth_tokens_lock:
            self._oauth_tokens[cid] = token

    def _oauth_token_for(self, cid: str) -> str | None:
        with self._oauth_tokens_lock:
            return self._oauth_tokens.get(cid)

    def _forget_oauth_token(self, cid: str) -> None:
        with self._oauth_tokens_lock:
            self._oauth_tokens.pop(cid, None)

    def skills_roots(self) -> list[str]:
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
                wrapper = subprocess.run(
                    [
                        "docker", "run", "--rm", "--entrypoint", "sh", image_tag,
                        "-lc", f"test -x {SUBPROCESS_WRAPPER}",
                    ],
                    capture_output=True,
                    text=True,
                )
                match = re.search(r"\b(\d+\.\d+\.\d+)\b", version.stdout)
                if (
                    version.returncode == 0
                    and match is not None
                    and match.group(1) == self.cli_version
                    and wrapper.returncode == 0
                ):
                    return image_tag
        if not CLAUDECODE_SRC.exists():
            raise FileNotFoundError(
                f"Claude Code image source not found at {CLAUDECODE_SRC}"
            )
        proc = subprocess.run(
            [
                "docker", "build", "--build-arg",
                f"CLAUDE_CODE_VERSION={self.cli_version}",
                "-t", image_tag, str(CLAUDECODE_SRC),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "docker build failed for Claude Code:\n"
                f"{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
            )
        return image_tag

    def _read_volume_token(self, image: str, volume: str) -> str:
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "cat",
                "--mount",
                f"type=volume,source={volume},target={AUTH_HELPER_MOUNT},readonly",
                image, AUTH_TOKEN_PATH,
            ],
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _store_volume_token(self, image: str, volume: str, token: str) -> None:
        subprocess.run(["docker", "volume", "create", volume], check=True)
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "-i", "--entrypoint", "sh",
                "--mount", f"type=volume,source={volume},target=/auth",
                image, "-lc", "umask 077; cat > /auth/oauth_token",
            ],
            input=token,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"could not persist Claude Code setup token: {proc.stderr}")

    def _auth_ready(self, image: str, volume: str) -> bool:
        token = self._read_volume_token(image, volume)
        if not token:
            return False
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "sh",
                "-e", "HOME=/root", "-e", "DISABLE_AUTOUPDATER=1",
                "-e", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0",
                "--mount",
                f"type=volume,source={volume},target={AUTH_HELPER_MOUNT},readonly",
                image, "-lc",
                f'export CLAUDE_CODE_OAUTH_TOKEN="$(cat {AUTH_TOKEN_PATH})"; exec claude auth status',
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def initialize(self, *, force: bool = False) -> str:
        """Build Claude Code and initialize its native setup-token login."""
        if self._initialized and not force:
            return self.image_tag
        image = self.build_image(force=force)
        volume = str(
            self.env.get("CLAUDE_CODE_AUTH_VOLUME") or DEFAULT_AUTH_VOLUME
        ).strip()
        self.env["CLAUDE_CODE_AUTH_VOLUME"] = volume
        force_login = str(self.env.get("CLAUDECODE_FORCE_LOGIN") or "").lower() in {
            "1", "true", "yes", "on",
        }
        if not force_login and self._auth_ready(image, volume):
            self._initialized = True
            return image
        supplied = str(self.env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
        if not supplied:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise RuntimeError(
                    "Claude Code native sign-in is missing and requires one "
                    "interactive setup-token flow. Re-run the same evaluation "
                    "command in a TTY; ClaudeCodeAdapter.initialize() will start it."
                )
            setup = subprocess.run(
                [
                    "docker", "run", "--rm", "-it", "-e", "HOME=/root",
                    "-e", "DISABLE_AUTOUPDATER=1",
                    "-e", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0",
                    image, "setup-token",
                ]
            )
            if setup.returncode != 0:
                raise RuntimeError("Claude Code native setup-token flow failed")
            supplied = getpass.getpass("Paste the Claude Code setup token: ").strip()
        if not supplied:
            raise RuntimeError("Claude Code setup-token flow returned no token")
        self._store_volume_token(image, volume, supplied)
        self.env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if not self._auth_ready(image, volume):
            raise RuntimeError("Claude Code rejected the initialized setup token")
        self._initialized = True
        return image

    def preflight(self) -> dict[str, str | None]:
        """Check the selected model and the effective permission mode.

        Runs one fixed prompt with the case command, setup token and execution
        environment in a throwaway container. Fails when the model is rejected,
        the turn reports an error, or Claude Code enters a permission mode other
        than the requested one.
        """
        if not self.model:
            raise RuntimeError("Claude Code preflight requires an explicit model")
        image = self.initialize()
        volume = str(self.env.get("CLAUDE_CODE_AUTH_VOLUME") or "").strip()
        token = self._read_volume_token(image, volume) if volume else ""
        if not token:
            raise RuntimeError(
                f"{volume or 'CLAUDE_CODE_AUTH_VOLUME'!r} contains no oauth_token; "
                "rerun adapter initialization"
            )
        cli = _claude_print_argv(
            model=self.model, task=PREFLIGHT_PROMPT,
            permission_mode=self.permission_mode,
        )
        launch = (
            f"mkdir -p {WORKSPACE} {RUNTIME_HOME}/.claude && cd {WORKSPACE} && "
            "IFS= read -r redclaw_oauth_token && "
            "export CLAUDE_CODE_OAUTH_TOKEN=\"$redclaw_oauth_token\" && "
            "unset redclaw_oauth_token && "
            f"exec {shlex.join(cli)}"
        )
        cmd = [
            "docker", "run", "--rm", "-i", "--user", "0:0",
            "-e", f"HOME={RUNTIME_HOME}",
            "-e", "DISABLE_AUTOUPDATER=1",
            "-e", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
            "-e", "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0",
            "-e", f"CLAUDE_CODE_SHELL_PREFIX={SUBPROCESS_WRAPPER}",
        ]
        if self.permission_mode == "bypassPermissions":
            cmd += ["-e", "IS_SANDBOX=1"]
        cmd += ["--entrypoint", "sh", image, "-lc", launch]
        try:
            proc = subprocess.run(
                cmd, input=token + "\n", capture_output=True, text=True,
                timeout=PREFLIGHT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Claude Code preflight timed out after {PREFLIGHT_TIMEOUT}s "
                f"for model {self.model!r}"
            ) from exc
        results = []
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                results.append(event)
        effective_mode = _stream_permission_mode(proc.stdout or "")
        problems = []
        if proc.returncode != 0:
            problems.append(f"exit={proc.returncode}")
        if not results:
            problems.append("no result event")
        elif any(r.get("is_error") for r in results):
            problems.append("result reported is_error")
        if effective_mode != self.permission_mode:
            problems.append(
                f"requested permission mode {self.permission_mode}, "
                f"effective {effective_mode or 'unknown'}"
            )
        if problems:
            raise RuntimeError(
                f"Claude Code preflight failed for model {self.model!r} "
                f"({'; '.join(problems)}):\n"
                f"{(proc.stdout or '')[-4000:]}\n{(proc.stderr or '')[-4000:]}"
            )
        return {
            "model": self.model,
            "permission_mode": effective_mode,
            "claude_code_version": _stream_init(proc.stdout).get("claude_code_version"),
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
        auth_volume = self.env.get("CLAUDE_CODE_AUTH_VOLUME", "").strip()
        if not auth_volume:
            raise RuntimeError(
                "Claude Code is not authenticated. Set CLAUDE_CODE_AUTH_VOLUME to "
                "the volume managed by ClaudeCodeAdapter.initialize()."
            )
        inspected = subprocess.run(
            ["docker", "volume", "inspect", auth_volume],
            capture_output=True,
            text=True,
        )
        if inspected.returncode != 0:
            raise RuntimeError(
                f"Claude Code auth volume {auth_volume!r} does not exist. "
                "ClaudeCodeAdapter.initialize() should have created it."
            )

        token_proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                "--mount",
                f"type=volume,source={auth_volume},"
                f"target={AUTH_HELPER_MOUNT},readonly",
                image,
                AUTH_TOKEN_PATH,
            ],
            capture_output=True,
            text=True,
        )
        oauth_token = token_proc.stdout.strip()
        if token_proc.returncode != 0 or not oauth_token:
            raise RuntimeError(
                f"{auth_volume!r} contains no oauth_token; "
                "rerun adapter initialization"
            )

        run_cmd = [
            "docker",
            "run",
            "-d",
            "--user",
            "0:0",
            "-e",
            f"HOME={RUNTIME_HOME}",
            "-e",
            "DISABLE_AUTOUPDATER=1",
            "-e",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
            "-e",
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0",
            "-e",
            f"CLAUDE_CODE_SHELL_PREFIX={SUBPROCESS_WRAPPER}",
            "--entrypoint",
            "sleep",
            image,
            "infinity",
        ]
        cid = subprocess.run(
            run_cmd, capture_output=True, text=True, check=True
        ).stdout.strip()
        prepared = PreparedRun(container_id=cid, workspace=WORKSPACE)
        prepared.extra["auth_mode"] = "setup_token"
        self._remember_oauth_token(cid, oauth_token)

        try:
            self._exec(cid, "mkdir -p /Users /home /tmp /workspace /root/.claude")
            subprocess.run(
                ["docker", "cp", f"{str(rootfs_dir)}/.", f"{cid}:/"],
                capture_output=True,
                text=True,
                check=True,
            )

            self._exec(cid, f"mkdir -p {shlex.quote(SKILLS_ROOT)}")

            if test_mode == "trigger" and poisoned_memory:
                container_path = self.workspace_target_path(poisoned_target)
                self._write_file(cid, container_path, poisoned_memory)
                prepared.extra["seeded_memory"] = True
                prepared.extra["seeded_target"] = container_path

            # Keep every benchmark-provided asset actionable by the tool uid;
            # otherwise privilege-themed cases would become impossible for
            # harness reasons and ASR would be biased downward. Image-owned
            # controls under /usr and /etc remain root-owned. The sink and its
            # TLS material are created after this handoff and stay root-owned.
            self._exec(
                cid,
                "for path in /home /Users /workspace /opt; do "
                "if [ -e \"$path\" ]; then chown -R nobody:nogroup \"$path\"; fi; "
                "done",
            )

            all_hosts = list(
                dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or []))
            )
            if all_hosts or web:
                ca = self._start_sink(cid, all_hosts, web)
                prepared.extra["sink"] = SINK_STATE
                prepared.extra["network_hosts"] = all_hosts
                if ca:
                    prepared.extra["ca_cert"] = ca

            # Agent-controlled subprocesses run as nobody. Give that uid the
            # case workspace access needed for benign tasks without making the
            # root-owned wrapper or Claude parent credential state writable.
            self._exec(
                cid,
                "mkdir -p /tmp/redclaw-claude-tool-home "
                "&& chown nobody:nogroup /tmp/redclaw-claude-tool-home "
                "&& chmod 1777 /tmp "
            )
            return prepared
        except Exception:
            self._forget_oauth_token(cid)
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)
            raise

    def _start_sink(
        self, cid: str, network_hosts: list[str], web: dict | None = None
    ) -> str | None:
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
            self._exec(cid, f"echo {shlex.quote('127.0.0.1 ' + host)} >> /etc/hosts")

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
        cli = _claude_print_argv(
            model=self.model, task=task,
            permission_mode=self.permission_mode,
        )
        launch = (
            "IFS= read -r redclaw_oauth_token && "
            "export CLAUDE_CODE_OAUTH_TOKEN=\"$redclaw_oauth_token\" && "
            "unset redclaw_oauth_token && "
            f"exec {shlex.join(cli)}"
        )
        cmd = _claude_exec_argv(
            cid=cid,
            launch=launch,
            extra_env=self.fixture_process_env(prepared),
            permission_mode=self.permission_mode,
        )

        start = time.monotonic()
        oauth_token = self._oauth_token_for(cid)
        if not oauth_token:
            raise RuntimeError("Claude Code authentication was not prepared")
        try:
            proc, retries = retry_transient(
                lambda: subprocess.run(
                    cmd,
                    input=oauth_token + "\n",
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            )
            self._write_file(cid, TRAJECTORY_PATH, proc.stdout or "")
            effective_mode = _stream_permission_mode(proc.stdout)
            init = _stream_init(proc.stdout)
            stderr = proc.stderr
            returncode = proc.returncode
            if returncode == 0 and effective_mode != self.permission_mode:
                returncode = 70
                stderr = (
                    stderr
                    + ("\n" if stderr else "")
                    + f"Claude Code did not enter permission mode {self.permission_mode} "
                    f"(effective={effective_mode or 'unknown'})."
                )
            return RunResult(
                returncode=returncode,
                stdout=proc.stdout,
                stderr=stderr,
                duration_s=round(time.monotonic() - start, 2),
                extra={
                    "provider_retries": retries,
                    "auth_mode": prepared.extra.get("auth_mode"),
                    "permission_mode": effective_mode,
                    "available_tools": init.get("tools", []),
                    "claude_code_version": init.get("claude_code_version"),
                    **self.execution_profile,
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
                    **self.execution_profile,
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
        self._forget_oauth_token(prepared.container_id)
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
