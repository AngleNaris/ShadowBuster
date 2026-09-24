"""Reference tone -> user EQ -> final mix width -> independent finalizer."""
from pathlib import Path
import numpy as np
import soundfile as sf

from . import ENGINE_VERSION
from .finalizer import finalize, write_output
from .guard import protected_match, snapshot, violations
from .matchering_adapter import candidate, UnsuitableReference
from .soundstage import widen
from .tone import apply_eq


def master_file(input_path, output_path, loudness="normal", *, reference=None,
                match_source=None, strength=.85, eq_profile="Neutral", lowpass=None,
                space_wet=0.0, width_db=0.0):
    if Path(input_path).resolve() == Path(output_path).resolve():
        raise ValueError("Input and output must be different files")
    if not np.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("Reference strength must be within [0, 1]")
    audio, sr = sf.read(input_path, always_2d=True, dtype="float64")
    if sr != 44100 or audio.shape[1] != 2 or not len(audio) or not np.isfinite(audio).all():
        raise ValueError("Mastering requires finite 44100 Hz stereo input")
    ref_report = {"status": "disabled", "requested_strength": strength, "accepted_strength": 0.0}
    matched = audio
    if reference and strength > 0:
        print("MASTERING_PROGRESS 10", flush=True)
        try:
            source, raw, metadata = candidate(match_source or input_path, reference,
                                              temp_parent=Path(output_path).parent)
            matched, ref_report = protected_match(audio, source, raw,
                                                   metadata["reference_bandwidth_hz"], strength)
            ref_report["adapter"] = metadata
        except UnsuitableReference as exc:
            ref_report.update(status="fallback", reason=str(exc))
    elif reference:
        ref_report["reason"] = "zero_strength"
    print("MASTERING_PROGRESS 35", flush=True)
    toned = apply_eq(matched, eq_profile, lowpass)
    shaped, width_report = widen(toned, space_wet, width_db)
    result, stats = finalize(shaped, sr, loudness)
    if ref_report["accepted_strength"] > 0:
        # Validate after limiting against the same EQ/width/loudness controls.
        # A safe pre-limiter candidate need not remain safe after limiting.
        base = apply_eq(audio, eq_profile, lowpass)
        base, base_width = widen(base, space_wet, width_db)
        baseline, base_stats = finalize(base, sr, loudness)
        before, after = snapshot(baseline), snapshot(result)
        reasons = violations(before, after)
        ref_report["final_validation"] = {"before": before, "after": after, "violations": reasons}
        if reasons:
            result, stats, width_report = baseline, base_stats, base_width
            ref_report.update(status="fallback", accepted_strength=0.0, reason="final_output_budget")
    stats.update(engine=ENGINE_VERSION, style_mode="reference" if reference else (
        "eq_only" if eq_profile != "Neutral" else "off"),
        reference=ref_report, soundstage=width_report, eq_profile=eq_profile,
        spectral_processing=bool(ref_report["accepted_strength"] or eq_profile != "Neutral" or lowpass))
    print("MASTERING_PROGRESS 90", flush=True)
    return write_output(result, sr, output_path, stats)
