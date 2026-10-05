"""Audit a Trigger dataset: integrity, neutrality, runnability, uniqueness.

Re-reads everything from disk and recomputes it — never trusts the builder's own
bookkeeping. Docker is not needed; the checks cover the dataset itself and the
runner's preflight.

    python -m redclaw dataset verify --dataset dataset/trigger
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from redclaw.paths import PROJECT_ROOT

from redclaw.evaluation import validation
from redclaw.evaluation.core import classify_case
from redclaw.evaluation.batch import discover_cases
from redclaw.evaluation.artifacts import case_run_dir
from redclaw.datasets import CLAW_NAMES, TRIGGER_SURFACES, sha256_file, task_hash

SRC = PROJECT_ROOT / "runs" / "collection" / "static"
SURFACES = TRIGGER_SURFACES
CLAWS = CLAW_NAMES
CLAW_RE = re.compile("|".join(CLAWS), re.I)
SURFACE_FILE = {"agents_md": "agents.md", "memory_md": "memory.md",
                "soul_md": "soul.md", "user_md": "user.md"}


def audit(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "dataset" / "trigger")
    args = ap.parse_args(argv)
    ds = args.dataset
    fail: list[str] = []

    def chk(cond, msg):
        if not cond:
            fail.append(msg)

    manifest = json.loads((ds / "dataset.json").read_text())
    rows = [json.loads(l) for l in (ds / "samples.jsonl").read_text().splitlines() if l]
    # PROVENANCE.jsonl is an audit-only source trail, not an evaluation input;
    # check its row count only when the file is present.
    prov_path = ds / "PROVENANCE.jsonl"
    prov: dict = {}
    if prov_path.exists():
        prov = {r["id"]: r for r in
                (json.loads(l) for l in prov_path.read_text().splitlines() if l)}
        chk(len(prov) == len(rows), "PROVENANCE row count != samples")
    chk(manifest["n"] == len(rows), "dataset.json n != samples.jsonl rows")

    ids, hashes, stems = {}, {}, {}
    stats = collections.Counter()

    for surface in SURFACES:
        sdir = ds / surface
        idx = json.loads((sdir / "index.json").read_text())
        cases = sorted((sdir / "cases").glob("*.json"))
        chk(len(cases) == idx["n"], f"{surface}: {len(cases)} cases vs index n={idx['n']}")

        loaded = discover_cases([sdir / "cases"])
        chk(len(loaded) == len(cases),
            f"{surface}: discover_cases loaded {len(loaded)}/{len(cases)} (id dedup loss)")
        for cs in loaded:
            cls = classify_case(cs)
            chk(cls["runnable"], f"{surface}/{cs.case_id}: not runnable ({cls['reason']})")
            chk(cls["dimension"] == "trigger", f"{surface}/{cs.case_id}: dimension {cls['dimension']}")
            stem = case_run_dir(Path("/x"), "eval_model", "m", cs)
            chk(stem not in stems, f"{cs.case_id}: run-dir collision with {stems.get(stem)}")
            stems[stem] = cs.case_id

        for row in idx["samples"]:
            sid = row["id"]
            stats["samples"] += 1
            chk(sid not in ids, f"duplicate id {sid}")
            ids[sid] = surface
            chk(not CLAW_RE.search(sid), f"{sid}: harness name in id")

            case = json.loads((sdir / "cases" / f"{sid}.json").read_text())
            chk(case["id"] == sid, f"{sid}: case id mismatch")
            chk("source_claw" not in case, f"{sid}: case still has source_claw")
            for why in validation.case_violations(case.get("verification")):
                chk(False, f"{sid}: unevaluable check — {why}")
            env = json.loads((sdir / "env" / sid / "env.json").read_text())
            chk(env["id"] == sid and env["surface"] == surface, f"{sid}: env.json id/surface")
            chk("provenance" not in env, f"{sid}: env.json still has provenance")
            chk(env["task"] == case["benign_user_goal"], f"{sid}: task != case benign_user_goal")
            chk(env["verification"] == case.get("verification", {}), f"{sid}: oracle drift")

            h = task_hash(case["benign_user_goal"], case["attacker_instruction"])
            chk(h not in hashes, f"DUPLICATE TASK {sid} == {hashes.get(h)}")
            hashes[h] = sid

            # rootfs integrity + byte-identity with the collection
            root = sdir / "env" / sid / "rootfs"
            on_disk = {"/" + f.relative_to(root).as_posix(): sha256_file(f)
                       for f in sorted(root.rglob("*")) if f.is_file()}
            chk(on_disk == {f["container_path"]: f["sha256"] for f in env["rootfs"]["files"]},
                f"{sid}: rootfs != hashes in env.json")
            stats["rootfs_files"] += len(on_disk)
            # Audit-only cross-check against the original collection rootfs; skipped
            # when PROVENANCE is absent or the collection is not local.
            if sid in prov:
                old = prov[sid].get("original_sample_id")
                if old:
                    src_root = SRC / "trigger" / surface / "env" / old / "rootfs"
                else:
                    src_root = PROJECT_ROOT / str(prov[sid].get("collection_artifacts", "")) / "rootfs"
                if src_root.is_dir():
                    chk(on_disk == {"/" + f.relative_to(src_root).as_posix(): sha256_file(f)
                                    for f in sorted(src_root.rglob("*")) if f.is_file()},
                        f"{sid}: rootfs != collection rootfs")

            # memory poison: present iff the case declares one, logical target only
            ms = env["memory_surface"]
            if ms:
                stats["with_memory_poison"] += 1
                pf = sdir / "env" / sid / ms["file"]
                chk(pf.exists() and sha256_file(pf) == ms["sha256"], f"{sid}: poison hash")
                chk("resolved_container_path" not in ms, f"{sid}: per-harness path table left in")
                want = SURFACE_FILE.get(surface)
                chk(want is None or ms["logical_target"].lower() == want,
                    f"{sid}: surface {surface} but logical_target {ms['logical_target']}")
                chk(not CLAW_RE.search(pf.read_text(errors="replace")),
                    f"{sid}: poison text mentions a harness")
            else:
                chk(surface in ("environment", "skill_md"),
                    f"{sid}: no memory poison on surface {surface}")

            if surface == "skill_md":
                declared = case.get("environment", {}).get("skill_surfaces")
                chk(isinstance(declared, list) and declared,
                    f"{sid}: skill_md case has no environment.skill_surfaces")
                ss = env.get("skill_surfaces")
                chk(isinstance(ss, dict) and ss.get("dir") == "skills",
                    f"{sid}: env.json has no static skill_surfaces descriptor")
                entries = ss.get("entries") if isinstance(ss, dict) else None
                chk(isinstance(entries, list) and len(entries) == len(declared or []),
                    f"{sid}: static skill count does not match case")
                for entry in entries or []:
                    sf = entry.get("skill_file") if isinstance(entry, dict) else None
                    rel = sf.get("path") if isinstance(sf, dict) else None
                    skill_path = sdir / "env" / sid / str(rel or "")
                    chk(skill_path.exists(), f"{sid}: missing static SKILL.md {rel}")
                    if skill_path.exists():
                        chk(sha256_file(skill_path) == sf.get("sha256"), f"{sid}: SKILL.md hash {rel}")
                        stats["with_skill_surface"] += 1
                    for script in entry.get("scripts", []) if isinstance(entry, dict) else []:
                        rel = script.get("path") if isinstance(script, dict) else None
                        script_path = sdir / "env" / sid / str(rel or "")
                        chk(script_path.exists(), f"{sid}: missing static skill script {rel}")
                        if script_path.exists():
                            chk(sha256_file(script_path) == script.get("sha256"),
                                f"{sid}: skill script hash {rel}")

            # web: every url_map page present, hash-matched, non-empty
            if env["web"]:
                stats["with_web"] += 1
                um = json.loads((sdir / "env" / sid / env["web"]["url_map"]).read_text())
                chk(set(um) == set(env["web"]["pages"]), f"{sid}: url_map vs pages mismatch")
                for url, e in um.items():
                    f = sdir / "env" / sid / env["web"]["dir"] / e["file"]
                    chk(f.exists() and f.stat().st_size > 0, f"{sid}: missing/empty page {e['file']}")
                    if f.exists():
                        chk(sha256_file(f) == env["web"]["pages"][url]["sha256"], f"{sid}: page hash {url}")
                chk(env["needs_sink"] is True, f"{sid}: has web but needs_sink false")

            chk(row["has_web"] == (env["web"] is not None), f"{sid}: index has_web mismatch")
            chk(row["has_memory_poison"] == (ms is not None), f"{sid}: index has_memory_poison")

    # harness-neutrality sweep over every shipped byte except PROVENANCE
    leaks = 0
    sample_files = (
        path
        for surface in SURFACES
        for path in (ds / surface).rglob("*")
        if path.is_file()
    )
    for path in sorted(sample_files):
        rel = path.relative_to(ds).as_posix()
        if CLAW_RE.search(rel):
            fail.append(f"{rel}: harness name in path"); leaks += 1
        # An unreadable file is a HOLE in the neutrality proof, not a clean
        # file: it may or may not name a harness and there is no way to know.
        # Recorded as a finding rather than raised, so one bad file does not
        # abort the sweep and hide every other result.
        try:
            text = path.read_text(errors="replace")
        except Exception as exc:
            fail.append(f"{rel}: UNSCANNABLE ({type(exc).__name__}: {exc})"); leaks += 1
            continue
        for m in set(x.lower() for x in CLAW_RE.findall(text)):
            fail.append(f"{rel}: mentions {m}"); leaks += 1
    for name in ("samples.jsonl", "dataset.json"):
        for m in set(x.lower() for x in CLAW_RE.findall((ds / name).read_text())):
            fail.append(f"{name}: mentions {m}"); leaks += 1

    chk(len(hashes) == stats["samples"], "task texts not globally distinct")
    chk(stats["samples"] == len(rows), "index rows vs surface indexes disagree")

    print(f"samples audited     : {stats['samples']}")
    print(f"  unique ids        : {len(ids)}")
    print(f"  unique tasks      : {len(hashes)}")
    print(f"  unique run stems  : {len(stems)}")
    print(f"  rootfs files      : {stats['rootfs_files']}")
    print(f"  with memory poison: {stats['with_memory_poison']}")
    print(f"  with skill surface: {stats['with_skill_surface']}")
    print(f"  with frozen web   : {stats['with_web']}")
    print(f"harness references in shipped samples: {leaks}")
    print("FAILURES:", len(fail))
    for f in fail[:30]:
        print("  -", f)
    return 1 if fail else 0
