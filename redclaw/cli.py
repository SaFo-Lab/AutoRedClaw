"""Unified command entry point; domain modules own their existing arguments."""

from __future__ import annotations

import argparse
import importlib
import sys

from redclaw import __version__

COMMANDS = {
    "generate": (None, "Generate inject or trigger cases"),
    "redteam": ("redclaw.redteam.pipeline", "Run the generation, probe and evaluation pipeline"),
    "probe": ("redclaw.redteam.probe", "Check attack feasibility"),
    "evaluate": ("redclaw.evaluation.batch", "Evaluate a corpus on a selected claw"),
    "run": ("redclaw.evaluation.runner", "Execute and score a single case"),
    "environment": ("redclaw.environments.harness", "Materialize and manage case environments"),
    "dataset": ("redclaw.datasets.cli", "Build, verify or merge frozen datasets"),
    "stage-assets": ("redclaw.datasets.assets", "Stage frozen assets for evaluation"),
    "runtime": ("redclaw.runtime.verify", "Verify runtime images against the experiment baseline"),
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="redclaw", description="RedClaw: data generation, red-teaming and multi-agent evaluation."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, (_, description) in COMMANDS.items():
        subparsers.add_parser(name, help=description, add_help=False)
    # Only consume the command. Forward all domain flags, including --help,
    # unchanged to the original parser.
    selected = parser.parse_args(args[:1])
    remainder = args[1:]
    command_name = f"redclaw {selected.command}"
    module_name = COMMANDS[selected.command][0]
    if selected.command == "generate":
        generate = argparse.ArgumentParser(prog="redclaw generate", description="Generate attack cases.")
        generate.add_argument("dimension", choices=("inject", "trigger"))
        dimension = generate.parse_args(remainder[:1]).dimension
        command_name += f" {dimension}"
        module_name = f"redclaw.generation.{dimension}"
        remainder = remainder[1:]
    module = importlib.import_module(module_name)
    previous_argv = sys.argv
    try:
        sys.argv = [command_name, *remainder]
        return module.main(remainder) or 0
    finally:
        sys.argv = previous_argv
