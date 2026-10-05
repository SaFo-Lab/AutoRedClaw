"""Single public entry point for RedClaw dataset maintenance.

Examples::

    # Build the Trigger corpus (base surfaces plus skill_md) from collected runs.
    python -m redclaw dataset build --dimension trigger --skill-model gpt-5-mini

    # Build Inject from portable cases and the probe/eval assets collected for them.
    python -m redclaw dataset build --dimension inject \
        --source dataset/inject_raw --out dataset/inject

    # Audit a frozen dataset (dimension auto-detected from dataset.json).
    python -m redclaw dataset verify --dataset dataset/trigger
    python -m redclaw dataset verify --dataset dataset/inject

    # Merge frozen Inject datasets without rematerializing them.
    python -m redclaw dataset merge \
        --source-dataset dataset/inject \
        --source-dataset dataset/inject_extra \
        --out dataset/inject_merged

All default paths are relative to the project root.

The command is intentionally thin.  Shared command semantics live here; the
only dimension-specific code is under ``redclaw.datasets``.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any


from redclaw.paths import PROJECT_ROOT

from redclaw.datasets import inject, skill_md, trigger, verify_inject, verify_trigger


DIMENSIONS = ("trigger", "inject")


def _dimension(dataset: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    manifest_path = dataset / "dataset.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read dataset dimension from {manifest_path}: {exc}") from exc
    dimension = str(manifest.get("dimension") or "").lower()
    if dimension not in DIMENSIONS:
        raise ValueError(f"{manifest_path}: unsupported dimension {dimension!r}")
    return dimension


def _write_readme(dataset: Path) -> None:
    """Write a dimension-neutral README from the manifest and flat index."""
    manifest = json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (dataset / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    by_surface: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_surface[str(row["surface"])].append(row)

    dimension = str(manifest["dimension"])
    tree = "<surface>/..."
    lines = [
        f"# {manifest['name']} v{manifest['version']}",
        "",
        f"**{manifest['n']} samples.** Dimension: `{dimension}`.",
        "",
        str(manifest.get("description") or ""),
        "",
        "The cases and their rootfs/web assets are frozen evaluation inputs. "
        "Each claw adapter only resolves registered portable workspace placeholders.",
        "",
        "## Layout",
        "",
        "```text",
        "dataset.json       # manifest",
        "samples.jsonl      # flat index",
        "PROVENANCE.jsonl   # audit-only source trail; not an evaluation input",
        f"{tree}",
        "  index.json",
        "  cases/<id>.json",
        "  env/<id>/{env.json,rootfs/,web/,...}",
        "```",
        "",
        "## Composition",
        "",
        "| surface | n | memory poison | frozen web | sink |",
        "|---|---:|---:|---:|---:|",
    ]
    for surface in sorted(by_surface):
        surface_rows = by_surface[surface]
        lines.append(
            f"| {surface} | {len(surface_rows)} | "
            f"{sum(bool(row.get('has_memory_poison')) for row in surface_rows)} | "
            f"{sum(bool(row.get('has_web')) for row in surface_rows)} | "
            f"{sum(bool(row.get('needs_sink')) for row in surface_rows)} |"
        )
    lines.extend(
        [
            "",
            "## Maintenance",
            "",
            "```bash",
            "python -m redclaw dataset verify --dataset <dataset>",
            "# Rebuild explicitly with: build --dimension trigger|inject --source ... --out ...",
            "```",
            "",
        ]
    )
    (dataset / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _build(args: argparse.Namespace) -> int:
    if args.dimension == "trigger":
        if args.asset_runs is not None:
            raise ValueError("--asset-runs is only valid with --dimension inject")
        source = (args.source or trigger.SRC).resolve()
        output = (args.out or PROJECT_ROOT / "dataset" / "trigger").resolve()
        result = trigger.build(["--source", str(source), "--out", str(output)])
        if not args.without_skill_md:
            if not args.skill_model:
                raise ValueError("--skill-model is required to build the skill_md surface "
                                 "(or pass --without-skill-md)")
            skill_result = skill_md.build(
                [
                    "--source",
                    str(args.skill_source.resolve()),
                    "--dataset",
                    str(output),
                    "--model",
                    args.skill_model,
                    "--count",
                    str(args.skill_count),
                ]
            )
            result = max(result, skill_result)
    else:
        source = (args.source or PROJECT_ROOT / "dataset" / "inject_raw").resolve()
        output = (args.out or PROJECT_ROOT / "dataset" / "inject").resolve()
        manifest = inject.freeze(
            source,
            output,
            asset_runs=args.asset_runs.resolve() if args.asset_runs is not None else None,
        )
        print(json.dumps({"output": str(output), "n": manifest["n"],
                          "by_surface": manifest["by_surface"]},
                         ensure_ascii=False, indent=2))

    _write_readme(output)
    return result if args.dimension == "trigger" else 0


def _verify(args: argparse.Namespace) -> int:
    dataset = args.dataset.resolve()
    dimension = _dimension(dataset, args.dimension)
    audit = verify_trigger if dimension == "trigger" else verify_inject
    argv = ["--dataset", str(dataset)]
    if args.collection_dir is not None:
        if dimension != "inject":
            raise ValueError("--collection-dir applies to Inject datasets only")
        argv += ["--collection-dir", str(args.collection_dir)]
    return audit.audit(argv)


def _merge(args: argparse.Namespace) -> int:
    output = args.out.resolve()
    manifest = inject.merge_frozen(
        [source.resolve() for source in args.source_dataset],
        output,
    )
    _write_readme(output)
    print(
        json.dumps(
            {"output": str(output), "n": manifest["n"], "by_surface": manifest["by_surface"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build one frozen dataset dimension")
    build.add_argument("--dimension", choices=DIMENSIONS, required=True)
    build.add_argument("--source", type=Path, help="dimension source (defaults are dimension-specific)")
    build.add_argument("--out", type=Path, help="output dataset root (defaults are dimension-specific)")
    build.add_argument(
        "--asset-runs",
        type=Path,
        help=(
            "archived inject runs supplying frozen rootfs/web assets; inject only "
            "(never invokes the materializer or a model)"
        ),
    )
    build.add_argument("--skill-source", type=Path,
                       default=PROJECT_ROOT / "runs" / "collection",
                       help="collected skill_md runs used by the Trigger skill surface builder")
    build.add_argument("--skill-model", default=None,
                       help="eval model whose collected skill_md runs feed the Trigger skill_md surface")
    build.add_argument("--skill-count", type=int, default=30)
    build.add_argument("--without-skill-md", action="store_true",
                       help="build only the five base Trigger surfaces")
    build.set_defaults(handler=_build)

    verify = commands.add_parser("verify", help="audit a frozen dataset")
    verify.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "dataset")
    verify.add_argument("--dimension", choices=("auto", *DIMENSIONS), default="auto")
    verify.add_argument("--collection-dir", type=Path, default=None,
                        help="Inject only: collection archive to byte-check frozen rootfs bundles against")
    verify.set_defaults(handler=_verify)

    merge = commands.add_parser(
        "merge", help="merge frozen Inject datasets without rematerializing assets"
    )
    merge.add_argument(
        "--source-dataset",
        type=Path,
        action="append",
        required=True,
        help="frozen Inject dataset to include; repeat in precedence order",
    )
    merge.add_argument("--out", type=Path, required=True, help="new merged dataset root")
    merge.set_defaults(handler=_merge)

    readme = commands.add_parser("readme", help="regenerate README.md from manifest/index")
    readme.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "dataset")
    readme.set_defaults(handler=lambda args: (_write_readme(args.dataset.resolve()) or 0))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
