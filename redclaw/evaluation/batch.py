"""Batch-run a claw over a corpus of RedClaw cases and aggregate results.

Honest coverage: each case is preflighted with `classify_case`. Cases whose
delivery surface this runner doesn't reproduce (web pages, clipboard, email) or
whose attack oracle we can't observe for a real claw are SKIPPED with a reason —
never silently scored. Runnable cases are executed on the claw and
aggregated by risk_category x attack_strategy x dimension.

    # see what's runnable vs skipped, no docker/API:
    python -m redclaw evaluate --model openai/gpt-4o-mini --model-provider openrouter \
        --case-dir dataset/inject/agents_md/cases --dry-run

    # run the runnable subset on nanobot:
    python -m redclaw evaluate --claw nanobot --model openai/gpt-4o-mini \
        --model-provider openrouter --case-dir data/generated --limit 12
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


from redclaw.evaluation.core import CaseSpec, classify_case
from redclaw.adapters import get_adapter
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
)

from redclaw.paths import PROJECT_ROOT
DEFAULT_CASE_DIRS = [PROJECT_ROOT / "data" / "generated"]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs" / "collection"


def discover_cases(dirs: list[Path]) -> list[CaseSpec]:
    cases: list[CaseSpec] = []
    seen: set[str] = set()
    for d in dirs:
        if not d.exists():
            continue
        for jf in sorted(d.glob("*.json")):
            # An unparsable case file is a hard error, so a batch never silently
            # evaluates fewer cases than it reports.
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"{jf}: unreadable case file ({exc})") from exc
            for rec in (data if isinstance(data, list) else [data]):
                if isinstance(rec, dict) and rec.get("id") and rec["id"] not in seen:
                    seen.add(rec["id"])
                    cases.append(CaseSpec(rec))
    return cases


def _pct(num: int, den: int) -> str:
    return f"{(100 * num / den):.0f}%" if den else "-"


def _bucket() -> dict:
    return {"n": 0, "error": 0, "attack_success": 0, "task_success": 0,
            "attack_and_task": 0, "attack_only": 0, "task_only": 0, "neither": 0}


def _finalize(b: dict) -> dict:
    """Add rates over the evaluated cases (executed minus errors)."""
    evaluated = b["n"] - b["error"]
    b["evaluated"] = evaluated
    b["attack_success_rate"] = round(b["attack_success"] / evaluated, 4) if evaluated else None
    b["task_success_rate"] = round(b["task_success"] / evaluated, 4) if evaluated else None
    return b


def aggregate(rows: list[dict]) -> dict:
    """Attack success and task success, counted independently per case.

    Errored cases (agent/runtime failure) are counted separately and excluded
    from both rates. The four joint counts split the evaluated cases by
    (attack succeeded, task succeeded).
    """
    overall = _bucket()
    by_dim: dict[str, dict] = defaultdict(_bucket)
    by_risk: dict[str, dict] = defaultdict(_bucket)
    by_strategy: dict[str, dict] = defaultdict(_bucket)
    for r in rows:
        for b in (overall, by_dim[r["dimension"]], by_risk[r["risk_category"]], by_strategy[r["attack_strategy"]]):
            b["n"] += 1
            if r.get("execution_error"):
                b["error"] += 1
                continue
            attack, task = bool(r["attack_succeeded"]), bool(r["benign_completed"])
            b["attack_success"] += attack
            b["task_success"] += task
            joint = ("attack_and_task" if attack and task else "attack_only" if attack
                     else "task_only" if task else "neither")
            b[joint] += 1
    return {
        "overall": _finalize(overall),
        "by_dimension": {k: _finalize(v) for k, v in by_dim.items()},
        "by_risk_category": {k: _finalize(v) for k, v in by_risk.items()},
        "by_attack_strategy": {k: _finalize(v) for k, v in by_strategy.items()},
    }


def _rate(num: int, den: int) -> str:
    return f"{_pct(num, den)} ({num}/{den})" if den else "-"


def _print_bucket_table(title: str, buckets: dict[str, dict]) -> None:
    print(f"\n{title}")
    print(f"  {'key':<40} {'n':>3} {'errors':>6} {'attack success':>16} {'task success':>16}")
    for key, b in sorted(buckets.items()):
        print(f"  {key[:40]:<40} {b['n']:>3} {b['error']:>6} "
              f"{_rate(b['attack_success'], b['evaluated']):>16} {_rate(b['task_success'], b['evaluated']):>16}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch-run a claw over the RedClaw corpus and aggregate.")
    parser.add_argument("--claw", default="nanobot")
    add_model_args(parser, role="Claw (agent under test)")
    parser.add_argument("--case-dir", action="append", type=Path, default=None, help="Case dir(s); repeatable. Defaults to data/generated/.")
    parser.add_argument("--risk-category", default=None, help="Filter by risk_category substring.")
    parser.add_argument("--strategy", default=None, help="Filter by attack_strategy substring.")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of runnable cases executed.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--web-assets", choices=["offline", "llm"], default="offline", help="Served web page synthesis: offline template or LLM (realistic).")
    parser.add_argument("--file-assets", choices=["offline", "llm"], default="offline", help="Rootfs file synthesis: offline placeholders or LLM-generated realistic files.")
    add_asset_args(parser)
    parser.add_argument("--reasoning-effort", choices=["minimal", "low", "medium", "high"], default="medium", help="Reasoning effort for LLM asset generation.")
    parser.add_argument("--no-reuse-assets", action="store_true", help="Materialize every environment asset fresh instead of reusing the probe stage's render. Reuse is the default and pins the WHOLE environment (rootfs and web alike): each asset is taken from the sibling probe_model=*/ render, and a miss aborts the case rather than silently materializing a different one. Keep reuse on to compare models/harnesses against a byte-identical environment; turn it off to (re)materialize.")
    parser.add_argument("--dry-run", action="store_true", help="Only classify (no docker/API); print runnable vs skipped.")
    parser.add_argument("--force-dimension", choices=["inject", "trigger"], default=None, help="Override the test dimension for all cases regardless of test_mode field.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel Docker containers to run simultaneously.")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args(argv)
    if not args.dry_run:
        check_asset_args(parser, args, synthesizes=args.no_reuse_assets)

    dirs = args.case_dir or DEFAULT_CASE_DIRS
    cases = discover_cases(dirs)
    if args.risk_category:
        cases = [c for c in cases if args.risk_category.lower() in c.risk_category.lower()]
    if args.strategy:
        cases = [c for c in cases if args.strategy.lower() in c.attack_strategy.lower()]

    runnable: list[tuple[CaseSpec, dict]] = []
    skipped: list[tuple[CaseSpec, dict]] = []
    for c in cases:
        cls = classify_case(c)
        if args.force_dimension:
            cls["dimension"] = args.force_dimension
        (runnable if cls["runnable"] else skipped).append((c, cls))

    print(f"[batch] discovered {len(cases)} cases | runnable {len(runnable)} | skipped {len(skipped)}")
    skip_reasons: dict[str, int] = defaultdict(int)
    for _, cls in skipped:
        # collapse list-bearing reasons to their prefix for a clean histogram
        skip_reasons[cls["reason"].split(":")[0]] += 1
    if skipped:
        print("[batch] skip reasons:")
        for reason, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>3}  {reason}")
    dim_counts: dict[str, int] = defaultdict(int)
    for _, cls in runnable:
        dim_counts[cls["dimension"]] += 1
    print("[batch] runnable by dimension:", dict(dim_counts))

    if args.dry_run:
        print("\n[batch] --dry-run: classification only. Runnable cases:")
        for c, cls in runnable:
            print(f"    [{cls['dimension']}] {c.case_id}  (risk={c.risk_category}, strat={c.attack_strategy}, sink={cls['needs_sink']})")
        return 0

    to_run = runnable[: args.limit] if args.limit else runnable
    env = load_env(args.env_file)
    check_provider_credentials(env, args.model_provider)
    adapter = get_adapter(args.claw, model=args.model, model_provider=args.model_provider, env=env)
    print(f"[batch] initializing runtime for claw={args.claw} ...")
    adapter.initialize()

    def _run_one(idx_case_cls: tuple[int, CaseSpec, dict]) -> dict:
        i, case, cls = idx_case_cls
        dim = cls["dimension"]
        run_dir = case_run_dir(args.runs_dir, "eval_model", args.model, case)
        reset_case_dir(run_dir)

        def finish(row: dict) -> dict:
            write_case_metadata(
                run_dir,
                case=case,
                stage="eval",
                model_role="eval_model",
                model=args.model,
                claw=args.claw,
                dimension=dim,
                surface=args.runs_dir.name,
                summary_row=row,
            )
            return row

        print(f"[batch] ({i}/{len(to_run)}) {case.case_id} [{dim}] ...", flush=True)
        try:
            rep = execute_case(adapter, case, test_mode=dim, timeout=args.timeout, run_dir=run_dir, verbose=False, web_assets=args.web_assets, file_assets=args.file_assets, asset_model=args.asset_model, asset_provider=args.asset_model_provider, reasoning_effort=args.reasoning_effort, reuse_assets=not args.no_reuse_assets)
            row = {
                "case_id": case.case_id, "dimension": dim,
                "risk_category": case.risk_category, "attack_strategy": case.attack_strategy,
                "benign_completed": bool(rep["benign_completed"]), "safe_ok": bool(rep["safe_ok"]),
                "attack_succeeded": bool(rep["attack_succeeded"]), "defended": bool(rep["defended"]),
                "returncode": rep["run"]["returncode"], "timed_out": rep["run"]["timed_out"],
                "execution_error": bool(
                    rep["run"]["returncode"] != 0 or rep["run"]["timed_out"]
                ),
            }
            if row["execution_error"]:
                row["error"] = (
                    "agent timed out" if row["timed_out"]
                    else f"agent exited with return code {row['returncode']}"
                )
        except Exception as exc:
            try:
                (run_dir / "run_error.json").write_text(
                    json.dumps(
                        {
                            "case_id": case.case_id,
                            "dimension": dim,
                            "risk_category": case.risk_category,
                            "attack_strategy": case.attack_strategy,
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
            row = {"case_id": case.case_id, "dimension": dim, "risk_category": case.risk_category,
                   "attack_strategy": case.attack_strategy, "benign_completed": False, "safe_ok": False,
                   "attack_succeeded": False, "defended": False, "execution_error": True,
                   "error": str(exc)[:300]}
        if row.get("execution_error"):
            print(f"        -> ERROR  ({row.get('error', '')})", flush=True)
        else:
            print(f"        -> attack={'SUCCESS' if row['attack_succeeded'] else 'failed'}  "
                  f"task={'SUCCESS' if row['benign_completed'] else 'failed'}", flush=True)
        return finish(row)

    rows: list[dict] = []
    jobs = [(i, case, cls) for i, (case, cls) in enumerate(to_run, 1)]
    if args.workers <= 1:
        for job in jobs:
            rows.append(_run_one(job))
    else:
        print(f"[batch] running {len(jobs)} cases with {args.workers} parallel workers", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_run_one, job): job for job in jobs}
            for fut in as_completed(futures):
                rows.append(fut.result())

    agg = aggregate(rows)
    out = {
        "claw": args.claw, "model": args.model,
        "counts": {"discovered": len(cases), "runnable": len(runnable), "skipped": len(skipped), "executed": len(rows)},
        "skip_reasons": dict(skip_reasons),
        "aggregate": agg,
        "rows": rows,
        "skipped": [{"case_id": c.case_id, "reason": cls["reason"]} for c, cls in skipped],
    }
    report_path = stage_report_path(args.runs_dir, "eval_model", args.model, "batch_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    o = agg["overall"]
    print("\n================ BATCH SUMMARY ================")
    print(f"executed {o['n']} | evaluated {o['evaluated']} | errors {o['error']}")
    print(f"attack success rate: {_rate(o['attack_success'], o['evaluated'])}")
    print(f"task success rate:   {_rate(o['task_success'], o['evaluated'])}")
    print(f"  attack+task {o['attack_and_task']} | attack only {o['attack_only']} | "
          f"task only {o['task_only']} | neither {o['neither']}")
    print("  rates exclude errors (agent/runtime failures)")
    _print_bucket_table("by dimension:", agg["by_dimension"])
    _print_bucket_table("by risk_category:", agg["by_risk_category"])
    _print_bucket_table("by attack_strategy:", agg["by_attack_strategy"])
    if o["attack_success"]:
        print("\nATTACK SUCCEEDED:")
        for r in rows:
            if r["attack_succeeded"] and not r.get("execution_error"):
                task = "task success" if r["benign_completed"] else "task failed"
                print(f"    {r['case_id']} [{r['dimension']}] -> {task}")
    print(f"\nbatch report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
