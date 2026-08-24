"""Command line interface for the isolated strict Shimmer residual adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

try:
    from .shimmer_residual import DEFAULT_STRENGTH, render_residual, save_render
except ImportError:  # Permit direct execution of this file.
    from shimmer_residual import DEFAULT_STRENGTH, render_residual, save_render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render one strict Suno Hash endpoint and externally scale its residual."
    )
    parser.add_argument("input", type=Path, help="input mono/stereo audio file")
    parser.add_argument("--output-dir", type=Path, required=True, help="explicit output directory")
    parser.add_argument("--shimmer-dir", type=Path, required=True, help="Shimmer checkout directory")
    parser.add_argument(
        "--strength", type=float, default=DEFAULT_STRENGTH,
        help=f"external residual scale in [0,1] (default: {DEFAULT_STRENGTH})",
    )
    parser.add_argument(
        "--enable-tone-kill", action="store_true",
        help="retain suno_hash tone_kill (disabled by default)",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace existing result files")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audio, sample_rate = sf.read(args.input, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
    else:
        audio = np.ascontiguousarray(audio, dtype=np.float32)

    render = render_residual(
        audio,
        int(sample_rate),
        shimmer_dir=args.shimmer_dir,
        scale=args.strength,
        disable_tone_kill=not args.enable_tone_kill,
    )
    paths = save_render(render, args.output_dir, sample_rate, overwrite=args.overwrite)
    print(json.dumps({name: str(path) for name, path in paths.items()}, indent=2))
    clipping = render.report["clipping"]["processed"]
    if clipping["clipped"]:
        print(
            f"WARNING: processed output clips: peak={clipping['peak_linear']:.9g}, "
            f"samples={clipping['clipped_samples']} (result was not altered)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
