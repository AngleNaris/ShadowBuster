"""Audio-runtime subprocess entry point; keeps scientific imports out of Qt."""
import argparse

from . import LOUDNESS_TARGETS
from .pipeline import master_file


def main():
    parser = argparse.ArgumentParser(description="ShadowBuster independent mastering")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--loudness", choices=tuple(LOUDNESS_TARGETS), default="normal")
    parser.add_argument("--reference")
    parser.add_argument("--match-source")
    parser.add_argument("--strength", type=float, default=.85)
    parser.add_argument("--eq-profile", default="Neutral")
    parser.add_argument("--lowpass-cutoff", type=float)
    parser.add_argument("--space-wet", type=float, default=0.0)
    parser.add_argument("--width-db", type=float, default=0.0)
    args = parser.parse_args()
    print("MASTERING_PROGRESS 5", flush=True)
    stats = master_file(args.input, args.output, args.loudness, reference=args.reference,
        match_source=args.match_source, strength=args.strength, eq_profile=args.eq_profile,
        lowpass=args.lowpass_cutoff, space_wet=args.space_wet, width_db=args.width_db)
    print(f"LUFS {stats['actual_lufs']:.2f}; TP {stats['true_peak_dbtp']:.2f} dBTP", flush=True)
    print("MASTERING_PROGRESS 100", flush=True)


if __name__ == "__main__":
    main()
