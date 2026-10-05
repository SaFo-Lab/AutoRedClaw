"""Canonical dataset build and audit hooks used by ``-m redclaw dataset``.

This is the single source of truth for the surfaces understood by the frozen
dataset format.  Trigger and Inject intentionally have different surface sets:
``environment`` is a Trigger delivery channel, while ``heartbeat_md`` is an
Inject memory target. ``skill_md`` covers seeded skills in Trigger and skill
installation in Inject.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

TRIGGER_BASE_SURFACES = ("agents_md", "environment", "memory_md", "soul_md", "user_md")
TRIGGER_SURFACES = ("agents_md", "environment", "memory_md", "skill_md", "soul_md", "user_md")
INJECT_TARGETS = {
    "agents_md": "agents.md",
    "heartbeat_md": "heartbeat.md",
    "memory_md": "memory.md",
    "skill_md": "skill.md",
    "soul_md": "soul.md",
    "user_md": "user.md",
}
CLAW_NAMES = ("nanobot", "nemoclaw", "openclaw", "seclaw", "zeroclaw")
ROOTFS_FILE_MODE = 0o755


def normalize_rootfs_file_modes(rootfs: Path) -> int:
    """Set every regular, non-symlink frozen rootfs file to mode 0755."""
    normalized = 0
    for path in sorted(rootfs.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        path.chmod(ROOTFS_FILE_MODE)
        normalized += 1
    return normalized


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task_hash(task: str, attack: str) -> str:
    return hashlib.sha256(f"{task.strip()}||{attack.strip()}".encode()).hexdigest()


def tree_files(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    return [
        {
            "container_path": "/" + path.relative_to(root).as_posix(),
            "bundle_path": path.relative_to(root.parent).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
