"""Vocal Mid control relative to the pre-enhancement vocal/accompaniment balance."""
import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

from audio_validation import finite_range, validate_audio_pair

FRAME_SECONDS = 0.100
HOP_SECONDS = 0.050
# Historical fixed target retained only for explicitly requested legacy callers.
# Production defaults use each original recording via REFERENCE_MODE.
DEFAULT_BALANCE_TARGET_DB = -3.0  # Explicit legacy target only.
from vocal_config import REFERENCE_MODE, GAIN_LIMIT_DB


def _audio(value, name):
    value = np.asarray(value)
    if value.ndim != 2 or value.shape[1] not in (1, 2):
        raise ValueError(f"{name} must be mono/stereo 2-D audio")
    if not len(value) or not np.isfinite(value).all():
        raise ValueError(f"{name} must contain nonempty finite audio")
    return value.astype(np.float64, copy=False)


def _rate(sr):
    if not np.isfinite(sr) or sr <= 0 or int(sr) != sr:
        raise ValueError("sample rate must be a positive integer")
    return int(sr)


def _mid(audio):
    return audio.mean(axis=1)


def mid_rms_windows(audio, sr, frame_seconds=FRAME_SECONDS, hop_seconds=HOP_SECONDS):
    """Include the final partial window without inventing a negative start index."""
    x = _mid(_audio(audio, "audio"))
    sr = _rate(sr)
    finite_range(frame_seconds, "frame_seconds", 0.001, 1)
    finite_range(hop_seconds, "hop_seconds", 0.001, frame_seconds)
    frame = max(1, round(sr * frame_seconds))
    hop = max(1, round(sr * hop_seconds))
    starts = np.arange(0, max(1, len(x) - frame + 1), hop)
    last = max(0, len(x) - frame)
    if starts[-1] != last:
        starts = np.append(starts, last)
    ends = np.minimum(starts + frame, len(x))
    power = np.r_[0.0, np.cumsum(x * x)]
    rms = np.sqrt(np.maximum(0, (power[ends] - power[starts]) / (ends - starts)))
    return (starts + ends) / (2 * sr), rms


def vocal_activity_metrics(mix, vocals, sr, frame_seconds=FRAME_SECONDS, hop_seconds=HOP_SECONDS):
    """Shared offline calibration/control windows, not a semantic vocal detector.

    ``active`` is sustained vocal Mid energy excluding obvious (< -30 dB)
    leakage. ``ratio_valid``/``valid`` also require measurable accompaniment.
    ``gate`` holds reliable activity 150 ms then releases to zero in 250 ms.
    Separated instruments can still pass; acapella/Side-only vocals cannot
    supply a reliable Mid balance target. Arrays share ``centers`` in seconds.
    """
    m, v = _audio(mix, "mix"), _audio(vocals, "vocals")
    sr = _rate(sr)
    validate_audio_pair(m, v, sr)
    centers, vr = mid_rms_windows(v, sr, frame_seconds, hop_seconds)
    _, ar = mid_rms_windows(m - v, sr, frame_seconds, hop_seconds)
    on = max(1e-6, float(np.percentile(vr, 95)) * .10)
    off = on * .5
    ratio = np.full(len(vr), np.nan)
    good = (vr > 1e-12) & (ar > 1e-12)
    ratio[good] = 20 * np.log10(vr[good] / ar[good])
    leakage = good & (ratio < -30)
    active = np.zeros(len(vr), dtype=bool)
    state = False
    for i, x in enumerate(vr):
        state = bool(x >= (off if state else on) and not leakage[i])
        active[i] = state

    def segments(mask):
        edges = np.diff(np.r_[False, mask, False].astype(int))
        return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))

    # Offline lookahead rejects isolated windows, rather than delaying every
    # onset by a confirmation timer. Sub-100 ms clips conservatively bypass.
    for start, end in segments(active):
        duration = min(len(m) / sr, centers[end-1] - centers[start] + hop_seconds)
        if duration < .100 - 1e-9:
            active[start:end] = False
    ratio_valid = active & (ar > np.maximum(1e-6, vr * .01)) & good
    gate = np.zeros(len(centers))
    for start, end in segments(ratio_valid):
        # Smooth onset; steady audio starting at the file boundary needs no fade.
        attack = (np.ones(end-start) if start == 0 else
                  np.clip((centers[start:end] - centers[start] + hop_seconds) / .100, 0, 1))
        gate[start:end] = np.maximum(gate[start:end], attack)
        last = centers[end-1]
        stop = np.searchsorted(centers, last + .150 + .250, side="right")
        release = np.clip((centers[end:stop] - last - .150) / .250, 0, 1)
        tail = attack[-1] * (1 - release * release * (3 - 2 * release))
        gate[end:stop] = np.maximum(gate[end:stop], tail)
    return {"centers": centers, "vocal_rms": vr, "accompaniment_rms": ar,
            "ratio_db": ratio, "active": active, "ratio_valid": ratio_valid,
            "valid": ratio_valid, "gate": gate, "leakage": leakage,
            "on_threshold": on, "off_threshold": off}


def reference_mid_prominence_metrics(reference_mix, reference_vocals, sr,
                                      current_mix=None, current_vocals=None,
                                      frame_seconds=FRAME_SECONDS, hop_seconds=HOP_SECONDS):
    """Ratios are residual estimates, not a separation confidence score."""
    rm = _audio(reference_mix, "reference-mix")
    rv = _audio(reference_vocals, "reference-vocals")
    validate_audio_pair(rm, rv, sr)
    cm = rm if current_mix is None else _audio(current_mix, "current-mix")
    cv = rv if current_vocals is None else _audio(current_vocals, "current-vocals")
    validate_audio_pair(rm, cm, sr)
    validate_audio_pair(cm, cv, sr)
    def rms(x):
        return mid_rms_windows(x, sr, frame_seconds, hop_seconds)
    centers, vr = rms(rv)
    _, ar = rms(rm - rv)
    _, vc = rms(cv)
    _, ac = rms(cm - cv)
    def ratio(v, a):
        result = np.full(len(v), np.nan)
        good = (v > 1e-12) & (a > 1e-12)
        result[good] = 20 * np.log10(v[good] / a[good])
        return result
    # Activity is relative to vocal energy, never correlation with a loud accompaniment.
    active = vr >= max(1e-6, float(vr.max()) * 0.05)
    ref_floor = np.maximum(1e-6, vr * 0.01)
    cur_floor = np.maximum(1e-6, vc * 0.01)
    valid = active & (ar > ref_floor) & (ac > cur_floor) & (vc > 1e-8)
    return {"centers": centers, "reference_ratio_db": ratio(vr, ar),
            "current_ratio_db": ratio(vc, ac), "active": active, "valid": valid}


def _json_numbers(values):
    return [float(v) if np.isfinite(v) else None for v in values]


def _spatial(audio):
    mid = _mid(audio)
    side = (audio[:, 0] - audio[:, -1]) * 0.5
    return {"mid_rms": float(np.sqrt(np.mean(mid * mid))),
            "side_rms": float(np.sqrt(np.mean(side * side)))}


def _masking_eq(residual, vocals, sr, metrics, frame_seconds=FRAME_SECONDS,
                hop_seconds=HOP_SECONDS):
    """Fixed bandpass deltas on residual Mid only; no coefficient modulation.

    Each unity-peak band gets half the linear 1.5 dB attenuation budget.
    This bounds the frozen-envelope frequency response, not instantaneous
    waveform amplitude or perceptual masking/separation accuracy.
    """
    centers = metrics["centers"]
    times = np.arange(len(residual)) / sr
    result = np.zeros(len(residual))
    envelopes = []
    band_reports = []
    budget = (1 - 10 ** (-1.5 / 20)) / 2
    for frequency in (1500.0, 3000.0):
        envelope = np.zeros(len(centers))
        if frequency < sr * .45:
            b, a = signal.iirpeak(frequency, Q=.8, fs=sr)
            accompaniment_band = signal.lfilter(b, a, _mid(residual))
            vocal_band = signal.lfilter(b, a, _mid(vocals))
            _, ar = mid_rms_windows(accompaniment_band[:, None], sr, frame_seconds, hop_seconds)
            _, vr = mid_rms_windows(vocal_band[:, None], sr, frame_seconds, hop_seconds)
            reliable = (metrics["ratio_valid"] &
                        (vr > np.maximum(1e-6, metrics["vocal_rms"] * .10)))
            request = np.where(reliable, np.clip((ar / np.maximum(vr, 1e-12) - .5) / 1.5, 0, 1), 0)
            previous = 0.0
            hold_until = -1.0
            for i, now in enumerate(centers):
                dt = min(HOP_SECONDS, now) if i == 0 else now - centers[i-1]
                if request[i] > 0:
                    hold_until = now + .200
                target = request[i] if request[i] > 0 else (previous if now < hold_until else 0)
                # Finite slew, not asymptotic release; zero within 500 ms.
                previous += np.clip(target - previous, -dt / .500, dt / .100)
                envelope[i] = np.clip(previous, 0, 1)
            sample_envelope = np.interp(times, np.r_[0, centers, centers[-1] + .700],
                                        np.r_[0, envelope, 0])
            result -= budget * accompaniment_band * sample_envelope
        envelopes.append(envelope)
        band_reports.append({"center_hz": frequency, "q": .8,
                             "max_envelope": float(envelope.max()),
                             "envelope": _json_numbers(envelope)})
    return result[:, None], {"bands": band_reports, "max_total_attenuation_db": 1.5,
        "linear_delta_budget": 2 * budget, "attack_ms": 100, "hold_ms": 200,
        "release_ms": 500, "scope": "accompaniment_residual_mid_only",
        "max_used_linear_budget": float(budget * np.max(np.sum(envelopes, axis=0))),
        "bound_interpretation": "Frozen-envelope frequency response; not instantaneous sample gain."}


def apply_mid_prominence_control(in_mix, vocals, sr, user_gain_db=0.0,
                                 reference_mix=None, reference_vocals=None,
                                 frame_seconds=FRAME_SECONDS, hop_seconds=HOP_SECONDS,
                                 vocal_scale=1.0, return_report=False,
                                 balance_target_db=None, balance_mode=None):
    """Return unnormalized audio; reports measure estimated sources before mastering.

    vocal_scale follows ONLY global attenuation upstream. Separation leakage and
    unscaled stem deltas remain part of the accompaniment residual estimate.
    """
    mix = _audio(in_mix, "in-mix")
    raw_v = _audio(vocals, "vocals")
    sr = _rate(sr)
    user = finite_range(user_gain_db, "vocal-gain-db", -12, 12)
    target_db = None if balance_target_db is None else finite_range(
        balance_target_db, "balance-target-db", -np.finfo(float).max, np.finfo(float).max)
    scale = finite_range(vocal_scale, "vocal-scale", np.finfo(float).tiny, 1)
    validate_audio_pair(mix, raw_v, sr)
    if reference_vocals is not None and reference_mix is None:
        raise ValueError("reference-vocals requires reference-mix")
    v = raw_v * scale
    metrics = None
    auto_metrics = None
    sample_db = np.full(len(mix), user)
    limited = np.zeros(0, dtype=bool)
    original_mode = balance_mode == REFERENCE_MODE
    if balance_mode not in (None, REFERENCE_MODE):
        raise ValueError("unknown balance mode")
    if original_mode and (reference_mix is None or reference_vocals is None):
        raise ValueError("original reference mode requires explicit original mix and vocals")
    reference_median = None
    if target_db is not None or original_mode:
        finite_range(user, "Mid target dB", -6, 6)
        # Auto takes precedence, but supplied references must still be valid.
        if reference_mix is not None:
            rm = _audio(reference_mix, "reference-mix")
            rv = _audio(raw_v if reference_vocals is None else reference_vocals, "reference-vocals")
            validate_audio_pair(mix, rm, sr)
            validate_audio_pair(rm, rv, sr)
        auto_metrics = vocal_activity_metrics(mix, v, sr, frame_seconds, hop_seconds)
        centers, valid = auto_metrics["centers"], auto_metrics["ratio_valid"]
        ratio = auto_metrics["ratio_db"]
        if original_mode:
            reference_metrics = vocal_activity_metrics(rm, rv, sr, frame_seconds, hop_seconds)
            # The identical intersection mask evaluates BOTH medians, never
            # median(window differences). Original activity owns eligibility.
            valid = valid & reference_metrics["ratio_valid"]
            auto_metrics["ratio_valid"] = auto_metrics["valid"] = valid
            auto_metrics["gate"] = auto_metrics["gate"] * valid
            reference_median = float(np.median(reference_metrics["ratio_db"][valid])) if valid.any() else None
            target_db = reference_median if reference_median is not None else 0.0
        median_ratio = float(np.median(ratio[valid])) if valid.any() else None
        desired_gain = target_db + user - median_ratio if median_ratio is not None else 0.0
        fixed_gain = float(np.clip(desired_gain, -GAIN_LIMIT_DB, GAIN_LIMIT_DB))
        limited = valid & (abs(desired_gain - fixed_gain) > 1e-8)
        sample_db.fill(fixed_gain)
        vocal_delta = (_mid(v) * np.expm1(fixed_gain*np.log(10)/20))[:, None]
        eq_delta, eq_report = _masking_eq(mix - v, v, sr, auto_metrics,
                                         frame_seconds, hop_seconds)
        delta = vocal_delta + eq_delta
    elif reference_mix is not None:
        finite_range(user, "Mid target dB", -6, 6)
        metrics = reference_mid_prominence_metrics(
            reference_mix, raw_v if reference_vocals is None else reference_vocals,
            sr, mix, v, frame_seconds, hop_seconds)
        valid = metrics["valid"]
        correction = np.zeros(len(valid))
        correction[valid] = (metrics["reference_ratio_db"][valid]
                             - metrics["current_ratio_db"][valid])
        desired = user + correction
        low, high = (0.0, 9.0) if user > 0 else (-9.0, 0.0)
        bounded = np.clip(user + np.clip(correction, -3, 3), low, high)
        limited = valid & (np.abs(bounded - desired) > 1e-8)
        if user == 0:
            bounded[:] = 0
            limited[:] = False
        # Smooth calibration, not the user's constant offset. 150 ms avoids
        # chasing individual syllables; interpolation keeps the envelope continuous.
        smooth = bounded.copy()
        centers = metrics["centers"]
        for index in range(1, len(smooth)):
            alpha = -np.expm1(-(centers[index] - centers[index - 1]) / 0.150)
            smooth[index] = smooth[index - 1] + alpha * (bounded[index] - smooth[index - 1])
        sample_db = np.interp(np.arange(len(mix)) / sr, centers, smooth)
        delta = (_mid(v) * np.expm1(sample_db * np.log(10) / 20))[:, None]
    else:
        # Standalone callers without a reference retain the legacy full-stem gain.
        delta = v * np.expm1(user * np.log(10) / 20)
    out = mix.copy() if (user == 0 and auto_metrics is None) else mix + delta
    if not np.isfinite(out).all():
        raise ValueError("non-finite control output")
    if not return_report:
        return out
    report = {"schema_version": 1, "mode": "auto_fixed_target" if auto_metrics is not None else ("mid_ratio" if metrics is not None else "legacy_stem_gain"),
              "target_offset_db": user, "fixed_baseline_db": target_db, "vocal_scale": scale, "bypass": user == 0 and target_db is None,
              "sample_rate": sr, "frames": len(mix), "channels": mix.shape[1],
              "evaluation_endpoint": "pre_master", "source_model": "vocals plus accompaniment residual estimate",
              "actual_gain_min_db": float(sample_db.min()), "actual_gain_max_db": float(sample_db.max()),
              "gain_max_step_db": float(np.max(np.abs(np.diff(sample_db)), initial=0)),
              "before": _spatial(mix), "after": _spatial(out)}
    if auto_metrics is not None:
        estimated_v = v + vocal_delta
        estimated_a = mix - v + eq_delta
        _, after_vr = mid_rms_windows(estimated_v, sr, frame_seconds, hop_seconds)
        _, after_ar = mid_rms_windows(estimated_a, sr, frame_seconds, hop_seconds)
        measured = 20 * np.log10(np.maximum(after_vr, 1e-12) / np.maximum(after_ar, 1e-12))
        errors = measured[valid] - (target_db + user)
        report.update({"algorithm": "stable_mid_gain_residual_dynamic_eq_v1",
                       "fixed_vocal_mid_gain_db": fixed_gain,
                       "requested_target_db": target_db + user,
                       "median_input_ratio_db": median_ratio,
                       "gain_bounds_db": [-9, 9], "gain_is_activity_gated": False,
                       "limited_window_count": int(limited.sum()),
                       "eq": eq_report,
                       "eq_gate": "reliable vocal activity AND band vocal energy AND accompaniment masking; finite hold/release",
                       "estimated_ratio_error_p50_db": float(np.median(errors)) if len(errors) else None,
                       "estimated_absolute_ratio_error_p95_db": float(np.percentile(abs(errors), 95)) if len(errors) else None,
                       "reconstruction_max_error": float(np.max(abs(out - (estimated_v + estimated_a)))),
                       "side_delta_max_error": float(np.max(abs((out[:, 0]-out[:, -1]) - (mix[:, 0]-mix[:, -1]))))})
        report.update({"active_window_count": int(auto_metrics["active"].sum()),
                       "ratio_valid_window_count": int(auto_metrics["ratio_valid"].sum()),
                       "windows": {"center_seconds": _json_numbers(auto_metrics["centers"]),
                                   "active": auto_metrics["active"].tolist(), "ratio_valid": auto_metrics["ratio_valid"].tolist(),
                                   "ratio_db": _json_numbers(auto_metrics["ratio_db"]),
                                   "gain_db": _json_numbers(sample_db[::max(1, round(sr*hop_seconds))])},
                       "limitations": "Energy/confidence heuristic, not semantic VAD; leakage and acapella ratios are invalid."})
    if original_mode:
        report.update(schema_version=2, mode=REFERENCE_MODE, algorithm=REFERENCE_MODE,
                      target_source="original_input_htdemucs", median_reference_ratio_db=reference_median,
                      estimator="median(reference[matched_valid])-median(current[matched_valid])+offset",
                      reference_scale=1.0, reference_and_current_same_mask=True,
                      matched_valid_mask=valid.tolist(),
                      median_output_ratio_db=float(np.median(measured[valid])) if valid.any() else None,
                      median_target_error_db=float(np.median(measured[valid]) - target_db - user) if valid.any() else None)
    if metrics is not None:
        v_out = v + (out - mix)
        measured = reference_mid_prominence_metrics(reference_mix,
            raw_v if reference_vocals is None else reference_vocals, sr, out, v_out,
            frame_seconds, hop_seconds)
        target = metrics["reference_ratio_db"] + user
        errors = measured["current_ratio_db"] - target
        valid = metrics["valid"] & np.isfinite(errors)
        report.update({"frame_ms": frame_seconds * 1000, "hop_ms": hop_seconds * 1000,
                       "window_count": len(valid), "active_window_count": int(metrics["active"].sum()),
                       "valid_window_count": int(valid.sum()), "valid_coverage": float(valid.mean()),
                       "fallback_window_count": int((~valid).sum()), "limited_window_count": int(limited.sum()),
                       "absolute_error_p50_db": float(np.median(np.abs(errors[valid]))) if valid.any() else None,
                       "absolute_error_p95_db": float(np.percentile(np.abs(errors[valid]), 95)) if valid.any() else None,
                       "windows": {"center_seconds": _json_numbers(metrics["centers"]),
                                   "reference_ratio_db": _json_numbers(metrics["reference_ratio_db"]),
                                   "before_ratio_db": _json_numbers(metrics["current_ratio_db"]),
                                   "target_ratio_db": _json_numbers(target),
                                   "after_ratio_db": _json_numbers(measured["current_ratio_db"]),
                                   "error_db": _json_numbers(errors), "valid": valid.tolist()}})
    return out, report


def main():
    ap = argparse.ArgumentParser(description="Vocal Mid prominence relative to pre-enhancement balance")
    ap.add_argument("--vocals", required=True, type=Path)
    ap.add_argument("--in-mix", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--vocal-gain-db", type=float, default=0.0)
    ap.add_argument("--balance-target-db", type=float)
    ap.add_argument("--balance-mode", choices=[REFERENCE_MODE])
    ap.add_argument("--vocal-scale", type=float, default=1.0)
    ap.add_argument("--reference-mix", type=Path)
    ap.add_argument("--reference-vocals", type=Path)
    ap.add_argument("--report-json", type=Path)
    args = ap.parse_args()
    vocals, sr = sf.read(args.vocals, always_2d=True)
    mix, sr2 = sf.read(args.in_mix, always_2d=True)
    try:
        if sr != sr2:
            raise ValueError("sample rate mismatch between vocals and mix")
        ref_mix = ref_vocals = None
        if args.reference_vocals and not args.reference_mix:
            raise ValueError("reference-vocals requires reference-mix")
        if args.reference_mix:
            ref_mix, rsr = sf.read(args.reference_mix, always_2d=True)
            ref_vocals, vsr = sf.read(args.reference_vocals or args.vocals, always_2d=True)
            if rsr != sr or vsr != sr:
                raise ValueError("reference files must use the same sample rate")
        out, report = apply_mid_prominence_control(
            mix, vocals, sr, args.vocal_gain_db, ref_mix, ref_vocals,
            vocal_scale=args.vocal_scale, return_report=True, balance_target_db=args.balance_target_db,
            balance_mode=args.balance_mode)
    except ValueError as exc:
        ap.error(str(exc))
    peak = float(np.abs(out).max())
    ceiling = 10 ** (-0.5 / 20)
    auto_mode = args.balance_target_db is not None or args.balance_mode == REFERENCE_MODE
    scale = 1.0 if auto_mode else (min(1.0, ceiling / peak) if peak and args.vocal_gain_db != 0 else 1.0)
    out *= scale
    if args.vocal_gain_db == 0 and not auto_mode:
        if args.in_mix.resolve() != args.out.resolve():
            shutil.copyfile(args.in_mix, args.out)
    else:
        sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    report_path = args.report_json or args.out.with_suffix(".vocal.json")
    written, _ = sf.read(args.out, always_2d=True)
    oversampled = signal.resample_poly(written, 4, 1, axis=0, window=("kaiser", 8.6), padtype="constant")
    report.update({"reference_mix_path": str(args.reference_mix) if args.reference_mix else None,
                   "reference_vocals_path": str(args.reference_vocals) if args.reference_vocals else None,
                   "current_mix_path": str(args.in_mix), "current_vocals_path": str(args.vocals),
                   "alignment": "common 44100-Hz decode/sample origin; exact frame/rate validation, no time stretching",
                   "output_global_scale": scale, "sample_peak": float(np.abs(written).max()),
                   "true_peak_4x": float(max(np.abs(written).max(), np.abs(oversampled).max()))})
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Vocal control: mode={report['mode']} algorithm={report.get('algorithm', 'compatible')} fixed_gain={report.get('fixed_vocal_mid_gain_db')} target={args.vocal_gain_db:+.1f}dB | global scale={scale:.8f} | {report_path}")


if __name__ == "__main__":
    main()
