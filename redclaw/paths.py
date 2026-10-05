"""Locations for bundled resources and user-owned data.

Source checkouts use the project directory. Installed distributions use the
current directory for credentials and outputs.
"""

from __future__ import annotations

import sysconfig
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
_checkout = PACKAGE_ROOT.parent
PROJECT_ROOT = _checkout if (_checkout / "pyproject.toml").is_file() else Path.cwd()
RESOURCES_DIR = PACKAGE_ROOT / "resources"
RUNTIME_DIR = PACKAGE_ROOT / "runtime"
CLAWS_DIR = (
    _checkout / "claws"
    if (_checkout / "claws" / "compose.yaml").is_file()
    else Path(sysconfig.get_path("data")) / "share" / "redclaw" / "claws"
)
