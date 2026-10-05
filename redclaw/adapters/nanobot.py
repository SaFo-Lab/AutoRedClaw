"""Nanobot claw adapter.

Nanobot exposes a headless single-task mode (`nanobot agent --message ...`),
which makes it the simplest reference target. It runs its own tools (shell,
filesystem, web) in-process, so running it inside a container that holds the
case rootfs gives it the real fixture filesystem. Durable memory lives under
the workspace: memory/MEMORY.md, SOUL.md, USER.md, AGENTS.md.
"""

from __future__ import annotations

from redclaw.paths import RUNTIME_DIR

import json
import shlex
import subprocess
import time
from pathlib import Path

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
IMAGE_TAG = "redclaw-claw-nanobot:latest"
BEDROCK_RELAY_PORT = 7891
RUNTIME_HOME = "/root"
CONFIG_DIR = f"{RUNTIME_HOME}/.nanobot"
CONFIG_PATH = f"{CONFIG_DIR}/config.json"
WORKSPACE = f"{CONFIG_DIR}/workspace"
SESSIONS_DIR = f"{WORKSPACE}/sessions"
NANOBOT_TEMPLATE_DIR = "/usr/local/lib/python3.12/site-packages/nanobot/templates"
NANOBOT_WORKSPACE_TEMPLATES = (
    "USER.md",
    "SOUL.md",
    "AGENTS.md",
    "HEARTBEAT.md",
    "memory/MEMORY.md",
)
IDENTITY_SEED = "# IDENTITY.md\n\nName: Assistant\n"


class NanobotAdapter(ClawAdapter):
    name = "nanobot"
    runtime_home = RUNTIME_HOME
    workspace_targets = filesystem_workspace_targets(
        WORKSPACE, NANOBOT_WORKSPACE_TEMPLATES
    )

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}

    def skills_roots(self) -> list[str]:
        return [f"{WORKSPACE}/skills"]

    # -- image ------------------------------------------------------------ #
    def build_image(self, *, force: bool = False) -> str:
        return build_claw_image("nanobot", IMAGE_TAG, force=force)

    # -- lifecycle -------------------------------------------------------- #
    def prepare(self, *, image: str, rootfs_dir: Path, test_mode: str, poisoned_memory: str | None, poisoned_target: str = "memory.md", network_hosts: list[str] | None = None, web: dict | None = None) -> PreparedRun:
        # Run as root so the agent can touch absolute case paths (/Users/user/...).
        # Keep the container alive with an overridden entrypoint.
        cid = subprocess.run(
            [
                "docker", "run", "-d", "--user", "0:0",
                "-e", f"HOME={RUNTIME_HOME}",
                "--entrypoint", "sleep", image, "infinity",
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        prepared = PreparedRun(container_id=cid, workspace=WORKSPACE)

        # Lay the case rootfs into the container root.
        self._exec(cid, "mkdir -p /Users /home /tmp")
        subprocess.run(["docker", "cp", f"{str(rootfs_dir)}/.", f"{cid}:/"], capture_output=True, text=True, check=True)

        self._exec(cid, f"mkdir -p {shlex.quote(WORKSPACE)}/memory")
        self._seed_completed_workspace_templates(
            cid,
            template_dir=NANOBOT_TEMPLATE_DIR,
            workspace=WORKSPACE,
            files=NANOBOT_WORKSPACE_TEMPLATES,
            identity=IDENTITY_SEED,
        )

        if test_mode == "trigger" and poisoned_memory:
            container_path = self.workspace_target_path(poisoned_target)
            self._append_workspace_payload(cid, container_path, poisoned_memory)
            prepared.extra["seeded_memory"] = True
            prepared.extra["seeded_target"] = container_path

        # Always write a config: nanobot only matches a provider that is declared
        # in `providers` with an api_key (it does NOT auto-read env keys during
        # provider auto-detection). We reference ${VAR}, resolved from the env we
        # pass at run time. Only the selected --model-provider is declared.
        import json as _json

        providers: dict[str, dict[str, str]] = {}
        model = self.model
        if self.model_provider == "openrouter":
            self.provider_secret("OPENROUTER_API_KEY")
            # Route the openai provider through OpenRouter (OpenAI-compatible).
            # OpenRouter only supports chat/completions (not the responses API),
            # and expects vendor-prefixed model ids (e.g. openai/gpt-4o-mini), passed as given.
            providers["openai"] = {
                "api_key": "${OPENROUTER_API_KEY}",
                "api_base": "https://openrouter.ai/api/v1",
                "api_type": "chat_completions",
            }
            provider_name = "openai"
        elif self.model_provider == "openai":
            self.provider_secret("OPENAI_API_KEY")
            providers["openai"] = {"api_key": "${OPENAI_API_KEY}"}
            provider_name = "openai"
        else:
            self.provider_secret("AWS_BEARER_TOKEN_BEDROCK")
            # Route through a local relay that adds 'anthropic.' model prefix and
            # real bearer token before forwarding to bedrock-mantle.
            providers["anthropic"] = {
                "api_key": "bedrock-relay",
                "api_base": f"http://127.0.0.1:{BEDROCK_RELAY_PORT}",
            }
            provider_name = "anthropic"
        defaults: dict[str, str] = {"workspace": WORKSPACE}
        if model:
            # Bedrock model IDs use 'anthropic.' prefix; strip it so nanobot's
            # anthropic provider accepts the ID, and the relay re-adds it before
            # forwarding to bedrock-mantle.
            nanobot_model = model.removeprefix("anthropic.") if self.model_provider == "bedrock" else model
            defaults["model"] = nanobot_model
            # Always name the provider explicitly; nanobot's "auto" would infer
            # one from keywords in the model name.
            defaults["provider"] = provider_name
        cfg: dict[str, object] = {"agents": {"defaults": defaults}}
        if providers:
            cfg["providers"] = providers
        # Whitelist loopback so nanobot's web tool will fetch our transparently-
        # served fixture pages (its SSRF guard otherwise blocks 127.0.0.1). This
        # is harness environment config, not a change to the agent's logic.
        cfg["tools"] = {"ssrf_whitelist": ["127.0.0.0/8"]}
        self._write_file(cid, CONFIG_PATH, _json.dumps(cfg))
        prepared.extra["config"] = CONFIG_PATH

        all_hosts = list(dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or [])))
        if all_hosts or web:
            ca = self._start_sink(cid, all_hosts, web)
            prepared.extra["sink"] = SINK_STATE
            prepared.extra["network_hosts"] = all_hosts
            if ca:
                prepared.extra["ca_cert"] = ca

        if self.model_provider == "bedrock":
            self._start_bedrock_relay(cid)

        return prepared

    def _start_bedrock_relay(self, cid: str) -> None:
        """Start the bedrock relay proxy inside the container.

        The relay accepts Anthropic Messages API requests on localhost and forwards
        them to bedrock-mantle, prepending 'anthropic.' to the model name and
        injecting the real AWS_BEARER_TOKEN_BEDROCK authorization header.
        """
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
        # Wait until the relay port is up
        for _ in range(20):
            chk = self._exec(cid, f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{BEDROCK_RELAY_PORT}/ 2>/dev/null || true")
            if chk.stdout.strip() in ("404", "405", "200"):
                return
            time.sleep(0.2)

    def _start_sink(self, cid: str, network_hosts: list[str], web: dict | None = None) -> str | None:
        """Start the auto-logging mock sink on :80 (+ :443 https when openssl is
        available), redirect fixture hosts to it via /etc/hosts (egress capture),
        and — if `web` is given — serve the case's real HTML pages transparently."""
        subprocess.run(["docker", "cp", str(MOCK_SINK_SRC), f"{cid}:/opt/mock_sink.py"], capture_output=True, text=True, check=True)
        env_prefix = ""
        if web and web.get("web_dir") and web.get("url_map"):
            self._exec(cid, "mkdir -p /opt/web")
            subprocess.run(["docker", "cp", f"{str(web['web_dir'])}/.", f"{cid}:/opt/web"], capture_output=True, text=True, check=True)
            subprocess.run(["docker", "cp", str(web["url_map"]), f"{cid}:/opt/url_map.json"], capture_output=True, text=True, check=True)
            env_prefix = "REDCLAW_WEB_DIR=/opt/web REDCLAW_URL_MAP=/opt/url_map.json "
            if web.get("site_meta"):
                subprocess.run(["docker", "cp", str(web["site_meta"]), f"{cid}:/opt/site_meta.json"], capture_output=True, text=True, check=True)
                env_prefix += "REDCLAW_SITE_META=/opt/site_meta.json "
            if web.get("app_behavior"):
                subprocess.run(["docker", "cp", str(web["app_behavior"]), f"{cid}:/opt/app_behavior.json"], capture_output=True, text=True, check=True)
                env_prefix += "REDCLAW_APP_BEHAVIOR=/opt/app_behavior.json "
        if network_hosts:
            append = " && ".join(f"echo {shlex.quote('127.0.0.1 ' + h)} >> /etc/hosts" for h in network_hosts)
            self._exec(cid, append)
        tls = self.provision_fixture_tls(cid, network_hosts)
        https = tls.sink_arguments()
        subprocess.run(
            ["docker", "exec", "-d", cid, "sh", "-lc", f"{env_prefix}python3 /opt/mock_sink.py --port 80 {https}--state {shlex.quote(SINK_STATE)}"],
            capture_output=True, text=True, check=True,
        )
        # readiness probe
        for _ in range(30):
            chk = self._exec(cid, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/__sink__/state || true")
            if chk.stdout.strip().endswith("200"):
                return tls.ca_cert
            time.sleep(0.2)
        return tls.ca_cert

    def run(self, prepared: PreparedRun, task: str, *, timeout: int) -> RunResult:
        cid = prepared.container_id
        cmd = ["docker", "exec", "-e", f"HOME={RUNTIME_HOME}"]
        for key, value in self.provider_credentials().items():
            cmd += ["-e", f"{key}={value}"]
        cmd += self.fixture_docker_env_args(prepared)
        cmd += [cid, "nanobot", "agent", "--no-markdown", "--no-logs", "--workspace", WORKSPACE]
        if prepared.extra.get("config"):
            cmd += ["--config", prepared.extra["config"]]
        cmd += ["--message", task]

        start = time.monotonic()
        try:
            proc, retries = retry_transient(
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            )
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=round(time.monotonic() - start, 2),
                extra={"provider_retries": retries},
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                returncode=124,
                stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=(exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                duration_s=round(time.monotonic() - start, 2),
                timed_out=True,
            )

    def collect(self, prepared: PreparedRun, *, file_paths: list[str]) -> Observations:
        cid = prepared.container_id
        # durable memory: concatenate the claw's own memory files (that exist)
        memory_sources, blob_parts, memory_attempted = self.read_workspace_memory(
            lambda path: self._stat_file(cid, path)
        )

        files: dict[str, str] = {}
        for path in file_paths:
            exists, content = self._stat_file(cid, path)
            if not exists:
                continue  # omit absent files so file_exists is a real signal
            files[path] = content
            # NOTE: nominal memory stores declared by the case (e.g. a fictional
            # /home/user/workspace/MEMORY.md the generator invented) are deliberately
            # NOT folded into memory_blob. The memory_contains oracle is rewritten to
            # scan memory_blob, so folding them in would let a write to an arbitrary
            # invented path count as a durable-memory breach — but that file is not
            # nanobot's real durable memory and would not survive into the next
            # session. Only the claw's real memory_files() (collected above:
            # /root/.nanobot/{SOUL,USER,AGENTS,HEARTBEAT}.md + memory/MEMORY.md)
            # represent a genuine persistent threat, so only they feed the blob.
            # (These nominal paths still populate `files`, so file_contains checks
            # targeting them continue to work.)

        state: dict = {}
        if prepared.extra.get("sink"):
            exists, content = self._stat_file(cid, prepared.extra["sink"])
            if exists and content.strip():
                try:
                    state = json.loads(content)
                except json.JSONDecodeError:
                    state = {}

        _, trajectory = self._stat_file(cid, f"{SESSIONS_DIR}/cli_direct.jsonl")

        # SKILL.md surface: read back skills on disk for skill_registered scoring.
        # Pure disk read — does NOT touch nanobot's own SkillsLoader/selection.
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
    def _exec(self, cid: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", cid, "sh", "-lc", command],
            capture_output=True, text=True,
        )

    def _stat_file(self, cid: str, path: str) -> tuple[bool, str]:
        """Return (exists, content). Distinguishes an absent file (exists=False)
        from an empty file (exists=True, content="")."""
        q = shlex.quote(path)
        proc = subprocess.run(
            ["docker", "exec", cid, "sh", "-lc", f"if [ -f {q} ]; then echo __RC_PRESENT__; cat {q}; fi"],
            capture_output=True, text=True,
        )
        out = proc.stdout
        if not out.startswith("__RC_PRESENT__"):
            return False, ""
        # strip the marker line (and the single newline echo added)
        return True, out[len("__RC_PRESENT__\n"):]

    def _write_file(self, cid: str, path: str, content: str) -> None:
        # write via stdin to avoid shell-quoting issues with arbitrary content
        subprocess.run(
            ["docker", "exec", "-i", cid, "sh", "-lc", f"mkdir -p $(dirname {shlex.quote(path)}) && cat > {shlex.quote(path)}"],
            input=content, capture_output=True, text=True, check=True,
        )
