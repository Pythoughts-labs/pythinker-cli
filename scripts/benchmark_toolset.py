from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pythinker_code.benchmark.toolset_characterization import run_characterization


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Run the directional, local-only Pythinker Toolset characterization harness.")
    )
    parser.add_argument(
        "--scenario",
        choices=("all", "execution", "dedupe", "advertisement", "mcp"),
        default="all",
        help="scenario family to measure (default: all)",
    )
    parser.add_argument(
        "--runs",
        type=_positive_int,
        default=5,
        help="isolated measured runs per fixture after warm-up (default: 5)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the machine-readable JSON report to this path",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="run only the smallest fixture in each selected scenario family",
    )
    return parser


async def _run(args: argparse.Namespace) -> str:
    report = await run_characterization(
        scenario=args.scenario,
        runs=args.runs,
        smoke=args.smoke,
    )
    return report.model_dump_json(indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = asyncio.run(_run(args))
        if args.output is not None:
            args.output.write_text(payload, encoding="utf-8")
        else:
            sys.stdout.write(payload)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
