from __future__ import annotations

import argparse
from pathlib import Path

from experiments.official_apollo import run_experiment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run official Apollo and audit a conservative residual endpoint."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--inference-script", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="cuda")
    parser.add_argument("--chunk-seconds", type=float)
    parser.add_argument("--overlap-seconds", type=float, default=1.0)
    parser.add_argument("--chunk-batch-size", type=int, default=1)
    parser.add_argument("--strength", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--allow-over", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    options = vars(args)
    for name in ("python", "inference_script", "checkpoint"):
        if options[name] is None:
            options.pop(name)
    report = run_experiment(
        options.pop("input"), options.pop("output_dir"), **options
    )
    print(report["report"]["path"])


if __name__ == "__main__":
    main()
