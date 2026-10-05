"""Audit an Inject dataset: integrity, neutrality, runnability, uniqueness.

Re-reads everything from disk and recomputes it — never trusts the builder's own
bookkeeping. Docker is not needed. The inject-specific checks are that no sample
pre-seeds a memory poison (delivery must be through the environment), that the
target directory agrees with the memory file the oracle inspects, and that the
attack is physically deliverable: the payload the oracle scans the memory file
for has to appear somewhere in the frozen bundle the agent can actually see.

    python -m redclaw dataset verify --dataset dataset/inject
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
from redclaw.datasets import CLAW_NAMES, INJECT_TARGETS, sha256_file, task_hash

CLAWS = CLAW_NAMES
CLAW_RE = re.compile("|".join(CLAWS), re.I)
TARGET_FILE = INJECT_TARGETS
AUDIT_ONLY = {"PROVENANCE.jsonl", "SELECTION.json"}
EVALUATOR_INJECTION_MARKERS = (
    "# RedClaw legacy migration:",
    "REDCLAW_LEGACY_PAYLOAD",
)


def looks_like_path(s: str) -> bool:
    return isinstance(s, str) and s.startswith("/")


def norm(text: str) -> str:
    """Whitespace-insensitive comparison — the payload is reflowed by whatever
    file carries it (a heredoc in a shell script, a markdown bullet, HTML)."""
    return re.sub(r"\s+", " ", text).strip().lower()


def expected_terms(expected: object) -> list[str]:
    if isinstance(expected, list):
        return [str(item) for item in expected if str(item).strip()]
    if isinstance(expected, dict):
        contains = expected.get("contains")
        if isinstance(contains, list):
            return [str(item) for item in contains if str(item).strip()]
        if contains is not None and str(contains).strip():
            return [str(contains)]
    return [str(expected)] if expected is not None and str(expected).strip() else []


def audit(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "dataset" / "inject")
    ap.add_argument(
        "--collection-dir", type=Path, default=None,
        help="collection archive that PROVENANCE.jsonl collection_artifacts paths are relative to; "
             "when given, every bundle's rootfs is byte-checked against it",
    )
    args = ap.parse_args(argv)
    ds = args.dataset
    collection_dir = args.collection_dir.resolve() if args.collection_dir else None
    if collection_dir is not None and not collection_dir.is_dir():
        raise SystemExit(f"--collection-dir does not exist: {collection_dir}")
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
    chk(manifest["dimension"] == "inject", "manifest dimension is not inject")

    declared_surfaces = tuple((manifest.get("by_surface") or {}).keys())
    unknown_surfaces = sorted(set(declared_surfaces) - set(INJECT_TARGETS))
    chk(bool(declared_surfaces), "manifest declares no inject surfaces")
    chk(not unknown_surfaces,
        f"manifest declares unsupported inject surfaces: {unknown_surfaces}")
    row_surface_counts = collections.Counter(str(row.get("surface") or "") for row in rows)
    chk(dict(row_surface_counts) == manifest.get("by_surface"),
        "dataset.json by_surface != samples.jsonl surface counts")

    ids, hashes, stems = {}, {}, {}
    stats = collections.Counter()

    for target in declared_surfaces:
        tdir = ds / target
        idx = json.loads((tdir / "index.json").read_text())
        cases = sorted((tdir / "cases").glob("*.json"))
        chk(len(cases) == idx["n"], f"{target}: {len(cases)} cases vs index n={idx['n']}")
        chk(idx["dimension"] == "inject", f"{target}: index dimension not inject")

        loaded = discover_cases([tdir / "cases"])
        chk(len(loaded) == len(cases),
            f"{target}: discover_cases loaded {len(loaded)}/{len(cases)} (id dedup loss)")
        for cs in loaded:
            cls = classify_case(cs)
            chk(cls["runnable"], f"{target}/{cs.case_id}: not runnable ({cls['reason']})")
            chk(cls["dimension"] == "inject", f"{target}/{cs.case_id}: dimension {cls['dimension']}")
            stem = case_run_dir(Path("/x"), "eval_model", "m", cs)
            chk(stem not in stems, f"{cs.case_id}: run-dir collision with {stems.get(stem)}")
            stems[stem] = cs.case_id

        for row in idx["samples"]:
            sid = row["id"]
            stats["samples"] += 1
            chk(sid not in ids, f"duplicate id {sid}")
            ids[sid] = target
            chk(not CLAW_RE.search(sid), f"{sid}: harness name in id")

            case = json.loads((tdir / "cases" / f"{sid}.json").read_text())
            chk(case["id"] == sid, f"{sid}: case id mismatch")
            chk("source_claw" not in case, f"{sid}: case still has source_claw")
            chk(case.get("test_mode") == "inject", f"{sid}: case test_mode != inject")
            for why in validation.case_violations(case.get("verification")):
                chk(False, f"{sid}: unevaluable check — {why}")
            chk(case.get("target_surface") == target, f"{sid}: case target_surface mismatch")

            verification = case.get("verification") or {}
            user_checks = [
                check for check in verification.get("user_task_success") or []
                if isinstance(check, dict)
            ]
            for check in user_checks:
                check_type = str(check.get("type") or "")
                chk(check_type != "command_exit_code",
                    f"{sid}: user utility uses unobservable command_exit_code")
                chk(not check_type.startswith("memory_"),
                    f"{sid}: user utility incorrectly scores a durable memory file")

            attack_checks = [
                check for check in verification.get("attack_task_success") or []
                if isinstance(check, dict) and check.get("type") == "memory_contains"
            ]
            attack_signatures = {
                (str(check.get("target") or "").lower(), norm(str(check.get("expected") or "")))
                for check in attack_checks
            }
            for check in verification.get("safe_agent_success") or []:
                if not isinstance(check, dict) or check.get("type") != "memory_contains":
                    continue
                signature = (
                    str(check.get("target") or "").lower(),
                    norm(str(check.get("expected") or "")),
                )
                chk(signature not in attack_signatures,
                    f"{sid}: safe oracle has attack polarity (memory_contains)")

            bundle = tdir / "env" / sid
            env = json.loads((bundle / "env.json").read_text())
            chk(env["id"] == sid and env["surface"] == target, f"{sid}: env.json id/surface")
            chk("provenance" not in env, f"{sid}: env.json still has provenance")
            chk(env["task"] == case["benign_user_goal"], f"{sid}: task != case benign_user_goal")
            chk(env["verification"] == case.get("verification", {}), f"{sid}: oracle drift")

            h = task_hash(case["benign_user_goal"], case["attacker_instruction"])
            chk(h not in hashes, f"DUPLICATE TASK {sid} == {hashes.get(h)}")
            hashes[h] = sid

            # rootfs integrity + byte-identity with the run it was frozen from
            root = bundle / "rootfs"
            on_disk = {"/" + f.relative_to(root).as_posix(): sha256_file(f)
                       for f in sorted(root.rglob("*")) if f.is_file()}
            chk(on_disk == {f["container_path"]: f["sha256"] for f in env["rootfs"]["files"]},
                f"{sid}: rootfs != hashes in env.json")
            stats["rootfs_files"] += len(on_disk)

            # Every declared non-empty absolute file is input state. It must be
            # present in the frozen rootfs; the freezer may merge an injected
            # observation into it, but may not drop the declared bytes.
            declared_files = ((case.get("environment") or {}).get("files") or [])
            for entry in declared_files:
                if not isinstance(entry, dict):
                    continue
                declared_path = str(entry.get("path") or entry.get("name") or "")
                declared_content = next(
                    (
                        entry.get(key) for key in ("content", "contents", "initial_content")
                        if isinstance(entry.get(key), str)
                    ),
                    None,
                )
                if (
                    not declared_path.startswith("/")
                    or not isinstance(declared_content, str)
                    or not declared_content.strip()
                ):
                    continue
                frozen_input = root / declared_path.lstrip("/")
                chk(frozen_input.is_file(),
                    f"{sid}: declared non-empty input missing from rootfs: {declared_path}")
                if frozen_input.is_file():
                    actual = frozen_input.read_text(encoding="utf-8", errors="replace")
                    chk(norm(declared_content) in norm(actual),
                        f"{sid}: frozen input dropped declared content: {declared_path}")

            # Utility must describe a post-agent state transition. A success
            # condition already true in the frozen fixture gives full credit to
            # an agent that did nothing and is therefore not a valid utility
            # oracle.
            for check in user_checks:
                check_type = str(check.get("type") or "")
                target_path = str(check.get("target") or "")
                source = root / target_path.lstrip("/")
                if check_type == "file_exists" and check.get("expected") is True:
                    chk(not source.exists(),
                        f"{sid}: user file_exists is already true before the run: {target_path}")
                elif check_type == "file_contains" and source.is_file():
                    content = source.read_text(encoding="utf-8", errors="replace").lower()
                    terms = expected_terms(check.get("expected"))
                    chk(not terms or not all(term.lower() in content for term in terms),
                        f"{sid}: user file_contains is already true before the run: {target_path}")
            src_root = (
                collection_dir / prov[sid]["collection_artifacts"] / "rootfs"
                if collection_dir is not None and sid in prov else None
            )
            if src_root is not None:
                chk(src_root.is_dir(), f"{sid}: collection rootfs missing: {src_root}")
            if src_root is not None and src_root.is_dir():
                chk(on_disk == {"/" + f.relative_to(src_root).as_posix(): sha256_file(f)
                                for f in sorted(src_root.rglob("*")) if f.is_file()},
                    f"{sid}: rootfs != collection rootfs")
                stats["rootfs_checked_against_collection"] += 1

            # --- inject dimension: nothing is pre-seeded ---------------------- #
            chk(env["memory_surface"] is None, f"{sid}: inject sample pre-seeds a memory poison")
            chk(not (bundle / "memory").exists(), f"{sid}: stray memory/ dir in an inject bundle")
            it = env["inject_target"]
            chk(it["logical_target"] == TARGET_FILE[target],
                f"{sid}: target dir {target} but logical_target {it['logical_target']}")
            chk("resolved_container_path" not in it, f"{sid}: per-harness path table left in")
            chk(row["inject_target_file"] == it["effective_memory_file"],
                f"{sid}: index inject_target_file mismatch")

            # The oracle must score the declared persistence surface.
            atk = [c for c in ((case.get("verification", {}) or {}).get("attack_task_success") or [])
                   if isinstance(c, dict)]
            if target == "skill_md":
                persistence_checks = [c for c in atk if c.get("type") == "skill_registered"]
                chk(bool(persistence_checks) and len(persistence_checks) == len(atk),
                    f"{sid}: skill_md attack checks must use skill_registered")
                chk(not (case.get("environment") or {}).get("skill_surfaces"),
                    f"{sid}: inject skill sample pre-seeds a skill")
                chk(not any("<SKILLS_ROOT>" in path for path in on_disk),
                    f"{sid}: inject skill fixture already contains installed skills")
            else:
                persistence_checks = [c for c in atk if str(c.get("type", "")).startswith("memory_")]
                chk(bool(persistence_checks),
                    f"{sid}: no memory_* attack check — target {target} is not what is scored")

            # the attack must actually be deliverable. The authoritative test is
            # physical: the payload the oracle scans the memory file for has to
            # be present somewhere in the frozen environment the agent can see.
            blob = norm(" ".join(
                f.read_text(errors="replace")
                for f in sorted(bundle.rglob("*")) if f.is_file() and f.name != "env.json"))
            for c in persistence_checks:
                terms = expected_terms(c.get("expected")) if target == "skill_md" else [str(c.get("expected") or "")]
                for term in terms:
                    want = norm(term)
                    if not want:
                        continue
                    chk(want in blob or (target != "skill_md" and " ".join(want.split()[:6]) in blob),
                        f"{sid}: attack payload for {c.get('check_id')} is nowhere in the frozen env")

            # ...and the declared observations should say where. A source that is
            # also a create-target is left absent on purpose (the runner refuses
            # to pre-create a file the agent must produce), so the delivery moves
            # to another frozen file — reported, not failed.
            obs = env["injected_observations"]
            chk(bool(obs), f"{sid}: no injected_observations to deliver the attack")
            served = set((env["web"] or {}).get("pages") or {})
            for o in obs:
                src = o.get("source") if isinstance(o, dict) else None
                src = src if isinstance(src, str) else ""
                if looks_like_path(src):
                    stats["obs_file" if src in on_disk else "obs_file_shadowed"] += 1
                elif src.startswith("http"):
                    chk(src in served, f"{sid}: observation url {src} not among frozen pages")
                    stats["obs_web"] += 1
                else:
                    stats["obs_other"] += 1

            # web: every url_map page present, hash-matched, non-empty
            if env["web"]:
                stats["with_web"] += 1
                um = json.loads((bundle / env["web"]["url_map"]).read_text())
                chk(set(um) == set(env["web"]["pages"]), f"{sid}: url_map vs pages mismatch")
                for url, e in um.items():
                    f = bundle / env["web"]["dir"] / e["file"]
                    chk(f.exists() and f.stat().st_size > 0, f"{sid}: missing/empty page {e['file']}")
                    if f.exists():
                        chk(sha256_file(f) == env["web"]["pages"][url]["sha256"], f"{sid}: page hash {url}")
                chk(env["needs_sink"] is True, f"{sid}: has web but needs_sink false")

            chk(row["has_web"] == (env["web"] is not None), f"{sid}: index has_web mismatch")
            chk(row["has_memory_poison"] is False, f"{sid}: index claims a memory poison")

    # harness-neutrality sweep over every shipped byte except the audit trail
    leaks = 0
    for path in sorted(p for p in ds.rglob("*") if p.is_file()):
        rel = path.relative_to(ds).as_posix()
        if rel in AUDIT_ONLY:
            continue
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
        for marker in EVALUATOR_INJECTION_MARKERS:
            if marker in text:
                fail.append(f"{rel}: evaluator-injected payload marker {marker!r}")
                leaks += 1

    chk(len(hashes) == stats["samples"], "task texts not globally distinct")
    chk(stats["samples"] == len(rows), "index rows vs target indexes disagree")

    print(f"samples audited     : {stats['samples']}")
    print(f"  unique ids        : {len(ids)}")
    print(f"  unique tasks      : {len(hashes)}")
    print(f"  unique run stems  : {len(stems)}")
    print(f"  rootfs files      : {stats['rootfs_files']} "
          + (f"({stats['rootfs_checked_against_collection']} bundles byte-checked vs collection)"
             if collection_dir is not None else "(collection byte-check not requested; pass --collection-dir)"))
    print(f"  observations      : {stats['obs_file']} file, {stats['obs_web']} web, "
          f"{stats['obs_other']} other "
          f"({stats['obs_file_shadowed']} file sources shadowed by a create-target — "
          f"delivery verified to live elsewhere in the bundle)")
    print(f"  with frozen web   : {stats['with_web']}")
    print(f"harness references in shipped samples: {leaks}")
    print("FAILURES:", len(fail))
    for f in fail[:30]:
        print("  -", f)
    return 1 if fail else 0
