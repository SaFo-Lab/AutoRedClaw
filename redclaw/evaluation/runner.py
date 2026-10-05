"""Orchestrate one real-claw run against a RedClaw case and score it.

    python -m redclaw run \
        --case-file data/generated/secret_credential_exfiltration_poisoned_memory_propagation.json \
        --case-id secret_credential_exfiltration_poisoned_memory_propagation_002 \
        --claw nanobot --test-mode inject \
        --model openai/gpt-4o-mini --model-provider openrouter

Pipeline: load case -> build consistent rootfs -> adapter.prepare/run/collect
-> score with redclaw.environments.harness.evaluate_check -> write report.

The claw runs unmodified inside a container; we observe its durable memory and
the case's declared files afterward. No agent instrumentation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


from redclaw.evaluation.core import (
    PROVIDER_CREDENTIALS,
    CaseSpec,
    build_case_rootfs,
    build_case_web,
    file_check_paths,
    fixture_network_hosts,
    has_behavioral_checks,
    nominal_memory_paths,
    resolve_inject_workspace_placeholders,
    score,
)
from redclaw.environments import harness
from redclaw.adapters import get_adapter
from redclaw.evaluation.skills import (
    adapt_skill_inject_runtime,
    bind_fixture_text,
    bind_reused_skill_web,
    reset_bound_skill_rootfs,
)

from redclaw.paths import PROJECT_ROOT
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "collection"


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


MODEL_PROVIDERS = ("openrouter", "openai", "bedrock", "codex", "claudecode")


def add_model_args(parser: argparse.ArgumentParser, *, role: str) -> None:
    """--model and --model-provider, both required: nothing is inferred from .env."""
    parser.add_argument("--model", required=True, help=f"{role} model id (required; no default).")
    parser.add_argument(
        "--model-provider",
        required=True,
        choices=MODEL_PROVIDERS,
        help=("Provider for --model (required). codex and claudecode use saved CLI "
              "subscription logins."),
    )


def add_asset_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--asset-model", dest="asset_model", default=None,
                        help="Model for LLM rootfs/web asset synthesis (required when it runs; no default).")
    parser.add_argument("--asset-model-provider", dest="asset_model_provider",
                        choices=harness.ASSET_PROVIDERS, default=None,
                        help="API for --asset-model (required when LLM asset synthesis runs).")


def check_asset_args(parser: argparse.ArgumentParser, args: argparse.Namespace, *, synthesizes: bool) -> None:
    """Fail at startup when LLM assets will be synthesized without an explicit model/provider/key.

    Keys from --env-file are exported first so synthesis uses the same file the
    agent credentials come from.
    """
    if not synthesizes or "llm" not in (args.web_assets, args.file_assets):
        return
    for key, value in load_env_file(args.env_file).items():
        os.environ.setdefault(key, value)
    try:
        harness.require_asset_llm(args.asset_model, args.asset_model_provider)
    except harness.AssetConfigError as exc:
        parser.error(f"{exc} (pass --asset-model and --asset-model-provider, or use offline assets)")

def load_case(case_file: Path, case_id: str | None) -> CaseSpec:
    data = json.loads(case_file.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    if case_id:
        for rec in records:
            if isinstance(rec, dict) and str(rec.get("id")) == case_id:
                return CaseSpec(rec)
        raise SystemExit(f"case id {case_id!r} not found in {case_file}")
    if len(records) != 1:
        ids = [r.get("id") for r in records if isinstance(r, dict)]
        raise SystemExit(f"{case_file} holds multiple cases; pass --case-id. Available: {ids}")
    return CaseSpec(records[0])


def poisoned_workspace_seed(case: CaseSpec) -> tuple[str | None, str]:
    """For trigger mode: returns (content, target_path) to pre-seed before the run.

    - Workspace-trigger (Option A): memory_surfaces present → seed that file.
    - Env-trigger (Option B): injected_observations present, no memory_surfaces
      → return (None, '') so the runner materialises env assets normally.
    Cases without either delivery mechanism are invalid; the runner never
    invents a memory target or payload.
    """
    memory = case.environment.get("memory_surfaces")
    if isinstance(memory, list):
        for entry in memory:
            if isinstance(entry, dict):
                content = entry.get("content") or entry.get("state")
                path = entry.get("path", "memory.md")
                if isinstance(content, str) and content.strip() and content.strip().lower() != "present":
                    return (content if content.endswith("\n") else content + "\n"), path

    # Env-trigger (Option B): injected_observations drives delivery — no pre-seeding.
    if case.injected_observations:
        return None, ""

    raise ValueError(
        f"{case.case_id}: trigger case declares neither memory_surfaces, "
        "skill_surfaces nor injected_observations"
    )


def poisoned_skill_seed(case: CaseSpec) -> list[dict]:
    """Trigger skill entries from environment.skill_surfaces (claw-neutral).

    Each entry: {name, description, body, scripts?, metadata?}. The adapter wraps
    the body in a generic name+description manifest — identical across claws, the
    way a real third-party skill ships — and writes it under the claw's skills
    dir; each claw runs its OWN selection over that. Entries with no body are
    dropped (nothing to deliver)."""
    entries = case.environment.get("skill_surfaces")
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict) and str(e.get("body") or "").strip()]


def load_env(env_file: Path) -> dict[str, str]:
    runtime_keys = {
        "CODEX_AUTH_VOLUME", "CLAUDE_CODE_AUTH_VOLUME",
        # Keep batch initialization on the image/version selected by preflight.
        "CODEX_IMAGE", "CODEX_CLI_VERSION",
        "CLAUDECODE_PERMISSION_MODE",
        # Read legacy filters so the adapter rejects stale hand-picked rules.
        "CLAUDECODE_ALLOWED_TOOLS",
    }
    return {
        **load_env_file(env_file),
        **{
            k: v
            for k, v in os.environ.items()
            if k.endswith("_API_KEY") or k in runtime_keys
        },
    }


DEFAULT_AUTH_VOLUMES = {
    "codex": ("CODEX_AUTH_VOLUME", "redclaw-codex-auth"),
    "claudecode": ("CLAUDE_CODE_AUTH_VOLUME", "redclaw-claudecode-auth"),
}


def check_provider_credentials(env: dict[str, str], provider: str) -> None:
    """Fail early when the selected provider has no credential.

    Codex and Claude Code fall back to their default Docker auth volumes, which
    adapter.initialize() creates or validates.
    """
    default_auth = DEFAULT_AUTH_VOLUMES.get(provider)
    if default_auth and not env.get(default_auth[0]):
        env[default_auth[0]] = default_auth[1]
    required = PROVIDER_CREDENTIALS.get(provider, ())
    if required and not env.get(required[0]):
        raise SystemExit(
            f"--model-provider {provider} requires {required[0]}, which is not set "
            f"(looked in the --env-file and the process environment)."
        )


def execute_case(adapter, case: CaseSpec, *, test_mode: str, timeout: int, run_dir: Path, keep: bool = False, verbose: bool = True, web_assets: str = "offline", file_assets: str = "offline", asset_model: str | None = None, asset_provider: str | None = None, reasoning_effort: str | None = None, reuse_assets: bool = False, task_override: str | None = None) -> dict:
    """Run one already-built claw against one case and score it. The adapter's
    image must already be built. Returns the report dict and writes artifacts to
    run_dir. Shared by the single-case CLI and the batch runner.

    web_assets: "offline" (template HTML, free) or "llm" (realistic HTML via the
    framework's asset generator). file_assets independently controls whether
    rootfs files use offline placeholders or LLM-generated contents.

    reuse_assets pins the environment: EVERY asset (rootfs and web alike) is
    taken from the sibling probe-stage render, and a miss raises
    AssetReuseError rather than materializing a possibly-different one. Off, all
    assets are materialized fresh. The probe stage is the producer, so it always
    runs with this off."""
    run_dir.mkdir(parents=True, exist_ok=True)
    template_case = case
    if test_mode == "inject":
        (run_dir / "setting.template.json").write_text(
            json.dumps(template_case.raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    case, placeholder_debug = resolve_inject_workspace_placeholders(
        adapter, template_case, test_mode=test_mode
    )
    if test_mode == "inject":
        (run_dir / "placeholder_bindings.json").write_text(
            json.dumps(placeholder_debug, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if task_override is not None:
        if task_override == template_case.benign_user_goal:
            task_override = case.benign_user_goal
        elif task_override == template_case.attacker_instruction:
            task_override = case.attacker_instruction
        else:
            for placeholder, replacement in placeholder_debug.get("bindings", {}).items():
                task_override = task_override.replace(placeholder, replacement)
    (run_dir / "setting.json").write_text(json.dumps(case.raw, ensure_ascii=False, indent=2), encoding="utf-8")
    reset_bound_skill_rootfs(
        adapter, case, test_mode=test_mode, run_dir=run_dir, reuse_assets=reuse_assets,
    )
    rootfs_dir = build_case_rootfs(
        case,
        run_dir,
        reuse=reuse_assets,
        placeholder_bindings=placeholder_debug.get("bindings") or None,
        file_llm=(file_assets == "llm"),
        web_llm=(web_assets == "llm"),
        model=asset_model,
        provider=asset_provider,
        reasoning_effort=reasoning_effort,
    )
    web = build_case_web(
        case,
        run_dir,
        llm=(web_assets == "llm"),
        file_llm=(file_assets == "llm"),
        reuse=reuse_assets,
        model=asset_model,
        provider=asset_provider,
        reasoning_effort=reasoning_effort,
    )  # None if no web pages

    if reuse_assets:
        bind_reused_skill_web(
            case, test_mode=test_mode, web=web,
            bindings=placeholder_debug.get("bindings") or {},
        )

    case, fixture_bindings = adapt_skill_inject_runtime(
        adapter, case, test_mode=test_mode, rootfs_dir=rootfs_dir, web=web,
    )
    if fixture_bindings:
        if task_override is not None:
            task_override = bind_fixture_text(task_override, fixture_bindings)
        (run_dir / "setting.json").write_text(
            json.dumps(case.raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "fixture_path_bindings.json").write_text(
            json.dumps(fixture_bindings, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # SKILL.md surface: a poisoned skill seeded via the claw's own skills dir. It
    # takes precedence over the memory-poison fallback so a skill_md case never
    # also seeds memory.md. Delivery is left to the claw's native selection —
    # the harness only writes the skill to disk (see adapter.seed_skills).
    skill_entries = poisoned_skill_seed(case) if test_mode == "trigger" else []
    if skill_entries:
        poisoned, poisoned_path = None, ""
    else:
        poisoned, poisoned_path = poisoned_workspace_seed(case) if test_mode == "trigger" else (None, "")
    network_hosts = fixture_network_hosts(case) if has_behavioral_checks(case) else []
    prepared = adapter.prepare(
        image=adapter.initialize(), rootfs_dir=rootfs_dir, test_mode=test_mode,
        poisoned_memory=poisoned, poisoned_target=poisoned_path,
        network_hosts=network_hosts, web=web,
    )
    if skill_entries:
        adapter.seed_skills(prepared, skill_entries)
    try:
        # Capture the FULL initial state (same paths we score against) BEFORE the
        # agent runs, so score() can require a baseline-fail → final-pass
        # transition for attack success (rejects trivially-satisfied oracles).
        collect_paths = list(dict.fromkeys(file_check_paths(case) + nominal_memory_paths(case)))
        baseline_obs = adapter.collect(prepared, file_paths=collect_paths)
        pre_memory = baseline_obs.memory_sources
        if verbose:
            print(f"[run] container {prepared.container_id[:12]} running (timeout={timeout}s) ...")
        run_result = adapter.run(prepared, task=task_override or case.benign_user_goal, timeout=timeout)
        observations = adapter.collect(prepared, file_paths=collect_paths)
        report = score(case, observations, test_mode=test_mode, scoring_root=run_dir / "scoring", state=observations.state, baseline=baseline_obs)
        report["run"] = {
            "claw": adapter.name, "model": getattr(adapter, "model", None),
            "returncode": run_result.returncode, "timed_out": run_result.timed_out, "duration_s": run_result.duration_s,
            # harness-side bookkeeping (provider retries, confirmation round-trips)
            # so a limit that suppressed an action is visible rather than looking
            # like the agent defended.
            **(run_result.extra or {}),
        }
        if fixture_bindings:
            report["run"]["fixture_path_bindings"] = fixture_bindings
        report["memory_sources"] = dict(observations.memory_sources)
        report["behavioral_state"] = observations.state
    finally:
        if not keep:
            adapter.teardown(prepared)

    def _save_memory_snapshot(parent: Path, sources: dict[str, str]) -> None:
        parent.mkdir(parents=True, exist_ok=True)
        for path, content in sources.items():
            fname = path.rsplit("/", 1)[-1] or "memory"
            (parent / fname).write_text(content, encoding="utf-8")

    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "stdout.txt").write_text(run_result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(run_result.stderr, encoding="utf-8")
    (run_dir / "memory_blob.md").write_text(observations.memory_blob, encoding="utf-8")
    if observations.trajectory:
        (run_dir / "trajectory.jsonl").write_text(observations.trajectory, encoding="utf-8")
    _save_memory_snapshot(run_dir / "workspace_before", pre_memory)
    _save_memory_snapshot(run_dir / "workspace_after", observations.memory_sources)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real claw against a RedClaw case and score inject/trigger.")
    parser.add_argument("--case-file", required=True, type=Path)
    parser.add_argument("--case-id", default=None)
    parser.add_argument("--claw", default="nanobot")
    parser.add_argument("--test-mode", choices=["inject", "trigger"], default="inject")
    add_model_args(parser, role="Claw (agent under test)")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to let the claw run.")
    parser.add_argument("--build", action="store_true", help="Force rebuild of the claw image.")
    parser.add_argument("--keep", action="store_true", help="Do not remove the container after the run.")
    parser.add_argument("--web-assets", choices=["offline", "llm"], default="offline", help="How to synthesize served web pages: offline template (free) or LLM (realistic; needs --asset-model/--asset-model-provider).")
    parser.add_argument("--file-assets", choices=["offline", "llm"], default="offline", help="How to synthesize rootfs files: offline placeholders or LLM-generated realistic files.")
    add_asset_args(parser)
    parser.add_argument("--reasoning-effort", choices=["minimal", "low", "medium", "high"], default="medium", help="Reasoning effort for LLM asset generation.")
    parser.add_argument("--reuse-assets", action="store_true", help="Reuse the probe stage's already-synthesized rootfs and web assets for this case instead of regenerating (skips the asset LLM call).")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args(argv)
    check_asset_args(parser, args, synthesizes=not args.reuse_assets)

    case = load_case(args.case_file, args.case_id)
    env = load_env(args.env_file)
    check_provider_credentials(env, args.model_provider)
    run_dir = args.runs_dir / f"{case.case_id}__{args.claw}__{args.test_mode}"

    print(f"[run] case={case.case_id} claw={args.claw} mode={args.test_mode}")
    print(f"[run] task: {case.benign_user_goal}")
    adapter = get_adapter(args.claw, model=args.model, model_provider=args.model_provider, env=env)
    print("[run] initializing claw runtime ...")
    adapter.initialize(force=args.build)

    report = execute_case(adapter, case, test_mode=args.test_mode, timeout=args.timeout, run_dir=run_dir, keep=args.keep, web_assets=args.web_assets, file_assets=args.file_assets, asset_model=args.asset_model, asset_provider=args.asset_model_provider, reasoning_effort=args.reasoning_effort, reuse_assets=args.reuse_assets)

    verdict_key = "inject_succeeded" if args.test_mode == "inject" else "trigger_succeeded"
    print("\n=== RESULT ===")
    print(f"  benign_completed : {report['benign_completed']}")
    print(f"  safe_ok          : {report['safe_ok']}")
    print(f"  {verdict_key:<16} : {report.get(verdict_key)}")
    for group, rows in report["check_results"].items():
        for r in rows:
            print(f"    [{group}] {r['check_id']} {r['label']} -> {'PASS' if r['passed'] else 'fail'}")
    print(f"\nartifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
