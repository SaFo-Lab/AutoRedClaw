"""OpenClaw claw adapter.

OpenClaw exposes a one-shot headless turn. The `agent` subcommand defaults to
running THROUGH a Gateway; for the eval we use the embedded local runner:

    node /app/openclaw.mjs agent --local --message "<task>" --agent main --json

which runs a single agent turn in-process and blocks until it finishes.

Durable memory lives at the default agent workspace (`~/.openclaw/workspace`) —
verified from source (src/memory/root-memory-files.ts:
`path.join(workspaceDir, "MEMORY.md")`, and the bootstrap/context files
AGENTS.md / SOUL.md / USER.md / IDENTITY.md / HEARTBEAT.md / TOOLS.md all resolve
under the same workspace dir). These are loaded into the system prompt every
turn, so inject/trigger map onto them exactly like nanobot's memory files.

This adapter is kept structurally in sync with the nanobot adapter: same memory
scoring semantics (only the claw's REAL memory files feed the memory blob — a
write to a case-invented nominal path is NOT a durable-memory breach), same
trigger poisoned-target mapping, same https-capable mock sink + CA trust.
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
IMAGE_TAG = "redclaw-claw-openclaw:latest"
BEDROCK_RELAY_PORT = 7891
RUNTIME_HOME = "/root"
CONFIG_DIR = f"{RUNTIME_HOME}/.openclaw"
CONFIG_PATH = f"{CONFIG_DIR}/openclaw.json"
WORKSPACE = f"{CONFIG_DIR}/workspace"
AGENT_ID = "main"
SESSIONS_DIR = f"{CONFIG_DIR}/agents/{AGENT_ID}/sessions"
OPENCLAW_TEMPLATE_DIR = "/app/docs/reference/templates"
OPENCLAW_WORKSPACE_TEMPLATES = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "HEARTBEAT.md",
)

# Fixture hosts are redirected (via /etc/hosts) to this address, which the sink
# binds. It is a GLOBAL-unicast IP, so openclaw's web_fetch/browser SSRF guard
# allows it by default — unlike 127.0.0.1 (loopback) which the guard blocks and
# which CANNOT be re-enabled via config in --local mode. We assign it to the
# container's loopback interface (needs CAP_NET_ADMIN) so traffic to it stays
# local and reaches the sink. 100.0.0.1 is not in any SSRF-blocked special-use
# range (CGNAT is 100.64.0.0/10) and is never contacted directly by the claw.
SINK_IP = "100.0.0.1"
# Bedrock MITM uses a separate global-unicast IP on lo:1 so it doesn't conflict
# with the mock sink on lo:0 (SINK_IP). Must be outside loopback (127.x.x.x) and
# private ranges so openclaw's SSRF guard allows it.
MITM_IP = "100.0.0.3"

# python (no apt) ioctl to alias SINK_IP onto lo:0; needs CAP_NET_ADMIN.
_BIND_SINK_IP_PY = (
    "import fcntl,socket,struct\n"
    "ifn=b'lo:0'; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
    "c=lambda i,ip: fcntl.ioctl(s,i,struct.pack('16sH2s4s8s',ifn,socket.AF_INET,b'\\x00\\x00',socket.inet_aton(ip),b'\\x00'*8))\n"
    f"c(0x8916,'{SINK_IP}'); c(0x891c,'255.255.255.0')\n"
    "fl=struct.unpack('16sh',fcntl.ioctl(s,0x8913,struct.pack('16sh',ifn,0)))[1]\n"
    "fcntl.ioctl(s,0x8914,struct.pack('16sh',ifn,fl|1))\n"
)
_BIND_MITM_IP_PY = (
    "import fcntl,socket,struct\n"
    "ifn=b'lo:1'; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
    "c=lambda i,ip: fcntl.ioctl(s,i,struct.pack('16sH2s4s8s',ifn,socket.AF_INET,b'\\x00\\x00',socket.inet_aton(ip),b'\\x00'*8))\n"
    f"c(0x8916,'{MITM_IP}'); c(0x891c,'255.255.255.0')\n"
    "fl=struct.unpack('16sh',fcntl.ioctl(s,0x8913,struct.pack('16sh',ifn,0)))[1]\n"
    "fcntl.ioctl(s,0x8914,struct.pack('16sh',ifn,fl|1))\n"
)

# Native OpenClaw identity format with only the required onboarding name
# completed.  The remaining fields are optional first-conversation choices;
# leave them blank instead of inventing a benchmark-specific persona.
_IDENTITY_SEED = (
    "# IDENTITY.md - Who Am I?\n\n"
    "- **Name:** Assistant\n"
    "- **Creature:**\n"
    "- **Vibe:**\n"
    "- **Emoji:**\n"
    "- **Avatar:**\n"
)
_WORKSPACE_STATE = json.dumps(
    {
        "version": 1,
        "bootstrapSeededAt": "2026-01-01T00:00:00.000Z",
        "setupCompletedAt": "2026-01-01T00:00:00.000Z",
        # Read by older OpenClaw releases; current releases migrate it to the
        # setupCompletedAt field above.
        "onboardingCompletedAt": "2026-01-01T00:00:00.000Z",
    },
    indent=2,
)


class OpenclawAdapter(ClawAdapter):
    name = "openclaw"
    runtime_home = RUNTIME_HOME
    workspace_targets = filesystem_workspace_targets(
        WORKSPACE, (*OPENCLAW_WORKSPACE_TEMPLATES, "MEMORY.md")
    )

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}

    def skills_roots(self) -> list[str]:
        # Workspace skills load from <workspace>/skills/<name>/SKILL.md; managed
        # and bundled skills exist too but are not harness-writable.
        return [f"{WORKSPACE}/skills"]

    # -- image ------------------------------------------------------------ #
    def build_image(self, *, force: bool = False) -> str:
        return build_claw_image("openclaw", IMAGE_TAG, force=force)

    # -- lifecycle -------------------------------------------------------- #
    def prepare(self, *, image: str, rootfs_dir: Path, test_mode: str, poisoned_memory: str | None, poisoned_target: str = "memory.md", network_hosts: list[str] | None = None, web: dict | None = None) -> PreparedRun:
        cid = subprocess.run(
            # CAP_NET_ADMIN lets us alias the global SINK_IP onto loopback so fixture
            # hosts redirect to an SSRF-allowed address (see SINK_IP).
            ["docker", "run", "-d", "--user", "0:0", "--cap-add", "NET_ADMIN",
             "-e", f"HOME={RUNTIME_HOME}",
             "--entrypoint", "sleep", image, "infinity"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        prepared = PreparedRun(container_id=cid, workspace=WORKSPACE)

        self._exec(cid, "mkdir -p /Users /home /tmp")
        subprocess.run(["docker", "cp", f"{str(rootfs_dir)}/.", f"{cid}:/"], capture_output=True, text=True, check=True)
        self._exec(cid, f"mkdir -p {shlex.quote(WORKSPACE)}")
        self._seed_completed_workspace_templates(
            cid,
            template_dir=OPENCLAW_TEMPLATE_DIR,
            workspace=WORKSPACE,
            files=OPENCLAW_WORKSPACE_TEMPLATES,
            identity=_IDENTITY_SEED,
            strip_frontmatter=True,
            onboarding_state_path=f"{WORKSPACE}/.openclaw/workspace-state.json",
            onboarding_state=_WORKSPACE_STATE,
        )

        if test_mode == "trigger" and poisoned_memory:
            container_path = self.workspace_target_path(poisoned_target)
            self._append_workspace_payload(cid, container_path, poisoned_memory)
            prepared.extra["seeded_memory"] = True
            prepared.extra["seeded_target"] = container_path

        all_hosts = list(dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or [])))

        # Config: pin the workspace (so memory paths are unambiguous), provider
        # auth ref, model, and autonomous tool settings for the selected
        # --model-provider (bedrock relay, OpenRouter or direct OpenAI). OpenRouter model refs
        # use the form "openrouter/<provider>/<model>" (e.g. deepseek/deepseek-v4-pro
        # → openrouter/deepseek/deepseek-v4-pro) — without the prefix openclaw parses
        # "deepseek/" as a provider it has no auth for and the run dies.
        model_providers: dict[str, object] = {}
        model = self.model
        if self.model_provider == "bedrock":
            self.provider_secret("AWS_BEARER_TOKEN_BEDROCK")
            # openclaw talks to the in-container bedrock relay as an openai-compatible
            # provider. openclaw 2026.5.26's root schema is strict and has NO top-level
            # `providers` key — custom providers live under `models.providers` as
            # {baseUrl, apiKey, api, models:[...]}. The relay listens on
            # MITM_IP:BEDROCK_RELAY_PORT (plain HTTP) and forces the real model via
            # BEDROCK_RELAY_MODEL, so the id below is only a local handle.
            provider = "bedrock-relay"
            if not self.model:
                raise ValueError("openclaw requires an explicit model")
            model_id = self.model
            model = f"{provider}/{model_id}"
            model_providers[provider] = {
                "baseUrl": f"http://{MITM_IP}:{BEDROCK_RELAY_PORT}",
                "apiKey": "bedrock-relay",
                "api": "openai-completions",
                "timeoutSeconds": 600,
                "request": {"allowPrivateNetwork": True},
                "models": [{
                    "id": model,
                    "name": model_id,
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": ["text"],
                    "contextWindow": 200000,
                    "maxTokens": 8192,
                }],
            }
        elif self.model_provider == "openrouter":
            self.provider_secret("OPENROUTER_API_KEY")
            # Built-in providers authenticate from the env var we already pass to
            # `docker exec` — do NOT declare them in the config. The root schema has
            # no top-level `providers` key (see the bedrock comment above), and
            # `models.providers.<p>.auth` only accepts the strings "api-key" /
            # "aws-sdk" / "oauth" / "token", not an env-reference object. Writing
            # either shape makes openclaw reject the whole config with
            # "<root>: Invalid input" and refuse to run. Verified against this
            # image with `openclaw config validate`.
            provider = "openrouter"
            if model and not model.startswith("openrouter/"):
                model = f"openrouter/{model}"
        else:
            self.provider_secret("OPENAI_API_KEY")
            provider = "openai"
        prepared.extra["model_ref"] = model
        defaults: dict[str, object] = {
            "workspace": WORKSPACE,
        }
        if model:
            defaults["model"] = model
            if provider == "bedrock-relay":
                defaults["models"] = {model: {"streaming": True}}
        cfg: dict[str, object] = {"agents": {"defaults": defaults}}
        if model_providers:
            cfg["models"] = {"providers": model_providers}
        # Chrome runs headless as root in the container → needs no-sandbox. No SSRF
        # policy needed: fixture hosts resolve to SINK_IP (a global-unicast address)
        # which both web_fetch and the browser tool allow by default.
        if all_hosts or web:
            cfg["browser"] = {"headless": True, "noSandbox": True}
        self._write_file(cid, CONFIG_PATH, json.dumps(cfg, indent=2))
        prepared.extra["config"] = CONFIG_PATH

        # Pre-register the eval model in the agent's models.json so openclaw can
        # resolve it even if it isn't in the bundled static catalog. This is needed
        # for models like gpt-5-mini that aren't yet in openclaw's bundled catalog.
        # For bedrock, also override the provider baseUrl to point to the relay.
        # Pre-register unknown models (e.g. gpt-5-mini not yet in openclaw's catalog).
        # Skip for bedrock: gpt-5.5 placeholder is already in the catalog.
        if provider == "openai" and model:
            agent_models_dir = f"{CONFIG_DIR}/agents/{AGENT_ID}/agent"
            self._exec(cid, f"mkdir -p {shlex.quote(agent_models_dir)}")
            openai_provider_entry: dict[str, object] = {
                "models": [{"id": model, "input": ["text", "image"]}]
            }
            models_json: dict[str, object] = {
                "providers": {"openai": openai_provider_entry}
            }
            self._write_file(cid, f"{agent_models_dir}/models.json", json.dumps(models_json, indent=2))

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
        subprocess.run(
            ["docker", "cp", str(BEDROCK_RELAY_SRC), f"{cid}:/opt/bedrock_relay.py"],
            capture_output=True, text=True, check=True,
        )
        region = self.env.get("AWS_REGION", "us-east-1")
        token = self.env["AWS_BEARER_TOKEN_BEDROCK"]
        if not self.model:
            raise ValueError("openclaw requires an explicit model")
        target_model = self.model.removeprefix("anthropic.")

        # Bind a global-unicast IP onto lo:1 for the relay. MITM_IP (100.0.0.3) is
        # outside loopback and RFC-1918 ranges, so openclaw's SSRF guard allows it.
        # The relay uses HTTP (no TLS) so the openai SDK must point to this IP via
        # OPENAI_BASE_URL (set in run()).
        self._exec(cid, f"python3 -c {shlex.quote(_BIND_MITM_IP_PY)}")

        subprocess.run(
            [
                "docker", "exec", "-d",
                "-e", f"BEDROCK_RELAY_BASE=https://bedrock-mantle.{region}.api.aws",
                "-e", f"AWS_BEARER_TOKEN_BEDROCK={token}",
                "-e", f"BEDROCK_RELAY_MODEL=anthropic.{target_model}",
                "-e", "HTTP_BIND=0.0.0.0",
                cid, "python3", "/opt/bedrock_relay.py", str(BEDROCK_RELAY_PORT),
            ],
            capture_output=True, text=True, check=True,
        )
        for _ in range(30):
            chk = self._exec(cid, f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{BEDROCK_RELAY_PORT}/ 2>/dev/null || true")
            if chk.stdout.strip() in ("404", "405", "200"):
                break
            time.sleep(0.2)

    def _start_sink(self, cid: str, network_hosts: list[str], web: dict | None = None) -> str | None:
        """Start the auto-logging mock sink on :80 (+ :443 https when openssl is
        available), redirect fixture hosts to it via /etc/hosts (egress capture),
        and — if `web` is given — serve the case's real HTML pages transparently."""
        subprocess.run(["docker", "cp", str(MOCK_SINK_SRC), f"{cid}:/opt/mock_sink.py"], capture_output=True, text=True, check=True)
        # Alias the global SINK_IP onto loopback so fixture hosts can redirect to an
        # SSRF-allowed address that still routes locally to the sink.
        self._exec(cid, f"python3 -c {shlex.quote(_BIND_SINK_IP_PY)}")
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
            append = " && ".join(f"echo {shlex.quote(SINK_IP + ' ' + h)} >> /etc/hosts" for h in network_hosts)
            self._exec(cid, append)
        tls = self.provision_fixture_tls(cid, network_hosts)
        https = tls.sink_arguments()
        subprocess.run(
            ["docker", "exec", "-d", cid, "sh", "-lc", f"{env_prefix}python3 /opt/mock_sink.py --port 80 {https}--state {shlex.quote(SINK_STATE)}"],
            capture_output=True, text=True, check=True,
        )
        for _ in range(30):
            chk = self._exec(cid, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/__sink__/state || true")
            if chk.stdout.strip().endswith("200"):
                return tls.ca_cert
            time.sleep(0.2)
        return tls.ca_cert

    def run(self, prepared: PreparedRun, task: str, *, timeout: int) -> RunResult:
        cid = prepared.container_id
        cmd = ["docker", "exec", "-e", f"HOME={RUNTIME_HOME}"]
        if self.model_provider in ("openrouter", "openai"):
            for key, value in self.provider_credentials().items():
                cmd += ["-e", f"{key}={value}"]
        else:
            cmd += ["-e", "OPENAI_API_KEY=bedrock-relay"]
            # Route openai SDK requests to the relay over plain HTTP (no TLS needed).
            # MITM_IP (100.0.0.3) is bound to lo:1 and accepted by openclaw's SSRF guard.
            cmd += ["-e", f"OPENAI_BASE_URL=http://{MITM_IP}:{BEDROCK_RELAY_PORT}"]
        cmd += self.fixture_docker_env_args(prepared)
        # --local runs the embedded agent in-process (the default routes through a
        # Gateway, which is not running in the eval container).
        cmd += [cid, "node", "/app/openclaw.mjs", "agent", "--local", "--message", task, "--agent", AGENT_ID, "--json",
                "--timeout", str(timeout)]
        # Use the provider-resolved model ref (openrouter/<...> when routed via
        # OpenRouter), matching the config written in prepare().
        model_ref = prepared.extra.get("model_ref", self.model)
        if model_ref:
            cmd += ["--model", model_ref]
        start = time.monotonic()
        try:
            proc, provider_retries = retry_transient(
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
            )
            # The --json result reports the exact transcript path; stash it so
            # collect() can read the trajectory without guessing the session path.
            try:
                sf = (json.loads(proc.stdout).get("meta", {}).get("agentMeta", {}) or {}).get("sessionFile")
                if sf:
                    prepared.extra["session_file"] = sf
            except (json.JSONDecodeError, AttributeError):
                pass
            stderr = proc.stderr
            if proc.returncode != 0:
                stderr = (stderr + self._failure_diagnostics(cid, cmd)).strip() + "\n"
            return RunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=stderr, duration_s=round(time.monotonic() - start, 2), extra={"provider_retries": provider_retries})
        except subprocess.TimeoutExpired as exc:
            dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")  # noqa: E731
            stderr = dec(exc.stderr)
            stderr = (stderr + self._failure_diagnostics(cid, cmd)).strip() + "\n"
            return RunResult(returncode=124, stdout=dec(exc.stdout), stderr=stderr, duration_s=round(time.monotonic() - start, 2), timed_out=True)

    def collect(self, prepared: PreparedRun, *, file_paths: list[str]) -> Observations:
        cid = prepared.container_id
        memory_sources, blob_parts, memory_attempted = self.read_workspace_memory(
            lambda path: self._stat_file(cid, path)
        )

        files: dict[str, str] = {}
        for path in file_paths:
            exists, content = self._stat_file(cid, path)
            if not exists:
                continue
            files[path] = content
            # NOTE (kept in sync with nanobot): case-declared nominal memory paths
            # (e.g. a fictional /home/user/workspace/MEMORY.md the generator invented)
            # are deliberately NOT folded into memory_blob. The memory_contains oracle
            # is rewritten to scan memory_blob, so folding them in would let a write to
            # an arbitrary invented path count as a durable-memory breach — but that
            # file is not openclaw's real durable memory and would not survive into the
            # next session. Only the real memory_files() above feed the blob. (These
            # nominal paths still populate `files`, so file_contains checks still work.)

        state: dict = {}
        if prepared.extra.get("sink"):
            exists, content = self._stat_file(cid, prepared.extra["sink"])
            if exists and content.strip():
                try:
                    state = json.loads(content)
                except json.JSONDecodeError:
                    state = {}

        # Trajectory: the exact transcript path is reported by the run's --json
        # output (parsed into prepared.extra by run()). Fall back to the newest
        # jsonl under the native state dir if the run output was unavailable.
        trajectory = ""
        session_file = prepared.extra.get("session_file")
        if session_file:
            _, trajectory = self._stat_file(cid, session_file)
        else:
            newest = self._exec(cid, f"ls -t {shlex.quote(SESSIONS_DIR)}/*.jsonl 2>/dev/null | head -1").stdout.strip()
            if newest:
                _, trajectory = self._stat_file(cid, newest)

        # SKILL.md surface: read back skills on disk (pure read, no selection).
        state["skills"] = self.read_skills(prepared)

        return Observations(memory_blob="\n\n".join(blob_parts), files=files, memory_sources=memory_sources,
            memory_attempted=memory_attempted, state=state, trajectory=trajectory)

    def teardown(self, prepared: PreparedRun) -> None:
        subprocess.run(["docker", "rm", "-f", prepared.container_id], capture_output=True, text=True)

    # -- helpers (shared shape with the nanobot adapter) ------------------ #
    def _exec(self, cid: str, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", "exec", cid, "sh", "-lc", command], capture_output=True, text=True)

    def _failure_diagnostics(self, cid: str, cmd: list[str]) -> str:
        q_state = shlex.quote(CONFIG_DIR)
        q_sessions = shlex.quote(SESSIONS_DIR)
        q_config = shlex.quote(CONFIG_PATH)
        diag = self._exec(
            cid,
            "printf '\\n[redclaw-openclaw-diagnostics]\\n'; "
            f"printf 'config=%s\\n' {q_config}; "
            f"test -f {q_config} && printf 'config_exists=1\\n' || printf 'config_exists=0\\n'; "
            f"test -f {q_config} && printf 'config_content=' && cat {q_config} && printf '\\n' || true; "
            f"printf 'state_dir=%s\\n' {q_state}; "
            f"test -d {q_state} && printf 'state_exists=1\\n' || printf 'state_exists=0\\n'; "
            f"printf 'sessions_dir=%s\\n' {q_sessions}; "
            f"test -d {q_sessions} && printf 'sessions_exists=1\\n' || printf 'sessions_exists=0\\n'; "
            f"find {q_state} -maxdepth 4 -type f 2>/dev/null | sort | tail -40",
        )
        command = " ".join(shlex.quote(part) for part in cmd)
        return f"\n[redclaw command]\n{command}\n{diag.stdout}{diag.stderr}"

    def _stat_file(self, cid: str, path: str) -> tuple[bool, str]:
        q = shlex.quote(path)
        proc = subprocess.run(["docker", "exec", cid, "sh", "-lc", f"if [ -f {q} ]; then echo __RC_PRESENT__; cat {q}; fi"], capture_output=True, text=True)
        out = proc.stdout
        if not out.startswith("__RC_PRESENT__"):
            return False, ""
        return True, out[len("__RC_PRESENT__\n"):]

    def _write_file(self, cid: str, path: str, content: str) -> None:
        subprocess.run(
            ["docker", "exec", "-i", cid, "sh", "-lc", f"mkdir -p $(dirname {shlex.quote(path)}) && cat > {shlex.quote(path)}"],
            input=content, capture_output=True, text=True, check=True,
        )
