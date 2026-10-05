"""Runtime adaptations limited to skill-inject evaluations.

Frozen web assets bind the registered skills root for the adapter under test.

For NemoClaw, new Linux tasks use /home/user. Legacy tasks used /root, or were relocated
into NemoClaw's workspace. Move only those legacy copies into the shared user
home handled by the adapter's existing /home permissions setup.
Bind the task, its oracles, and copies of its assets together; never grant the
agent access to the real root user's home or modify the source dataset.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from redclaw.evaluation.core import (
    AssetReuseError,
    CaseSpec,
    SKILLS_ROOT_PLACEHOLDER,
    _inject_skill_registry_target,
)


LEGACY_NEMOCLAW_SKILL_FIXTURE_HOME = "/sandbox/.openclaw/workspace/case-files/root"
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`]+")


def legacy_fixture_home(case: CaseSpec) -> str:
    """Use the same business home as new cases, independent of case identity."""
    return "/home/user"


def legacy_fixture_bindings(case: CaseSpec) -> dict[str, str]:
    """Also accept frozen copies produced by the previous per-case home mapper."""
    home = legacy_fixture_home(case)
    old_account = hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:10]
    return {"/root": home, LEGACY_NEMOCLAW_SKILL_FIXTURE_HOME: home,
            f"/home/user_{old_account}": home}


def _applies(adapter: Any, case: CaseSpec, test_mode: str) -> bool:
    declared = str(case.raw.get("inject_target_file") or "").strip().lower()
    is_skill = declared in {"skill.md", "skill_md"} or (
        not declared and _inject_skill_registry_target(case)
    )
    return adapter.name == "nemoclaw" and test_mode == "inject" and is_skill


def reset_bound_skill_rootfs(adapter: Any, case: CaseSpec, *, test_mode: str,
                            run_dir: Path, reuse_assets: bool) -> None:
    """A fresh probe must not overlay raw paths onto its last bound rootfs.

    Only discard this run's recorded, derived copy. Frozen-source reuse already
    replaces the destination rootfs itself; original dataset assets stay intact.
    """
    marker = run_dir / "fixture_path_bindings.json"
    if reuse_assets or not _applies(adapter, case, test_mode) or not marker.is_file():
        return
    rootfs = run_dir / "rootfs"
    if rootfs.exists():
        shutil.rmtree(rootfs)
    marker.unlink()


def bind_fixture_text(text: str, bindings: dict[str, str]) -> str:
    if not bindings:
        return text
    pattern = re.compile(
        r"(?<![\w./~:%$-])(?:"
        + "|".join(re.escape(path) for path in sorted(bindings, key=len, reverse=True))
        + r")(?=/|$|[^\w./~-])"
    )
    # A URL's /root segment (including query parameters) is not a local path.
    chunks: list[str] = []
    end = 0
    for match in _URL.finditer(text):
        chunks.append(pattern.sub(lambda m: bindings[m.group()], text[end:match.start()]))
        chunks.append(match.group())
        end = match.end()
    chunks.append(pattern.sub(lambda m: bindings[m.group()], text[end:]))
    return "".join(chunks)


def _bind_value(value: Any, bindings: dict[str, str]) -> Any:
    if isinstance(value, str):
        return bind_fixture_text(value, bindings)
    if isinstance(value, list):
        return [_bind_value(item, bindings) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            bound_key = bind_fixture_text(key, bindings) if isinstance(key, str) else key
            if bound_key in result:
                raise AssetReuseError(f"fixture path binding produces duplicate key: {bound_key!r}")
            # app_behavior routes use URL paths, including relative /root/...
            # endpoints; their response bodies can still reference local files.
            result[bound_key] = item if key == "path" and "method" in value else _bind_value(item, bindings)
        return result
    return value


def _bind_asset_text(path: Path, bindings: dict[str, str]) -> bool:
    if path.is_symlink():
        raise AssetReuseError(f"cannot rebind a symlinked fixture asset: {path}")
    if not path.is_file():
        return False
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if path.suffix == ".json":
        try:
            value = json.loads(text)
        except ValueError:
            resolved_text = bind_fixture_text(text, bindings)
        else:
            bound = _bind_value(value, bindings)
            # Decode JSON first: an escaped newline before /root is a real path
            # boundary in the served document, not the literal letter 'n'.
            resolved_text = json.dumps(bound, ensure_ascii=False, indent=2) if bound != value else text
    else:
        resolved_text = bind_fixture_text(text, bindings)
    resolved = resolved_text.encode("utf-8")
    if resolved != original:
        path.write_bytes(resolved)
        return True
    return False


def bind_reused_skill_web(case: CaseSpec, *, test_mode: str,
                          web: dict[str, Any] | None,
                          bindings: dict[str, str]) -> None:
    """Resolve the skills root in per-run copies of frozen Inject web assets."""
    if test_mode != "inject" or not web or not _inject_skill_registry_target(case):
        return
    declared = str(case.raw.get("inject_target_file") or "").strip().lower()
    if declared and declared not in {"skill.md", "skill_md"}:
        return
    root = bindings.get(SKILLS_ROOT_PLACEHOLDER)
    if not root:
        return
    skill_binding = {SKILLS_ROOT_PLACEHOLDER: root}
    paths = list(Path(web["web_dir"]).rglob("*"))
    paths.extend(Path(web[key]) for key in ("site_meta", "app_behavior") if web.get(key))
    for path in paths:
        _bind_asset_text(path, skill_binding)


def adapt_skill_inject_runtime(
    adapter: Any,
    case: CaseSpec,
    *,
    test_mode: str,
    rootfs_dir: Path,
    web: dict[str, Any] | None,
) -> tuple[CaseSpec, dict[str, str]]:
    """Rebind only per-run copies, including assets reused from older probes.

    Applied after materialization so cached/frozen content receives exactly the
    same binding as fresh content. Reapplying to an already-bound probe is a
    no-op. Output files stay absent: only their existing parent dirs move.
    """
    if not _applies(adapter, case, test_mode):
        return case, {}

    home = legacy_fixture_home(case)
    bindings = legacy_fixture_bindings(case)
    bound_case = CaseSpec(_bind_value(case.raw, bindings))
    sources = [rootfs_dir / path.lstrip("/") for path in bindings]
    sources = [path for path in sources if path.exists()]
    destination = rootfs_dir / home.lstrip("/")
    assets = list(rootfs_dir.rglob("*"))
    assets.append(rootfs_dir.parent / "asset_sanity_report.json")
    if web:
        assets.extend(Path(web["web_dir"]).rglob("*"))
        # Served bodies may reference files. url_map's paths are URL routing,
        # so the routing table remains byte-for-byte unchanged.
        assets.extend(
            Path(web[key]) for key in ("site_meta", "app_behavior")
            if web.get(key) is not None
        )
    if any(path.is_symlink() for path in assets):
        raise AssetReuseError("NemoClaw skill fixture rebinding requires regular assets, not symlinks")
    if sources and (len(sources) > 1 or destination.exists()):
        raise AssetReuseError(f"fixture home binding collides at {destination}")
    changed = bound_case.raw != case.raw or bool(sources)
    for source in sources:
        if not source.is_dir():
            raise AssetReuseError(f"fixture home must be a directory: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = source.parent
        source.rename(destination)
        # Do not copy obsolete, empty sandbox/.openclaw ancestors over native
        # directory metadata when reusing a probe made by the older adapter.
        while parent != rootfs_dir and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for path in rootfs_dir.rglob("*"):
        changed = _bind_asset_text(path, bindings) or changed
    for path in assets:
        if not path.is_relative_to(rootfs_dir):
            changed = _bind_asset_text(path, bindings) or changed
    return (bound_case, bindings) if changed else (case, {})
