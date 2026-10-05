"""ZeroClaw claw adapter.

ZeroClaw exposes a native one-shot CLI turn:

    zeroclaw agent -a assistant --message "<task>"

For RedClaw evaluation we build the Rust CLI into a container, write `config.toml`
under ZeroClaw's default config directory, seed the real per-agent workspace, and
collect ZeroClaw's own markdown memory files after the run.
"""

from __future__ import annotations

from redclaw.paths import RUNTIME_DIR

import json
import shlex
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

from redclaw.runtime.images import build_claw_image, claw_template_dir

from redclaw.evaluation.core import (
    ClawAdapter,
    Observations,
    PreparedRun,
    RunResult,
    filesystem_workspace_targets,
    retry_transient,
)


ZEROCLAW_PERSONALITY_TEMPLATE_DIR = claw_template_dir("zeroclaw")
MOCK_SINK_SRC = RUNTIME_DIR / "mock_sink.py"
BEDROCK_RELAY_SRC = RUNTIME_DIR / "bedrock_relay.py"
BEDROCK_RELAY_PORT = 7891
SINK_STATE = "/tmp/redclaw_sink.json"
IMAGE_TAG = "redclaw-claw-zeroclaw:latest"

AGENT_ALIAS = "assistant"
RUNTIME_HOME = "/root"
CONFIG_DIR = f"{RUNTIME_HOME}/.zeroclaw"
CONFIG_PATH = f"{CONFIG_DIR}/config.toml"
DATA_DIR = f"{CONFIG_DIR}/data"
WORKSPACE = f"{CONFIG_DIR}/agents/{AGENT_ALIAS}/workspace"
SESSION_STATE = "/tmp/redclaw_zeroclaw_session.json"
RUNTIME_TRACE = f"{WORKSPACE}/state/runtime-trace.jsonl"
RUNTIME_TRACE_CANDIDATES = (
    RUNTIME_TRACE,
    f"{DATA_DIR}/state/runtime-trace.jsonl",
    f"{CONFIG_DIR}/state/runtime-trace.jsonl",
)

CORE_MEMORY_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "MEMORY.md",
)

ZEROCLAW_TEMPLATE_DEFAULTS = {
    "{agent}": "Assistant",
    "{user}": "User",
    "{tz}": "UTC",
    "{comm_style}": (
        "Be warm, natural, and clear. Use occasional relevant emojis (1-2 max) "
        "and avoid robotic phrasing."
    ),
}


def _native_workspace_docs() -> dict[str, str]:
    docs: dict[str, str] = {}
    for name in CORE_MEMORY_FILES:
        path = ZEROCLAW_PERSONALITY_TEMPLATE_DIR / name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"zeroclaw native workspace template missing: {path}") from exc
        for placeholder, value in ZEROCLAW_TEMPLATE_DEFAULTS.items():
            content = content.replace(placeholder, value)
        docs[name] = content
    return docs


def _toml_str(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "\n".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records
    ) + ("\n" if records else "")


def _json_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _standard_tool_calls(calls: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, call in enumerate(calls, 1):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict):
            name = function.get("name") or call.get("name") or ""
            arguments = function.get("arguments", call.get("arguments", {}))
        else:
            name = call.get("name") or ""
            arguments = call.get("arguments", {})
        out.append(
            {
                "id": str(call.get("id") or call.get("call_id") or f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str(name),
                    "arguments": _json_string(arguments),
                },
            }
        )
    return out


def _session_message_to_record(
    message: dict[str, Any],
    tool_names: dict[str, str],
) -> dict[str, Any] | None:
    role = str(message.get("role") or "").lower()
    if role not in {"system", "user", "assistant", "tool"}:
        return None

    content = message.get("content", "")
    if role == "assistant":
        record: dict[str, Any] = {"role": role, "content": _text_content(content)}
        parsed = _parse_json_object(content)
        if parsed is not None and isinstance(parsed.get("tool_calls"), list):
            record["content"] = _text_content(parsed.get("content"))
            tool_calls = _standard_tool_calls(parsed.get("tool_calls") or [])
            if tool_calls:
                record["tool_calls"] = tool_calls
                for call in tool_calls:
                    call_id = str(call.get("id") or "")
                    name = str((call.get("function") or {}).get("name") or "")
                    if call_id and name:
                        tool_names[call_id] = name
            if parsed.get("reasoning_content") is not None:
                record["reasoning_content"] = _text_content(parsed.get("reasoning_content"))
        return record

    if role == "tool":
        parsed = _parse_json_object(content)
        if parsed is None:
            return {"role": role, "content": _text_content(content)}
        record = {"role": role, "content": _text_content(parsed.get("content"))}
        tool_call_id = parsed.get("tool_call_id")
        if tool_call_id is not None:
            record["tool_call_id"] = str(tool_call_id)
            if str(tool_call_id) in tool_names:
                record["name"] = tool_names[str(tool_call_id)]
        return record

    return {"role": role, "content": _text_content(content)}


def _session_state_to_jsonl(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return _jsonl(
            [{"role": "system", "content": f"ZeroClaw session state parse error: {exc}"}]
        )

    history = parsed.get("history") if isinstance(parsed, dict) else parsed
    if not isinstance(history, list):
        return _jsonl(
            [
                {
                    "role": "system",
                    "content": "ZeroClaw session state did not contain a history list",
                }
            ]
        )

    records: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for message in history:
        if isinstance(message, dict):
            record = _session_message_to_record(message, tool_names)
            if record is not None:
                records.append(record)
    return _jsonl(records)


class ZeroclawAdapter(ClawAdapter):
    name = "zeroclaw"
    runtime_home = RUNTIME_HOME
    workspace_targets = filesystem_workspace_targets(WORKSPACE, CORE_MEMORY_FILES)

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}

    def skills_roots(self) -> list[str]:
        return [f"{WORKSPACE}/skills"]

    # -- image ------------------------------------------------------------ #
    def build_image(self, *, force: bool = False) -> str:
        return build_claw_image("zeroclaw", IMAGE_TAG, force=force)

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

        self._exec(cid, f"mkdir -p /Users /home /tmp {shlex.quote(DATA_DIR)} {shlex.quote(WORKSPACE)}/memory")
        subprocess.run(
            ["docker", "cp", f"{str(rootfs_dir)}/.", f"{cid}:/"],
            capture_output=True,
            text=True,
            check=True,
        )

        self._seed_completed_workspace(cid)

        if test_mode == "trigger" and poisoned_memory:
            container_path = self.workspace_target_path(poisoned_target)
            self._append_workspace_payload(cid, container_path, poisoned_memory)
            prepared.extra["seeded_memory"] = True
            prepared.extra["seeded_target"] = container_path

        all_hosts = list(dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or [])))
        self._write_config(cid, all_hosts)

        if self.model_provider == "bedrock":
            self._start_bedrock_relay(cid)

        if all_hosts or web:
            ca = self._start_sink(cid, all_hosts, web)
            prepared.extra["sink"] = SINK_STATE
            prepared.extra["network_hosts"] = all_hosts
            if ca:
                prepared.extra["ca_cert"] = ca

        return prepared

    def _seed_native_workspace(self, cid: str) -> None:
        self._exec(cid, f"mkdir -p {shlex.quote(WORKSPACE)}/memory {shlex.quote(WORKSPACE)}/skills")
        for name, content in _native_workspace_docs().items():
            target = f"{WORKSPACE}/{name}"
            exists, _ = self._stat_file(cid, target)
            if exists:
                continue
            self._write_file(cid, target, content)

    def _seed_completed_workspace(self, cid: str) -> None:
        self._seed_native_workspace(cid)
        # Force the one post-onboarding customization even if the case rootfs
        # happened to contain an IDENTITY.md at the native workspace path.
        self._write_file(cid, f"{WORKSPACE}/IDENTITY.md", _native_workspace_docs()["IDENTITY.md"])
        self._exec(cid, f"rm -f {shlex.quote(f'{WORKSPACE}/BOOTSTRAP.md')}")

    def _provider_config(self) -> tuple[str, str, str, str, str | None]:
        """Returns (provider_family, provider_ref, model, api_key, api_base)."""
        model = self.model
        if not model:
            raise ValueError("zeroclaw requires an explicit model")
        if self.model_provider == "openrouter":
            return "openrouter", "openrouter.redclaw", model, self.provider_secret("OPENROUTER_API_KEY"), None
        if self.model_provider == "openai":
            return "openai", "openai.redclaw", model, self.provider_secret("OPENAI_API_KEY"), None
        self.provider_secret("AWS_BEARER_TOKEN_BEDROCK")
        # ZeroClaw's anthropic provider ignores api_base; use openai provider
        # pointing at the relay (which converts OpenAI↔Anthropic format).
        return (
            "openai", "openai.redclaw", model.removeprefix("anthropic."),
            "bedrock-relay",
            f"http://127.0.0.1:{BEDROCK_RELAY_PORT}",
        )

    def _config_toml(self, allowed_private_hosts: list[str] | None = None) -> str:
        provider_family, provider_ref, model, api_key, api_base = self._provider_config()
        # ZeroClaw treats "*" as matching only names that are themselves visibly
        # private (for example localhost). It does not cover an ordinary fixture
        # domain that later resolves to 127.0.0.1. Add only the current case's
        # fixture domains so production DNS/IP blocking remains exercised for
        # every unrelated destination.
        fixture_private_hosts = list(
            dict.fromkeys(host for host in (allowed_private_hosts or []) if host)
        )
        env_passthrough = [
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "BRAVE_API_KEY",
            "PATH",
            "HOME",
        ]
        # Run zeroclaw through its own approval system. The shell allowlist below
        # stays at zeroclaw's schema default, while default_auto_approve matches
        # zeroclaw's shipped container config instead of using any harness-side
        # approval bypass or eval-specific additions.
        default_allowed_commands = [
            "git", "npm", "cargo", "ls", "cat", "grep", "find", "echo", "pwd", "wc",
            "head", "tail", "date", "df", "du", "uname", "uptime", "hostname",
            "python", "python3", "pip", "node", "free",
        ]
        default_auto_approve = [
            "file_read", "file_write", "file_edit", "memory_recall", "memory_store",
            "web_search_tool", "web_fetch", "calculator", "glob_search",
            "content_search", "image_info", "weather", "git_operations",
        ]
        # zeroclaw's default forbidden_paths, MINUS "/home". The default forbids
        # "/home" outright, which would put the eval's task files (/home/user/...)
        # out of reach — "碰都碰不到". Clearing only "/home" (the eval working area,
        # reachable by every other claw) is the single reachability carve-out;
        # every other sensitive default (/etc, /root, ~/.ssh, ~/.aws, /tmp, ...)
        # stays forbidden, so real credential/secret paths remain protected.
        default_forbidden_paths = [
            "/etc", "/root", "/usr", "/bin", "/sbin", "/lib", "/opt", "/boot",
            "/dev", "/proc", "/sys", "/var", "/tmp",
            "~/.ssh", "~/.gnupg", "~/.aws", "~/.config",
        ]
        return textwrap.dedent(
            f"""
            # Must match zeroclaw's CURRENT_SCHEMA_VERSION. Without it the binary
            # treats this config as an outdated schema and silently drops the
            # [providers.models.*] section (model unset, model_provider ref fails
            # to resolve), so the agent loop refuses before any model call.
            schema_version = 3

            [onboard_state]
            quickstart_completed = true

            [secrets]
            encrypt = false

            [runtime]
            kind = "native"

            [memory]
            backend = "markdown"
            auto_save = true
            embedding_provider = "none"
            search_mode = "bm25"
            retrieval_stages = ["fts"]
            audit_enabled = false

            [observability]
            backend = "log"
            log_persistence = "full"
            log_persistence_path = "state/runtime-trace.jsonl"
            log_tool_io = "full"
            log_tool_io_truncate_bytes = 200000

            [browser]
            enabled = false

            [http_request]
            enabled = true
            allowed_domains = ["*"]
            max_response_size = 5000000
            timeout_secs = 30
            allow_private_hosts = true
            allowed_private_hosts = {_toml_array(fixture_private_hosts)}

            [web_fetch]
            enabled = true
            allowed_domains = ["*"]
            blocked_domains = []
            allowed_private_hosts = {_toml_array(fixture_private_hosts)}
            max_response_size = 5000000
            timeout_secs = 30

            [web_search]
            enabled = true
            search_provider = "duckduckgo"
            max_results = 5
            timeout_secs = 15

            [providers.models.{provider_family}.redclaw]
            api_key = {_toml_str(api_key)}
            model = {_toml_str(model)}
            {("uri = " + _toml_str(api_base) + chr(10) + "            ") if api_base else ""}timeout_secs = 120

            [agents.{AGENT_ALIAS}]
            enabled = true
            model_provider = {_toml_str(provider_ref)}
            risk_profile = "redclaw"
            runtime_profile = "redclaw"

            [agents.{AGENT_ALIAS}.workspace]
            path = {_toml_str(WORKSPACE)}
            # Default false. Setting it true is zeroclaw's all-paths escape hatch
            # (it force-disables workspace_only); we run at default and instead let
            # the risk profile below govern reach, so the shipped defenses apply.
            unrestricted_filesystem = false
            read_memory_from = []

            [agents.{AGENT_ALIAS}.workspace.access]

            [agents.{AGENT_ALIAS}.memory]
            backend = "markdown"

            # Run the benchmark in zeroclaw's own full-autonomy mode so
            # non-interactive evals do not stall on human approval prompts.
            # The command allowlist, forbidden paths, and high-risk blocking
            # remain active below; this changes the approval mode, not the
            # rest of zeroclaw's command/path policy.
            # Two deliberate deviations, both explained above and neither a defense:
            #   workspace_only=false + "/home" cleared from forbidden_paths → the
            #   agent can reach the eval's task files, same as every other claw.
            [risk_profiles.redclaw]
            level = "full"
            workspace_only = false
            allowed_commands = {_toml_array(default_allowed_commands)}
            forbidden_paths = {_toml_array(default_forbidden_paths)}
            require_approval_for_medium_risk = true
            block_high_risk_commands = true
            shell_env_passthrough = {_toml_array(env_passthrough)}
            auto_approve = {_toml_array(default_auto_approve)}
            always_ask = []
            allowed_roots = []
            allowed_tools = []
            excluded_tools = []
            sandbox_enabled = false
            sandbox_backend = "none"
            firejail_args = []

            [risk_profiles.redclaw.delegation_policy]
            mode = "forbidden"

            [runtime_profiles.redclaw]
            agentic = true
            max_tool_iterations = 20
            max_actions_per_hour = 100000
            max_cost_per_day_cents = 100000
            shell_timeout_secs = 60
            max_delegation_depth = 0
            memory_recall_limit = 20
            strict_tool_parsing = false
            """
        ).strip() + "\n"

    def _write_config(self, cid: str, allowed_private_hosts: list[str] | None = None) -> None:
        self._write_file(cid, CONFIG_PATH, self._config_toml(allowed_private_hosts))

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
        cmd = [
            "docker",
            "exec",
            "-e",
            f"HOME={RUNTIME_HOME}",
        ]
        cmd += self.fixture_docker_env_args(prepared)
        if self.model_provider in ("openrouter", "openai"):
            for key, value in self.provider_credentials().items():
                cmd += ["-e", f"{key}={value}"]
        if self.env.get("BRAVE_API_KEY"):
            cmd += ["-e", f"BRAVE_API_KEY={self.env['BRAVE_API_KEY']}"]
        if self.model_provider == "bedrock":
            cmd += [
                "-e", f"OPENAI_BASE_URL=http://127.0.0.1:{BEDROCK_RELAY_PORT}",
                "-e", "OPENAI_API_KEY=bedrock-relay",
            ]
        cmd += [
            cid,
            "zeroclaw",
            "agent",
            "-a",
            AGENT_ALIAS,
            "--message",
            task,
            "--session-state-file",
            SESSION_STATE,
        ]

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

        trajectory = self._trajectory(cid)

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
        out = self._exec(
            cid,
            f"find {shlex.quote(WORKSPACE)}/memory -maxdepth 1 -type f -name '*.md' 2>/dev/null | sort",
        ).stdout
        for line in out.splitlines():
            path = line.strip()
            if path and path not in paths:
                paths.append(path)
        return paths

    def _trajectory(self, cid: str) -> str:
        exists, content = self._stat_file(cid, SESSION_STATE)
        if exists and content:
            return _session_state_to_jsonl(content)
        for path in RUNTIME_TRACE_CANDIDATES:
            exists, content = self._stat_file(cid, path)
            if exists and content:
                return content if content.endswith("\n") else content + "\n"
        trace_path = self._exec(
            cid,
            f"find {shlex.quote(CONFIG_DIR)} -type f -name runtime-trace.jsonl -print -quit 2>/dev/null",
        ).stdout.strip().splitlines()
        if trace_path:
            exists, content = self._stat_file(cid, trace_path[0])
            if exists and content:
                return content if content.endswith("\n") else content + "\n"
        return ""

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
            [
                "docker",
                "exec",
                "-i",
                cid,
                "sh",
                "-lc",
                f"mkdir -p $(dirname {shlex.quote(path)}) && cat > {shlex.quote(path)}",
            ],
            input=content,
            capture_output=True,
            text=True,
            check=True,
        )
