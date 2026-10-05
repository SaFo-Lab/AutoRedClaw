"""Stage a dataset's frozen environment assets where the runner's reuse path looks.

`build_case_rootfs` / `build_case_web` reuse assets from a sibling render at
`<runs-dir>/probe_model=*/<risk_category>/<case_stem>/`, and with reuse on they
refuse to materialize anything they cannot find there. So running the dataset is
a matter of copying each sample's frozen `rootfs/`, `web/` and sink sidecars into
that location first — after which the runner serves exactly the bytes the attack
was collected against, and no synthesis (or web-asset LLM call) happens.

    python -m redclaw stage-assets \
        --dataset dataset/inject --dimension inject --surface agents_md \
        --runs-dir runs/evaluation/nanobot/inject/agents_md
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


from redclaw.evaluation.core import CaseSpec
from redclaw.evaluation.artifacts import case_run_dir

#: `probe_model=` prefix is what `_probe_asset_dir` globs for; the suffix records
#: that these came from a frozen dataset rather than a live probe run.
FROZEN_DIR_NAME = "probe_model=frozen"
SIDECARS = ("site_meta.json", "app_behavior.json")


def _surface_index(dataset: Path, dimension: str, surface: str) -> Path:
    candidates = [
        dataset / surface / "index.json",
        dataset / dimension / surface / "index.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit(f"no such {dimension} surface in {dataset}: {surface}")


def _dataset_row_path(dataset: Path, dimension: str, rel: str) -> Path:
    candidates = [
        dataset / rel,
        dataset / dimension / rel,
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def stage_surface(dataset: Path, surface: str, runs_dir: Path, dimension: str = "trigger") -> tuple[int, int]:
    """Copy every sample's frozen assets for one surface. -> (staged, with_web)."""
    index = _surface_index(dataset, dimension, surface)
    frozen_root = runs_dir / FROZEN_DIR_NAME
    if frozen_root.exists():
        shutil.rmtree(frozen_root)

    staged = web_n = 0
    for row in json.loads(index.read_text())["samples"]:
        env_dir = _dataset_row_path(dataset, dimension, row["env_dir"])
        case = CaseSpec(json.loads(_dataset_row_path(dataset, dimension, row["case_file"]).read_text()))
        # mirror the path the runner will compute for this case
        run_dir = case_run_dir(runs_dir, "eval_model", "placeholder", case)
        dest = frozen_root / run_dir.parent.name / run_dir.name
        dest.mkdir(parents=True, exist_ok=True)

        shutil.copytree(env_dir / "rootfs", dest / "rootfs")
        if (env_dir / "web").is_dir():
            shutil.copytree(env_dir / "web", dest / "web")
            shutil.copy2(env_dir / "url_map.json", dest / "url_map.json")
            for side in SIDECARS:
                if (env_dir / side).exists():
                    shutil.copy2(env_dir / side, dest / side)
            web_n += 1
        staged += 1
    return staged, web_n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--dimension", choices=["inject", "trigger"], default="trigger")
    ap.add_argument("--surface", required=True)
    ap.add_argument("--runs-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    args.runs_dir.mkdir(parents=True, exist_ok=True)
    staged, web_n = stage_surface(args.dataset, args.surface, args.runs_dir, args.dimension)
    print(f"[assets] staged {staged} frozen environments ({web_n} with web) into "
          f"{args.runs_dir / FROZEN_DIR_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
