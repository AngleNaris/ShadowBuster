"""Command-line entry point for the isolated Lew residual experiment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.lew_residual import (
    DEFAULT_CHECKPOINT,
    DEFAULT_LEW_SCRIPT,
    DEFAULT_PYTHON,
    run_experiment,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run conservative Lew residual extraction")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--extra-wet", action="append", default=[], type=Path)
    parser.add_argument("--strength", default=0.5, type=float)
    parser.add_argument("--device", default="cuda", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--chunk-seconds", default=15.0, type=float)
    parser.add_argument("--overlap-seconds", default=2.0, type=float)
    parser.add_argument("--timeout", default=7200.0, type=float)
    parser.add_argument("--python", default=DEFAULT_PYTHON, type=Path)
    parser.add_argument("--lew-script", default=DEFAULT_LEW_SCRIPT, type=Path)
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT, type=Path)
    parser.add_argument("--chunk-batch-size", default=1, type=int)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--n-fft", default=4096, type=int)
    parser.add_argument("--hop", default=1024, type=int)
    parser.add_argument("--max-lag", default=64, type=int)
    parser.add_argument("--min-correlation", default=0.90, type=float)
    parser.add_argument("--alignment-windows", default=7, type=int)
    parser.add_argument("--alignment-lowpass-hz", default=5000.0, type=float)
    parser.add_argument("--ratio-limit", default=0.5, type=float)
    parser.add_argument("--activity-floor-db", default=-60.0, type=float)
    parser.add_argument("--support-floor-db", default=-70.0, type=float)
    parser.add_argument("--constructive-gate-dry-floor-db", default=-120.0, type=float)
    parser.add_argument("--lowband-max-db", default=-35.0, type=float)
    parser.add_argument("--silent-max-db", default=-40.0, type=float)
    parser.add_argument("--silence-db", default=-60.0, type=float)
    parser.add_argument("--allow-over", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = run_experiment(
        args.input, args.output_dir, extra_wet_paths=args.extra_wet, strength=args.strength,
        device=args.device, chunk_seconds=args.chunk_seconds, overlap_seconds=args.overlap_seconds,
        timeout=args.timeout, allow_over=args.allow_over, overwrite=args.overwrite,
        python=args.python, lew_script=args.lew_script, checkpoint=args.checkpoint,
        chunk_batch_size=args.chunk_batch_size, ffmpeg=args.ffmpeg, n_fft=args.n_fft,
        hop=args.hop, max_lag=args.max_lag, min_correlation=args.min_correlation,
        alignment_windows=args.alignment_windows,
        alignment_lowpass_hz=args.alignment_lowpass_hz, ratio_limit=args.ratio_limit,
        activity_floor_db=args.activity_floor_db, support_floor_db=args.support_floor_db,
        constructive_gate_dry_floor_db=args.constructive_gate_dry_floor_db,
        lowband_max_db=args.lowband_max_db, silent_max_db=args.silent_max_db,
        silence_db=args.silence_db,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
