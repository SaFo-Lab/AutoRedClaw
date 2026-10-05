"""End-to-end RedClaw red-teaming pipeline (`python -m redclaw redteam`).

Stages (skip any with --skip):
  clean         remove this claw/surface's generated cases, run artifacts and image
  generate      generate inject and/or trigger cases per category x strategy
  initialize    build/verify the claw runtime; Codex and Claude Code also run a model preflight
  attack_probe  run each case once with --probe-model and quarantine infeasible ones
  eval          run the batch evaluator with --model
  report        print the combined summary

Every stage that runs needs its model and provider passed explicitly
(--gen-model*, --probe-model*, --model*, and --asset-model* when LLM assets are
synthesized). `--dataset` evaluates a frozen dataset instead of generating.

Case layout:
  data/<claw>/<dimension>/<surface>/<category>/<attack_strategy>.json
Run layout:
  runs/collection/<claw>/<dimension>/<surface>/{probe_model=...,eval_model=...}/<category>/<case>/
  runs/evaluation/<claw>/<dimension>/<surface>/...   (with --dataset)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from redclaw.paths import PROJECT_ROOT

from redclaw.paths import RESOURCES_DIR

TAXONOMY_DIR = RESOURCES_DIR / "taxonomy"
DATA_DIR = PROJECT_ROOT / "data"
RUNS_ROOT = PROJECT_ROOT / "runs"
RUNS_DIR = RUNS_ROOT / "collection"
COLLECTION_ROOT: Path | None = None
# Dataset evaluations write here instead of RUNS_DIR. RUNS_DIR holds the generated
# corpus and the runs the published ASR tables were computed from; a dataset sweep
# reuses the same <claw>/<dimension>/<surface> layout, so pointing it at RUNS_DIR
# would interleave 150 dataset cases with the corpus's per-claw case set and
# overwrite its batch_report.json.
DATASET_RUNS_DIR = PROJECT_ROOT / "runs" / "evaluation"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, cwd: Path = PROJECT_ROOT, check: bool = True, **kw) -> subprocess.CompletedProcess:
    print(f"[pipeline] $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, **kw)


from redclaw.datasets.assets import stage_surface as stage_frozen_assets


def _path_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "default"


def _model_report_path(runs_dir: Path, model_role: str, model: str, filename: str) -> Path:
    return runs_dir / f"{model_role}={_path_slug(model)}" / "_stage_reports" / filename


def _surface_slug(dimension: str, surface: str) -> str:
    """Directory bucket for one (dimension, surface) pair.

    The surface value means opposite ends of the attack in the two dimensions:
    for trigger it is where the poison is PLANTED, for inject it is where the
    agent is induced to WRITE. Inject used to collapse to a single `environment`
    bucket because the destination was whatever the generator felt like, which
    made a per-destination comparison impossible. `environment` stays the bucket
    for an unlocked inject run, so existing data keeps its path.
    """
    surface = str(surface or "any").strip().lower()
    if dimension == "inject":
        return "environment" if surface in ("", "any", "environment") else _path_slug(surface)
    return _path_slug(surface or "any")


def _case_namespace_dir(claw: str, dimension: str, surface: str) -> Path:
    return DATA_DIR / _path_slug(claw) / _path_slug(dimension) / _surface_slug(dimension, surface)


def _case_dir(claw: str, category: str, dimension: str, surface: str) -> Path:
    return _case_namespace_dir(claw, dimension, surface) / _path_slug(category)


def _case_file(claw: str, category: str, dimension: str, surface: str, strategy: str) -> Path:
    return _case_dir(claw, category, dimension, surface) / f"{_path_slug(strategy)}.json"


def _runs_namespace_dir(claw: str, dimension: str, surface: str, root: Path | None = None) -> Path:
    return (root or RUNS_DIR) / _path_slug(claw) / _path_slug(dimension) / _surface_slug(dimension, surface)


def _raw_runs_namespace_dir(claw: str, dimension: str, surface: str) -> Path:
    return (COLLECTION_ROOT or RUNS_ROOT) / "_raw_runner_layout" / "runs" / _path_slug(claw) / _path_slug(dimension) / _surface_slug(dimension, surface)


def _attack_strategy_types() -> list[str]:
    text = (TAXONOMY_DIR / "attack_strategy.md").read_text(encoding="utf-8")
    headings = re.findall(r"^##\s+(.+?)\s*:?\s*$", text, flags=re.MULTILINE)
    strategies: list[str] = []
    for heading in headings:
        strategy = heading.strip()
        if strategy and strategy not in strategies:
            strategies.append(strategy)
    return strategies


def _strategy_counts(total: int, strategies: list[str]) -> dict[str, int]:
    counts = {strategy: 0 for strategy in strategies}
    for idx in range(total):
        counts[strategies[idx % len(strategies)]] += 1
    return {strategy: count for strategy, count in counts.items() if count}


def _json_case_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, list):
        return sum(1 for item in data if isinstance(item, dict))
    return 1 if isinstance(data, dict) else 0


def _case_count_in_dir(path: Path) -> int:
    return sum(_json_case_count(p) for p in path.glob("*.json"))


def _is_skill_inject(dimension: str, surface: str) -> bool:
    return dimension == "inject" and _surface_slug(dimension, surface) == "skill_md"


def _is_workspace_inject(dimension: str, surface: str) -> bool:
    return dimension == "inject" and not _is_skill_inject(dimension, surface)


def _workspace_history_path(dim: str = "inject", surface: str = "any") -> Path:
    # Scoped per (dimension, surface): coverage and pattern guidance never cross
    # surfaces (agents.md is not steered by user.md) or dimensions.
    from redclaw.generation import experience

    return experience.scoped_store_path(DATA_DIR, dim, surface)


def _require_skill_inject_cases(dirs: list[Path], *, stage: str) -> None:
    # Skill collection must not turn a failed generation into a successful empty
    # probe/evaluation. Keep the other surfaces' existing behavior unchanged.
    from redclaw.evaluation.batch import discover_cases
    from redclaw.evaluation.core import classify_case

    cases = discover_cases(dirs)
    if not dirs or not cases:
        locations = ", ".join(str(path) for path in dirs) or "no case directories"
        raise RuntimeError(
            f"inject-skill {stage}: no cases discovered in {locations}. "
            "Generation may have failed or rejected every candidate; inspect the "
            "category's _generation_logs directory before running probe/eval."
        )
    unsupported = [(case.case_id, classify_case(case)) for case in cases]
    unsupported = [(case_id, result["reason"]) for case_id, result in unsupported if not result["runnable"]]
    if unsupported:
        raise RuntimeError(f"inject-skill {stage}: eval preflight rejected {len(unsupported)}/{len(cases)} cases: {unsupported[:5]}")


def _nonempty_skill_inject_dirs(dirs: list[Path], *, stage: str, require_any: bool = True) -> list[Path]:
    """Return skill-inject dirs with runnable cases, skipping empty partial-generation dirs."""
    from redclaw.evaluation.batch import discover_cases
    from redclaw.evaluation.core import classify_case

    unique_dirs = sorted(set(dirs))
    nonempty: list[Path] = []
    skipped: list[Path] = []
    unsupported: list[tuple[str, str]] = []
    for directory in unique_dirs:
        cases = discover_cases([directory])
        if not cases:
            skipped.append(directory)
            continue
        nonempty.append(directory)
        for case in cases:
            result = classify_case(case)
            if not result["runnable"]:
                unsupported.append((case.case_id, result["reason"]))
    if unsupported:
        raise RuntimeError(
            f"inject-skill {stage}: eval preflight rejected "
            f"{len(unsupported)} case(s): {unsupported[:5]}"
        )
    if skipped:
        print(
            f"[pipeline] inject-skill {stage}: skipping {len(skipped)} empty partial-generation dir(s)",
            flush=True,
        )
    if require_any and not nonempty:
        _require_skill_inject_cases(unique_dirs, stage=stage)
    return nonempty


def _case_dirs_for_category(claw: str, category: str, dimension: str, surface: str) -> list[Path]:
    path = _case_dir(claw, category, dimension, surface)
    if not path.exists() or _case_count_in_dir(path) <= 0:
        return []
    return [path]


def _dataset_dimension(dataset: Path) -> str:
    meta_path = dataset / "dataset.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"could not read dataset metadata {meta_path}: {exc}") from exc
    dimension = str(meta.get("dimension") or "").strip().lower()
    if dimension not in {"inject", "trigger"}:
        raise SystemExit(f"dataset {meta_path} has unsupported dimension {dimension!r}")
    return dimension


def _dataset_surface_cases_dir(dataset: Path, dimension: str, surface_slug: str) -> Path:
    candidates = [
        dataset / surface_slug / "cases",
        dataset / dimension / surface_slug / "cases",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


# ---------------------------------------------------------------------------
# stage 1: clean
# ---------------------------------------------------------------------------

def stage_clean(
    categories: list[str],
    keep_docker: bool,
    dimensions: list[str] | None = None,
    claw: str = "nanobot",
    surface: str = "any",
) -> None:
    dims = dimensions or ["inject", "trigger"]
    print(f"\n===== STAGE 1: CLEAN (claw={claw}, dimensions={dims}) =====", flush=True)

    # case output dirs
    for cat in categories:
        for dim in dims:
            d = _case_dir(claw, cat, dim, surface)
            if d.exists():
                shutil.rmtree(d)
                print(f"  rm -rf {d}")

    # run artifacts
    for dim in dims:
        d = _runs_namespace_dir(claw, dim, surface)
        if d.exists():
            shutil.rmtree(d)
            print(f"  rm -rf {d}")
        raw_d = _raw_runs_namespace_dir(claw, dim, surface)
        if raw_d.exists():
            shutil.rmtree(raw_d)
            print(f"  rm -rf {raw_d}")

    # docker image (per-claw: redclaw-claw-<claw>)
    if not keep_docker:
        image = f"redclaw-claw-{claw}"
        result = subprocess.run(
            ["docker", "images", "-q", image],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            subprocess.run(["docker", "rmi", image], check=False)
            print(f"  docker rmi {image}")
        else:
            print(f"  docker image {image} not present, skipping rmi")

    print("[pipeline] clean done", flush=True)


# ---------------------------------------------------------------------------
# stage 2: generate
# ---------------------------------------------------------------------------

INJECT_GENERATOR = "redclaw.generation.inject"
TRIGGER_GENERATOR = "redclaw.generation.trigger"


def _run_generate_job(claw: str, cat: str, dim: str, strategy: str,
                      num: int, gen_model: str | None,
                      reasoning_effort: str, os_profile: str,
                      surface: str = "any",
                      gen_model_provider: str | None = None,
                      generator: str = TRIGGER_GENERATOR,
                      persistence_mode: str | None = None,
                      max_attempts: int | None = None,
                      diversity_store: Path | None = None,
                      script_shape: str | None = None,
                      experience_summary: Path | None = None) -> tuple[str, str, Path, str]:
    """Run one generation subprocess. Returns (cat, dim, out_dir, log_text)."""
    out_dir = _case_dir(claw, cat, dim, surface)
    output = _case_file(claw, cat, dim, surface, strategy)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = _json_case_count(output)
    if existing >= num:
        return cat, dim, out_dir, f"[skip] {cat}/{dim}/{_path_slug(strategy)}: already has {existing}/{num} cases"

    cmd = [
        sys.executable, "-m", generator,
        "--risk-category", cat,
        "--strategy-type", strategy,
        "--num", str(num),
        "--output-dir", str(out_dir),
        "--output", str(output),
        "--reasoning-effort", reasoning_effort,
        "--batch-size", "5",
    ]
    if not gen_model or not gen_model_provider:
        raise ValueError("generation requires an explicit --gen-model and --gen-model-provider")
    cmd.extend(["--model", gen_model, "--gen-model-provider", gen_model_provider])
    if max_attempts is not None:
        cmd.extend(["--max-attempts", str(max_attempts)])
    if persistence_mode is not None:
        cmd.extend(["--persistence-mode", persistence_mode])
    if script_shape is not None:
        cmd.extend(["--script-shape", script_shape])
    if experience_summary is not None:
        cmd.extend(["--experience-summary", str(experience_summary)])
    if diversity_store is not None:
        cmd.extend(["--diversity-store", str(diversity_store)])
    if os_profile:
        cmd.extend(["--os-profile", os_profile])
    if surface and surface != "any":
        # Same value, opposite ends of the attack: trigger constrains where the
        # poison is planted, inject constrains where it must land.
        cmd.extend(["--inject-target" if dim == "inject" else "--trigger-surface", surface])
    if existing > 0:
        cmd.append("--resume")

    import subprocess as _sp
    skill_inject = _is_skill_inject(dim, surface)
    if skill_inject or _is_workspace_inject(dim, surface):
        log_dir = out_dir / "_generation_logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{_path_slug(strategy)}.log"
        # Persist output as it arrives so long or interrupted collections retain
        # the earliest rejects. Keep progress visible while parallel jobs run.
        with log_path.open("w", encoding="utf-8") as output_log:
            with _sp.Popen(cmd, cwd=PROJECT_ROOT, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                           text=True, bufsize=1) as proc:
                for line in proc.stdout:
                    output_log.write(line)
                    output_log.flush()
                    print(f"[generate {cat}/{_path_slug(strategy)}] {line}", end="", flush=True)
                returncode = proc.wait()
            n = _json_case_count(output)
            summary = f"\n[ERROR] exit {returncode}" if returncode else ""
            summary += f"\n  → {n} cases in {output}"
            output_log.write(summary)
        return cat, dim, out_dir, f"Generation log: {log_path}{summary}"

    proc = _sp.run(cmd, cwd=PROJECT_ROOT, stdout=_sp.PIPE, text=True)
    n = _json_case_count(output)
    log = proc.stdout or ""
    if proc.returncode != 0:
        log += f"\n[ERROR] exit {proc.returncode}"
    log += f"\n  → {n} cases in {output}"
    return cat, dim, out_dir, log


def stage_generate(
    claw: str,
    categories: list[str],
    num: int,
    gen_model: str | None,
    reasoning_effort: str,
    os_profile: str,
    workers: int = 4,
    dimensions: list[str] | None = None,
    surface: str = "any",
    gen_model_provider: str | None = None,
    inject_generator: str = INJECT_GENERATOR,
    trigger_generator: str = TRIGGER_GENERATOR,
    max_attempts: int | None = None,
) -> dict[str, dict[str, list[Path]]]:
    """Returns {category: {dimension: [category_output_dir]}}. Runs all jobs in parallel."""
    dims = dimensions or ["inject", "trigger"]
    strategies = _attack_strategy_types()
    if not strategies:
        raise RuntimeError("No attack strategies found in redclaw/resources/taxonomy/attack_strategy.md")
    strategy_counts = _strategy_counts(num, strategies)
    print(
        f"\n===== STAGE 2: GENERATE (claw={claw}, workers={workers}, dimensions={dims}, "
        f"strategy_files={len(strategy_counts)}) =====",
        flush=True,
    )
    outputs: dict[str, dict[str, list[Path]]] = {cat: {dim: [] for dim in dims} for cat in categories}
    from redclaw.generation import experience
    from redclaw.generation.diversity import DiversityStore

    diversity_store = None
    store = None
    if "inject" in dims and _is_workspace_inject("inject", surface) and inject_generator == INJECT_GENERATOR:
        diversity_store = _workspace_history_path("inject", surface)
        store = DiversityStore(diversity_store)
        # Seed from this (dim, surface) scope only — across claws (as before) but
        # NOT across surfaces, so agents.md is never guided by user.md samples.
        surface_slug = _surface_slug("inject", surface)
        for path in sorted(DATA_DIR.glob(f"*/inject/{surface_slug}/*/*.json")):
            if path.parent.name.startswith("_") or path.parents[1].name == "skill_md":
                continue
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for record in records if isinstance(records, list) else [records]:
                if isinstance(record, dict) and record.get("benign_user_goal") and record.get("injected_observations"):
                    store.claim(record, str(path.resolve()))

    experience_guided = experience.is_enabled() and store is not None
    n_rounds = experience.rounds() if experience_guided else 1
    summary_path = (diversity_store.parent / "experience_summary.json") if diversity_store else None

    for round_idx in range(1, n_rounds + 1):
        if experience_guided and round_idx > 1:
            # Refresh the experience summary from the grown pool before this round:
            # coverage profile (exact, code) + an LLM-distilled avoid-list (soft).
            coverage = experience.coverage_summary(store)
            avoidlist = experience.distill_avoidlist(
                store, model=gen_model, provider=gen_model_provider,
                reasoning_effort=reasoning_effort,
            )
            summary_path.write_text(
                experience.format_experience_block(coverage, avoidlist), encoding="utf-8")
            print(f"[experience] round {round_idx}: refreshed summary "
                  f"(pool={coverage['total']}, avoid_list={'yes' if avoidlist else 'no'})", flush=True)

        jobs = experience.plan_assignments(
            categories, dims, strategy_counts, surface,
            is_workspace_inject=_is_workspace_inject,
            inject_generator=inject_generator, trigger_generator=trigger_generator,
            INJECT_GENERATOR=INJECT_GENERATOR,
            store=store, experience_guided=experience_guided,
        )
        # Across rounds the per-strategy target grows so a rerun tops up rather
        # than skipping (the generator resumes from existing cases).
        pass_summary = summary_path if (experience_guided and round_idx > 1 and summary_path and summary_path.exists()) else None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_generate_job, claw, j["cat"], j["dim"], j["strategy"],
                            j["count"] * round_idx, gen_model, reasoning_effort,
                            os_profile, surface, gen_model_provider, j["generator"],
                            j["persistence_mode"], max_attempts,
                            diversity_store if j["dim"] == "inject" else None,
                            j["script_shape"], pass_summary): (j["cat"], j["dim"], j["strategy"], j["persistence_mode"], j["script_shape"])
                for j in jobs
            }
            for fut in as_completed(futures):
                cat, dim, strategy, persistence_mode, script_shape = futures[fut]
                _, _, out_dir, log = fut.result()
                mode_label = f" mode={persistence_mode}" if persistence_mode else ""
                if script_shape:
                    mode_label += f"/{script_shape}"
                round_label = f" round={round_idx}" if n_rounds > 1 else ""
                print(f"\n[generate {cat}/{dim}/{_path_slug(strategy)}{mode_label}{round_label}]\n{log}", flush=True)
                if out_dir not in outputs[cat][dim]:
                    outputs[cat][dim].append(out_dir)

    if diversity_store is not None:
        coverage = DiversityStore(diversity_store).context()
        coverage["experience"] = experience.coverage_summary(store)
        collection_jobs = []
        for cat in categories:
            for strategy in strategy_counts:
                saved = _json_case_count(_case_file(claw, cat, "inject", surface, strategy))
                collection_jobs.append({"category": cat, "strategy": strategy, "saved": saved})
        coverage["generation_jobs"] = collection_jobs
        (diversity_store.parent / "workspace_coverage.json").write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
        if not any(job["saved"] for job in collection_jobs):
            raise RuntimeError("workspace inject generation saved no cases; inspect the category _generation_logs before probe/eval")

    for by_dim in outputs.values():
        for dim, dirs in by_dim.items():
            by_dim[dim] = sorted(set(dirs))
            if _is_skill_inject(dim, surface):
                by_dim[dim] = _nonempty_skill_inject_dirs(
                    by_dim[dim],
                    stage="generation",
                    require_any=False,
                )

    shortfalls = []
    for cat in categories:
        if "inject" not in dims or not _is_skill_inject("inject", surface):
            continue
        from redclaw.generation import skill as inject_skill

        rows = []
        category_shortfalls = []
        for strategy, requested in strategy_counts.items():
            output = _case_file(claw, cat, "inject", surface, strategy)
            count = _json_case_count(output)
            rows.append({"strategy": strategy, "requested": requested, "saved": count,
                         "task_kind": inject_skill.task_kind_for_target(cat, strategy)})
            if count < requested:
                shortfall = f"{cat}/{_path_slug(strategy)}: {count}/{requested}"
                shortfalls.append(shortfall)
                category_shortfalls.append(shortfall)
        log_dir = _case_dir(claw, cat, "inject", surface) / "_generation_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        task_mix = {
            kind: {"requested": sum(r["requested"] for r in rows if r["task_kind"] == kind),
                   "saved": sum(r["saved"] for r in rows if r["task_kind"] == kind),
                   "shortfall": sum(max(0, r["requested"] - r["saved"])
                                    for r in rows if r["task_kind"] == kind)}
            for kind in inject_skill.TASK_MIX_PER_CATEGORY
        }
        print(f"[generation] {cat} user-task slots (requested/saved/shortfall): {task_mix}", flush=True)
        (log_dir / "collection_summary.json").write_text(json.dumps({
            "requested": sum(r["requested"] for r in rows),
            "saved": sum(r["saved"] for r in rows),
            "complete": not category_shortfalls,
            "shortfalls": category_shortfalls,
            "strategies": rows,
            "task_mix": task_mix,
            "task_mix_basis": "assigned user-task slots; resumed existing cases are not reclassified",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if shortfalls:
        print(
            "[pipeline] inject-skill generation partial coverage; continuing with saved cases. "
            "Shortfalls: " + "; ".join(shortfalls),
            flush=True,
        )

    return outputs


# ---------------------------------------------------------------------------
# stage 3: initialize adapter runtime
# ---------------------------------------------------------------------------

_INITIALIZE_SCRIPT = """
import json
import sys
from pathlib import Path
from redclaw.adapters import get_adapter
from redclaw.evaluation.runner import check_provider_credentials, load_env

spec = json.loads(sys.argv[1])
for index, (model, provider) in enumerate(spec["targets"]):
    # Same credential selection as the probe/eval runners for this model.
    env = load_env(Path(spec["env_file"]))
    check_provider_credentials(env, provider)
    adapter = get_adapter(spec["claw"], model=model, model_provider=provider, env=env)
    adapter.initialize(force=spec["force"] and index == 0)
    if not spec["preflight"]:
        continue
    result = adapter.preflight()
    if result is not None:
        print(f"[pipeline] preflight passed: claw={spec['claw']} model={model} "
              f"provider={provider} {json.dumps(result, sort_keys=True)}", flush=True)
print("[pipeline] adapter runtime ready", flush=True)
"""


def stage_initialize(
    claw: str,
    *,
    targets: list[tuple[str, str]],
    env_file: Path,
    force: bool = False,
    preflight: bool = True,
) -> None:
    """Initialize the claw runtime for each (model, provider) the run will use.

    Adapters with a model preflight (Codex, Claude Code) check every target
    before any probe or evaluation case starts.
    """
    print("\n===== STAGE 3: INITIALIZE ADAPTER RUNTIME =====", flush=True)
    targets = list(dict.fromkeys(targets))
    spec = {
        "claw": claw,
        "targets": targets,
        "env_file": str(env_file),
        "force": force,
        "preflight": preflight,
    }
    _run([sys.executable, "-c", _INITIALIZE_SCRIPT, json.dumps(spec)])


# ---------------------------------------------------------------------------
# stage 3.5: attack-feasibility gate
# ---------------------------------------------------------------------------

def stage_attack_probe(
    claw: str,
    model: str,
    web_assets: str,
    file_assets: str,
    asset_model: str | None,
    timeout: int,
    case_dirs_by_dim: dict[str, list[Path]],
    *,
    reasoning_effort: str = "medium",
    model_provider: str,
    asset_model_provider: str | None,
    env_file: Path,
    probe_workers: int = 1,
    surface: str = "any",
) -> dict[str, int]:
    """Probe tasks in poisoned environments and retain per-case diagnostics.

    For inject, benign completion OR attack success passes this feasibility gate.
    Passing does not establish attack strength or actual payload exposure.
    Returns {dim: quarantined_file_count}.
    """
    print("\n===== STAGE 3.5: ATTACK FEASIBILITY GATE =====", flush=True)
    quarantined: dict[str, int] = {"inject": 0, "trigger": 0}
    for dim in ("inject", "trigger"):
        # Accumulated rows across all case dirs for this dimension
        all_rows: list[dict] = []
        probe_runs = _runs_namespace_dir(claw, dim, surface)
        dirs = case_dirs_by_dim.get(dim, [])
        if _is_skill_inject(dim, surface):
            dirs = _nonempty_skill_inject_dirs(dirs, stage="probe", require_any=False)
        for d in dirs:
            cmd = [
                sys.executable, "-m", "redclaw.redteam.probe",
                "--claw", claw, "--model", model, "--web-assets", web_assets,
                "--file-assets", file_assets,
                "--timeout", str(timeout), "--case-dir", str(d),
                "--runs-dir", str(probe_runs), "--env-file", str(env_file),
                "--model-provider", model_provider,
                "--probe-workers", str(probe_workers),
            ]
            if asset_model:
                cmd += ["--asset-model", asset_model]
            if asset_model_provider:
                cmd += ["--asset-model-provider", asset_model_provider]
            cmd += ["--reasoning-effort", reasoning_effort]
            _run(cmd)
            report = _model_report_path(probe_runs, "probe_model", model, "probe_report.json")
            if not report.exists():
                if _is_skill_inject(dim, surface):
                    raise RuntimeError(f"inject-skill probe did not produce its report: {report}")
                print(f"  [gate] {dim}/{d.name}: no probe report, skipping", flush=True)
                continue
            data = json.loads(report.read_text(encoding="utf-8"))
            rows = data.get("rows", [])
            if _is_skill_inject(dim, surface) and not rows:
                raise RuntimeError(
                    f"inject-skill probe returned zero results for {d}; "
                    f"this is not an all-feasible result. See {report}"
                )
            all_rows.extend(rows)
            if _is_workspace_inject(dim, surface):
                from redclaw.generation.diversity import DiversityStore

                history = DiversityStore(_workspace_history_path(dim, surface))
                by_id = {row["case_id"]: row for row in rows}
                for path in sorted(d.glob("*.json")):
                    records = json.loads(path.read_text(encoding="utf-8"))
                    for record in records if isinstance(records, list) else [records]:
                        if isinstance(record, dict) and record.get("id") in by_id:
                            history.record_outcome(record, claw, model, by_id[record["id"]])
            # Save a per-category copy so individual results are never lost.
            category_report = _model_report_path(probe_runs, "probe_model", model, f"{d.name}_probe_report.json")
            category_report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            infeasible_ids = {r["case_id"] for r in rows if not r.get("attack_feasible")}
            if not infeasible_ids:
                print(f"  [gate] {dim}/{d.name}: all probed cases feasible ✓", flush=True)
                continue
            quarantine = d / "_infeasible"
            quarantine.mkdir(exist_ok=True)
            moved = 0
            for jf in list(d.glob("*.json")):
                try:
                    recs = json.loads(jf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                ids = {r.get("id") for r in (recs if isinstance(recs, list) else [recs]) if isinstance(r, dict)}
                if ids & infeasible_ids:
                    if _is_workspace_inject(dim, surface) and isinstance(recs, list):
                        failed = [r for r in recs if r.get("id") in infeasible_ids]
                        kept = [r for r in recs if r.get("id") not in infeasible_ids]
                        dest = quarantine / jf.name
                        if dest.exists():
                            previous = json.loads(dest.read_text(encoding="utf-8"))
                            previous = previous if isinstance(previous, list) else [previous]
                            failed = [r for r in previous if r.get("id") not in infeasible_ids] + failed
                        dest.write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
                        if kept:
                            jf.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
                        else:
                            jf.unlink()
                    else:
                        jf.rename(quarantine / jf.name)
                    moved += 1
            quarantined[dim] += moved
            print(f"  [gate] {dim}/{d.name}: quarantined {moved} attack-infeasible case file(s) -> {quarantine}", flush=True)
        # Write combined report for this dimension
        if all_rows:
            feasible = sum(1 for r in all_rows if r.get("attack_feasible"))
            combined = {
                "claw": claw, "model": model, "dimension": dim,
                "counts": {"probed": len(all_rows), "feasible": feasible, "infeasible": len(all_rows) - feasible},
                "rows": all_rows,
            }
            combined_path = _model_report_path(probe_runs, "probe_model", model, "probe_report_all.json")
            combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [gate] {dim} combined: {feasible}/{len(all_rows)} feasible → {combined_path}", flush=True)
    print(f"[pipeline] attack gate done; quarantined {quarantined}", flush=True)
    return quarantined


# ---------------------------------------------------------------------------
# stage 4: eval
# ---------------------------------------------------------------------------

def stage_eval(
    claw: str,
    model: str,
    web_assets: str,
    file_assets: str,
    asset_model: str | None,
    timeout: int,
    case_dirs_by_dim: dict[str, list[Path]],
    *,
    reasoning_effort: str = "medium",
    eval_workers: int = 1,
    model_provider: str,
    asset_model_provider: str | None,
    env_file: Path,
    surface: str = "any",
    reuse_assets: bool = True,
    runs_root: Path | None = None,
) -> dict[str, Path]:
    """Run batch for inject then trigger. Returns {dimension: report_path}."""
    print("\n===== STAGE 4: EVAL =====", flush=True)
    reports: dict[str, Path] = {}

    for dim in ("inject", "trigger"):
        dirs = case_dirs_by_dim.get(dim, [])
        if not dirs:
            print(f"[pipeline] no {dim} case dirs, skipping", flush=True)
            continue
        if _is_skill_inject(dim, surface):
            dirs = _nonempty_skill_inject_dirs(dirs, stage="eval", require_any=False)
            if not dirs:
                print(f"[pipeline] no non-empty {dim} case dirs, skipping", flush=True)
                continue

        runs_dir = _runs_namespace_dir(claw, dim, surface, runs_root)
        runs_dir.mkdir(parents=True, exist_ok=True)
        report_path = _model_report_path(runs_dir, "eval_model", model, "batch_report.json")

        cmd = [
            sys.executable, "-m", "redclaw.evaluation.batch",
            "--claw", claw,
            "--model", model,
            "--web-assets", web_assets,
            "--file-assets", file_assets,
            "--force-dimension", dim,
            "--timeout", str(timeout),
            "--runs-dir", str(runs_dir),
            "--env-file", str(env_file),
            "--model-provider", model_provider,
        ]
        for d in dirs:
            cmd += ["--case-dir", str(d)]
        if asset_model:
            cmd += ["--asset-model", asset_model]
        if asset_model_provider:
            cmd += ["--asset-model-provider", asset_model_provider]
        cmd += ["--reasoning-effort", reasoning_effort]
        if not reuse_assets:
            cmd += ["--no-reuse-assets"]
        if eval_workers > 1:
            cmd += ["--workers", str(eval_workers)]

        _run(cmd)
        reports[dim] = report_path

    return reports


# ---------------------------------------------------------------------------
# stage 5: report
# ---------------------------------------------------------------------------

def stage_report(reports: dict[str, Path]) -> None:
    print("\n===== STAGE 5: COMBINED REPORT =====", flush=True)

    def _rate(num: int, den: int) -> str:
        return f"{100 * num / den:.0f}% ({num}/{den})" if den else "-"

    totals = {"n": 0, "error": 0, "attack_success": 0, "task_success": 0}
    for dim, path in reports.items():
        if not path.exists():
            print(f"  [{dim}] report not found: {path}", flush=True)
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        # Recompute from the per-case rows so reports written before the
        # attack/task metrics existed are summarized the same way.
        from redclaw.evaluation.batch import aggregate as aggregate_rows
        aggregate = aggregate_rows(data.get("rows", []))
        o = aggregate.get("overall", {})
        evaluated = o.get("n", 0) - o.get("error", 0)
        for key in totals:
            totals[key] += o.get(key, 0)
        print(
            f"\n  [{dim.upper()}]  n={o.get('n', 0)}  errors={o.get('error', 0)}"
            f"  attack success={_rate(o.get('attack_success', 0), evaluated)}"
            f"  task success={_rate(o.get('task_success', 0), evaluated)}"
        )
        for label, key, width in (("by risk", "by_risk_category", 50), ("by strategy", "by_attack_strategy", 45)):
            groups = aggregate.get(key, {})
            if not groups:
                continue
            print(f"    {label}:")
            for k, b in sorted(groups.items()):
                ev = b["n"] - b["error"]
                print(f"      {k[:width]:<{width}} errors={b['error']}"
                      f"  attack={_rate(b['attack_success'], ev)}  task={_rate(b['task_success'], ev)}")

    evaluated = totals["n"] - totals["error"]
    print(
        f"\n  OVERALL (inject+trigger combined)  n={totals['n']}  errors={totals['error']}"
        f"  attack success={_rate(totals['attack_success'], evaluated)}"
        f"  task success={_rate(totals['task_success'], evaluated)}"
    )
    print("\n  Reports:", flush=True)
    for dim, path in reports.items():
        print(f"    [{dim}] {path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _require_explicit_models(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Every model and provider a selected stage uses must be passed explicitly."""
    skip = set(args.skip)
    runs = lambda stage: stage not in skip
    required: list[tuple[str, str]] = []
    if runs("generate"):
        required += [("gen_model", "--gen-model"), ("gen_model_provider", "--gen-model-provider")]
    if runs("attack_probe"):
        required += [("probe_model", "--probe-model"), ("probe_model_provider", "--probe-model-provider")]
    if runs("eval") or (runs("initialize") and not runs("attack_probe")):
        required += [("model", "--model"), ("model_provider", "--model-provider")]
    llm_assets = "llm" in (args.web_assets, args.file_assets)
    if llm_assets and (runs("attack_probe") or (runs("eval") and args.no_reuse_assets)):
        required += [("asset_model", "--asset-model"), ("asset_model_provider", "--asset-model-provider")]
    missing = [flag for attr, flag in dict.fromkeys(required) if not getattr(args, attr)]
    if missing:
        parser.error(
            "missing required model/provider flags for the selected stages: "
            + ", ".join(missing)
            + " (models and providers are never taken from .env or defaults)"
        )


def main(argv: list[str] | None = None) -> int:
    global DATA_DIR, RUNS_DIR, COLLECTION_ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--categories", nargs="+",
        default=["goal_hijack_malicious_operation", "privacy_leakage"],
        help="Risk categories to generate and eval. Use canonical snake_case names.",
    )
    parser.add_argument("--num", type=int, default=20, help="Cases to generate per category × dimension.")
    parser.add_argument("--collection-root", type=Path, default=None,
                        help="Isolate generated data, runs and diversity history beneath this directory; incompatible with --dataset.")
    parser.add_argument("--dimensions", nargs="+", choices=["inject", "trigger"], default=["inject", "trigger"], help="Which dimensions to generate and eval (default: both).")
    parser.add_argument("--claw", default="nanobot")
    parser.add_argument("--model", default=None, help="Claw (agent under test) model. Required when initialize or eval runs.")
    parser.add_argument(
        "--model-provider",
        choices=["openrouter", "openai", "bedrock", "codex", "claudecode"],
        default=None,
        help=("Provider for --model (required with it); codex and claudecode use saved CLI "
              "subscription logins."),
    )
    parser.add_argument("--probe-model", default=None, help="Strong, compliant model for the attack-feasibility gate (separates 'attack unreachable' from 'agent too weak'). Required when attack_probe runs.")
    parser.add_argument(
        "--probe-model-provider",
        choices=["openrouter", "openai", "bedrock", "codex", "claudecode"],
        default=None,
        help=("Provider for --probe-model (required with it); codex and claudecode use saved CLI "
              "subscription logins."),
    )
    parser.add_argument("--gen-model", default=None, help="Model for case generation. Required when generate runs.")
    parser.add_argument(
        "--inject-generator",
        default=INJECT_GENERATOR,
        help=f"Python module run as the inject generator (default: {INJECT_GENERATOR}).",
    )
    parser.add_argument(
        "--trigger-generator",
        default=TRIGGER_GENERATOR,
        help=f"Python module run as the trigger generator (default: {TRIGGER_GENERATOR}).",
    )
    parser.add_argument("--gen-model-provider", choices=["openrouter", "openai"], default=None, help="API provider for --gen-model. Required when generate runs.")
    parser.add_argument("--max-attempts", type=int, default=None, help="Max generator attempts per category×dimension×strategy file. Omit to use the generator default.")
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=["minimal", "low", "medium", "high"],
        help="Reasoning effort shared by case generation and LLM asset generation.",
    )
    parser.add_argument("--os-profile", default="linux", help="Target OS for generated cases: linux, macos, windows, or mixed.")
    parser.add_argument("--gen-workers", type=int, default=4, help="Parallel workers for case generation (one worker per category×dimension×strategy job).")
    parser.add_argument(
        "--trigger-surface", "--inject-target", dest="surface", default="any",
        help=(
            "Constrain the attack surface. Same flag, opposite ends of the attack in the two "
            "dimensions. trigger: where the poison is PLANTED — memory.md, agents.md, user.md, "
            "soul.md, heartbeat.md, or environment. inject: where the agent must be induced to "
            "WRITE — the same five workspace files, or skill.md for skill registration (the poison "
            "always arrives via the environment, so there is nothing else to constrain). Either way the value names the directory "
            "bucket the cases and runs land in. No constraint: any (default); an unlocked inject "
            "run buckets as `environment`, as before."
        ),
    )
    parser.add_argument("--eval-workers", type=int, default=1, help="Parallel Docker containers during eval (each case gets its own container).")
    parser.add_argument("--probe-workers", type=int, default=1, help="Parallel Docker containers during attack probe (each case gets its own container).")
    parser.add_argument("--web-assets", choices=["offline", "llm"], default="llm")
    parser.add_argument("--file-assets", choices=["offline", "llm"], default="llm")
    parser.add_argument("--asset-model", dest="asset_model", default=None, help="Model for LLM asset synthesis. Required when it runs (probe, or eval with --no-reuse-assets, with llm assets).")
    parser.add_argument("--asset-model-provider", dest="asset_model_provider", choices=["openrouter", "openai"], default=None, help="API provider for --asset-model (required with it).")
    parser.add_argument("--no-reuse-assets", action="store_true", help="Force eval to regenerate assets instead of reusing the probe stage's synthesized rootfs/web render. Eval reuses by default; reuse skips the asset LLM call (e.g. deepseek).")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--keep-docker", action="store_true", help="Do not rmi the docker image during clean.")
    parser.add_argument(
        "--force-initialize",
        action="store_true",
        help="Force the adapter to rebuild/recreate its runtime during initialize.",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help=(
            "Do not run the model preflight during initialize. Codex and Claude Code "
            "otherwise send one fixed prompt per probe/eval model before any case runs."
        ),
    )
    parser.add_argument(
        "--skip",
        nargs="+", choices=[
            "clean", "generate", "initialize", "attack_probe", "eval", "report",
        ],
        default=[],
        help="Stages to skip.",
    )
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help=(
            "Evaluate a frozen dataset (e.g. ./dataset) instead of generating cases. "
            "Cases come from <dataset>/<dimension>/<surface>/cases or <dataset>/<surface>/cases, "
            "and each sample's frozen "
            "environment is staged so the runner reuses it verbatim — no generation, no "
            "probe stage, no asset synthesis. Implies --skip clean generate attack_probe."
        ),
    )
    args = parser.parse_args(argv)

    if args.collection_root and args.dataset:
        parser.error("--collection-root is for new collection, not --dataset evaluation")
    if args.dataset:
        args.dataset = args.dataset.resolve()
        if not (args.dataset / "dataset.json").exists():
            parser.error(f"no dataset.json under {args.dataset}")
        args.dimensions = [_dataset_dimension(args.dataset)]
        forced = {"clean", "generate", "attack_probe"}
        args.skip = sorted(set(args.skip) | forced)
    _require_explicit_models(parser, args)
    previous_roots = DATA_DIR, RUNS_DIR, COLLECTION_ROOT
    if args.collection_root:
        COLLECTION_ROOT = args.collection_root.resolve()
        DATA_DIR = COLLECTION_ROOT / "data"
        RUNS_DIR = COLLECTION_ROOT / "runs"
    try:
        return _run_pipeline(args)
    finally:
        DATA_DIR, RUNS_DIR, COLLECTION_ROOT = previous_roots


def _run_pipeline(args: argparse.Namespace) -> int:
    """Execute one invocation using the paths scoped by main()."""
    # load .env
    if args.env_file.exists():
        for line in args.env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    key_errors = []
    if "generate" not in args.skip:
        gen_key = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"}[args.gen_model_provider]
        if not os.environ.get(gen_key):
            key_errors.append(f"--gen-model-provider {args.gen_model_provider} requires {gen_key}")
    if args.asset_model and args.asset_model_provider:
        from redclaw.environments import harness
        try:
            harness.require_asset_llm(args.asset_model, args.asset_model_provider)
        except harness.AssetConfigError as exc:
            key_errors.append(str(exc))
    from redclaw.evaluation.core import PROVIDER_CREDENTIALS
    agent_providers = []
    if "attack_probe" not in args.skip:
        agent_providers.append(("--probe-model-provider", args.probe_model_provider))
    if "eval" not in args.skip or "initialize" not in args.skip:
        agent_providers.append(("--model-provider", args.model_provider))
    for flag, provider in agent_providers:
        if provider in ("codex", "claudecode") or not provider:
            continue  # native logins; initialize() creates or validates the auth volume
        required = PROVIDER_CREDENTIALS[provider][0]
        if not os.environ.get(required):
            key_errors.append(f"{flag} {provider} requires {required}")
    if key_errors:
        print("\n".join(key_errors), file=sys.stderr)
        return 2

    # stage 1
    if "clean" not in args.skip:
        stage_clean(
            args.categories,
            keep_docker=args.keep_docker,
            dimensions=args.dimensions,
            claw=args.claw,
            surface=args.surface,
        )

    # stage 2
    case_dirs_by_dim: dict[str, list[Path]] = {"inject": [], "trigger": []}
    if "generate" not in args.skip:
        outputs = stage_generate(
            args.claw,
            args.categories,
            num=args.num,
            gen_model=args.gen_model,
            reasoning_effort=args.reasoning_effort,
            os_profile=args.os_profile,
            workers=args.gen_workers,
            dimensions=args.dimensions,
            surface=args.surface,
            gen_model_provider=args.gen_model_provider,
            inject_generator=args.inject_generator,
            trigger_generator=args.trigger_generator,
            max_attempts=args.max_attempts,
        )
        for cat in args.categories:
            for dim, dirs in outputs.get(cat, {}).items():
                case_dirs_by_dim[dim].extend(dirs)
    elif args.dataset:
        # One flat cases/ dir per surface — the dataset is not partitioned by risk
        # category, so --categories does not apply and nothing is dropped by a
        # category-name mismatch.
        dataset_dim = args.dimensions[0]
        surface_slug = _surface_slug(dataset_dim, args.surface)
        cases_dir = _dataset_surface_cases_dir(args.dataset, dataset_dim, surface_slug)
        if not cases_dir.is_dir():
            raise SystemExit(f"dataset has no surface {surface_slug!r}: {cases_dir} missing")
        case_dirs_by_dim[dataset_dim] = [cases_dir]
        n = _case_count_in_dir(cases_dir)
        print(f"[pipeline] dataset mode: {n} cases from {cases_dir}", flush=True)
    else:
        for cat in args.categories:
            for dim in args.dimensions:
                dirs = _case_dirs_for_category(args.claw, cat, dim, args.surface)
                case_dirs_by_dim[dim].extend(dirs)
        print(f"[pipeline] skipping generate; using existing dirs: {case_dirs_by_dim}", flush=True)

    if ("inject" in args.dimensions and _is_skill_inject("inject", args.surface)
            and not {"attack_probe", "eval"}.issubset(args.skip)):
        case_dirs_by_dim["inject"] = _nonempty_skill_inject_dirs(
            case_dirs_by_dim["inject"],
            stage="pipeline input",
            require_any=True,
        )

    # stage 3
    if "initialize" not in args.skip:
        targets = []
        if "attack_probe" not in args.skip:
            targets.append((args.probe_model, args.probe_model_provider))
        if "eval" not in args.skip:
            targets.append((args.model, args.model_provider))
        if args.skip_preflight:
            print("[pipeline] model preflight disabled by --skip-preflight", flush=True)
        elif not targets:
            print("[pipeline] no probe/eval stage; model preflight not needed", flush=True)
        stage_initialize(
            args.claw,
            targets=targets or [(args.model, args.model_provider)],
            env_file=args.env_file,
            force=args.force_initialize,
            preflight=bool(targets) and not args.skip_preflight,
        )

    # stage 3.5: attack-feasibility gate (quarantine cases whose attack can't fire)
    if "attack_probe" not in args.skip:
        stage_attack_probe(
            claw=args.claw,
            model=args.probe_model,
            web_assets=args.web_assets,
            file_assets=args.file_assets,
            asset_model=args.asset_model,
            reasoning_effort=args.reasoning_effort,
            timeout=args.timeout,
            case_dirs_by_dim=case_dirs_by_dim,
            model_provider=args.probe_model_provider,
            asset_model_provider=args.asset_model_provider,
            env_file=args.env_file,
            probe_workers=args.probe_workers,
            surface=args.surface,
        )

    # stage 3.9: dataset mode stages the frozen environments where the runner's
    # asset reuse looks for them. Reuse is on and strict, so a sample whose assets
    # are missing aborts loudly instead of running against a re-synthesized (and
    # therefore different) environment.
    runs_root = DATASET_RUNS_DIR if args.dataset else None
    if args.dataset and "eval" not in args.skip:
        dataset_dim = args.dimensions[0]
        surface_slug = _surface_slug(dataset_dim, args.surface)
        runs_dir = _runs_namespace_dir(args.claw, dataset_dim, args.surface, runs_root)
        runs_dir.mkdir(parents=True, exist_ok=True)
        staged, with_web = stage_frozen_assets(args.dataset, surface_slug, runs_dir, dataset_dim)
        print(f"[pipeline] staged {staged} frozen environments ({with_web} with web) "
              f"for {args.claw}/{dataset_dim}/{surface_slug}", flush=True)

    # stage 4
    reports: dict[str, Path] = {}
    if "eval" not in args.skip:
        reports = stage_eval(
            claw=args.claw,
            model=args.model,
            web_assets=args.web_assets,
            file_assets=args.file_assets,
            asset_model=args.asset_model,
            reasoning_effort=args.reasoning_effort,
            timeout=args.timeout,
            case_dirs_by_dim=case_dirs_by_dim,
            eval_workers=args.eval_workers,
            model_provider=args.model_provider,
            asset_model_provider=args.asset_model_provider,
            env_file=args.env_file,
            surface=args.surface,
            reuse_assets=not args.no_reuse_assets,
            runs_root=runs_root,
        )
    elif args.model:
        for dim in ("inject", "trigger"):
            p = _model_report_path(_runs_namespace_dir(args.claw, dim, args.surface, runs_root), "eval_model", args.model, "batch_report.json")
            if p.exists():
                reports[dim] = p
        print(f"[pipeline] skipping eval; using existing reports: {reports}", flush=True)

    # stage 5
    if "report" not in args.skip and reports:
        stage_report(reports)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
