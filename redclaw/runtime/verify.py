"""Compare selected runtime images with the recorded local experiment baseline."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from redclaw.paths import CLAWS_DIR, PROJECT_ROOT


def inspect_image(reference: str) -> dict:
    """Read immutable identity and platform without exposing image environment."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", reference], capture_output=True, text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    if result.returncode:
        return {"error": result.stderr.strip() or "Docker image inspection failed"}
    try:
        record = json.loads(result.stdout)[0]
        return {
            "image_id": record["Id"],
            "platform": f"{record['Os']}/{record['Architecture']}",
        }
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return {"error": f"Invalid Docker image metadata: {exc}"}


def verify_images(baseline: dict, claws: list[str], env: dict[str, str]) -> list[dict]:
    results = []
    for name in claws:
        expected = baseline["images"][name]
        reference = expected["reference"]
        if name in ("codex", "claudecode"):
            reference = env.get(f"{name.upper()}_IMAGE") or reference
        observed = inspect_image(reference)
        row = {"claw": name, "reference": reference, "expected_id": expected["image_id"], **observed}
        row["matches"] = (
            observed.get("image_id") == expected["image_id"]
            and observed.get("platform") == expected["platform"]
        )
        if name == "codex":
            from redclaw.adapters.codex import CODEX_CLI_VERSION
            row["configured_cli_version"] = env.get("CODEX_CLI_VERSION") or CODEX_CLI_VERSION
        elif name == "claudecode":
            from redclaw.adapters.claudecode import CLAUDE_CODE_CLI_VERSION
            row["configured_cli_version"] = env.get("CLAUDE_CODE_CLI_VERSION") or CLAUDE_CODE_CLI_VERSION
        if "configured_cli_version" in row:
            row["expected_cli_version"] = expected["cli_version"]
            row["matches"] &= row["configured_cli_version"] == expected["cli_version"]
        results.append(row)
        for dependency in expected.get("dependencies", []):
            observed = inspect_image(dependency["reference"])
            results.append({
                "claw": f"{name}/{dependency['role']}",
                "reference": dependency["reference"],
                "expected_id": dependency["image_id"],
                **observed,
                "matches": observed.get("image_id") == dependency["image_id"]
                and observed.get("platform") == dependency["platform"],
            })
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify"])
    parser.add_argument("--claw", nargs="+", help="Claws to check; defaults to every claw in the baseline.")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--output", type=Path, help="Optionally save the identity comparison as JSON.")
    args = parser.parse_args(argv)
    baseline = json.loads((CLAWS_DIR / "runtime-baseline.json").read_text(encoding="utf-8"))
    claws = args.claw or list(baseline["images"])
    unknown = set(claws) - baseline["images"].keys()
    if unknown:
        parser.error(f"Unknown claws: {', '.join(sorted(unknown))}")
    from redclaw.evaluation.runner import load_env
    results = verify_images(baseline, claws, load_env(args.env_file))
    matches = all(row["matches"] for row in results)
    report = {
        "reference": baseline["description"],
        "matches": matches,
        "images": results,
    }
    for row in results:
        print(f"{'MATCH' if row['matches'] else 'DIFFERENT'} {row['claw']}: {row['reference']}")
        print(f"  expected: {row['expected_id']}")
        print(f"  observed: {row.get('image_id', row.get('error', 'unavailable'))}")
        if "configured_cli_version" in row:
            print(f"  CLI setting: {row['configured_cli_version']} (reference: {row['expected_cli_version']})")
    if not matches:
        print("Images/settings differ from the reference; a source rebuild is not an identical-image replay.")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
