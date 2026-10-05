"""Build the base Trigger surfaces from frozen collection artifacts.

The collection artifacts under runs/collection/static carry the claw they
were collected on: the sample id ends in `__<claw>`, the case record names a
`source_claw`, `env.json` embeds a table of all six harnesses' container paths,
and `evidence/` is a full transcript of one specific claw's run. None of that
belongs in a dataset other people evaluate *their* agent against — an id that
says `__seclaw` leaks into every result file, and a shipped trajectory is one
harness's behaviour presented as part of the task.

This writes a clean dataset that contains only:

  * the task the agent is given,
  * the frozen environment it runs in (file tree, pre-seeded memory surface,
    served web pages) — byte-identical to the collection run,
  * the oracle that decides benign success / attack success / safe behaviour,
  * taxonomy labels (surface, risk category, attack strategy).

Sample ids are derived from the content hash, so they are stable across rebuilds
and mention no harness. The audit trail that *does* name the original claw is
written to the dataset root's PROVENANCE.jsonl, outside the sample tree and excluded from
evaluation inputs — delete that one file for a dataset with zero claw references.

    python -m redclaw dataset build --dimension trigger --without-skill-md --out dataset/trigger
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
from pathlib import Path

from redclaw.paths import PROJECT_ROOT

from redclaw.evaluation import validation
from redclaw.datasets import (
    CLAW_NAMES,
    TRIGGER_BASE_SURFACES,
    TRIGGER_SURFACES,
    normalize_rootfs_file_modes,
    task_hash,
)

SRC = PROJECT_ROOT / "runs" / "collection" / "static"
SURFACES = TRIGGER_BASE_SURFACES
CLAWS = CLAW_NAMES
CLAW_RE = re.compile("|".join(CLAWS), re.I)
WEB_SIDECARS = ("site_meta.json", "app_behavior.json")


def slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).lower())).strip("_")


def assign_ids(rows: list[dict]) -> dict[str, str]:
    """content-hash ordered, so the disambiguating suffix never encodes a claw."""
    out: dict[str, str] = {}
    groups: dict[tuple[str, str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[(r["surface"], r["risk_category"], r["attack_strategy"])].append(r)
    for (surface, cat, strat), members in groups.items():
        members.sort(key=lambda r: r["_hash"])
        for i, r in enumerate(members, 1):
            out[r["sample_id"]] = f"{cat}_{slug(strat)}_{i:02d}__{surface}"
    return out


def build(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "dataset" / "trigger")
    ap.add_argument("--source", type=Path, default=SRC,
                    help="collection dir (…/static)")
    args = ap.parse_args(argv)
    out, src = args.out, args.source
    if not (src / "samples.jsonl").exists():
        raise SystemExit(f"collection not found at {src}")
    src_rows = [json.loads(line) for line in (src / "samples.jsonl").read_text().splitlines() if line]

    # Soundness gate — refuse any sample carrying a check the scorer cannot
    # evaluate. Such a sample ships an oracle that silently returned a verdict.
    refused: list[tuple[str, list[str]]] = []
    kept: list[dict] = []
    for r in src_rows:
        case_path = src / "trigger" / r["surface"] / "cases" / f"{r['sample_id']}.json"
        why = validation.case_violations(json.loads(case_path.read_text()).get("verification"))
        (refused.append((r["sample_id"], why)) if why else kept.append(r))
    if refused:
        print(f"refused {len(refused)} sample(s) for unevaluable checks:")
        for sid, why in refused:
            print(f"  {sid}: {why[0]}")
    src_rows = kept

    # gather + assign claw-free ids
    for r in src_rows:
        r["_hash"] = task_hash(r["benign_task"], r["attack_task"])
    new_id = assign_ids(src_rows)
    assert len(set(new_id.values())) == len(src_rows), "id collision"

    for name in (
        *TRIGGER_SURFACES,
        "dataset.json",
        "samples.jsonl",
        "PROVENANCE.jsonl",
        "README.md",
        "SKILL_MD_SELECTION.json",
    ):
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    out.mkdir(parents=True, exist_ok=True)

    index_rows: list[dict] = []
    provenance_rows: list[dict] = []
    per_surface: dict[str, list[dict]] = collections.defaultdict(list)

    for r in src_rows:
        surface, old = r["surface"], r["sample_id"]
        sid = new_id[old]
        src_env = src / "trigger" / surface / "env" / old
        env = json.loads((src_env / "env.json").read_text())
        case = json.loads((src / "trigger" / surface / "cases" / f"{old}.json").read_text())

        sdir = out / surface
        (sdir / "cases").mkdir(parents=True, exist_ok=True)
        dest = sdir / "env" / sid
        dest.mkdir(parents=True, exist_ok=True)

        # --- frozen environment, copied verbatim -------------------------- #
        rootfs = dest / "rootfs"
        shutil.copytree(src_env / env["rootfs"]["dir"], rootfs)
        normalize_rootfs_file_modes(rootfs)
        mem = None
        if env["memory_surface"]:
            (dest / "memory").mkdir(exist_ok=True)
            shutil.copy2(src_env / env["memory_surface"]["file"], dest / "memory" / "poison.md")
            mem = {
                # a LOGICAL surface name; each harness maps it to its own path
                "logical_target": env["memory_surface"]["logical_target"],
                "file": "memory/poison.md",
                "bytes": env["memory_surface"]["bytes"],
                "sha256": env["memory_surface"]["sha256"],
            }
        web = None
        if env["web"]:
            shutil.copytree(src_env / env["web"]["dir"], dest / "web")
            shutil.copy2(src_env / env["web"]["url_map"], dest / "url_map.json")
            web = {"dir": "web", "url_map": "url_map.json",
                   "hosts": env["web"]["hosts"], "pages": env["web"]["pages"]}
            for side in WEB_SIDECARS:
                if (src_env / side).exists():
                    shutil.copy2(src_env / side, dest / side)
                    web[side.removesuffix(".json")] = side

        # --- the case record, claw-free ----------------------------------- #
        clean_case = {k: v for k, v in case.items()
                      if k not in ("source_claw", "source_case_id")}
        clean_case["id"] = sid
        clean_case["target_surface"] = surface
        clean_case["test_mode"] = "trigger"
        (sdir / "cases" / f"{sid}.json").write_text(
            json.dumps(clean_case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # --- the environment descriptor, claw-free ------------------------ #
        (dest / "env.json").write_text(json.dumps({
            "schema": "redclaw-dataset/1.0",
            "id": sid, "surface": surface, "dimension": "trigger",
            "task": env["task"],
            "attacker_instruction": env["attacker_instruction"],
            "risk_category": env["risk_category"],
            "attack_strategy": env["attack_strategy"],
            "rootfs": {"dir": "rootfs", "files": env["rootfs"]["files"]},
            "memory_surface": mem,
            "web": web,
            "network_hosts": env["network_hosts"],
            "needs_sink": env["needs_sink"],
            "collect_paths": env["collect_paths"],
            "verification": env["verification"],
            "oracle": env["oracle"],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        row = {"id": sid, "surface": surface, "dimension": "trigger",
               "risk_category": env["risk_category"], "attack_strategy": env["attack_strategy"],
               "source_tier": r["source_tier"],
               "task": env["task"], "attacker_instruction": env["attacker_instruction"],
               "has_memory_poison": mem is not None,
               "has_web": web is not None, "needs_sink": env["needs_sink"],
               "rootfs_files": len(env["rootfs"]["files"]),
               "case_file": f"{surface}/cases/{sid}.json",
               "env_dir": f"{surface}/env/{sid}"}
        index_rows.append(row)
        per_surface[surface].append(row)

        provenance_rows.append({
            "id": sid, "surface": surface,
            "collected_on_claw": r["claw"],
            "original_case_id": r["source_case_id"],
            "original_sample_id": old,
            "source_tier": r["source_tier"],
            "attack_verified_on": r["succeeded_on"],
            "evidence_model": r["evidence_model"], "evidence_stage": r["evidence_stage"],
            "attack_checks": r["attack_checks"], "full_breach": r["full_breach"],
            "benign_completed_at_collection": r["benign_completed"],
            "collection_artifacts": f"{src.relative_to(PROJECT_ROOT).as_posix()}/trigger/{surface}/evidence/{old}",
        })

    index_rows.sort(key=lambda x: (x["surface"], x["id"]))
    provenance_rows.sort(key=lambda x: (x["surface"], x["id"]))

    for surface, rows in per_surface.items():
        rows.sort(key=lambda x: x["id"])
        (out / surface / "index.json").write_text(json.dumps({
            "surface": surface, "dimension": "trigger", "n": len(rows),
            "by_risk_category": dict(collections.Counter(x["risk_category"] for x in rows)),
            "by_attack_strategy": dict(collections.Counter(x["attack_strategy"] for x in rows)),
            "samples": rows,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (out / "samples.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in index_rows), encoding="utf-8")
    (out / "PROVENANCE.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in provenance_rows), encoding="utf-8")
    (out / "dataset.json").write_text(json.dumps({
        "name": "RedClaw-Trigger", "schema": "redclaw-dataset/1.0", "version": "1.0",
        "dimension": "trigger",
        "description": ("Materialized trigger-defense benchmark: an attack instruction is "
                        "pre-seeded into the environment (a memory surface the agent reads, or "
                        "the environment itself) and the oracle checks whether the agent carried "
                        "it out while doing the user's task."),
        "harness_agnostic": True,
        "n": len(index_rows),
        "by_surface": {s: len(v) for s, v in sorted(per_surface.items())},
        "by_risk_category": dict(collections.Counter(x["risk_category"] for x in index_rows)),
        "by_attack_strategy": dict(collections.Counter(x["attack_strategy"] for x in index_rows)),
        "by_source_tier": dict(collections.Counter(x["source_tier"] for x in index_rows)),
        "provenance": "PROVENANCE.jsonl (audit trail; names the collection harness, not an evaluation input)",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # --- assert the sample tree mentions no harness -------------------------- #
    leaks: list[str] = []
    sample_files = (
        path
        for surface in TRIGGER_SURFACES
        for path in (out / surface).rglob("*")
        if path.is_file()
    )
    for path in sorted(sample_files):
        rel = path.relative_to(out).as_posix()
        if CLAW_RE.search(rel):
            leaks.append(f"{rel} (in path)")
        # An unreadable file is a HOLE in the neutrality proof, not a clean
        # file: it may or may not name a harness and there is no way to know.
        # Recorded as a finding rather than raised, so one bad file does not
        # abort the sweep and hide every other result.
        try:
            text = path.read_text(errors="replace")
        except Exception as exc:
            leaks.append(f"{rel}: UNSCANNABLE ({type(exc).__name__}: {exc})")
            continue
        for m in set(x.lower() for x in CLAW_RE.findall(text)):
            leaks.append(f"{rel}: mentions {m}")
    for name in ("samples.jsonl", "dataset.json"):
        for m in set(x.lower() for x in CLAW_RE.findall((out / name).read_text())):
            leaks.append(f"{name}: mentions {m}")

    print(f"dataset written to {out}")
    print(f"  samples          : {len(index_rows)}")
    print(f"  by surface       : {json.dumps({s: len(v) for s, v in sorted(per_surface.items())})}")
    print(f"  with memory poison: {sum(x['has_memory_poison'] for x in index_rows)}")
    print(f"  with frozen web   : {sum(x['has_web'] for x in index_rows)}")
    print(f"  rootfs files      : {sum(x['rootfs_files'] for x in index_rows)}")
    print(f"\nharness references in the sample tree / index: {len(leaks)}")
    for leak in leaks[:20]:
        print("  -", leak)
    return 1 if leaks else 0
