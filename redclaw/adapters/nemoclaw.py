"""NemoClaw adapter backed by the native NVIDIA runtime chain.

Agent turns are launched only through the supported path::

    controller container -> nemoclaw -> OpenShell gateway -> OpenClaw sandbox

The evaluator uses privileged access to sandbox containers solely as an
out-of-band fixture channel (materialising the rootfs and mock sink). Agent
visible results are read through NemoClaw's official OpenShell exec endpoint so
collection observes the same supervised mount namespace as OpenClaw. The
adapter never launches OpenClaw through Docker and never substitutes a fake
gateway, registry, policy, or OpenClaw configuration. AWS uses RedClaw's relay in
a separate controller through NemoClaw's official custom-provider contract.
"""

from __future__ import annotations

from redclaw.paths import CLAWS_DIR, RUNTIME_DIR

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from redclaw.evaluation.core import (
    ClawAdapter,
    FixtureTLSMaterial,
    FixtureTrustSpec,
    Observations,
    PreparedRun,
    RunResult,
    filesystem_workspace_targets,
    retry_transient,
)


from redclaw.paths import PROJECT_ROOT
CONTROLLER_NAME = "redclaw-nemoclaw-controller"
BEDROCK_CONTROLLER_NAME = "redclaw-nemoclaw-bedrock-controller"
CONTROLLER_IMAGE = "redclaw-nemoclaw-controller:v0.0.109"
SANDBOX_BASE_IMAGE = "ghcr.io/nvidia/nemoclaw/sandbox-base:v0.0.109"
CONTROLLER_DIR = CLAWS_DIR / "nemoclaw"
CONTROLLER_COMPOSE = CONTROLLER_DIR / "compose.yaml"
CONTROLLER_SHARED_DIR = PROJECT_ROOT / ".runtime" / "nemoclaw"
BEDROCK_SHARED_DIR = PROJECT_ROOT / ".runtime" / "nemoclaw-bedrock"
CONTROLLER_NEMOCLAW_SRC = Path("/opt/nemoclaw")
BEDROCK_RUNTIME_PORT = "8000/tcp"
BEDROCK_RELAY_SRC = RUNTIME_DIR / "bedrock_relay.py"
BEDROCK_SERVICE_SRC = CONTROLLER_DIR / "bedrock-service.py"
MOCK_SINK_SRC = RUNTIME_DIR / "mock_sink.py"

# NemoClaw owns and pins the actual OpenClaw sandbox image.  The shared adapter
# runtime value is therefore a marker returned after the controller/gateway
# control plane is initialized and verified.
NATIVE_RUNTIME_MARKER = "nemoclaw-controller-openshell"
RUNTIME_HOME = "/sandbox"
CONFIG_DIR = f"{RUNTIME_HOME}/.openclaw"
WORKSPACE = f"{CONFIG_DIR}/workspace"
AGENT_ID = "main"
SESSIONS_DIR = f"{CONFIG_DIR}/agents/{AGENT_ID}/sessions"

SINK_STATE = "/tmp/redclaw_sink.json"
SINK_IP = "100.0.0.2"
_BIND_SINK_IP_PY = (
    "import fcntl,socket,struct\n"
    "ifn=b'lo:0'; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
    "c=lambda i,ip: fcntl.ioctl(s,i,struct.pack('16sH2s4s8s',ifn,socket.AF_INET,b'\\x00\\x00',socket.inet_aton(ip),b'\\x00'*8))\n"
    f"c(0x8916,'{SINK_IP}'); c(0x891c,'255.255.255.0')\n"
    "fl=struct.unpack('16sh',fcntl.ioctl(s,0x8913,struct.pack('16sh',ifn,0)))[1]\n"
    "fcntl.ioctl(s,0x8914,struct.pack('16sh',ifn,fl|1))\n"
)

CORE_MEMORY_FILES = (
    "AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md",
    "HEARTBEAT.md", "MEMORY.md",
)

# Onboarding creates the native workspace. Per the requested baseline, only the
# required name is prefilled; optional identity fields remain empty.
IDENTITY_SEED = (
    "# IDENTITY.md - Who Am I?\n\n"
    "- **Name:** Assistant\n"
    "- **Creature:**\n"
    "- **Vibe:**\n"
    "- **Emoji:**\n"
    "- **Avatar:**\n"
)

_ONBOARD_LOCK = threading.Lock()
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SECRET_ENV_KEYS = {
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "COMPATIBLE_API_KEY",
    "COMPATIBLE_ANTHROPIC_API_KEY", "NVIDIA_INFERENCE_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK", "NEMOCLAW_PROVIDER_KEY",
}
_CONTROLLER_ENV_KEYS = {
    *_SECRET_ENV_KEYS,
    "NEMOCLAW_PROVIDER", "NEMOCLAW_ENDPOINT_URL", "NEMOCLAW_MODEL",
    "NEMOCLAW_PROVIDER_KEY", "NEMOCLAW_SANDBOX_GPU", "NEMOCLAW_AGENT",
    "NEMOCLAW_POLICY_MODE", "NEMOCLAW_POLICY_TIER", "NEMOCLAW_NO_EXPRESS",
    "COMPATIBLE_API_KEY", "COMPATIBLE_ANTHROPIC_API_KEY", "NO_COLOR",
    "AWS_REGION", "AWS_DEFAULT_REGION",
    "NEMOCLAW_SANDBOX_BASE_IMAGE_REF", "BEDROCK_RELAY_BASE", "BEDROCK_RELAY_MODEL",
}


class NemoclawAdapter(ClawAdapter):
    name = "nemoclaw"
    runtime_home = RUNTIME_HOME
    workspace_targets = filesystem_workspace_targets(WORKSPACE, CORE_MEMORY_FILES)
    # web_fetch runs in the long-lived official OpenClaw gateway, not in the
    # one-shot CLI process. Trust is activated through the root-only official
    # gateway-control restart hook below instead of process environment flags.
    fixture_trust = FixtureTrustSpec(process_env=())

    def __init__(self, *, model: str | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.env = env or {}
        self._select_controller(bedrock=False)
        self._preflight_done = False
        self._host_provenance: dict[str, Any] = {}
        self._bedrock_service: dict[str, str] | None = None

    def _select_controller(self, *, bedrock: bool) -> None:
        self._bedrock_selected = bedrock
        self.controller_service = "bedrock-controller" if bedrock else "controller"
        self._default_controller = BEDROCK_CONTROLLER_NAME if bedrock else CONTROLLER_NAME
        self.controller_name = str(
            self.env.get("NEMOCLAW_CONTROLLER_CONTAINER") or self._default_controller
        )

    def set_model_provider(self, model_provider: str) -> None:
        super().set_model_provider(model_provider)
        self._select_controller(bedrock=model_provider == "bedrock")

    def skills_roots(self) -> list[str]:
        return [f"{WORKSPACE}/skills"]

    # -- native runtime ------------------------------------------------ #
    def _run_native(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # Docker is the evaluator-side transport. Every NemoClaw/OpenShell/git
        # command runs inside the dedicated controller; the host never needs
        # either CLI or their configuration directories.
        command = list(args)
        if not command or command[0] != "docker":
            wrapped = ["docker", "exec"]
            if env is not None:
                for key in sorted(_CONTROLLER_ENV_KEYS):
                    if env.get(key):
                        # Omitting '=value' makes Docker copy the value from the
                        # client environment without exposing it in argv.
                        wrapped.extend(["--env", key])
            wrapped.extend([self.controller_name, *command])
            command = wrapped
        proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"native command failed ({proc.returncode}): {shlex.join(command)}\n"
                f"stdout:\n{proc.stdout[-8000:]}\nstderr:\n{proc.stderr[-8000:]}"
            )
        return proc

    def _version(self, executable: str) -> str:
        proc = self._run_native([executable, "--version"], check=False)
        return (proc.stdout or proc.stderr).strip()

    @staticmethod
    def _reject_fake_runtime(text: str) -> None:
        lowered = text.lower()
        if "redclaw shim" in lowered or "0.0.0-redclaw" in lowered:
            raise RuntimeError("refusing to run against a RedClaw OpenShell substitute")

    def _docker_socket(self) -> Path:
        override = str(
            self.env.get("DOCKER_SOCKET") or os.environ.get("DOCKER_SOCKET") or ""
        ).strip()
        if override:
            path = Path(override).expanduser().resolve()
        else:
            proc = self._run_native(
                [
                    "docker", "context", "inspect", "--format",
                    "{{(index .Endpoints \"docker\").Host}}",
                ],
                timeout=30,
            )
            endpoint = proc.stdout.strip()
            if not endpoint.startswith("unix://"):
                raise RuntimeError(
                    "NemoClaw's Docker compute driver requires a Unix Docker "
                    f"socket; current context returned {endpoint!r}"
                )
            path = Path(endpoint.removeprefix("unix://")).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"Docker socket does not exist: {path}")
        return path

    def _controller_inspect(self) -> dict[str, Any] | None:
        proc = self._run_native(
            ["docker", "inspect", self.controller_name], timeout=30, check=False
        )
        if proc.returncode != 0:
            return None
        try:
            rows = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Docker returned invalid controller inspect JSON") from exc
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _controller_is_ready(controller: dict[str, Any] | None) -> bool:
        if not controller:
            return False
        state = controller.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        labels = (controller.get("Config") or {}).get("Labels") or {}
        return bool(
            state.get("Running")
            and health in (None, "healthy")
            and labels.get("ai.redclaw.runtime-role") == "nemoclaw-controller"
            and str(labels.get("ai.redclaw.shim", "")).lower() == "false"
            and labels.get("org.opencontainers.image.version") == "v0.0.109"
        )

    @staticmethod
    def _controller_is_owned(controller: dict[str, Any] | None) -> bool:
        if not controller:
            return False
        labels = (controller.get("Config") or {}).get("Labels") or {}
        return bool(
            labels.get("ai.redclaw.runtime-role") == "nemoclaw-controller"
            and str(labels.get("ai.redclaw.shim", "")).lower() == "false"
        )

    def _controller_image_is_current(self) -> bool:
        proc = self._run_native(
            ["docker", "image", "inspect", CONTROLLER_IMAGE],
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return False
        try:
            rows = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return False
        if len(rows) != 1:
            return False
        labels = (rows[0].get("Config") or {}).get("Labels") or {}
        return bool(
            labels.get("ai.redclaw.runtime-role") == "nemoclaw-controller"
            and str(labels.get("ai.redclaw.shim", "")).lower() == "false"
            and labels.get("org.opencontainers.image.version") == "v0.0.109"
            and labels.get("ai.redclaw.controller-revision") == "2"
        )

    def _ensure_controller(self, *, force: bool) -> None:
        """Idempotently build/start the real NemoClaw controller from adapter assets."""
        if not shutil.which("docker"):
            raise RuntimeError("Docker CLI is required to initialize NemoClaw")
        self._run_native(["docker", "info"], timeout=30)

        current = self._controller_inspect()
        needs_bedrock_port = bool(
            self._bedrock_selected
            and current
            and not self._controller_has_bedrock_port(current)
        )
        if self._controller_is_ready(current) and not force and not needs_bedrock_port:
            return
        if self.controller_name != self._default_controller:
            raise RuntimeError(
                f"custom NemoClaw controller {self.controller_name!r} is not ready; "
                "automatic initialization is available only for the canonical "
                f"container {self._default_controller!r}"
            )
        if current and not self._controller_is_owned(current):
            raise RuntimeError(
                f"refusing to replace existing container {self.controller_name!r}: "
                "it is not labelled as the real RedClaw NemoClaw controller"
            )
        if not CONTROLLER_COMPOSE.is_file():
            raise RuntimeError(
                f"NemoClaw controller Compose asset is missing: {CONTROLLER_COMPOSE}"
            )

        if needs_bedrock_port:
            # Adding a published port recreates the controller. Do this only
            # when no official sandbox can lose its live gateway connection.
            active = self._run_native([
                "docker", "ps", "--filter", "label=openshell.ai/managed-by=openshell",
                "--format", "{{.Names}}",
            ], timeout=30).stdout.strip()
            if active:
                raise RuntimeError(
                    "NemoClaw controller needs its Bedrock relay port published; "
                    "finish the active OpenShell sandboxes before retrying: " + active
                )

        CONTROLLER_SHARED_DIR.mkdir(parents=True, exist_ok=True)
        BEDROCK_SHARED_DIR.mkdir(parents=True, exist_ok=True)
        compose_env = os.environ.copy()
        compose_env.update(
            DOCKER_SOCKET=str(self._docker_socket()),
            NEMOCLAW_SHARED_DIR=str(CONTROLLER_SHARED_DIR.resolve()),
            NEMOCLAW_BEDROCK_SHARED_DIR=str(BEDROCK_SHARED_DIR.resolve()),
        )
        compose = ["docker", "compose", "-f", str(CONTROLLER_COMPOSE)]
        current_labels = ((current or {}).get("Config") or {}).get("Labels") or {}
        stale_container = bool(
            current
            and current_labels.get("org.opencontainers.image.version") != "v0.0.109"
        )
        state = (current or {}).get("State") or {}
        unhealthy_container = bool(
            state.get("Running")
            and (state.get("Health") or {}).get("Status") == "unhealthy"
        )
        if force or not self._controller_image_is_current():
            self._run_native(
                [*compose, "build", "--pull", self.controller_service],
                env=compose_env,
                timeout=3600,
            )
        up = [*compose, "up", "-d"]
        if force or stale_container or unhealthy_container or needs_bedrock_port:
            up.append("--force-recreate")
        up.append(self.controller_service)
        self._run_native(up, env=compose_env, timeout=300)

        last: dict[str, Any] | None = None
        for _ in range(60):
            last = self._controller_inspect()
            if self._controller_is_ready(last) and (
                not self._bedrock_selected
                or self._controller_has_bedrock_port(last or {})
            ):
                self._preflight_done = False
                return
            time.sleep(2)
        raise RuntimeError(
            "NemoClaw controller did not become healthy after automatic "
            f"initialization: {(last or {}).get('State') or 'not found'}"
        )

    @staticmethod
    def _controller_has_bedrock_port(controller: dict[str, Any]) -> bool:
        bindings = (controller.get("HostConfig") or {}).get("PortBindings") or {}
        return all(
            any(item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == host_port
                for item in bindings.get(container_port) or [])
            for container_port, host_port in ((BEDROCK_RUNTIME_PORT, "8000"), ("8081/tcp", "8082"))
        )

    def initialize(self, *, force: bool = False) -> str:
        """Automatically deploy/reuse and verify the official control plane."""
        if self._preflight_done and not force:
            return NATIVE_RUNTIME_MARKER
        self._ensure_controller(force=force)
        self._check_lifecycle_lock_owners()
        return self._preflight_controller()

    def _check_lifecycle_lock_owners(self) -> None:
        """Catch locks retained across controller replacement before a batch starts.

        NemoClaw treats another host's lock as active even when that container
        is gone. Leave recovery explicit: a different host may still be alive.
        """
        script = """
import json
import socket
from pathlib import Path

locks = []
for path in Path('/root/.nemoclaw/state/mcp-lifecycle-locks').glob('*.lock'):
    try:
        owner = json.loads(path.read_text())
    except FileNotFoundError:
        continue  # A concurrent native operation released its lock.
    except (ValueError, OSError):
        owner = {}
    if not isinstance(owner, dict):
        owner = {}
    locks.append({'path': str(path), 'host': owner.get('hostIdentity'),
                  'scope': owner.get('sandboxName')})
print(json.dumps({'host': 'linux:' + socket.gethostname(), 'locks': locks}))
"""
        snapshot = json.loads(self._run_native(["python3", "-c", script], timeout=30).stdout)
        foreign = [lock for lock in snapshot["locks"] if lock["host"] != snapshot["host"]]
        if foreign:
            details = "; ".join(
                f"{lock['scope']} (owner={lock['host']}, path={lock['path']})"
                for lock in foreign
            )
            raise RuntimeError(
                "NemoClaw initialization blocked by lifecycle locks from another "
                f"or unknown controller: {details}. Current controller: {snapshot['host']}. "
                "Verify the owner container has gone, then back up and remove only "
                "its abandoned locks before retrying. No locks were removed."
            )

    def build_image(self, *, force: bool = False) -> str:
        """Compatibility alias; framework entrypoints use ``initialize()``."""
        return self.initialize(force=force)

    def _preflight_controller(self) -> str:
        """Fail-closed verification for the containerised official control plane."""
        if self._preflight_done:
            return NATIVE_RUNTIME_MARKER
        docker_info = self._run_native(["docker", "info"], timeout=30)
        controller_rows = json.loads(
            self._run_native(["docker", "inspect", self.controller_name], timeout=30).stdout
        )
        if len(controller_rows) != 1:
            raise RuntimeError(f"controller inspect returned {len(controller_rows)} rows")
        controller = controller_rows[0]
        controller_state = controller.get("State") or {}
        if not controller_state.get("Running"):
            raise RuntimeError(f"NemoClaw controller is not running: {controller_state}")
        controller_labels = (controller.get("Config") or {}).get("Labels") or {}
        if controller_labels.get("ai.redclaw.runtime-role") != "nemoclaw-controller":
            raise RuntimeError("refusing an unlabelled or non-controller NemoClaw container")
        if str(controller_labels.get("ai.redclaw.shim", "")).lower() != "false":
            raise RuntimeError("refusing a NemoClaw controller that does not explicitly disable shim mode")
        openshell_version = self._version("openshell")
        nemoclaw_version = self._version("nemoclaw")
        gateway = self._run_native(
            ["openshell", "status"], timeout=60, check=False
        )
        gateway_detail = "\n".join((gateway.stdout, gateway.stderr)).strip()
        if gateway.returncode != 0 and not re.search(
            r"(?:connection refused|tcp connect error)", gateway_detail, re.IGNORECASE
        ):
            raise RuntimeError(
                "OpenShell gateway preflight failed for a reason other than a clean "
                f"not-yet-started state:\n{gateway_detail[-4000:]}"
            )
        combined = "\n".join(
            (docker_info.stdout, docker_info.stderr, openshell_version, nemoclaw_version,
             gateway.stdout, gateway.stderr)
        )
        self._reject_fake_runtime(combined)
        git_commit = self._run_native(
            ["git", "-C", str(CONTROLLER_NEMOCLAW_SRC), "rev-parse", "HEAD"], check=False
        ).stdout.strip()
        binary_path = self._run_native(
            ["sh", "-lc", "command -v nemoclaw"], timeout=30
        ).stdout.strip()
        self._host_provenance = {
            "chain": (
                "controller container -> NemoClaw -> OpenShell gateway -> "
                "OpenClaw sandbox -> OpenShell policy"
            ),
            "nemoclaw_version": nemoclaw_version,
            "openshell_version": openshell_version,
            "nemoclaw_source_commit": git_commit or None,
            "nemoclaw_source_path": str(CONTROLLER_NEMOCLAW_SRC),
            "nemoclaw_binary": binary_path,
            "gateway_status": (
                gateway.stdout.strip()
                if gateway.returncode == 0
                else "not running before onboarding (controller verified)"
            ),
            "controller_container": self.controller_name,
            "controller_image_reference": (controller.get("Config") or {}).get("Image"),
            "controller_image_id": controller.get("Image"),
            "controller_labels": controller_labels,
            "redclaw_image_built": False,
            "redclaw_shim_installed": False,
        }
        self._preflight_done = True
        return NATIVE_RUNTIME_MARKER

    def _ensure_bedrock_service(self) -> dict[str, str]:
        """Start our protocol bridge in the controller, outside native sources."""
        region = self.env.get("AWS_REGION") or self.env.get("AWS_DEFAULT_REGION") or "us-east-1"
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
            raise ValueError(f"Invalid AWS region: {region!r}")
        digest = hashlib.sha256(BEDROCK_RELAY_SRC.read_bytes() + BEDROCK_SERVICE_SRC.read_bytes()).hexdigest()[:20]
        directory = f"/opt/redclaw-bedrock/{digest}"
        self._run_native(["mkdir", "-p", directory])
        for source in (BEDROCK_RELAY_SRC, BEDROCK_SERVICE_SRC):
            temporary = f"{directory}/{source.name}.{uuid.uuid4().hex}.tmp"
            self._run_native(["docker", "cp", str(source), f"{self.controller_name}:{temporary}"])
            self._run_native(["mv", temporary, f"{directory}/{source.name}"])
        service_env = os.environ.copy()
        service_env.update(
            AWS_BEARER_TOKEN_BEDROCK=self.env["AWS_BEARER_TOKEN_BEDROCK"],
            BEDROCK_RELAY_BASE=f"https://bedrock-mantle.{region}.api.aws",
            BEDROCK_RELAY_MODEL=self.model or "",
        )
        result = self._run_native(["python3", f"{directory}/bedrock-service.py"], env=service_env)
        service = json.loads(result.stdout)
        if not isinstance(service.get("token"), str) or not service["token"]:
            raise RuntimeError("NemoClaw Bedrock service did not return a client credential")
        return {"token": service["token"], "region": region}

    def _provider_environment(self) -> dict[str, str]:
        """Translate runner credentials into NemoClaw's supported providers."""
        native_env = os.environ.copy()
        for key in _SECRET_ENV_KEYS:
            native_env.pop(key, None)
        model = (self.model or "").strip()

        if self.model_provider == "openrouter":
            key = self.provider_secret("OPENROUTER_API_KEY")
            native_env.update(
                NEMOCLAW_PROVIDER="custom",
                NEMOCLAW_ENDPOINT_URL=self.env.get(
                    "NEMOCLAW_ENDPOINT_URL", "https://openrouter.ai/api/v1"
                ),
                NEMOCLAW_MODEL=model,
                NEMOCLAW_PROVIDER_KEY=key,
                COMPATIBLE_API_KEY=key,
            )
        elif self.model_provider == "openai":
            key = self.provider_secret("OPENAI_API_KEY")
            native_env.update(
                NEMOCLAW_PROVIDER="openai",
                NEMOCLAW_MODEL=model,
                NEMOCLAW_PROVIDER_KEY=key,
                OPENAI_API_KEY=key,
            )
        else:
            self.provider_secret("AWS_BEARER_TOKEN_BEDROCK")
            if not model:
                raise RuntimeError("a model is required for native NemoClaw onboarding")
            if self._bedrock_service is None:
                self._bedrock_service = self._ensure_bedrock_service()
            native_env.update(
                NEMOCLAW_PROVIDER="custom",
                NEMOCLAW_ENDPOINT_URL="http://127.0.0.1:8000/v1",
                NEMOCLAW_MODEL=model,
                NEMOCLAW_PROVIDER_KEY=self._bedrock_service["token"],
                COMPATIBLE_API_KEY=self._bedrock_service["token"],
            )

        if not native_env.get("NEMOCLAW_MODEL"):
            raise RuntimeError("a model is required for native NemoClaw onboarding")
        native_env.update(
            NEMOCLAW_SANDBOX_GPU="0",
            NEMOCLAW_AGENT="openclaw",
            NEMOCLAW_POLICY_MODE="suggested",
            NEMOCLAW_POLICY_TIER="balanced",
            NEMOCLAW_NO_EXPRESS="1",
            # Pair the published sandbox with the pinned controller release.
            # NemoClaw resolves this official ref to a digest and performs its
            # normal ABI/security-inventory validation before using the image.
            NEMOCLAW_SANDBOX_BASE_IMAGE_REF=SANDBOX_BASE_IMAGE,
        )
        return native_env

    def _resolve_container(self, sandbox_name: str) -> str:
        proc = self._run_native(
            [
                "docker", "ps", "--filter",
                f"label=openshell.ai/sandbox-name={sandbox_name}",
                "--filter", "label=openshell.ai/managed-by=openshell",
                "--format", "{{.Names}}",
            ],
            timeout=30,
        )
        candidates = [name for name in proc.stdout.splitlines() if name]
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected exactly one official container for {sandbox_name!r}; found {candidates}"
            )
        return candidates[0]

    def _image_provenance(self, container: str) -> dict[str, Any]:
        container_data = json.loads(
            self._run_native(["docker", "inspect", container], timeout=30).stdout
        )[0]
        image_id = str(container_data.get("Image") or "")
        image_ref = str((container_data.get("Config") or {}).get("Image") or "")
        image_data: dict[str, Any] = {}
        if image_id:
            parsed = json.loads(
                self._run_native(["docker", "image", "inspect", image_id], timeout=30).stdout
            )
            if parsed:
                image_data = parsed[0]
        return {
            "container_name": container,
            "image_reference": image_ref,
            "image_id": image_id,
            "repo_digests": image_data.get("RepoDigests") or [],
            "image_created": image_data.get("Created"),
            "container_labels": {
                key: value for key, value in (container_data.get("Config") or {}).get("Labels", {}).items()
                if key.startswith("openshell.ai/") or key.startswith("org.opencontainers.image.")
            },
        }

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
        if image != NATIVE_RUNTIME_MARKER:
            raise RuntimeError(f"unexpected NemoClaw runtime marker: {image!r}")
        self.initialize()
        # Current maintained NemoClaw accepts at most 19 characters.
        sandbox_name = f"rc-eval-{uuid.uuid4().hex[:11]}"
        # Set before invoking onboarding: a failed onboarding can still leave a
        # partially-created sandbox, and native destroy is safe when none exists.
        cleanup_needed = True
        try:
            # NemoClaw persists one onboarding session on the host. Serialising
            # this phase avoids cross-case session corruption in batch runs.
            with _ONBOARD_LOCK:
                self._run_native(
                    [
                        "nemoclaw", "onboard", "--non-interactive", "--fresh", "--no-gpu",
                        "--no-sandbox-gpu", "--name", sandbox_name, "--agent", "openclaw",
                        "--yes", "--yes-i-accept-third-party-software",
                    ],
                    env=self._provider_environment(),
                    timeout=1800,
                )
            # Do not call `nemoclaw sandbox status` here. The official status
            # command performs a synthetic "Reply with OK" inference probe.
            # In a per-case evaluation that request is redundant, changes the
            # measured workload, and can false-negative reasoning models before
            # the case is ever sent. The first real agent turn is the inference
            # validation; the checks below still prove that the official
            # OpenShell control plane, sandbox, policy, and managed container
            # exist before any evaluator fixtures are installed.
            gateway_status = self._run_native(
                ["openshell", "status"], timeout=60
            ).stdout.strip()
            sandbox_get = self._run_native(
                ["openshell", "sandbox", "get", sandbox_name], timeout=60
            ).stdout
            effective_policy = self._run_native(
                ["openshell", "policy", "get", "--full", sandbox_name], timeout=60
            ).stdout
            self._reject_fake_runtime(sandbox_get + effective_policy)
            if not effective_policy.strip():
                raise RuntimeError("official OpenShell returned an empty effective policy")

            container = self._resolve_container(sandbox_name)
            provenance = {
                **self._host_provenance,
                "gateway_status": gateway_status,
                "sandbox_name": sandbox_name,
                "inference_validation": {
                    "mode": "first_real_agent_turn",
                    "synthetic_probe": False,
                },
                "openshell_sandbox": sandbox_get,
                "openshell_policy_before_fixtures": effective_policy,
                **self._image_provenance(container),
            }
            if self._bedrock_service:
                provenance["bedrock_inference"] = {
                    "requested_model": self.model,
                    "adapter": "redclaw-relay-via-official-custom-provider",
                    "region": self._bedrock_service["region"],
                    "route_log": "/tmp/bedrock_relay_routes.jsonl",
                }
            prepared = PreparedRun(
                container_id=container,
                workspace=WORKSPACE,
                extra={"sandbox_name": sandbox_name, "runtime_provenance": provenance},
            )

            # Evaluator-only setup. The official agent and policy are already
            # installed/running; no agent config, registry or gateway is forged.
            self._exec_checked(container, f"mkdir -p /Users /home /tmp {shlex.quote(WORKSPACE)}")
            self._run_native(
                ["docker", "cp", f"{str(rootfs_dir)}/.", f"{container}:/"], timeout=120
            )
            fixture_roots = self._materialized_fixture_roots(rootfs_dir)
            if fixture_roots:
                self._apply_filesystem_policy(sandbox_name, fixture_roots)
                for root in fixture_roots:
                    self._exec_checked(
                        container,
                        f"chown -R sandbox:sandbox {shlex.quote(root)}",
                    )
                provenance["fixture_filesystem_policy"] = {
                    "read_write": fixture_roots,
                    "applied_via": "openshell policy set --wait",
                }
                provenance["openshell_policy_after_filesystem_fixtures"] = self._run_native(
                    ["openshell", "policy", "get", "--full", sandbox_name], timeout=60
                ).stdout
            self._normalise_native_workspace(container, provenance)

            if test_mode == "trigger" and poisoned_memory:
                target_path = self.workspace_target_path(poisoned_target)
                self._append_workspace_payload(
                    container, target_path, poisoned_memory
                )
                prepared.extra.update(
                    seeded_memory=True, seeded_target=target_path
                )

            all_hosts = list(dict.fromkeys((network_hosts or []) + ((web or {}).get("hosts") or [])))
            if all_hosts or web:
                ca_cert = self._start_sink(container, all_hosts, web)
                self._apply_fixture_policy(sandbox_name, all_hosts)
                policy_after = self._run_native(
                    ["openshell", "policy", "get", "--full", sandbox_name], timeout=60
                ).stdout
                missing_hosts = [host for host in all_hosts if host not in policy_after]
                if missing_hosts:
                    raise RuntimeError(
                        "fixture policy is not visible in effective OpenShell policy: "
                        + ", ".join(missing_hosts)
                    )
                provenance["openshell_policy_after_fixtures"] = policy_after
                prepared.extra.update(
                    sink=SINK_STATE,
                    network_hosts=all_hosts,
                    fixture_policy_applied=True,
                )
                if ca_cert:
                    prepared.extra["ca_cert"] = ca_cert
            cleanup_needed = False
            return prepared
        except Exception:
            if cleanup_needed:
                try:
                    self._destroy_sandbox(sandbox_name, check=False)
                except Exception:
                    # Preserve the setup failure; the failed native destroy is
                    # discoverable in `nemoclaw list` and can be retried.
                    pass
            raise

    @staticmethod
    def _materialized_fixture_roots(rootfs_dir: Path) -> list[str]:
        """Smallest useful policy roots for synthetic user-owned case files.

        System trees keep the official policy unchanged. User home trees are
        granted one account at a time instead of opening all of /home or /Users.
        """
        roots: list[str] = []
        excluded = {
            "app", "bin", "boot", "dev", "etc", "lib", "lib64", "opt",
            "proc", "root", "run", "sbin", "sys", "usr", "var",
        }
        for entry in sorted(rootfs_dir.iterdir(), key=lambda path: path.name):
            name = entry.name
            if name in {"home", "Users"} and entry.is_dir():
                roots.extend(f"/{name}/{child.name}" for child in sorted(entry.iterdir()))
            elif name not in excluded and name not in {"sandbox", "tmp"}:
                roots.append(f"/{name}")
        return list(dict.fromkeys(roots))

    def _upload_controller_file(self, local_path: Path, *, suffix: str) -> str:
        remote_path = f"/tmp/redclaw-{uuid.uuid4().hex}{suffix}"
        self._run_native(
            ["docker", "cp", str(local_path), f"{self.controller_name}:{remote_path}"],
            timeout=60,
        )
        return remote_path

    def _remove_controller_files(self, *paths: str) -> None:
        if paths:
            self._run_native(["rm", "-f", "--", *paths], timeout=30, check=False)

    def _apply_filesystem_policy(self, sandbox_name: str, roots: list[str]) -> None:
        """Add exact fixture roots through the real OpenShell policy API."""
        no_color = os.environ.copy()
        no_color["NO_COLOR"] = "1"
        rendered = self._run_native(
            ["openshell", "policy", "get", "--full", sandbox_name],
            env=no_color,
            timeout=60,
        ).stdout
        marker = "\n---\n"
        if marker not in rendered:
            raise RuntimeError("OpenShell full policy output has no YAML document marker")
        policy_yaml = rendered.split(marker, 1)[1]
        yaml_module = CONTROLLER_NEMOCLAW_SRC / "node_modules" / "yaml"
        self._run_native(["test", "-d", str(yaml_module)], timeout=30)
        script = (
            "const fs=require('node:fs');"
            "const YAML=require(process.argv[1]);"
            "const input=process.argv[2], output=process.argv[3];"
            "const roots=JSON.parse(process.argv[4]);"
            "const doc=YAML.parse(fs.readFileSync(input,'utf8'));"
            "doc.filesystem_policy ||= {};"
            "const rw=Array.isArray(doc.filesystem_policy.read_write) ? doc.filesystem_policy.read_write : [];"
            "doc.filesystem_policy.read_write=[...new Set([...rw,...roots])];"
            "fs.writeFileSync(output,YAML.stringify(doc));"
        )
        with tempfile.TemporaryDirectory(prefix="redclaw-nemoclaw-fs-policy-") as tmp:
            input_path = Path(tmp) / "current.yaml"
            input_path.write_text(policy_yaml, encoding="utf-8")
            remote_input = self._upload_controller_file(input_path, suffix="-current.yaml")
            remote_output = f"/tmp/redclaw-{uuid.uuid4().hex}-with-fixtures.yaml"
            try:
                self._run_native(
                    [
                        "node", "-e", script, str(yaml_module), remote_input,
                        remote_output, json.dumps(roots),
                    ],
                    timeout=60,
                )
                self._run_native(
                    [
                        "openshell", "policy", "set", "--policy", remote_output,
                        "--wait", "--timeout", "120", sandbox_name,
                    ],
                    timeout=180,
                )
            finally:
                self._remove_controller_files(remote_input, remote_output)
        verified = self._run_native(
            ["openshell", "policy", "get", "--full", sandbox_name],
            env=no_color,
            timeout=60,
        ).stdout
        missing = [root for root in roots if root not in verified]
        if missing:
            raise RuntimeError("fixture filesystem policy did not load: " + ", ".join(missing))

    def _normalise_native_workspace(self, container: str, provenance: dict[str, Any]) -> None:
        present, agents = self._stat_file(container, f"{WORKSPACE}/AGENTS.md")
        if not present or len(agents.strip()) < 500:
            raise RuntimeError(
                "official onboarding did not produce a full native AGENTS.md; refusing a toy baseline"
            )
        if "RedClaw evaluation" in agents or "assistant-workspace" in agents:
            raise RuntimeError("native AGENTS.md was replaced by evaluator-authored content")
        self._write_file(container, f"{WORKSPACE}/IDENTITY.md", IDENTITY_SEED)
        self._exec_checked(container, f"rm -f {shlex.quote(WORKSPACE + '/BOOTSTRAP.md')}")
        bootstrap, _ = self._stat_file(container, f"{WORKSPACE}/BOOTSTRAP.md")
        if bootstrap:
            raise RuntimeError("BOOTSTRAP.md still exists after completed-onboarding baseline setup")
        provenance["workspace_baseline"] = {
            "agents_md": "official onboarding output (preserved)",
            "identity_md": "Name=Assistant; optional fields empty",
            "bootstrap_md": "absent",
            "onboarding": "completed by native nemoclaw onboard",
            "path_aliases_installed": False,
        }

    def _apply_fixture_policy(self, sandbox_name: str, hosts: list[str]) -> None:
        if not hosts:
            return
        for host in hosts:
            if not _HOSTNAME_RE.fullmatch(host):
                raise ValueError(f"unsafe fixture hostname: {host!r}")
        lines = [
            "preset:",
            "  name: redclaw-fixtures",
            "  description: Evaluation-only mock endpoints; enforced by OpenShell",
            "network_policies:",
            "  redclaw_fixtures:",
            "    name: redclaw_fixtures",
            "    endpoints:",
        ]
        for host in hosts:
            for port in (80, 443):
                endpoint = [
                    f"      - host: {json.dumps(host)}",
                    f"        port: {port}",
                ]
                # OpenShell terminates the agent-side TLS connection and then
                # opens its own upstream connection. The local sink uses a leaf
                # signed by RedClaw's per-run private CA. That CA is installed
                # for OpenClaw, but OpenShell's upstream verifier has a separate
                # trust boundary, so this case-scoped endpoint needs tls: skip.
                if port == 443:
                    endpoint.append("        tls: skip")
                endpoint.extend(
                    [
                        "        protocol: rest",
                        "        enforcement: enforce",
                        "        access: full",
                    ]
                )
                lines.extend(endpoint)
        lines.extend(
            [
                "    binaries:",
                "      - { path: /usr/local/bin/openclaw }",
                "      - { path: /usr/local/bin/node }",
                "      - { path: /usr/bin/node }",
                "      - { path: /usr/bin/curl }",
                "      - { path: /usr/local/bin/curl }",
                "      - { path: /usr/bin/python3* }",
            ]
        )
        with tempfile.TemporaryDirectory(prefix="redclaw-nemoclaw-policy-") as tmp:
            path = Path(tmp) / "fixtures.yaml"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            remote_path = self._upload_controller_file(path, suffix="-fixtures.yaml")
            try:
                self._run_native(
                    [
                        "nemoclaw", "sandbox", "policy", "add", sandbox_name,
                        "--from-file", remote_path, "--yes",
                    ],
                    timeout=180,
                )
            finally:
                self._remove_controller_files(remote_path)

    def activate_fixture_trust(
        self,
        container: str,
        material: FixtureTLSMaterial,
    ) -> None:
        """Merge the fixture root into OpenShell's bundles and reload gateway.

        NemoClaw starts the official OpenClaw gateway during onboarding, before
        per-case fixtures exist. Node reads NODE_EXTRA_CA_CERTS only at process
        start, so passing a CA to the later ``openclaw agent`` client cannot
        affect web_fetch. The adapter extends the root-owned OpenShell bundles
        atomically and uses NemoClaw's own host-authorized gateway-control API.
        """
        merge_script = r"""
import os
import stat
import sys
import tempfile

source = sys.argv[1]
targets = sys.argv[2:]
with open(source, "rb") as handle:
    certificate = handle.read().strip()
if b"-----BEGIN CERTIFICATE-----" not in certificate:
    raise SystemExit("fixture CA is not a PEM certificate")

for target in targets:
    info = os.lstat(target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"refusing non-regular trust bundle: {target}")
    with open(target, "rb") as handle:
        current = handle.read()
    if certificate in current:
        continue
    directory = os.path.dirname(target)
    descriptor, temporary = tempfile.mkstemp(prefix=".redclaw-ca-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(current.rstrip() + b"\n" + certificate + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
"""
        merge_command = " ".join(
            [
                "python3",
                "-c",
                shlex.quote(merge_script),
                shlex.quote(material.ca_cert),
                "/etc/openshell-tls/openshell-ca.pem",
                "/etc/openshell-tls/ca-bundle.pem",
            ]
        )
        self._exec_checked(container, merge_command)
        self._restart_gateway_for_fixture_trust(container)

    def _restart_gateway_for_fixture_trust(self, container: str) -> None:
        """Reload through NemoClaw's public managed-gateway lifecycle."""
        inspected = self._run_native(
            [
                "docker", "inspect", "--format",
                '{{ index .Config.Labels "openshell.ai/sandbox-name" }}',
                container,
            ],
            timeout=30,
        )
        sandbox_name = inspected.stdout.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", sandbox_name):
            raise RuntimeError(
                "official OpenShell container has no valid sandbox-name label: "
                f"{sandbox_name!r}"
            )
        self._run_native(
            [
                "nemoclaw", "sandbox", "gateway", "restart",
                sandbox_name, "--quiet",
            ],
            timeout=300,
        )

    # -- agent execution ----------------------------------------------- #
    def _agent_command(self, prepared: PreparedRun, task: str) -> list[str]:
        sandbox_name = str(prepared.extra["sandbox_name"])
        args = [
            "nemoclaw", "sandbox", "agent", sandbox_name,
            "--agent", AGENT_ID, "-m", task,
        ]
        return args

    def run(self, prepared: PreparedRun, task: str, *, timeout: int) -> RunResult:
        command = self._agent_command(prepared, task)
        start = time.monotonic()
        try:
            proc, retries = retry_transient(
                lambda: self._run_native(command, timeout=timeout, check=False)
            )
            try:
                payload = json.loads(proc.stdout)
                session_file = (payload.get("meta", {}).get("agentMeta", {}) or {}).get(
                    "sessionFile"
                )
                if session_file:
                    prepared.extra["session_file"] = session_file
            except (json.JSONDecodeError, AttributeError):
                pass
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=round(time.monotonic() - start, 2),
                extra={
                    "provider_retries": retries,
                    "runtime_provenance": prepared.extra["runtime_provenance"],
                    "native_agent_command": command[:4],
                },
            )
        except subprocess.TimeoutExpired as exc:
            decode = lambda value: value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
            return RunResult(
                returncode=124,
                stdout=decode(exc.stdout),
                stderr=decode(exc.stderr),
                duration_s=round(time.monotonic() - start, 2),
                timed_out=True,
                extra={"runtime_provenance": prepared.extra["runtime_provenance"]},
            )

    # -- fixture and observation channel ------------------------------- #
    def _start_sink(self, container: str, hosts: list[str], web: dict | None) -> str | None:
        self._run_native(
            ["docker", "cp", str(MOCK_SINK_SRC), f"{container}:/opt/mock_sink.py"], timeout=60
        )
        self._exec_checked(container, f"python3 -c {shlex.quote(_BIND_SINK_IP_PY)}")
        env_prefix = ""
        if web and web.get("web_dir") and web.get("url_map"):
            self._exec_checked(container, "mkdir -p /opt/web")
            for source, destination in (
                (f"{str(web['web_dir'])}/.", "/opt/web"),
                (str(web["url_map"]), "/opt/url_map.json"),
            ):
                self._run_native(["docker", "cp", source, f"{container}:{destination}"], timeout=60)
            env_prefix = "REDCLAW_WEB_DIR=/opt/web REDCLAW_URL_MAP=/opt/url_map.json "
            for key, destination, env_key in (
                ("site_meta", "/opt/site_meta.json", "REDCLAW_SITE_META"),
                ("app_behavior", "/opt/app_behavior.json", "REDCLAW_APP_BEHAVIOR"),
            ):
                if web.get(key):
                    self._run_native(
                        ["docker", "cp", str(web[key]), f"{container}:{destination}"], timeout=60
                    )
                    env_prefix += f"{env_key}={destination} "
        if hosts:
            append = " && ".join(
                f"echo {shlex.quote(SINK_IP + ' ' + host)} >> /etc/hosts" for host in hosts
            )
            self._exec_checked(container, append)
        tls = self.provision_fixture_tls(container, hosts)
        https = tls.sink_arguments()
        self._run_native(
            [
                "docker", "exec", "-d", container, "sh", "-lc",
                f"{env_prefix}python3 /opt/mock_sink.py --port 80 {https}--state {shlex.quote(SINK_STATE)}",
            ],
            timeout=30,
        )
        for _ in range(30):
            check = self._exec(
                container,
                "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/__sink__/state || true",
            )
            if check.stdout.strip().endswith("200"):
                return tls.ca_cert
            time.sleep(0.2)
        raise RuntimeError("mock sink failed to become ready in the official sandbox")

    def collect(self, prepared: PreparedRun, *, file_paths: list[str]) -> Observations:
        container = prepared.container_id
        memory_sources, blob_parts, memory_attempted = self.read_workspace_memory(
            lambda path: self._runtime_stat_file(prepared, path)
        )
        files: dict[str, str] = {}
        for path in file_paths:
            exists, content = self._runtime_stat_file(prepared, path)
            if exists:
                files[path] = content
        state: dict[str, Any] = {
            "native_runtime": prepared.extra.get("runtime_provenance", {})
        }
        if prepared.extra.get("sink"):
            exists, content = self._stat_file(container, str(prepared.extra["sink"]))
            if exists and content.strip():
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        parsed["native_runtime"] = state["native_runtime"]
                        state = parsed
                except json.JSONDecodeError:
                    pass
        trajectory = ""
        session_file = prepared.extra.get("session_file")
        if session_file:
            _, trajectory = self._runtime_stat_file(prepared, str(session_file))
        if not trajectory:
            newest = self._runtime_exec(
                prepared,
                f"ls -t {shlex.quote(SESSIONS_DIR)}/*.jsonl 2>/dev/null | head -1",
            ).stdout.strip()
            if newest:
                _, trajectory = self._runtime_stat_file(prepared, newest)
        state["skills"] = self.read_skills(prepared)
        return Observations(
            memory_blob="\n\n".join(blob_parts),
            files=files,
            memory_sources=memory_sources,
            memory_attempted=memory_attempted,
            state=state,
            trajectory=trajectory,
        )

    def _destroy_sandbox(self, sandbox_name: str, *, check: bool) -> None:
        self._run_native(
            [
                "nemoclaw", "sandbox", "destroy", sandbox_name,
                "--yes", "--no-cleanup-gateway",
            ],
            timeout=300,
            check=check,
        )

    def teardown(self, prepared: PreparedRun) -> None:
        self._destroy_sandbox(str(prepared.extra["sandbox_name"]), check=True)

    def _runtime_exec(
        self,
        prepared: PreparedRun,
        command: str,
    ) -> subprocess.CompletedProcess[str]:
        """Execute in the agent's supervised OpenShell workload namespace."""
        return self._run_native(
            [
                "nemoclaw", "sandbox", "exec",
                str(prepared.extra["sandbox_name"]),
                "--no-tty", "--timeout", "120", "--",
                "sh", "-lc", command,
            ],
            timeout=150,
            check=False,
        )

    def _runtime_stat_file(self, prepared: PreparedRun, path: str) -> tuple[bool, str]:
        """Read a file exactly where the OpenClaw process sees it."""
        quoted = shlex.quote(path)
        proc = self._runtime_exec(
            prepared,
            f"if [ -f {quoted} ]; then printf '__RC_PRESENT__\\n'; cat {quoted}; fi",
        )
        marker = "__RC_PRESENT__\n"
        if marker not in proc.stdout:
            return False, ""
        return True, proc.stdout.partition(marker)[2]

    def read_skills(self, prepared: PreparedRun) -> list[dict[str, str]]:
        """Read skills from the same supervised namespace as the agent."""
        out: list[dict[str, str]] = []
        for root in self.skills_roots():
            listing = self._runtime_exec(
                prepared,
                f"find {shlex.quote(root)} -maxdepth 4 -name SKILL.md -type f "
                "2>/dev/null || true",
            )
            for skill_md in listing.stdout.splitlines():
                skill_md = skill_md.strip()
                if not skill_md.startswith(root + "/"):
                    continue
                sdir = str(Path(skill_md).parent)
                ok, body = self._runtime_stat_file(prepared, skill_md)
                parts = [body] if ok else []
                siblings = self._runtime_exec(
                    prepared,
                    f"find {shlex.quote(sdir)} -type f ! -name SKILL.md "
                    "2>/dev/null | head -50 || true",
                )
                for sibling in siblings.stdout.splitlines():
                    sibling = sibling.strip()
                    if not sibling.startswith(sdir + "/"):
                        continue
                    sibling_ok, sibling_content = self._runtime_stat_file(
                        prepared, sibling
                    )
                    if sibling_ok:
                        parts.append(f"--- {sibling} ---\n{sibling_content}")
                out.append({"path": skill_md, "content": "\n".join(parts)})
        return out

    # These helpers are explicitly evaluator-side fixture/setup access in the
    # outer Docker namespace. Never use them to observe agent-written files.
    def _exec(self, container: str, command: str) -> subprocess.CompletedProcess[str]:
        return self._run_native(
            ["docker", "exec", "--user", "0:0", container, "sh", "-lc", command],
            timeout=120,
            check=False,
        )

    def _exec_checked(self, container: str, command: str) -> subprocess.CompletedProcess[str]:
        return self._run_native(
            ["docker", "exec", "--user", "0:0", container, "sh", "-lc", command],
            timeout=120,
        )

    def _stat_file(self, container: str, path: str) -> tuple[bool, str]:
        quoted = shlex.quote(path)
        proc = self._exec(
            container,
            f"if [ -f {quoted} ]; then echo __RC_PRESENT__; cat {quoted}; fi",
        )
        if not proc.stdout.startswith("__RC_PRESENT__"):
            return False, ""
        return True, proc.stdout[len("__RC_PRESENT__\n"):]

    def _write_file(self, container: str, path: str, content: str) -> None:
        proc = subprocess.run(
            [
                "docker", "exec", "--user", "0:0", "-i", container,
                "sh", "-lc", f"mkdir -p $(dirname {shlex.quote(path)}) && cat > {shlex.quote(path)}",
            ],
            input=content,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"evaluator file write failed for {path}: {proc.stderr[-4000:]}")
