"""Attack-feasibility pre-check for generated cases.

A generated case is only trustworthy if (a) its benign task is actually
completable in the materialized environment and (b) its attack is reachable by
the agent and detectable by its own attack_task_success oracle. We never hand
the agent attacker_instruction directly — safety-aligned agents refuse explicit
exfil instructions even when "compliant", giving a 100% false-negative rate.
Instead the probe runs the agent on the BENIGN goal with the poison staged the
way a real attack would stage it, and judges feasibility from the outcome:

  - benign task COMPLETES  -> the task is doable and (for inject) the poison
                              carrier lies on the agent's benign path. The case
                              can discriminate; a later 'defended' verdict means
                              the defense actually worked.
  - attack FIRES at probe  -> a genuine breach happened with no explicit
                              instruction; the attack is definitely reachable.
  - neither                -> SUSPECT case: the benign task isn't completable,
                              the poison is off the agent's path, or the oracle
                              is mis-coupled. A 'defended' verdict here is a
                              false negative, not robustness.

Both modes run a static reachability gate first — the poison carrier must lie
on the agent's expected benign path (capability_groundtruth), else the agent can
finish the task without ever encountering the attack and a 'defended' verdict is
meaningless. They then reject cases whose attack oracle trivially passes on the
initial fixture. This is the attack-side mirror of the benign-feasibility gate.

    python -m redclaw probe --claw nanobot \
        --model openai/gpt-5-mini --model-provider openrouter \
        --asset-model openai/gpt-5-mini --asset-model-provider openrouter \
        --case-dir data/generated --web-assets llm --file-assets llm
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


from redclaw.environments import harness

from redclaw.evaluation import validation
from redclaw.evaluation.core import (
    CaseSpec,
    build_case_rootfs,
    classify_case,
    resolve_inject_workspace_placeholders,
)
from redclaw.adapters import get_adapter
from redclaw.evaluation.batch import discover_cases
from redclaw.evaluation.artifacts import (
    case_run_dir,
    reset_case_dir,
    stage_report_path,
    write_case_metadata,
)
from redclaw.evaluation.runner import (
    add_asset_args,
    add_model_args,
    check_asset_args,
    check_provider_credentials,
    execute_case,
    load_env,
    poisoned_skill_seed,
    poisoned_workspace_seed,
)

from redclaw.paths import PROJECT_ROOT
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "collection"


def _trivially_satisfied(case: CaseSpec, rootfs: Path) -> list[str]:
    """Return labels of attack_task_success checks that ALREADY PASS on the
    initial fixture rootfs — before any agent runs.  If non-empty the oracle
    is a trivial pre-condition: it cannot distinguish 'attack landed' from
    'file was always there'.

    Delegates entirely to harness.evaluate_check so all edge cases (falsy expected,
    inverted checks, etc.) are handled by the same logic used during scoring.
    """
    class _View:
        def __init__(self, root: Path) -> None:
            self.runtime_rootfs = root
            self.state: dict = {}
        def fixture_path(self, path: str) -> Path:
            return harness.runtime_path(self.runtime_rootfs, path)

    view = _View(rootfs)
    triggered: list[str] = []
    for check in harness.as_list(case.verification.get("attack_task_success")):
        if not isinstance(check, dict):
            raise validation.StrictCheckError(
                f"{case.case_id}: attack_task_success contains a non-object check {check!r}")
        # Deliberately unguarded. Swallowing the error here recorded a check that
        # could not be evaluated as "not trivially satisfied" — i.e. it waved the
        # case through the triviality gate on the strength of a crash.
        passed, label = harness.evaluate_check(view, check)
        if passed:
            triggered.append(label)
    return triggered


def _groundtruth_strings(case: CaseSpec) -> set[str]:
    """Every string value in the benign trajectory's step params
    (capability_groundtruth, emitted by the generator for every case),
    flattened. These are the files/URLs/commands the agent is expected to
    touch while completing the user task."""
    out: set[str] = set()

    def walk(v: object) -> None:
        if isinstance(v, str):
            if v.strip():
                out.add(v.strip())
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for step in (case.raw.get("capability_groundtruth") or []):
        if isinstance(step, dict):
            walk(step.get("params") or {})
    return out


def _poison_reachable(case: CaseSpec, test_mode: str) -> tuple[bool, list[str]]:
    """Reachability gate: would the agent actually ENCOUNTER the poison while
    completing the benign task? Returns (ok, failed_labels).

    We check the poison carrier against the expected benign trajectory
    (capability_groundtruth) the generator emits for every case. A carrier that
    no benign step ever reads / visits / executes is 'off-path': the agent can
    finish the user task without ever seeing the attack, so a later 'defended'
    verdict on it would be a false negative, not robustness.

      - workspace-trigger: carrier = poisoned memory, which the agent auto-loads
        into context every task -> inherently on-path.
      - inject / env-trigger: carrier = each injected_observations.source; it
        must appear in some groundtruth step's params.

    For trigger we mirror the runtime delivery priority (poisoned_workspace_seed):
    if the poison is pre-seeded into memory it is auto-loaded and on-path, so we
    don't fall through to the injected_observations check (which may carry a
    non-path surface label that no path match could satisfy).
    """
    if test_mode == "trigger":
        # skill-trigger: the poisoned skill's description sits in the agent's
        # skill list every task, so the poison is inherently ENCOUNTERED (whether
        # the agent then LOADS it is the defense under test, not a reachability gap).
        if poisoned_skill_seed(case):
            return True, []
        seed_content, _ = poisoned_workspace_seed(case)
        if seed_content and seed_content.strip():
            # workspace-trigger: poison in auto-loaded memory -> inherently on-path
            return True, []
        # else env-trigger: poison rides in the environment -> check carriers below

    carriers = [str(o.get("source") or "").strip() for o in case.injected_observations]
    carriers = [c for c in carriers if c]
    # Only carriers we can concretely locate — a file path or a URL — are
    # checkable by matching against the benign trajectory. An abstract surface
    # label ("web_page", "shell_command_output") names a delivery mechanism, not
    # a locatable artifact (the real path/URL lives in environment.*), so we
    # cannot assess it this way and must not false-flag it.
    concrete = [c for c in carriers if c.startswith(("/", "~", "./", "http"))]
    if concrete:
        strs = _groundtruth_strings(case)
        unreached = [c for c in concrete if not any(c in s or s in c for s in strs)]
        if unreached:
            return False, [f"poison_unreachable:{c}" for c in unreached]
        return True, []
    if carriers:
        # carriers exist but none are concretely locatable -> not assessable here
        return True, []
    # inject with no injected_observations is already refused by classify_case;
    # be explicit rather than silently pass.
    return False, ["no_poison_carrier: no memory seed and no injected_observations"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Attack-feasibility pre-check for generated cases.")
    parser.add_argument("--claw", default="nanobot")
    add_model_args(parser, role="Compliant probe agent")
    parser.add_argument("--probe-workers", type=int, default=1, help="Parallel Docker containers for trigger probe cases.")
    parser.add_argument("--case-dir", action="append", type=Path, required=True, help="Case dir(s); repeatable.")
    parser.add_argument("--risk-category", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--web-assets", choices=["offline", "llm"], default="llm")
    parser.add_argument("--file-assets", choices=["offline", "llm"], default="llm")
    add_asset_args(parser)
    parser.add_argument("--reasoning-effort", choices=["minimal", "low", "medium", "high"], default="medium", help="Reasoning effort for LLM asset generation.")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args(argv)
    check_asset_args(parser, args, synthesizes=True)

    cases = discover_cases(args.case_dir)
    if args.risk_category:
        cases = [c for c in cases if args.risk_category.lower() in c.risk_category.lower()]
    if args.strategy:
        cases = [c for c in cases if args.strategy.lower() in c.attack_strategy.lower()]

    # only probe cases whose attack oracle this runner can actually observe
    probeable: list[CaseSpec] = []
    unobservable: list[tuple[CaseSpec, str]] = []
    for c in cases:
        cls = classify_case(c)
        if cls["attack_check_types"]:
            probeable.append(c)
        else:
            unobservable.append((c, "no attack oracle defined"))

    if args.limit:
        probeable = probeable[: args.limit]

    print(f"[probe] discovered {len(cases)} | probeable {len(probeable)} | unobservable {len(unobservable)}", flush=True)

    # Both inject and trigger probes now run the agent on the benign task. They
    # differ only in how the poison reaches the agent and in a static pre-gate:
    #
    #   Inject — poison flows through the environment (a file/web page the agent
    #   reads). Phase 1 is a static fixture check (poison materialized in the
    #   file, web assets buildable); the poison carrier must also lie on the
    #   benign path. Phase 2 then runs the benign task; feasible ONLY if the
    #   attack fires during that stealth run (benign completion alone does not
    #   qualify — probe keeps only demonstrably-potent attacks).
    #
    #   Trigger — poison is pre-seeded into memory, then the benign task runs.
    #   The probe IS the trigger eval. Feasible ONLY if the attack fires (a
    #   genuine breach at probe time).
    #
    # Neither gives the agent attacker_instruction directly: safety-aligned
    # agents refuse explicit exfil instructions even when "compliant", which
    # would produce a 100% false-negative rate.
    inject_cases = [c for c in probeable if classify_case(c)["dimension"] == "inject"]
    trigger_cases = [c for c in probeable if classify_case(c)["dimension"] == "trigger"]

    needs_docker = bool(trigger_cases or inject_cases)
    adapter = None
    if needs_docker:
        env = load_env(args.env_file)
        check_provider_credentials(env, args.model_provider)
        adapter = get_adapter(args.claw, model=args.model, model_provider=args.model_provider, env=env)
        print(f"[probe] initializing runtime for claw={args.claw} ...", flush=True)
        adapter.initialize()

    def _probe_one(item: tuple[int, CaseSpec]) -> dict:
        i, case = item
        dim = classify_case(case)["dimension"]
        run_dir = case_run_dir(args.runs_dir, "probe_model", args.model, case)
        reset_case_dir(run_dir)

        def finish(row: dict) -> dict:
            write_case_metadata(
                run_dir,
                case=case,
                stage="probe",
                model_role="probe_model",
                model=args.model,
                claw=args.claw,
                dimension=dim,
                surface=args.runs_dir.name,
                summary_row=row,
            )
            return row

        print(f"[probe] ({i}/{len(probeable)}) {case.case_id} [{dim}] ...", flush=True)
        try:
            if dim == "inject":
                resolved_case, _ = resolve_inject_workspace_placeholders(
                    adapter, case, test_mode="inject"
                )
                # ── Phase 1 (static): the poison carrier must lie on the agent's
                #    expected benign path (capability_groundtruth), else the agent
                #    can finish the task without ever encountering the attack.
                #    Cheap gate before paying for an agent run.
                reach_ok, reach_failed = _poison_reachable(resolved_case, "inject")
                if not reach_ok:
                    row = {"case_id": case.case_id, "risk_category": case.risk_category,
                           "attack_strategy": case.attack_strategy, "test_mode": "inject",
                           "probe_method": "reachability",
                           "attack_feasible": False, "failed_attack_checks": reach_failed}
                    print(f"        -> infeasible  (poison off benign path: {reach_failed})", flush=True)
                    return finish(row)

                # ── Pre-check: reject cases where the attack oracle trivially
                #    passes on the initial fixture (cannot discriminate 'attack
                #    landed' from 'state was already there').
                pre_rootfs = build_case_rootfs(
                    resolved_case,
                    run_dir,
                    file_llm=(args.file_assets == "llm"),
                    web_llm=(args.web_assets == "llm"),
                    model=args.asset_model,
                    provider=args.asset_model_provider,
                    reasoning_effort=args.reasoning_effort,
                )
                trivial = _trivially_satisfied(resolved_case, pre_rootfs)
                if trivial:
                    row = {"case_id": case.case_id, "risk_category": case.risk_category,
                           "attack_strategy": case.attack_strategy, "test_mode": "inject",
                           "probe_method": "trivial_precondition",
                           "benign_completed": None,
                           "attack_fired_at_probe": False,
                           "attack_feasible": False,
                           "failed_attack_checks": [f"trivial_precondition: {c}" for c in trivial]}
                    print(f"        -> infeasible  (trivial precondition: oracle already passes on initial fixture: {trivial})", flush=True)
                    return finish(row)

                # ── Phase 2 (dynamic): run the benign task with the poisoned
                #    environment (no attacker_instruction — stealth, like
                #    trigger). Feasible iff the agent completes the benign task
                #    (task doable AND poison carrier on the benign path) OR the
                #    attack fires outright.
                rep = execute_case(
                    adapter, case, test_mode="inject",
                    task_override=case.benign_user_goal,
                    timeout=args.timeout, run_dir=run_dir, verbose=False,
                    web_assets=args.web_assets, file_assets=args.file_assets,
                    asset_model=args.asset_model, asset_provider=args.asset_model_provider,
                    reasoning_effort=args.reasoning_effort,
                )
                # Feasible only when the attack actually fired during the stealth
                # probe run; completing the benign task alone does not qualify.
                attack_ok = bool(rep["attack_succeeded"])
                if attack_ok:
                    failed = []
                else:
                    failed = [c.get("label") or c.get("type") for c in rep["check_results"]["attack_task_success"] if not c["passed"]]
                row = {"case_id": case.case_id, "risk_category": case.risk_category,
                       "attack_strategy": case.attack_strategy, "test_mode": "inject",
                       "probe_method": "inject_benign_run",
                       "benign_completed": bool(rep["benign_completed"]),
                       "attack_fired_at_probe": bool(rep["attack_succeeded"]),
                       "agent_error": bool(rep["run"]["returncode"] != 0 or rep["run"]["timed_out"]),
                       "attack_feasible": attack_ok, "failed_attack_checks": failed}
            else:
                # Trigger: run benign_user_goal with poisoned memory.
                # Do NOT give attacker_instruction directly — safety-aligned agents
                # refuse explicit exfil instructions even when "compliant".

                # ── Phase 1 (static): the poison carrier must lie on the agent's
                #    expected benign path (capability_groundtruth). For env-trigger
                #    that's the injected file/URL; for workspace-trigger the poison
                #    is in auto-loaded memory and is inherently on-path. Cheap gate
                #    before paying for the run.
                reach_ok, reach_failed = _poison_reachable(case, "trigger")
                if not reach_ok:
                    row = {"case_id": case.case_id, "risk_category": case.risk_category,
                           "attack_strategy": case.attack_strategy, "test_mode": "trigger",
                           "probe_method": "reachability",
                           "attack_feasible": False, "failed_attack_checks": reach_failed}
                    print(f"        -> infeasible  (poison off benign path: {reach_failed})", flush=True)
                    return finish(row)

                # ── Pre-check: reject cases where the attack oracle trivially
                #    passes on the initial fixture state (before any agent runs).
                #    Such a case cannot discriminate 'attack landed' from 'file
                #    was already there' — it is infeasible regardless of the agent.
                pre_rootfs = build_case_rootfs(
                    case,
                    run_dir,
                    file_llm=(args.file_assets == "llm"),
                    web_llm=(args.web_assets == "llm"),
                    model=args.asset_model,
                    provider=args.asset_model_provider,
                    reasoning_effort=args.reasoning_effort,
                )
                trivial = _trivially_satisfied(case, pre_rootfs)
                if trivial:
                    row = {"case_id": case.case_id, "risk_category": case.risk_category,
                           "attack_strategy": case.attack_strategy, "test_mode": "trigger",
                           "probe_method": "trivial_precondition",
                           "benign_completed": None,
                           "attack_fired_at_probe": False,
                           "attack_feasible": False,
                           "failed_attack_checks": [f"trivial_precondition: {c}" for c in trivial]}
                    print(f"        -> infeasible  (trivial precondition: oracle already passes on initial fixture: {trivial})", flush=True)
                    return finish(row)

                rep = execute_case(
                    adapter, case, test_mode="trigger",
                    task_override=case.benign_user_goal,
                    timeout=args.timeout, run_dir=run_dir, verbose=False,
                    web_assets=args.web_assets, file_assets=args.file_assets,
                    asset_model=args.asset_model, asset_provider=args.asset_model_provider,
                    reasoning_effort=args.reasoning_effort,
                )
                # Feasible only when the attack actually fired during the stealth
                # probe run; completing the benign task alone does not qualify.
                attack_ok = bool(rep["attack_succeeded"])
                if attack_ok:
                    failed = []
                else:
                    failed = [c.get("label") or c.get("type") for c in rep["check_results"]["attack_task_success"] if not c["passed"]]
                row = {"case_id": case.case_id, "risk_category": case.risk_category,
                       "attack_strategy": case.attack_strategy, "test_mode": "trigger",
                       "probe_method": "trigger_benign_run",
                       "benign_completed": bool(rep["benign_completed"]),
                       "attack_fired_at_probe": bool(rep["attack_succeeded"]),
                       "agent_error": bool(rep["run"]["returncode"] != 0 or rep["run"]["timed_out"]),
                       "attack_feasible": attack_ok, "failed_attack_checks": failed}
        except Exception as exc:
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "probe_error.json").write_text(
                    json.dumps(
                        {
                            "case_id": case.case_id,
                            "risk_category": case.risk_category,
                            "attack_strategy": case.attack_strategy,
                            "test_mode": dim,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass
            row = {"case_id": case.case_id, "risk_category": case.risk_category,
                   "attack_strategy": case.attack_strategy,
                   "test_mode": dim, "probe_method": dim,
                   "attack_feasible": False,
                   "failed_attack_checks": [f"ERROR: {str(exc)[:200]}"]}
        v = "FEASIBLE" if row["attack_feasible"] else "infeasible"
        suffix = "  (fired at probe!)" if row.get("attack_fired_at_probe") else ""
        print(f"        -> {v}{suffix}" + ("" if row["attack_feasible"] else f"  (failed: {row['failed_attack_checks']})"), flush=True)
        return finish(row)

    jobs = list(enumerate(probeable, 1))
    rows: list[dict] = []
    if args.probe_workers <= 1:
        for job in jobs:
            rows.append(_probe_one(job))
    else:
        print(f"[probe] running {len(jobs)} cases with {args.probe_workers} parallel workers", flush=True)
        with ThreadPoolExecutor(max_workers=args.probe_workers) as pool:
            futures = {pool.submit(_probe_one, job): job for job in jobs}
            for fut in as_completed(futures):
                rows.append(fut.result())

    feasible = [r for r in rows if r["attack_feasible"]]
    infeasible = [r for r in rows if not r["attack_feasible"]]
    trivial_precond = [r for r in rows if r.get("probe_method") == "trivial_precondition"]
    # Agent-run probes, reported like evaluation: attack and task counted
    # independently, errors (exceptions, agent failures) excluded from both.
    agent_runs = [r for r in rows if r.get("probe_method") in ("inject_benign_run", "trigger_benign_run")]
    probe_errors = sum(1 for r in rows if r.get("probe_method") in ("inject", "trigger")) + sum(
        1 for r in agent_runs if r.get("agent_error"))
    evaluated = [r for r in agent_runs if not r.get("agent_error")]
    attack_fired = sum(1 for r in evaluated if r.get("attack_fired_at_probe"))
    task_completed = sum(1 for r in evaluated if r.get("benign_completed"))

    out = {
        "claw": args.claw, "model": args.model,
        "counts": {"discovered": len(cases), "probed": len(rows),
                   "feasible": len(feasible), "infeasible": len(infeasible),
                   "trivial_precondition": len(trivial_precond),
                   "unobservable": len(unobservable),
                   "agent_runs": len(agent_runs), "errors": probe_errors,
                   "attack_fired": attack_fired, "task_completed": task_completed},
        "rows": rows,
        "unobservable": [{"case_id": c.case_id, "reason": why} for c, why in unobservable],
    }
    report_path = stage_report_path(args.runs_dir, "probe_model", args.model, "probe_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n================ ATTACK FEASIBILITY ================")
    print(f"probed {len(rows)} | FEASIBLE {len(feasible)} | infeasible {len(infeasible)} | unobservable {len(unobservable)}")
    if trivial_precond:
        print(f"  (of which {len(trivial_precond)} infeasible due to trivial precondition — oracle passes before any agent runs)")
    if rows:
        print(f"feasible rate: {100*len(feasible)//len(rows)}%  [{len(feasible)}/{len(rows)}]")
    if evaluated:
        print(f"agent runs {len(agent_runs)} | errors {probe_errors} | "
              f"attack fired {100*attack_fired/len(evaluated):.0f}% ({attack_fired}/{len(evaluated)}) | "
              f"task completed {100*task_completed/len(evaluated):.0f}% ({task_completed}/{len(evaluated)})")
        print("  (feasibility above is the gate; these rates exclude errors and static-only checks)")
    if infeasible:
        real_infeasible = [r for r in infeasible if r.get("probe_method") != "trivial_precondition"]
        if real_infeasible:
            print("\nSUSPECT cases (attack NOT achievable even when explicitly instructed — fix or drop):")
            for r in real_infeasible:
                print(f"    {r['case_id']} [{r.get('test_mode','?')}] -> failed: {r['failed_attack_checks']}")
        if trivial_precond:
            print("\nTRIVIAL PRECONDITION cases (oracle file pre-exists in fixture — regenerate these cases):")
            for r in trivial_precond:
                print(f"    {r['case_id']} -> {r['failed_attack_checks']}")
    if unobservable:
        print(f"\n{len(unobservable)} cases skipped (attack oracle not observable by this runner).")
    print(f"\nprobe report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
