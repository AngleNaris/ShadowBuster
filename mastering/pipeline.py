"""Reference tone -> user EQ -> final mix width -> density -> independent finalizer."""
from pathlib import Path
import numpy as np
import soundfile as sf

from . import ALL_DENSITY_BANDS, ENGINE_VERSION
from .finalizer import DEFAULT_SOFT_CLIP_DB, finalize, write_output
from .guard import protected_match_curve, snapshot, violations
from .matchering_adapter import candidate, UnsuitableReference
from .reference_tone import tonal_target
from .resonance import tame_resonances
from .soundstage import DEFAULT_LOW_SIDE_AMOUNT, DEFAULT_LOW_SIDE_HZ, tighten_low_side, widen
from .density import densify
from .drum_attack import RESTORE_FRACTION, detect_attacks, restore_attacks
from .loud import finalize_loud, section_profile
from .tone import apply_eq


def master_file(input_path, output_path, loudness="normal", *, reference=None,
                match_source=None, strength=.85, eq_profile="Neutral", lowpass=None,
                space_wet=0.0, width_db=0.0, density="light", reference_cache=None,
                reference_cache_artifact=None, section_source=None, drums_source=None,
                density_scope=ALL_DENSITY_BANDS, soft_clip_db=DEFAULT_SOFT_CLIP_DB,
                resonance_amount=0.0, low_side_amount=DEFAULT_LOW_SIDE_AMOUNT,
                low_side_hz=DEFAULT_LOW_SIDE_HZ):
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
            # The tonal target depends only on (source, reference); `reference_cache`
            # lets a warm run skip the expensive Matchering candidate when only
            # downstream loudness / width / EQ changed (audit §9.8).
            frequencies, db, metadata = tonal_target(
                candidate, match_source or input_path, reference,
                temp_parent=Path(output_path).parent,
                cache=reference_cache, artifact=reference_cache_artifact)
            matched, ref_report = protected_match_curve(audio, frequencies, db, strength)
            ref_report["adapter"] = metadata
        except UnsuitableReference as exc:
            ref_report.update(status="fallback", reason=str(exc))
    elif reference:
        ref_report["reason"] = "zero_strength"
    print("MASTERING_PROGRESS 35", flush=True)
    sections = section_profile(audio, sr, section_source) if loudness == "loud" else None
    attacks = None
    attack_report = {"applied": False, "reason": "no_drums_source"}
    if density != "off" and density_scope != "none" and drums_source is not None:
        drums, drum_rate = sf.read(drums_source, always_2d=True, dtype="float64")
        if drum_rate == sr and len(drums) == len(audio) and np.isfinite(drums).all():
            attacks = detect_attacks(drums, sr)
            counts = {name: len(hits) for name, hits in attacks.items()}
            attack_report = {"applied": bool(sum(counts.values())), "detected_hits": counts}
            if attack_report["applied"]:
                attack_report["restore_fraction"] = RESTORE_FRACTION
            else:
                attack_report["reason"] = "no_drum_attacks"
        else:
            attack_report["reason"] = "invalid_or_unaligned_drums_source"
        del drums

    def finish(data):
        focused, low_side_report = tighten_low_side(data, sr, cutoff_hz=low_side_hz, amount=low_side_amount)
        tamed, resonance_report = tame_resonances(focused, sr, amount=resonance_amount)
        compressed, density_report = densify(tamed, density, scope=density_scope)
        report = attack_report if density_report["applied"] else {
            "applied": False, "reason": "density_inactive"}
        if report["applied"]:
            compressed = restore_attacks(tamed, compressed, sr, attacks)
        if sections is not None:
            result, stats = finalize_loud(compressed, sr, sections)
        else:
            result, stats = finalize(compressed, sr, loudness, soft_clip_db=soft_clip_db)
        stats["drum_attack"] = report
        stats["resonance"] = resonance_report
        stats["low_side"] = low_side_report
        return result, stats, density_report

    toned = apply_eq(matched, eq_profile, lowpass)
    shaped, width_report = widen(toned, space_wet, width_db)
    result, stats, density_report = finish(shaped)
    if ref_report["accepted_strength"] > 0:
        # Validate after limiting against the same EQ/width/loudness controls.
        # A safe pre-limiter candidate need not remain safe after limiting.
        base = apply_eq(audio, eq_profile, lowpass)
        base, base_width = widen(base, space_wet, width_db)
        baseline, base_stats, base_density = finish(base)
        before, after = snapshot(baseline), snapshot(result)
        reasons = violations(before, after)
        ref_report["final_validation"] = {"before": before, "after": after, "violations": reasons}
        if reasons:
            result, stats, width_report = baseline, base_stats, base_width
            density_report = base_density
            ref_report.update(status="fallback", accepted_strength=0.0, reason="final_output_budget")
    stats.update(engine=ENGINE_VERSION, style_mode="reference" if reference else (
        "eq_only" if eq_profile != "Neutral" else "off"),
        reference=ref_report, soundstage=width_report, density=density_report,
        eq_profile=eq_profile,
        spectral_processing=bool(ref_report["accepted_strength"] or eq_profile != "Neutral" or lowpass))
    print("MASTERING_PROGRESS 90", flush=True)
    return write_output(result, sr, output_path, stats)
