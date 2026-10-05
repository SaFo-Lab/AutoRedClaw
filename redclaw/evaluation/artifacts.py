"""Helpers for writing runner artifacts directly into the archive layout."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from redclaw.evaluation.core import CaseSpec


SCHEMA = "redclaw_run_bundle_v1"


def slug(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"^_+|_+$", "", text)
    return text or default


def model_dir(base: Path, model_role: str, model: str) -> Path:
    return base / f"{model_role}={slug(model, 'unknown_model')}"


def stage_report_path(base: Path, model_role: str, model: str, filename: str) -> Path:
    return model_dir(base, model_role, model) / "_stage_reports" / filename


def _case_stem(case: CaseSpec) -> str:
    category = slug(case.risk_category)
    case_id = case.case_id
    prefix = f"{category}_"
    if case_id.startswith(prefix):
        stem = case_id[len(prefix):]
    else:
        stem = slug(case.attack_strategy)
        suffix = re.search(r"_(\d+)$", case_id)
        if suffix and not stem.endswith(f"_{suffix.group(1)}"):
            stem = f"{stem}_{suffix.group(1)}"
    return slug(stem)


def case_run_dir(base: Path, model_role: str, model: str, case: CaseSpec) -> Path:
    return model_dir(base, model_role, model) / slug(case.risk_category) / _case_stem(case)


def reset_case_dir(run_dir: Path) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    text = _load_text(path)
    if not text:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            rows.append({"raw": line})
    return rows


def artifact_index(run_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        try:
            key = path.relative_to(run_dir).as_posix()
        except ValueError:
            continue
        if key == "metadata.json":
            continue
        out[key] = key
    return out


def write_case_metadata(
    run_dir: Path,
    *,
    case: CaseSpec,
    stage: str,
    model_role: str,
    model: str,
    claw: str,
    dimension: str,
    surface: str,
    summary_row: dict[str, Any] | None = None,
) -> None:
    setting = _load_json(run_dir / "setting.json")
    report = _load_json(run_dir / "report.json")
    data: dict[str, Any] = {
        "archive_schema": SCHEMA,
        "stage": stage,
        "model_role": model_role,
        "model": model,
        "claw": claw,
        "dimension": dimension,
        "surface": surface,
        "risk_category": case.risk_category,
        "attack_strategy": case.attack_strategy,
        "case_id": case.case_id,
        "summary_row": summary_row or {},
        "setting": setting if isinstance(setting, dict) else case.raw,
        "report": report if isinstance(report, dict) else {},
        "artifacts": artifact_index(run_dir),
    }

    for name in ("web_asset_report", "site_meta", "url_map", "app_behavior", "probe_error", "run_error"):
        value = _load_json(run_dir / f"{name}.json")
        if value is not None:
            data[name] = value

    trajectory = _load_jsonl(run_dir / "trajectory.jsonl")
    if trajectory:
        data["trajectory"] = trajectory

    for name, filename in (
        ("stdout", "stdout.txt"),
        ("stderr", "stderr.txt"),
        ("memory_blob", "memory_blob.md"),
    ):
        text = _load_text(run_dir / filename)
        if text is not None:
            data[name] = text

    (run_dir / "metadata.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
