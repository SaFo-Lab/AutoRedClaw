"""Per-claw adapters implementing the ClawAdapter interface."""

from redclaw.evaluation.core import ClawAdapter


def get_adapter(
    name: str,
    *,
    model: str | None,
    model_provider: str,
    env: dict[str, str] | None = None,
) -> ClawAdapter:
    """Resolve a claw name to its adapter, configured for ``model_provider``."""
    if name == "nanobot":
        from redclaw.adapters.nanobot import NanobotAdapter as adapter_cls
    elif name == "openclaw":
        from redclaw.adapters.openclaw import OpenclawAdapter as adapter_cls
    elif name == "seclaw":
        from redclaw.adapters.seclaw import SeclawAdapter as adapter_cls
    elif name == "nemoclaw":
        from redclaw.adapters.nemoclaw import NemoclawAdapter as adapter_cls
    elif name == "zeroclaw":
        from redclaw.adapters.zeroclaw import ZeroclawAdapter as adapter_cls
    elif name == "codex":
        from redclaw.adapters.codex import CodexAdapter as adapter_cls
    elif name == "claudecode":
        from redclaw.adapters.claudecode import ClaudeCodeAdapter as adapter_cls
    else:
        raise ValueError(
            f"Unknown claw adapter: {name!r}. "
            "Known: nanobot, openclaw, seclaw, nemoclaw, zeroclaw, "
            "codex, claudecode"
        )
    adapter = adapter_cls(model=model, env=env)
    adapter.set_model_provider(model_provider)
    return adapter
