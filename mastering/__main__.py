"""Audio-runtime subprocess entry point; keeps scientific imports out of Qt."""
import argparse
import sys
import tempfile
from pathlib import Path

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
    parser.add_argument("--no-reference-cache", action="store_true",
                        help="Disable the content-addressed reference tonal target cache.")
    args = parser.parse_args()
    print("MASTERING_PROGRESS 5", flush=True)
    reference_cache = reference_cache_artifact = None
    if args.reference and not args.no_reference_cache:
        # Cache the expensive (source, reference) tonal target so moving loudness,
        # final width or EQ (which miss the outer stage cache) reuses the candidate.
        # Best-effort: a runtime without the shared cache infra stays on the cold path.
        try:
            import pipeline_cache
            from .reference_tone import identity as tonal_target_identity
        except ImportError:
            pipeline_cache = None
        if pipeline_cache is not None:
            reference_cache = pipeline_cache.StageCache(
                True, tonal_target_identity(Path(__file__).parent, sys.executable), tier='B')
            reference_cache_artifact = Path(tempfile.mkdtemp(prefix="sb-tonal-")) / "tonaltarget.json"
    stats = master_file(args.input, args.output, args.loudness,
        reference=args.reference, match_source=args.match_source,
        strength=args.strength, eq_profile=args.eq_profile,
        lowpass=args.lowpass_cutoff, space_wet=args.space_wet, width_db=args.width_db,
        reference_cache=reference_cache, reference_cache_artifact=reference_cache_artifact)
    print(f"LUFS {stats['actual_lufs']:.2f}; TP {stats['true_peak_dbtp']:.2f} dBTP", flush=True)
    print("MASTERING_PROGRESS 100", flush=True)


if __name__ == "__main__":
    main()
