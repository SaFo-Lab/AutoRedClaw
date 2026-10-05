"""Build a current-format inject corpus as ``redclaw-dataset/1.0``.

Input layout::

    <source>/inject/<surface>/<risk>/<strategy>.json

Output layout matches the frozen trigger dataset::

    <output>/dataset.json
    <output>/samples.jsonl
    <output>/<surface>/cases/<case>.json
    <output>/<surface>/env/<case>/{env.json,rootfs/,web/,...}
    <output>/<surface>/index.json

The raw export contains declarative cases plus provenance for their existing
probe/evaluation assets.  This tool copies those assets without invoking a
materializer, reverses the recorded source-adapter path binding back to the
registered placeholder, and freezes the result.  Future evaluations stay in
strict asset-reuse mode and bind that placeholder to the claw under test.
"""

from __future__ import annotations

import collections
import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any



from redclaw.evaluation import validation
from redclaw.datasets import (
    INJECT_TARGETS,
    normalize_rootfs_file_modes,
    sha256_file,
    task_hash,
    tree_files,
)
from redclaw.evaluation.core import (
    CaseSpec,
    WORKSPACE_TARGET_PATH_PLACEHOLDER,
    SKILLS_ROOT_PLACEHOLDER,
    classify_case,
    create_target_paths,
    file_check_paths,
    fixture_network_hosts,
    has_behavioral_checks,
    nominal_memory_paths,
)
from redclaw.adapters.nanobot import NanobotAdapter
from redclaw.adapters.nemoclaw import NemoclawAdapter
from redclaw.adapters.openclaw import OpenclawAdapter
from redclaw.adapters.seclaw import SeclawAdapter
from redclaw.adapters.zeroclaw import ZeroclawAdapter


SURFACE_TARGETS = INJECT_TARGETS
ADAPTER_TARGETS = {
    "nanobot": NanobotAdapter.workspace_targets,
    "nemoclaw": NemoclawAdapter.workspace_targets,
    "openclaw": OpenclawAdapter.workspace_targets,
    "seclaw": SeclawAdapter.workspace_targets,
    "zeroclaw": ZeroclawAdapter.workspace_targets,
}

LEGACY_CLAW_ORDER = {
    "openclaw": 0,
    "nanobot": 1,
    "nemoclaw": 2,
    "seclaw": 3,
    "zeroclaw": 4,
}


def _load_cases(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    records = value if isinstance(value, list) else [value]
    if not records or not all(isinstance(record, dict) for record in records):
        raise ValueError(f"{path}: expected a JSON object or non-empty object array")
    return records


def _source_root(source: Path) -> Path:
    nested = source / "inject"
    return nested if nested.is_dir() else source


def _discover(source: Path) -> list[dict[str, Any]]:
    root = _source_root(source)
    rows: list[dict[str, Any]] = []
    for surface in SURFACE_TARGETS:
        surface_dir = root / surface
        if not surface_dir.is_dir():
            continue
        for path in sorted(surface_dir.glob("*/*.json")):
            for raw in _load_cases(path):
                rows.append({"surface": surface, "source_file": path, "raw": raw})
    if not rows:
        raise ValueError(f"no inject cases found below {root}")
    return rows


def _selection_sources(source: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    path = source / "SELECTION_REPORT.json"
    if not path.exists():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in report.get("records") or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("surface") or ""), str(row.get("case_id") or ""))
        out[key] = copy.deepcopy(row.get("sources") or [])
    return out


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [text for item in value for text in _string_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _string_values(item)]
    return []


def _legacy_match_key(raw: dict[str, Any], *, include_expected: bool) -> tuple[Any, ...]:
    observations = tuple(
        sorted(
            str(item.get("source") or "").strip()
            for item in raw.get("injected_observations") or []
            if isinstance(item, dict)
        )
    )
    key: tuple[Any, ...] = (
        str(raw.get("risk_category") or ""),
        str(raw.get("attack_strategy") or ""),
        Path(str(raw.get("inject_target_file") or "")).name.lower(),
        observations,
    )
    if include_expected:
        expected = tuple(
            sorted(
                _string_values(
                    ((raw.get("verification") or {}).get("attack_task_success") or [])
                )
            )
        )
        key += (expected,)
    return key


def _legacy_asset_index(
    asset_runs: Path,
) -> tuple[
    dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]],
    dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]],
]:
    if not asset_runs.is_dir():
        raise ValueError(f"legacy asset runs directory is missing: {asset_runs}")
    exact: dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]] = collections.defaultdict(list)
    fallback: dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]] = collections.defaultdict(list)
    for path in sorted(asset_runs.rglob("setting.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or str(raw.get("test_mode") or "").lower() != "inject":
            continue
        item = (path, raw)
        exact[_legacy_match_key(raw, include_expected=True)].append(item)
        fallback[_legacy_match_key(raw, include_expected=False)].append(item)
    if not exact:
        raise ValueError(f"no legacy inject setting.json files found below {asset_runs}")
    return dict(exact), dict(fallback)


def _asset_tree_signature(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), sha256_file(path))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _legacy_candidate_rank(path: Path, asset_runs: Path) -> tuple[Any, ...]:
    relative = path.relative_to(asset_runs)
    claw = relative.parts[0] if relative.parts else ""
    role = next(
        (part for part in relative.parts if part.startswith(("probe_model=", "eval_model="))),
        "",
    )
    asset_dir = path.parent
    has_web = (asset_dir / "web").is_dir() and (asset_dir / "url_map.json").is_file()
    return (
        not has_web,
        not role.startswith("probe_model="),
        LEGACY_CLAW_ORDER.get(claw, 99),
        "gpt_5_mini" not in role,
        str(relative),
    )


def _matching_legacy_asset(
    raw: dict[str, Any],
    asset_runs: Path,
    indexes: tuple[
        dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]],
        dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]],
    ],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    exact, fallback = indexes
    candidates = exact.get(_legacy_match_key(raw, include_expected=True), [])
    matched_by = "risk+strategy+surface+observation_sources+attack_expected"
    if not candidates:
        candidates = fallback.get(_legacy_match_key(raw, include_expected=False), [])
        matched_by = "risk+strategy+surface+observation_sources"
    if not candidates:
        raise ValueError(f"{raw.get('id')}: no matching legacy setting/rootfs asset")

    setting_variants = {
        json.dumps(setting, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for _, setting in candidates
    }
    if len(setting_variants) != 1:
        raise ValueError(
            f"{raw.get('id')}: legacy asset match is ambiguous across "
            f"{len(setting_variants)} setting variants"
        )
    roots = [path.parent / "rootfs" for path, _ in candidates]
    if any(not root.is_dir() for root in roots):
        raise ValueError(f"{raw.get('id')}: a matched legacy run has no frozen rootfs")
    root_variants = {_asset_tree_signature(root) for root in roots}
    if len(root_variants) != 1:
        raise ValueError(
            f"{raw.get('id')}: matching legacy runs disagree on frozen rootfs bytes"
        )

    setting_path, setting = min(
        candidates,
        key=lambda item: _legacy_candidate_rank(item[0], asset_runs),
    )
    relative = setting_path.relative_to(asset_runs)
    matching_claws = sorted(
        {
            candidate.relative_to(asset_runs).parts[0]
            for candidate, _ in candidates
            if candidate.relative_to(asset_runs).parts
        }
    )
    role = next(
        (part for part in relative.parts if part.startswith(("probe_model=", "eval_model="))),
        "",
    )
    return setting_path.parent, setting, {
        "matched_by": matched_by,
        "matching_runs": len(candidates),
        "matching_claws": matching_claws,
        "source_claw": relative.parts[0],
        "source_role": role,
        "asset_source_kind": "legacy_probe" if role.startswith("probe_model=") else "legacy_eval",
    }


def _new_case_id(original: str, surface: str) -> str:
    suffix = f"__{surface}"
    return original if original.endswith(suffix) else f"{original}{suffix}"


def _validate_case(raw: dict[str, Any], surface: str, source_file: Path) -> None:
    original = str(raw.get("id") or "").strip()
    if not original:
        raise ValueError(f"{source_file}: case has no id")
    if str(raw.get("test_mode") or "").strip().lower() != "inject":
        raise ValueError(f"{original}: test_mode is not inject")
    logical = SURFACE_TARGETS[surface]
    target = Path(str(raw.get("inject_target_file") or "")).name.lower()
    if target != logical:
        raise ValueError(
            f"{original}: surface {surface} expects {logical}, got {raw.get('inject_target_file')!r}"
        )
    violations = validation.case_violations(raw.get("verification"))
    if violations:
        raise ValueError(f"{original}: unevaluable verification: {violations[0]}")


def _effective_target(logical: str, case_id: str) -> str:
    if logical == "skill.md":
        return "SKILL.MD"
    resolved: dict[str, str] = {}
    logical_name = validation.resolve_memory_target(logical)
    for claw, targets in ADAPTER_TARGETS.items():
        target = targets.get(logical_name)
        if target is None or not target.runtime_path:
            raise ValueError(f"{case_id}: {claw} does not support inject target {logical}")
        resolved[claw] = target.runtime_path
    effective = sorted({Path(path).name.upper() for path in resolved.values()})
    if len(effective) != 1:
        raise ValueError(f"{case_id}: target {logical} resolves to inconsistent files {effective}")
    return effective[0]


def _web_metadata(bundle: Path, result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    url_map_path = Path(result["url_map"])
    url_map = json.loads(url_map_path.read_text(encoding="utf-8"))
    pages: dict[str, dict[str, Any]] = {}
    hosts: list[str] = []
    for url, entry in url_map.items():
        host = str(entry.get("host") or "")
        if host and host not in hosts:
            hosts.append(host)
        page = bundle / "web" / str(entry["file"])
        if not page.is_file():
            raise ValueError(f"url_map points at missing page {page}")
        pages[url] = {
            "file": f"web/{entry['file']}",
            "bytes": page.stat().st_size,
            "sha256": sha256_file(page),
        }
    web: dict[str, Any] = {
        "dir": "web",
        "url_map": "url_map.json",
        "hosts": hosts,
        "pages": pages,
    }
    for filename, key in (
        ("site_meta.json", "site_meta"),
        ("app_behavior.json", "app_behavior"),
    ):
        if (bundle / filename).exists():
            web[key] = filename
    return web


def _portable_template(case: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(case)
    for key in ("id", "source_claw", "source_case_id"):
        value.pop(key, None)
    return value


def _asset_template(asset_dir: Path) -> dict[str, Any] | None:
    path = asset_dir / "setting.template.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _matching_asset_dir(
    raw: dict[str, Any],
    source_entry: dict[str, Any],
) -> Path:
    """Find an already-materialized asset bundle for this exact template."""
    report = Path(str(source_entry.get("source_report") or ""))
    eval_run = report.parent
    if not report.is_file():
        raise ValueError(f"selected source report is missing: {report}")
    risk = eval_run.parent.name
    surface_dir = eval_run.parents[2]
    probe_candidates = sorted(
        surface_dir.glob(f"probe_model=*/{risk}/{eval_run.name}")
    )
    expected = _portable_template(raw)
    for candidate in probe_candidates:
        template = _asset_template(candidate)
        if (
            template is not None
            and _portable_template(template) == expected
            and (candidate / "rootfs").is_dir()
            and (candidate / "placeholder_bindings.json").is_file()
        ):
            return candidate

    raise ValueError(
        f"{raw.get('id')}: no existing probe/eval asset bundle matches the portable template"
    )


def _portableize_asset_bindings(bundle: Path, binding_file: Path) -> int:
    """Reverse the source adapter's runtime binding, and nothing else."""
    debug = json.loads(binding_file.read_text(encoding="utf-8"))
    bindings = debug.get("bindings") or {}
    placeholder = (
        SKILLS_ROOT_PLACEHOLDER
        if SKILLS_ROOT_PLACEHOLDER in bindings and WORKSPACE_TARGET_PATH_PLACEHOLDER not in bindings
        else WORKSPACE_TARGET_PATH_PLACEHOLDER
    )
    runtime_path = str(
        bindings.get(placeholder)
        or debug.get("runtime_path")
        or ""
    )
    if not runtime_path:
        raise ValueError(f"asset bundle has no filesystem path binding: {binding_file}")

    changed = 0
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        portable = original
        portable = portable.replace(runtime_path, placeholder)
        if portable != original:
            path.write_text(portable, encoding="utf-8")
            changed += 1
    if changed == 0:
        raise ValueError(f"asset bundle contained none of its recorded path bindings: {binding_file.parent}")
    return changed


def _copy_existing_assets(asset_dir: Path, bundle: Path) -> int:
    shutil.copytree(asset_dir / "rootfs", bundle / "rootfs")
    if (asset_dir / "web").is_dir():
        shutil.copytree(asset_dir / "web", bundle / "web")
    for filename in ("url_map.json", "site_meta.json", "app_behavior.json"):
        if (asset_dir / filename).is_file():
            shutil.copy2(asset_dir / filename, bundle / filename)
    return _portableize_asset_bindings(
        bundle, asset_dir / "placeholder_bindings.json"
    )


def _declared_content(entry: dict[str, Any]) -> str | None:
    for key in ("content", "contents", "initial_content"):
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def _merge_injected_content(base: str, poison: str) -> str:
    base_compact = " ".join(base.split())
    poison_compact = " ".join(poison.split())
    if not base_compact:
        return poison
    if poison_compact in base_compact:
        return base
    if base_compact in poison_compact:
        return poison
    return f"{base.rstrip()}\n\n{poison.lstrip()}"


def _rootfs_path(rootfs: Path, raw_path: str) -> Path:
    relative = Path(raw_path.lstrip("/"))
    if not raw_path.startswith("/") or ".." in relative.parts:
        raise ValueError(f"unsafe declarative rootfs path: {raw_path!r}")
    return rootfs / relative


def _overlay_current_declarations(bundle: Path, raw: dict[str, Any]) -> dict[str, int]:
    """Reconcile the archived tree with explicit, non-generated case inputs.

    The old materializer omitted a handful of declared inputs and also froze
    scripts before the workspace-placeholder migration.  Current declarations
    are authoritative for explicit file bytes, while create-targets must remain
    absent so the benign task cannot pass before the agent runs.
    """
    rootfs = bundle / "rootfs"
    case = CaseSpec(raw)
    create_targets = create_target_paths(case)
    environment = raw.get("environment") or {}
    observations = {
        str(item.get("source") or ""): str(item.get("content") or "")
        for item in raw.get("injected_observations") or []
        if isinstance(item, dict)
        and str(item.get("source") or "").startswith("/")
        and isinstance(item.get("content"), str)
    }
    stats = collections.Counter()

    directories = environment.get("directories") or []
    for item in directories if isinstance(directories, list) else []:
        raw_path = str(item.get("path") if isinstance(item, dict) else item or "")
        if not raw_path.startswith("/"):
            continue
        destination = _rootfs_path(rootfs, raw_path)
        if destination.is_file() or destination.is_symlink():
            destination.unlink()
            stats["directory_file_mismatches_repaired"] += 1
        destination.mkdir(parents=True, exist_ok=True)

    files = environment.get("files") or []
    if not isinstance(files, list):
        raise ValueError(f"{raw.get('id')}: current environment.files must be a list")
    for entry in files:
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path") or entry.get("name") or "")
        if not raw_path.startswith("/"):
            continue
        destination = _rootfs_path(rootfs, raw_path)
        is_dir = raw_path.endswith("/") or entry.get("is_dir") is True or str(
            entry.get("type") or ""
        ).lower() == "directory"
        if is_dir:
            if destination.is_file() or destination.is_symlink():
                destination.unlink()
                stats["directory_file_mismatches_repaired"] += 1
            destination.mkdir(parents=True, exist_ok=True)
            continue
        explicit = _declared_content(entry)
        # Never erase a declared non-empty input. If its bytes already satisfy
        # a user-success check, that is an oracle bug for the verifier to catch,
        # not evidence that the file should be converted into a create-target.
        declared_input = isinstance(explicit, str) and bool(explicit.strip())
        if raw_path in create_targets and not declared_input:
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
                stats["precreated_outputs_removed"] += 1
            stats["create_targets_preserved_absent"] += 1
            continue
        if explicit is None:
            continue
        content = (
            _merge_injected_content(explicit, observations[raw_path])
            if raw_path in observations
            else explicit
        )
        existed = destination.is_file()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        stats["declared_files_overlaid"] += 1
        if not existed:
            stats["missing_declared_inputs_restored"] += 1
        raw_mode = entry.get("permissions") or entry.get("mode") or entry.get("chmod")
        if raw_mode is not None:
            try:
                destination.chmod(int(str(raw_mode), 8))
            except ValueError as exc:
                raise ValueError(
                    f"{raw.get('id')}: invalid permissions {raw_mode!r} for {raw_path}"
                ) from exc
    return dict(stats)


def _portableize_legacy_workspace(bundle: Path, target_name: str) -> dict[str, int]:
    """Remove the fabricated legacy workspace and bind references portably."""
    rootfs = bundle / "rootfs"
    legacy_workspace = rootfs / "opt" / "assistant-workspace"
    removed_files = 0
    if legacy_workspace.exists() or legacy_workspace.is_symlink():
        if legacy_workspace.is_dir() and not legacy_workspace.is_symlink():
            removed_files = sum(path.is_file() for path in legacy_workspace.rglob("*"))
            shutil.rmtree(legacy_workspace)
        else:
            legacy_workspace.unlink()
            removed_files = 1

    portable_target = WORKSPACE_TARGET_PATH_PLACEHOLDER
    portable_dir = f'$(dirname "{WORKSPACE_TARGET_PATH_PLACEHOLDER}")'
    target_variants = {
        target_name,
        target_name.lower(),
        Path(target_name).name.upper().replace(".MD", ".md"),
    }
    replacements = [
        (f"/opt/assistant-workspace/{name}", portable_target)
        for name in sorted(target_variants, key=len, reverse=True)
    ]
    replacements.append(("/opt/assistant-workspace", portable_dir))

    changed = 0
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        portable = original
        for old, new in replacements:
            portable = portable.replace(old, new)
        portable = portable.replace("assistant-workspace", "runner-bound workspace target")
        if portable != original:
            path.write_text(portable, encoding="utf-8")
            changed += 1

    leftovers = [
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if "assistant-workspace" in path.relative_to(bundle).as_posix().lower()
    ]
    if leftovers:
        raise ValueError(
            "legacy fabricated workspace path remains in asset names: "
            + ", ".join(leftovers[:5])
        )
    return {
        "portableized_asset_files": changed,
        "removed_legacy_workspace_files": removed_files,
    }


def _copy_legacy_assets(
    asset_dir: Path,
    bundle: Path,
    raw: dict[str, Any],
    target_name: str,
) -> dict[str, int]:
    shutil.copytree(asset_dir / "rootfs", bundle / "rootfs")
    if (asset_dir / "web").is_dir():
        shutil.copytree(asset_dir / "web", bundle / "web")
    for filename in ("url_map.json", "site_meta.json", "app_behavior.json"):
        if (asset_dir / filename).is_file():
            shutil.copy2(asset_dir / filename, bundle / filename)
    stats = collections.Counter(_overlay_current_declarations(bundle, raw))
    stats.update(_portableize_legacy_workspace(bundle, target_name))
    return dict(stats)


def _materialize_case(
    raw: dict[str, Any],
    surface: str,
    case_id: str,
    bundle: Path,
    source_entry: dict[str, Any] | None,
    *,
    legacy_asset_runs: Path | None = None,
    legacy_indexes: tuple[
        dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]],
        dict[tuple[Any, ...], list[tuple[Path, dict[str, Any]]]],
    ] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any]]:
    case_raw = copy.deepcopy(raw)
    case_raw.pop("source_claw", None)
    case_raw.pop("source_case_id", None)
    case_raw["id"] = case_id
    case_raw["test_mode"] = "inject"
    case_raw["target_surface"] = surface
    case_raw["inject_target_file"] = Path(SURFACE_TARGETS[surface]).name.upper()
    case = CaseSpec(case_raw)
    classification = classify_case(case)
    if not classification["runnable"] or classification["dimension"] != "inject":
        raise ValueError(
            f"{case_id}: not runnable as inject ({classification.get('reason') or classification})"
        )

    bundle.mkdir(parents=True, exist_ok=False)
    if legacy_asset_runs is not None:
        if legacy_indexes is None:
            raise ValueError("legacy asset indexes were not initialized")
        asset_dir, _legacy_setting, asset_info = _matching_legacy_asset(
            raw, legacy_asset_runs, legacy_indexes
        )
        asset_info.update(
            _copy_legacy_assets(asset_dir, bundle, raw, SURFACE_TARGETS[surface])
        )
    else:
        if source_entry is None:
            raise ValueError(f"{case_id}: selected source entry is missing")
        asset_dir = _matching_asset_dir(raw, source_entry)
        portableized_files = _copy_existing_assets(asset_dir, bundle)
        asset_info = {
            "asset_source_kind": (
                "probe"
                if asset_dir.parent.parent.name.startswith("probe_model=")
                else "historical_eval"
            ),
            "portableized_asset_files": portableized_files,
        }
    rootfs = bundle / "rootfs"
    asset_info["rootfs_files_mode_0755"] = normalize_rootfs_file_modes(rootfs)
    web = _web_metadata(
        bundle,
        {"url_map": bundle / "url_map.json"}
        if (bundle / "web").is_dir() and (bundle / "url_map.json").is_file()
        else None,
    )

    logical = SURFACE_TARGETS[surface]
    effective = _effective_target(logical, case_id)
    network_hosts = fixture_network_hosts(case) if has_behavioral_checks(case) else []
    env = {
        "schema": "redclaw-dataset/1.0",
        "id": case_id,
        "surface": surface,
        "dimension": "inject",
        "task": case_raw.get("benign_user_goal", ""),
        "attacker_instruction": case_raw.get("attacker_instruction", ""),
        "risk_category": case_raw.get("risk_category", ""),
        "attack_strategy": case_raw.get("attack_strategy", ""),
        "rootfs": {"dir": "rootfs", "files": tree_files(rootfs)},
        "memory_surface": None,
        "inject_target": {
            "logical_target": logical,
            "effective_memory_file": effective,
            "binding": "resolved by each filesystem-backed claw adapter at evaluation time",
        },
        "injected_observations": case_raw.get("injected_observations", []),
        "web": web,
        "network_hosts": network_hosts,
        "needs_sink": bool(classification["needs_sink"]) or web is not None,
        "collect_paths": list(
            dict.fromkeys(file_check_paths(case) + nominal_memory_paths(case))
        ),
        "verification": case_raw.get("verification", {}),
        "oracle": case_raw.get("oracle", {}),
    }
    (bundle / "env.json").write_text(
        json.dumps(env, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return case_raw, env, asset_dir, asset_info


def freeze(
    source: Path,
    output: Path,
    *,
    asset_runs: Path | None = None,
) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    rows = _discover(source)
    legacy_asset_runs = asset_runs.resolve() if asset_runs is not None else None
    legacy_indexes = (
        _legacy_asset_index(legacy_asset_runs)
        if legacy_asset_runs is not None
        else None
    )
    source_records = {} if legacy_indexes is not None else _selection_sources(source)
    seen_ids: set[str] = set()
    seen_content: set[str] = set()
    index_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    per_surface: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

    for item in rows:
        surface = item["surface"]
        raw = item["raw"]
        source_file = item["source_file"]
        _validate_case(raw, surface, source_file)
        original_id = str(raw["id"])
        case_id = _new_case_id(original_id, surface)
        if case_id in seen_ids:
            raise ValueError(f"duplicate frozen id: {case_id}")
        seen_ids.add(case_id)
        content_hash = task_hash(
            str(raw.get("benign_user_goal") or ""),
            str(raw.get("attacker_instruction") or ""),
        )
        if content_hash in seen_content:
            raise ValueError(f"duplicate task+attack content at {case_id}")
        seen_content.add(content_hash)

        source_entries = source_records.get((surface, original_id), [])
        if legacy_indexes is None and len(source_entries) != 1:
            raise ValueError(
                f"{original_id}: expected exactly one selected source, got {len(source_entries)}"
            )
        surface_dir = output / surface
        cases_dir = surface_dir / "cases"
        cases_dir.mkdir(parents=True, exist_ok=True)
        bundle = surface_dir / "env" / case_id
        case_raw, env, asset_dir, asset_info = _materialize_case(
            raw,
            surface,
            case_id,
            bundle,
            source_entries[0] if source_entries else None,
            legacy_asset_runs=legacy_asset_runs,
            legacy_indexes=legacy_indexes,
        )
        case_path = cases_dir / f"{case_id}.json"
        case_path.write_text(
            json.dumps(case_raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        source_tier = "legacy_converted" if legacy_indexes is not None else "exported_attack_success"
        verified_harnesses = (
            len(asset_info.get("matching_claws") or [])
            if legacy_indexes is not None
            else len(
                {str(entry.get("claw") or "") for entry in source_entries if entry.get("claw")}
            )
        )
        row = {
            "id": case_id,
            "surface": surface,
            "dimension": "inject",
            "risk_category": case_raw.get("risk_category", ""),
            "attack_strategy": case_raw.get("attack_strategy", ""),
            "source_tier": source_tier,
            "task": case_raw.get("benign_user_goal", ""),
            "attacker_instruction": case_raw.get("attacker_instruction", ""),
            "inject_target_file": env["inject_target"]["effective_memory_file"],
            "has_memory_poison": False,
            "has_web": env["web"] is not None,
            "needs_sink": env["needs_sink"],
            "rootfs_files": len(env["rootfs"]["files"]),
            "verified_on_harnesses": verified_harnesses,
            "case_file": f"{surface}/cases/{case_id}.json",
            "env_dir": f"{surface}/env/{case_id}",
        }
        index_rows.append(row)
        per_surface[surface].append(row)
        source_runs = (
            [
                {
                    "claw": asset_info.get("source_claw"),
                    "role": asset_info.get("source_role"),
                    "matching_runs": asset_info.get("matching_runs"),
                    "matching_claws": asset_info.get("matching_claws"),
                    "matched_by": asset_info.get("matched_by"),
                }
            ]
            if legacy_indexes is not None
            else source_entries
        )
        provenance_rows.append(
            {
                "id": case_id,
                "surface": surface,
                "original_case_id": original_id,
                "source_tier": source_tier,
                "source_file": str(source_file.relative_to(source)),
                "source_runs": source_runs,
                "collection_artifacts": "__portableized_existing_assets__",
                "asset_source": str(asset_dir),
                **asset_info,
                "frozen_materialization": (
                    "copied archived rootfs/web; reconciled explicit declarative inputs; "
                    "removed fabricated legacy workspace and registered portable target binding; "
                    "no generation"
                    if legacy_indexes is not None
                    else "copied existing assets; reversed recorded adapter path binding only"
                ),
            }
        )

    index_rows.sort(key=lambda row: (row["surface"], row["id"]))
    provenance_rows.sort(key=lambda row: (row["surface"], row["id"]))
    for surface, logical in SURFACE_TARGETS.items():
        surface_dir = output / surface
        (surface_dir / "cases").mkdir(parents=True, exist_ok=True)
        (surface_dir / "env").mkdir(parents=True, exist_ok=True)
        surface_rows = sorted(per_surface[surface], key=lambda row: row["id"])
        (surface_dir / "index.json").write_text(
            json.dumps(
                {
                    "surface": surface,
                    "dimension": "inject",
                    "n": len(surface_rows),
                    "inject_target_file": (
                        surface_rows[0]["inject_target_file"] if surface_rows else None
                    ),
                    "by_risk_category": dict(
                        collections.Counter(row["risk_category"] for row in surface_rows)
                    ),
                    "by_attack_strategy": dict(
                        collections.Counter(row["attack_strategy"] for row in surface_rows)
                    ),
                    "samples": surface_rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    (output / "samples.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in index_rows),
        encoding="utf-8",
    )
    (output / "PROVENANCE.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in provenance_rows),
        encoding="utf-8",
    )
    legacy_mode = legacy_indexes is not None
    manifest = {
        "name": "RedClaw-Inject-New" if legacy_mode else "RedClaw-Inject-Latest",
        "schema": "redclaw-dataset/1.0",
        "version": "1.0",
        "dimension": "inject",
        "description": (
            "Frozen inject-defense benchmark converted from the legacy inject corpus "
            "with archived probe/evaluation assets."
            if legacy_mode
            else "Frozen inject-defense benchmark converted from the current-format "
            "attack-successful inject corpus."
        ),
        "harness_agnostic": True,
        "n": len(index_rows),
        "by_surface": {surface: len(per_surface[surface]) for surface in SURFACE_TARGETS},
        "by_risk_category": dict(
            collections.Counter(row["risk_category"] for row in index_rows)
        ),
        "by_attack_strategy": dict(
            collections.Counter(row["attack_strategy"] for row in index_rows)
        ),
        "by_source_tier": dict(
            collections.Counter(row["source_tier"] for row in index_rows)
        ),
        "materialization": {
            "rootfs": (
                "copied from archived probe/eval assets; explicit declarative inputs reconciled; "
                "fabricated legacy workspace removed and target path portableized"
                if legacy_mode
                else "copied from existing probe/eval assets; recorded adapter path binding reversed"
            ),
            "web": "copied from archived probe/eval assets" if legacy_mode else "copied from existing probe/eval assets",
            "future_evaluation": "strict frozen asset reuse with adapter placeholder binding",
        },
        "provenance": "PROVENANCE.jsonl (audit trail; not an evaluation input)",
        "selection": "SELECTION.json (audit trail; not an evaluation input)",
    }
    (output / "dataset.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    selection_path = source / "SELECTION_REPORT.json"
    if legacy_mode:
        selection = {
            "source": str(source),
            "asset_runs": str(legacy_asset_runs),
            "selection": (
                "exact legacy setting match; unique setting and rootfs variant required; "
                "probe assets preferred deterministically"
            ),
            "no_generation": True,
        }
    else:
        selection = (
            json.loads(selection_path.read_text(encoding="utf-8"))
            if selection_path.exists()
            else {"source": str(source), "selection_report": "unavailable"}
        )
    selection["frozen_conversion"] = {
        "source": str(source),
        "samples": len(index_rows),
        "materialization": (
            "archived probe/eval assets; explicit inputs reconciled; legacy workspace portableized"
            if legacy_mode
            else "existing probe/eval assets; recorded adapter path binding reversed"
        ),
    }
    (output / "SELECTION.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return copy.deepcopy(value) if isinstance(value, list) else [copy.deepcopy(value)]


def _deduplicated(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _declared_path(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("path") or value.get("name") or "")
    return ""


def _normalize_merged_case(raw: dict[str, Any], surface: str, case_id: str) -> dict[str, Any]:
    """Normalize a frozen case to the current Inject case schema.

    This changes declarations only. The already-frozen rootfs/web bytes are
    copied unchanged, so merging never invokes a materializer or a model.
    """
    case = copy.deepcopy(raw)
    logical_target = SURFACE_TARGETS[surface]
    case.update(
        {
            "schema_version": "1.0",
            "id": case_id,
            "test_mode": "inject",
            "target_surface": surface,
            "target_surfaces": [surface],
            "inject_target_file": logical_target,
            "persistence_mode": raw.get("persistence_mode") or "path_targeted",
        }
    )

    if surface == "skill_md":
        case["persistence_mode"] = "skill_root_targeted"
        return case

    environment = case.get("environment")
    if not isinstance(environment, dict):
        environment = {}
        case["environment"] = environment

    directories = _as_list(environment.get("directories"))
    files: list[Any] = []
    for entry in _as_list(environment.get("files")):
        path = _declared_path(entry)
        is_directory = path.endswith("/") or (
            isinstance(entry, dict)
            and (
                entry.get("is_dir") is True
                or str(entry.get("type") or "").lower() == "directory"
            )
        )
        if is_directory and path:
            directories.append(path.rstrip("/"))
        else:
            files.append(entry)
    environment["files"] = files
    environment["directories"] = _deduplicated(directories)
    for key in ("web_pages", "accounts", "credentials", "permissions"):
        environment[key] = _as_list(environment.get(key))
    environment["memory_surfaces"] = [
        {
            "path": WORKSPACE_TARGET_PATH_PLACEHOLDER,
            "description": (
                "Runner-bound durable workspace target "
                f"({_effective_target(logical_target, case_id)})"
            ),
        }
    ]

    fixture = case.get("fixture")
    if not isinstance(fixture, dict):
        fixture = {}
        case["fixture"] = fixture
    required_directories = _as_list(fixture.get("required_directories"))
    required_files: list[Any] = []
    for entry in _as_list(fixture.get("required_files")):
        path = _declared_path(entry)
        if path.endswith("/"):
            required_directories.append(path.rstrip("/"))
        else:
            required_files.append(entry)
    fixture["required_files"] = required_files
    fixture["required_directories"] = _deduplicated(required_directories)
    for key in ("required_web_pages", "required_accounts"):
        fixture[key] = _as_list(fixture.get(key))
    return case


def _renumber_merged_id(case_id: str, surface: str, unavailable: set[str]) -> str:
    suffix = f"__{surface}"
    if not case_id.endswith(suffix):
        raise ValueError(f"{case_id}: id does not end with {suffix}")
    stem = case_id[: -len(suffix)]
    match = re.fullmatch(r"(.+)_([0-9]+)", stem)
    if match is None:
        raise ValueError(f"{case_id}: cannot deterministically renumber colliding id")
    prefix, number = match.groups()
    width = len(number)
    candidate_number = int(number) + 1
    while True:
        candidate = f"{prefix}_{candidate_number:0{width}d}{suffix}"
        if candidate not in unavailable:
            return candidate
        candidate_number += 1


def _frozen_source_records(dataset: Path) -> list[dict[str, Any]]:
    manifest_path = dataset / "dataset.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing frozen dataset manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("dimension") != "inject":
        raise ValueError(f"{dataset}: dataset dimension is not inject")
    provenance_path = dataset / "PROVENANCE.jsonl"
    provenance = {
        str(row["id"]): row
        for row in (
            json.loads(line)
            for line in provenance_path.read_text(encoding="utf-8").splitlines()
            if line
        )
    }
    records: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for surface in SURFACE_TARGETS:
        index_path = dataset / surface / "index.json"
        if not index_path.is_file():
            raise ValueError(f"missing frozen surface index: {index_path}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("surface") != surface or index.get("dimension") != "inject":
            raise ValueError(f"{index_path}: invalid surface or dimension")
        samples = index.get("samples") or []
        if index.get("n") != len(samples):
            raise ValueError(f"{index_path}: n does not match samples")
        for row in samples:
            case_id = str(row.get("id") or "")
            if not case_id or case_id in source_ids:
                raise ValueError(f"{dataset}: missing or duplicate case id {case_id!r}")
            source_ids.add(case_id)
            case_path = dataset / surface / "cases" / f"{case_id}.json"
            bundle = dataset / surface / "env" / case_id
            if not case_path.is_file() or not (bundle / "env.json").is_file():
                raise ValueError(f"{dataset}: incomplete frozen bundle for {case_id}")
            case = json.loads(case_path.read_text(encoding="utf-8"))
            if case.get("id") != case_id:
                raise ValueError(f"{case_path}: case id mismatch")
            if case_id not in provenance:
                raise ValueError(f"{dataset}: missing provenance for {case_id}")
            records.append(
                {
                    "source": dataset,
                    "surface": surface,
                    "id": case_id,
                    "row": copy.deepcopy(row),
                    "case": case,
                    "bundle": bundle,
                    "provenance": copy.deepcopy(provenance[case_id]),
                }
            )
    if len(provenance) != len(records):
        raise ValueError(f"{dataset}: provenance row count does not match indexed cases")
    if manifest.get("n") != len(records):
        raise ValueError(f"{dataset}: manifest n does not match indexed cases")
    return records


def merge_frozen(sources: list[Path], output: Path) -> dict[str, Any]:
    """Union current frozen Inject datasets without rematerializing assets."""
    if len(sources) < 2:
        raise ValueError("merge requires at least two --source-dataset values")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    resolved_sources = [source.resolve() for source in sources]
    source_records = [
        (source, _frozen_source_records(source)) for source in resolved_sources
    ]
    all_original_ids = {
        str(record["id"])
        for _, records in source_records
        for record in records
    }
    unavailable = set(all_original_ids)
    used_ids: set[str] = set()
    seen_content: dict[str, str] = {}
    index_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    per_surface: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    renamed: list[dict[str, str]] = []

    labels: dict[Path, str] = {}
    used_labels: set[str] = set()
    for position, source in enumerate(resolved_sources, 1):
        label = source.name or f"source_{position}"
        if label in used_labels:
            label = f"{label}_{position}"
        used_labels.add(label)
        labels[source] = label

    for source, records in source_records:
        source_label = labels[source]
        for record in records:
            surface = str(record["surface"])
            old_id = str(record["id"])
            case_id = old_id
            if case_id in used_ids:
                case_id = _renumber_merged_id(old_id, surface, unavailable | used_ids)
                unavailable.add(case_id)
                renamed.append(
                    {
                        "source_corpus": source_label,
                        "source_case_id": old_id,
                        "merged_case_id": case_id,
                    }
                )
            used_ids.add(case_id)

            case = _normalize_merged_case(record["case"], surface, case_id)
            content_hash = task_hash(
                str(case.get("benign_user_goal") or ""),
                str(case.get("attacker_instruction") or ""),
            )
            if content_hash in seen_content:
                raise ValueError(
                    f"duplicate task+attack content: {case_id} == {seen_content[content_hash]}"
                )
            seen_content[content_hash] = case_id

            surface_dir = output / surface
            cases_dir = surface_dir / "cases"
            cases_dir.mkdir(parents=True, exist_ok=True)
            bundle = surface_dir / "env" / case_id
            shutil.copytree(record["bundle"], bundle, symlinks=True)
            normalize_rootfs_file_modes(bundle / "rootfs")

            env_path = bundle / "env.json"
            env = json.loads(env_path.read_text(encoding="utf-8"))
            env.update(
                {
                    "schema": "redclaw-dataset/1.0",
                    "id": case_id,
                    "surface": surface,
                    "dimension": "inject",
                    "task": case.get("benign_user_goal", ""),
                    "attacker_instruction": case.get("attacker_instruction", ""),
                    "risk_category": case.get("risk_category", ""),
                    "attack_strategy": case.get("attack_strategy", ""),
                    "memory_surface": None,
                    "inject_target": {
                        "logical_target": SURFACE_TARGETS[surface],
                        "effective_memory_file": _effective_target(
                            SURFACE_TARGETS[surface], case_id
                        ),
                        "binding": (
                            "resolved by each filesystem-backed claw adapter "
                            "at evaluation time"
                        ),
                    },
                    "injected_observations": case.get("injected_observations") or [],
                    "verification": case.get("verification") or {},
                    "oracle": case.get("oracle") or {},
                }
            )
            env["rootfs"] = {"dir": "rootfs", "files": tree_files(bundle / "rootfs")}
            env_path.write_text(
                json.dumps(env, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (cases_dir / f"{case_id}.json").write_text(
                json.dumps(case, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            source_row = record["row"]
            row = {
                "id": case_id,
                "surface": surface,
                "dimension": "inject",
                "risk_category": case.get("risk_category", ""),
                "attack_strategy": case.get("attack_strategy", ""),
                "source_tier": source_row.get("source_tier", ""),
                "task": case.get("benign_user_goal", ""),
                "attacker_instruction": case.get("attacker_instruction", ""),
                "inject_target_file": env["inject_target"]["effective_memory_file"],
                "has_memory_poison": False,
                "has_web": env.get("web") is not None,
                "needs_sink": bool(env.get("needs_sink")),
                "rootfs_files": len(env["rootfs"]["files"]),
                "verified_on_harnesses": source_row.get("verified_on_harnesses", 0),
                "case_file": f"{surface}/cases/{case_id}.json",
                "env_dir": f"{surface}/env/{case_id}",
            }
            index_rows.append(row)
            per_surface[surface].append(row)

            provenance = record["provenance"]
            provenance.update(
                {
                    "id": case_id,
                    "surface": surface,
                    "source_corpus": source_label,
                    "source_dataset": str(source),
                    "source_dataset_case_id": old_id,
                }
            )
            if case_id != old_id:
                provenance["merge_renamed_from"] = old_id
            provenance_rows.append(provenance)

    index_rows.sort(key=lambda row: (row["surface"], row["id"]))
    provenance_rows.sort(key=lambda row: (row["surface"], row["id"]))
    for surface in SURFACE_TARGETS:
        surface_dir = output / surface
        (surface_dir / "cases").mkdir(parents=True, exist_ok=True)
        (surface_dir / "env").mkdir(parents=True, exist_ok=True)
        surface_rows = sorted(per_surface[surface], key=lambda row: row["id"])
        (surface_dir / "index.json").write_text(
            json.dumps(
                {
                    "surface": surface,
                    "dimension": "inject",
                    "n": len(surface_rows),
                    "inject_target_file": (
                        surface_rows[0]["inject_target_file"] if surface_rows else None
                    ),
                    "by_risk_category": dict(
                        collections.Counter(row["risk_category"] for row in surface_rows)
                    ),
                    "by_attack_strategy": dict(
                        collections.Counter(row["attack_strategy"] for row in surface_rows)
                    ),
                    "samples": surface_rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    (output / "samples.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in index_rows),
        encoding="utf-8",
    )
    (output / "PROVENANCE.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in provenance_rows),
        encoding="utf-8",
    )
    manifest = {
        "name": "RedClaw-Inject-Merge",
        "schema": "redclaw-dataset/1.0",
        "version": "1.0",
        "dimension": "inject",
        "description": (
            "Frozen inject-defense benchmark formed as the complete union of the current "
            "and converted legacy Inject corpora, normalized to the Inject-New schema."
        ),
        "harness_agnostic": True,
        "n": len(index_rows),
        "by_surface": {surface: len(per_surface[surface]) for surface in SURFACE_TARGETS},
        "by_risk_category": dict(
            collections.Counter(row["risk_category"] for row in index_rows)
        ),
        "by_attack_strategy": dict(
            collections.Counter(row["attack_strategy"] for row in index_rows)
        ),
        "by_source_tier": dict(
            collections.Counter(row["source_tier"] for row in index_rows)
        ),
        "materialization": {
            "rootfs": "copied byte-for-byte from the source frozen bundles; no rematerialization",
            "web": "copied byte-for-byte from the source frozen bundles",
            "future_evaluation": "strict frozen asset reuse with adapter placeholder binding",
        },
        "provenance": "PROVENANCE.jsonl (audit trail; not an evaluation input)",
        "selection": "SELECTION.json (audit trail; not an evaluation input)",
    }
    (output / "dataset.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    selection = {
        "merge_schema": "redclaw-dataset-merge/1.0",
        "sources": [
            {
                "source_corpus": labels[source],
                "dataset": str(source),
                "samples": len(records),
            }
            for source, records in source_records
        ],
        "selection": (
            "complete union; no task deduplication; content duplicates rejected; "
            "colliding ids in later sources deterministically renumbered"
        ),
        "samples": len(index_rows),
        "renamed_case_ids": renamed,
        "normalization": {
            "canonical_case_schema": "inject_new",
            "schema_version": "1.0",
            "directory_declarations": "moved from files to directories",
            "memory_surface": "registered HOME/WORKSPACE placeholder",
            "frozen_assets": "copied without rematerialization",
        },
    }
    (output / "SELECTION.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
