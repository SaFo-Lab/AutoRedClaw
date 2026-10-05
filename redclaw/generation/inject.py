"""Inject generation entry point.

Workspace-file persistence and skill installation are different inject target
profiles; ``--inject-target skill.md`` selects the skill profile.
"""

from __future__ import annotations

import sys

from redclaw.generation import skill, workspace


def requested_target(argv: list[str]) -> str:
    for index, arg in enumerate(argv):
        if arg.startswith("--inject-target="):
            return arg.split("=", 1)[1].strip().lower()
        if arg == "--inject-target" and index + 1 < len(argv):
            return argv[index + 1].strip().lower()
    return "any"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if requested_target(args) == skill.SKILL_INJECT_TARGET:
        return skill.main(args)
    return workspace.main(args, description=__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
