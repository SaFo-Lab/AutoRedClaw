"""Trigger generation entry point: cases whose poison is pre-seeded."""

from __future__ import annotations

from redclaw.generation import engine


def main(argv: list[str] | None = None) -> int:
    return engine.main(engine.TRIGGER_PROFILE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
