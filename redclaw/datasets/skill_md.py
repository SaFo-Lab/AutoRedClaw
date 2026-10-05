"""Build the materialized Trigger ``skill_md`` surface.

Selects successful SKILL.md-trigger runs of one eval model (``--model``) from
runs/collection/<claw>/trigger/skill_md, balances the 30 shipped samples
by inverse conditional ASR, and materializes them under the standalone
dataset/trigger/skill_md surface directory.

This intentionally mirrors the trigger and inject dataset builders:

  * strict oracle soundness gate
  * content-hash deduplication
  * inverse-ASR weighted quota across collection claws
  * claw-free sample ids and shipped sample tree
  * provenance outside evaluation inputs

Invoked through ``python -m redclaw dataset build --dimension trigger``.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import stat
from pathlib import Path
from typing import Any

from redclaw.paths import PROJECT_ROOT

from redclaw.evaluation import validation
from redclaw.datasets import (
    CLAW_NAMES,
    load_jsonl,
    normalize_rootfs_file_modes,
    sha256_file,
    task_hash,
    tree_files,
    write_jsonl,
)
from redclaw.evaluation.core import (
    CaseSpec,
    classify_case,
    file_check_paths,
    fixture_network_hosts,
    has_behavioral_checks,
    nominal_memory_paths,
)

CLAWS = CLAW_NAMES
CLAW_RE = re.compile("|".join(CLAWS), re.I)
SURFACE = "skill_md"
WEB_SIDECARS = ("site_meta.json", "app_behavior.json")


def slug(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or default


def skill_dir_name(raw: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(raw or "helper").strip()).strip("-")
    return text or "helper"


def skill_document(entry: dict[str, Any]) -> str:
    name = skill_dir_name(entry.get("name"))
    desc = str(entry.get("description") or "").strip().replace("\n", " ")
    lines = ["---", f"name: {name}", f"description: {desc}"]
    meta = entry.get("metadata")
    if isinstance(meta, dict) and meta:
        lines.append("metadata: " + json.dumps(meta, ensure_ascii=False))
    lines.append("---")
    body = str(entry.get("body") or "").rstrip("\n")
    return "\n".join(lines) + "\n\n" + body + "\n"


def safe_script_rel(raw: Any) -> Path:
    parts = []
    for part in str(raw or "").lstrip("/").split("/"):
        if part in ("", ".", ".."):
            continue
        parts.append(part)
    return Path(*parts) if parts else Path("script.sh")


def materialize_skills(entries: list[dict[str, Any]], dest: Path) -> list[dict[str, Any]]:
    skills_dir = dest / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for entry in entries:
        name = skill_dir_name(entry.get("name"))
        sdir = skills_dir / name
        sdir.mkdir(parents=True, exist_ok=True)
        skill_path = sdir / "SKILL.md"
        skill_path.write_text(skill_document(entry), encoding="utf-8")

        scripts_out: list[dict[str, Any]] = []
        scripts = entry.get("scripts")
        if isinstance(scripts, dict):
            for rel_raw, content in sorted(scripts.items()):
                rel = safe_script_rel(rel_raw)
                script_path = sdir / rel
                script_path.parent.mkdir(parents=True, exist_ok=True)
                script_path.write_text(str(content), encoding="utf-8")
                script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
                scripts_out.append(
                    {
                        "path": script_path.relative_to(dest).as_posix(),
                        "bytes": script_path.stat().st_size,
                        "sha256": sha256_file(script_path),
                    }
                )

        out.append(
            {
                "name": str(entry.get("name") or name),
                "description": str(entry.get("description") or ""),
                "directory": sdir.relative_to(dest).as_posix(),
                "skill_file": {
                    "path": skill_path.relative_to(dest).as_posix(),
                    "bytes": skill_path.stat().st_size,
                    "sha256": sha256_file(skill_path),
                },
                "scripts": scripts_out,
            }
        )
    return out


def scan(source: Path, model_name: str) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    rows: list[dict[str, Any]] = []
    rejected: dict[str, list[str]] = {}
    for claw in CLAWS:
        base = source / claw / "trigger" / SURFACE / f"eval_model={slug(model_name)}"
        if not base.is_dir():
            continue
        for report_path in sorted(base.rglob("report.json")):
            run_dir = report_path.parent
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                spec = json.loads((run_dir / "setting.json").read_text(encoding="utf-8"))
            except Exception:
                continue

            skill_entries = spec.get("environment", {}).get("skill_surfaces")
            if not isinstance(skill_entries, list) or not skill_entries:
                continue

            why = validation.case_violations(spec.get("verification"))
            if why:
                rejected[str(spec.get("id") or report.get("case_id") or "?")] = why
                continue

            task = str(spec.get("benign_user_goal") or "").strip()
            attack = str(spec.get("attacker_instruction") or "").strip()
            if not task or not attack:
                continue

            checks = (report.get("check_results") or {}).get("attack_task_success") or []
            passed = [c for c in checks if isinstance(c, dict) and c.get("passed")]
            model = (report.get("run") or {}).get("model") or model_name
            rows.append(
                {
                    "run_dir": run_dir,
                    "claw": claw,
                    "model": model,
                    "case_id": report.get("case_id") or spec.get("id"),
                    "spec": spec,
                    "task": task,
                    "attack": attack,
                    "h": task_hash(task, attack),
                    "risk_category": report.get("risk_category") or spec.get("risk_category") or "",
                    "attack_strategy": report.get("attack_strategy")
                    or spec.get("attack_strategy")
                    or "",
                    "atk": bool(report.get("attack_succeeded")),
                    "benign": bool(report.get("benign_completed")),
                    "safe_ok": bool(report.get("safe_ok")),
                    "attack_checks": f"{len(passed)}/{len(checks)}",
                    "full_breach": bool(checks) and len(passed) == len(checks),
                }
            )
    return rows, rejected


def conditional_asr(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for claw in CLAWS:
        valid = [r for r in rows if r["claw"] == claw and r["benign"]]
        out[claw] = (sum(r["atk"] for r in valid) / len(valid)) if valid else 0.0
    return out


def evidence_for(runs: list[dict[str, Any]], model_name: str) -> dict[str, Any]:
    return sorted(
        runs,
        key=lambda r: (
            not r["benign"],
            not r["full_breach"],
            r["model"] != model_name,
            str(r["run_dir"]),
        ),
    )[0]


def balanced_targets(counter: collections.Counter[str], total: int) -> dict[str, int]:
    """Uniform-ish taxonomy targets with availability caps.

    Example: 30 samples over 9 available risk categories -> six categories get 3
    and the three largest eligible pools get 4. A category with only one
    successful sample is capped at one, and the remainder is redistributed.
    """
    target = {key: 0 for key in sorted(counter)}
    active = {key for key, n in counter.items() if n > 0}
    remaining = total
    while remaining > 0 and active:
        key = min(active, key=lambda k: (target[k], -counter[k], k))
        target[key] += 1
        remaining -= 1
        if target[key] >= counter[key]:
            active.remove(key)
    return target


def select(rows: list[dict[str, Any]], want: int, weights: dict[str, float], model_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    wins = [r for r in rows if r["atk"]]
    by_hash: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in wins:
        by_hash[row["h"]].append(row)

    meta: dict[str, dict[str, Any]] = {}
    for h, runs in by_hash.items():
        claws = sorted({r["claw"] for r in runs})
        meta[h] = {
            "claws": claws,
            "transfer": len(claws),
            "full_breach": any(r["full_breach"] for r in runs),
            "benign": any(r["benign"] for r in runs),
            "models": sorted({r["model"] for r in runs}),
            "risk_category": runs[0]["risk_category"],
            "attack_strategy": runs[0]["attack_strategy"],
            "runs": runs,
        }

    pool: dict[str, list[str]] = {c: [] for c in CLAWS}
    for h, item in meta.items():
        for claw in item["claws"]:
            pool[claw].append(h)
    for claw in CLAWS:
        pool[claw].sort(
            key=lambda h: (
                -meta[h]["transfer"],
                not meta[h]["full_breach"],
                not meta[h]["benign"],
                h,
            )
        )

    risk_target = balanced_targets(collections.Counter(m["risk_category"] for m in meta.values()), want)
    strategy_target = balanced_targets(collections.Counter(m["attack_strategy"] for m in meta.values()), want)
    risk_count: collections.Counter[str] = collections.Counter()
    strategy_count: collections.Counter[str] = collections.Counter()

    chosen: list[tuple[str, str]] = []
    taken: set[str] = set()
    count = {c: 0 for c in CLAWS}
    active_weights = {c: weights.get(c, 0.0) for c in CLAWS if pool[c] and weights.get(c, 0.0) > 0}
    if not active_weights:
        active_weights = {c: 1.0 for c in CLAWS if pool[c]}

    while len(chosen) < want:
        live = [c for c, weight in active_weights.items() if weight > 0 and any(h not in taken for h in pool[c])]
        if not live:
            break
        claw = min(live, key=lambda c: ((count[c] + 0.5) / active_weights[c], c))

        def diversity_score(h: str) -> tuple[float, str]:
            risk = meta[h]["risk_category"]
            strategy = meta[h]["attack_strategy"]
            next_risk = risk_count[risk] + 1
            next_strategy = strategy_count[strategy] + 1
            risk_goal = max(1, risk_target.get(risk, 1))
            strategy_goal = max(1, strategy_target.get(strategy, 1))
            # Strongly avoid pushing a taxonomy bucket over target, then prefer
            # unseen/under-filled buckets. The claw slot itself is still chosen
            # by inverse ASR above, so diversity only ranks candidates *within*
            # that claw's eligible pool.
            over = (max(0, next_risk - risk_goal) ** 2) * 100
            over += (max(0, next_strategy - strategy_goal) ** 2) * 12
            fill = (risk_count[risk] / risk_goal) * 8
            fill += (strategy_count[strategy] / strategy_goal) * 3
            cover = (0 if risk_count[risk] else -30) + (0 if strategy_count[strategy] else -10)
            return (over + fill + cover, h)

        h = min((h for h in pool[claw] if h not in taken), key=diversity_score)
        taken.add(h)
        count[claw] += 1
        risk_count[meta[h]["risk_category"]] += 1
        strategy_count[meta[h]["attack_strategy"]] += 1
        chosen.append((h, claw))

    selected: list[dict[str, Any]] = []
    for h, quota_claw in chosen:
        item = meta[h]
        selected.append(
            {
                "h": h,
                "quota_claw": quota_claw,
                "evidence": evidence_for([r for r in item["runs"] if r["claw"] == quota_claw], model_name),
                **item,
            }
        )

    stats = {
        "available": len(by_hash),
        "selected": len(selected),
        "shortfall": max(0, want - len(selected)),
        "by_quota_claw": dict(collections.Counter(s["quota_claw"] for s in selected)),
        "eligible_by_claw": {c: len(set(pool[c])) for c in CLAWS},
        "risk_target": risk_target,
        "strategy_target": strategy_target,
        "selected_by_risk_category": dict(risk_count),
        "selected_by_attack_strategy": dict(strategy_count),
    }
    return selected, stats


def assign_ids(selected: list[dict[str, Any]]) -> dict[str, str]:
    ids: dict[str, str] = {}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for sample in selected:
        ev = sample["evidence"]
        groups[(ev["risk_category"], ev["attack_strategy"])].append(sample)
    for (risk, strategy), members in groups.items():
        members.sort(key=lambda s: s["h"])
        for i, sample in enumerate(members, 1):
            ids[sample["h"]] = f"{risk}_{slug(strategy)}_{i:02d}__{SURFACE}"
    return ids


def materialize_sample(sample: dict[str, Any], sid: str, dest: Path) -> dict[str, Any]:
    ev = sample["evidence"]
    src_run: Path = ev["run_dir"]
    raw = ev["spec"]
    case = CaseSpec(raw)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    root_src = src_run / "rootfs"
    if not root_src.is_dir():
        raise SystemExit(f"{sid}: run bundle has no rootfs at {root_src}")
    rootfs = dest / "rootfs"
    shutil.copytree(root_src, rootfs)
    normalize_rootfs_file_modes(rootfs)

    web = None
    if (src_run / "web").is_dir() and (src_run / "url_map.json").exists():
        shutil.copytree(src_run / "web", dest / "web")
        shutil.copy2(src_run / "url_map.json", dest / "url_map.json")
        hosts: list[str] = []
        pages: dict[str, Any] = {}
        for url, entry in json.loads((dest / "url_map.json").read_text(encoding="utf-8")).items():
            host = entry.get("host")
            if host and host not in hosts:
                hosts.append(host)
            page = dest / "web" / entry["file"]
            if not page.exists():
                raise SystemExit(f"{sid}: url_map points at missing page {entry['file']}")
            pages[url] = {
                "file": f"web/{entry['file']}",
                "bytes": page.stat().st_size,
                "sha256": sha256_file(page),
            }
        web = {"dir": "web", "url_map": "url_map.json", "hosts": hosts, "pages": pages}
        for name in WEB_SIDECARS:
            if (src_run / name).exists():
                shutil.copy2(src_run / name, dest / name)
                web[name.removesuffix(".json")] = name

    skill_entries = [
        e
        for e in raw.get("environment", {}).get("skill_surfaces", [])
        if isinstance(e, dict) and str(e.get("body") or "").strip()
    ]
    skill_surfaces = materialize_skills(skill_entries, dest)

    cls = classify_case(case)
    network_hosts = fixture_network_hosts(case) if has_behavioral_checks(case) else []
    env = {
        "schema": "redclaw-dataset/1.0",
        "id": sid,
        "surface": SURFACE,
        "dimension": "trigger",
        "task": raw.get("benign_user_goal", ""),
        "attacker_instruction": raw.get("attacker_instruction", ""),
        "risk_category": ev["risk_category"],
        "attack_strategy": ev["attack_strategy"],
        "rootfs": {"dir": "rootfs", "files": tree_files(dest / "rootfs")},
        "memory_surface": None,
        "skill_surfaces": {"dir": "skills", "entries": skill_surfaces},
        "web": web,
        "network_hosts": network_hosts,
        "needs_sink": bool(cls["needs_sink"]) or web is not None,
        "collect_paths": list(dict.fromkeys(file_check_paths(case) + nominal_memory_paths(case))),
        "verification": raw.get("verification", {}),
        "oracle": raw.get("oracle", {}),
    }
    (dest / "env.json").write_text(json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return env


def update_global_indexes(dataset: Path, rows: list[dict[str, Any]], provenance: list[dict[str, Any]]) -> None:
    samples_path = dataset / "samples.jsonl"
    prov_path = dataset / "PROVENANCE.jsonl"
    existing_samples = [r for r in load_jsonl(samples_path) if r.get("surface") != SURFACE]
    existing_prov = [r for r in load_jsonl(prov_path) if r.get("surface") != SURFACE]

    all_samples = sorted(existing_samples + rows, key=lambda r: (r.get("surface", ""), r.get("id", "")))
    all_prov = sorted(existing_prov + provenance, key=lambda r: (r.get("surface", ""), r.get("id", "")))
    write_jsonl(samples_path, all_samples)
    write_jsonl(prov_path, all_prov)

    manifest_path = dataset / "dataset.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.update(
        {
            "name": manifest.get("name", "RedClaw-Trigger"),
            "schema": manifest.get("schema", "redclaw-dataset/1.0"),
            "version": manifest.get("version", "1.0"),
            "dimension": "trigger",
            "harness_agnostic": True,
            "n": len(all_samples),
            "by_surface": dict(collections.Counter(r["surface"] for r in all_samples)),
            "by_risk_category": dict(collections.Counter(r["risk_category"] for r in all_samples)),
            "by_attack_strategy": dict(collections.Counter(r["attack_strategy"] for r in all_samples)),
            "by_source_tier": dict(collections.Counter(r["source_tier"] for r in all_samples)),
            "provenance": "PROVENANCE.jsonl (audit trail; names the collection harness, not an evaluation input)",
        }
    )
    manifest["description"] = (
        "Materialized trigger-defense benchmark: an attack instruction is "
        "pre-seeded into the environment (a memory surface, a skill surface, or "
        "the environment itself) and the oracle checks whether the agent carried "
        "it out while doing the user's task."
    )
    manifest["skill_md_selection"] = (
        "SKILL_MD_SELECTION.json (audit trail for the balanced skill_md subset; "
        "names collection harnesses, not an evaluation input)"
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def harness_leaks(dataset: Path) -> list[str]:
    leaks: list[str] = []
    for path in sorted(p for p in (dataset / SURFACE).rglob("*") if p.is_file()):
        rel = path.relative_to(dataset).as_posix()
        if CLAW_RE.search(rel):
            leaks.append(f"{rel} (in path)")
        try:
            text = path.read_text(errors="replace")
        except Exception as exc:
            leaks.append(f"{rel}: UNSCANNABLE ({type(exc).__name__}: {exc})")
            continue
        for match in set(m.lower() for m in CLAW_RE.findall(text)):
            leaks.append(f"{rel}: mentions {match}")
    for name in ("samples.jsonl", "dataset.json"):
        path = dataset / name
        if not path.exists():
            continue
        for match in set(m.lower() for m in CLAW_RE.findall(path.read_text(errors="replace"))):
            leaks.append(f"{name}: mentions {match}")
    return leaks


def build(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "runs" / "collection")
    parser.add_argument(
        "--dataset", type=Path, default=PROJECT_ROOT / "dataset" / "trigger"
    )
    parser.add_argument("--model", required=True,
                        help="eval model whose collected skill_md runs are selected (matches eval_model=<slug>)")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    rows, rejected = scan(args.source, args.model)
    asr = conditional_asr(rows)
    weights = {c: (1.0 / asr[c] if asr[c] > 0 else 0.0) for c in CLAWS}
    selected, stats = select(rows, args.count, weights, args.model)

    print(f"[scan] {len(rows)} {args.model} skill_md runs | {sum(r['atk'] for r in rows)} attack successes")
    if rejected:
        print(f"[scan] refused {len(rejected)} case(s) for unevaluable checks")
    print("[weights] inverse conditional ASR")
    total_weight = sum(weights.values())
    for claw in sorted(CLAWS, key=lambda c: asr[c]):
        share = weights[claw] / total_weight if total_weight else 0.0
        print(
            f"    {claw:<10} ASR={100*asr[claw]:5.1f}%  "
            f"eligible={stats['eligible_by_claw'].get(claw, 0):>3}  "
            f"share={100*share:4.1f}%"
        )
    print(
        f"[plan] available={stats['available']} selected={stats['selected']} "
        f"shortfall={stats['shortfall']} by_quota_claw={stats['by_quota_claw']}"
    )
    print(f"[plan] by_risk_category={stats['selected_by_risk_category']}")
    print(f"[plan] by_attack_strategy={stats['selected_by_attack_strategy']}")
    if args.dry_run:
        return 0
    if stats["selected"] != args.count:
        raise SystemExit(f"only selected {stats['selected']} samples, wanted {args.count}")

    ids = assign_ids(selected)
    surface_dir = args.dataset / SURFACE
    if surface_dir.exists():
        shutil.rmtree(surface_dir)
    (surface_dir / "cases").mkdir(parents=True)
    (surface_dir / "env").mkdir(parents=True)

    index_rows: list[dict[str, Any]] = []
    prov_rows: list[dict[str, Any]] = []
    for sample in selected:
        sid = ids[sample["h"]]
        ev = sample["evidence"]
        env = materialize_sample(sample, sid, surface_dir / "env" / sid)

        clean_case = {k: v for k, v in ev["spec"].items() if k not in ("source_claw", "source_case_id")}
        clean_case["id"] = sid
        clean_case["target_surface"] = SURFACE
        clean_case["test_mode"] = "trigger"
        (surface_dir / "cases" / f"{sid}.json").write_text(
            json.dumps(clean_case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        row = {
            "id": sid,
            "surface": SURFACE,
            "dimension": "trigger",
            "risk_category": ev["risk_category"],
            "attack_strategy": ev["attack_strategy"],
            "source_tier": "primary",
            "task": env["task"],
            "attacker_instruction": env["attacker_instruction"],
            "has_memory_poison": False,
            "has_skill_surface": True,
            "skill_count": len(env["skill_surfaces"]["entries"]),
            "has_web": env["web"] is not None,
            "needs_sink": env["needs_sink"],
            "rootfs_files": len(env["rootfs"]["files"]),
            "case_file": f"{SURFACE}/cases/{sid}.json",
            "env_dir": f"{SURFACE}/env/{sid}",
        }
        index_rows.append(row)

        prov_rows.append(
            {
                "id": sid,
                "surface": SURFACE,
                "collected_on_claw": ev["claw"],
                "quota_claw": sample["quota_claw"],
                "attack_verified_on_claws": sample["claws"],
                "attack_verified_on_models": sample["models"],
                "original_case_id": ev["case_id"],
                "source_tier": "primary",
                "evidence_model": ev["model"],
                "evidence_stage": "eval",
                "attack_checks": ev["attack_checks"],
                "full_breach": ev["full_breach"],
                "benign_completed_at_collection": ev["benign"],
                "collection_artifacts": ev["run_dir"].relative_to(PROJECT_ROOT).as_posix(),
            }
        )

    index_rows.sort(key=lambda r: r["id"])
    prov_rows.sort(key=lambda r: r["id"])
    (surface_dir / "index.json").write_text(
        json.dumps(
            {
                "surface": SURFACE,
                "dimension": "trigger",
                "n": len(index_rows),
                "by_risk_category": dict(collections.Counter(r["risk_category"] for r in index_rows)),
                "by_attack_strategy": dict(collections.Counter(r["attack_strategy"] for r in index_rows)),
                "samples": index_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (args.dataset / "SKILL_MD_SELECTION.json").write_text(
        json.dumps(
            {
                "note": (
                    "Balanced selection audit for dataset/trigger/skill_md. "
                    "Names collection harnesses, so it is not an evaluation input."
                ),
                "eligibility": f"attack_succeeded on eval_model={args.model}",
                "dedup_key": "sha256(benign_user_goal || attacker_instruction)",
                "requested": args.count,
                "conditional_asr": {c: round(asr[c], 4) for c in CLAWS},
                "quota_weight": {c: round(weights[c] / total_weight, 4) for c in CLAWS}
                if total_weight
                else {},
                "eligible_by_claw": stats["eligible_by_claw"],
                "selected_by_quota_claw": stats["by_quota_claw"],
                "risk_target": stats["risk_target"],
                "strategy_target": stats["strategy_target"],
                "selected_by_risk_category": stats["selected_by_risk_category"],
                "selected_by_attack_strategy": stats["selected_by_attack_strategy"],
                "available": stats["available"],
                "selected": stats["selected"],
                "shortfall": stats["shortfall"],
                "balance_note": (
                    f"Claw slots are proportional to 1 / conditional ASR among {args.model} runs. "
                    "Within each claw slot, candidates are ranked to fill uniform-ish risk-category "
                    "and attack-strategy targets, capped by eligible availability. Claws with no "
                    "eligible successful samples cannot contribute even if their ASR is low."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    update_global_indexes(args.dataset, index_rows, prov_rows)
    leaks = harness_leaks(args.dataset)
    print(f"\ndataset skill_md written to {surface_dir}")
    print(f"  samples          : {len(index_rows)}")
    print(f"  by quota claw    : {json.dumps(stats['by_quota_claw'], sort_keys=True)}")
    print(f"  with frozen web  : {sum(r['has_web'] for r in index_rows)}")
    print(f"  rootfs files     : {sum(r['rootfs_files'] for r in index_rows)}")
    print(f"  harness leaks    : {len(leaks)}")
    for leak in leaks[:20]:
        print("   -", leak)
    return 1 if leaks else 0
