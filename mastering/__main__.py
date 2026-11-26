"""Audio-runtime subprocess entry point; keeps scientific imports out of Qt."""
import argparse
import sys
import tempfile
from pathlib import Path

from . import ALL_DENSITY_BANDS, LOUDNESS_TARGETS, normalize_density_scope
from .density import DENSITY_MODES
from .finalizer import DEFAULT_SOFT_CLIP_DB
from .pipeline import master_file
from .soundstage import DEFAULT_LOW_SIDE_AMOUNT, DEFAULT_LOW_SIDE_HZ


def main():
    parser = argparse.ArgumentParser(description="ShadowBuster independent mastering")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--loudness", choices=tuple(LOUDNESS_TARGETS), default="normal")
    parser.add_argument("--reference")
    parser.add_argument("--match-source")
    parser.add_argument("--section-source", help="Original input for the loud preset's section curve.")
    parser.add_argument("--drums-source", help="Aligned drum stem for loud preset attack protection.")
    parser.add_argument("--strength", type=float, default=.85)
    parser.add_argument("--eq-profile", default="Neutral")
    parser.add_argument("--lowpass-cutoff", type=float)
    parser.add_argument("--space-wet", type=float, default=0.0)
    parser.add_argument("--width-db", type=float, default=0.0)
    parser.add_argument("--density", choices=DENSITY_MODES, default="light",
                        help="Section-relative multiband density compression before the limiter.")
    parser.add_argument("--no-reference-cache", action="store_true",
                        help="Disable the content-addressed reference tonal target cache.")
    parser.add_argument("--density-scope", type=normalize_density_scope, default=ALL_DENSITY_BANDS,
                        help="global, none or a comma-separated selection of low,mid,high")
    parser.add_argument("--soft-clip-db", type=float, default=DEFAULT_SOFT_CLIP_DB,
                        help="Bounded 4x oversampled pre-limiter soft clip depth in dB (0=off).")
    parser.add_argument("--resonance-amount", type=float, default=0.0,
                        help="Bounded dynamic resonance suppression 0-1 (0=off).")
    parser.add_argument("--low-side-amount", type=float, default=DEFAULT_LOW_SIDE_AMOUNT,
                        help="Gentle low-end Side Mono-Maker tightening 0-1 (0=off).")
    parser.add_argument("--low-side-hz", type=float, default=DEFAULT_LOW_SIDE_HZ,
                        help="Cutoff frequency (Hz) for low-end Side Mono-Maker.")
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
        lowpass=args.lowpass_cutoff, space_wet=args.space_wet, width_db=args.width_db, density=args.density,
        reference_cache=reference_cache, reference_cache_artifact=reference_cache_artifact,
        section_source=args.section_source, drums_source=args.drums_source, density_scope=args.density_scope,
        soft_clip_db=args.soft_clip_db, resonance_amount=args.resonance_amount,
        low_side_amount=args.low_side_amount, low_side_hz=args.low_side_hz)
    print(f"LUFS {stats['actual_lufs']:.2f}; TP {stats['true_peak_dbtp']:.2f} dBTP", flush=True)
    print("MASTERING_PROGRESS 100", flush=True)


if __name__ == "__main__":
    main()
