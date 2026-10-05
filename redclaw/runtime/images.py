"""Build bundled Docker contexts and locate pinned native workspace templates."""

from __future__ import annotations

import subprocess
from pathlib import Path


from redclaw.paths import CLAWS_DIR, RESOURCES_DIR

NATIVE_TEMPLATES = RESOURCES_DIR / "workspaces"


def build_claw_image(name: str, image_tag: str, *, force: bool = False) -> str:
    """Build a self-contained context; reuse an existing image unless forced."""
    context = CLAWS_DIR / name
    if not (context / "Dockerfile").is_file():
        raise FileNotFoundError(f"Claw Dockerfile not found: {context / 'Dockerfile'}")
    if not force:
        existing = subprocess.run(
            ["docker", "image", "inspect", image_tag], capture_output=True, text=True,
        )
        if existing.returncode == 0:
            return image_tag
    proc = subprocess.run(
        ["docker", "build", "--progress=plain", "-t", image_tag, str(context)],
        capture_output=True, text=True,
    )
    if proc.returncode:
        raise RuntimeError(
            f"docker build failed for {name}:\n{proc.stdout[-6000:]}\n{proc.stderr[-6000:]}"
        )
    return image_tag


def claw_template_dir(name: str) -> Path:
    """Templates are bundled from the same pinned revision as the Docker build."""
    return NATIVE_TEMPLATES / name
