"""Strict loudness/peak-only path; no reference audio or Mid/Side processing."""
import numpy as np
from scipy import signal


def process_transparent(target, config, requested_lufs, core):
    target = np.array(target, dtype=np.float64, copy=True)
    if target.ndim != 2 or target.shape[0] != 2 or not target.shape[1]:
        raise ValueError('Transparent mastering requires nonempty stereo audio')
    if not np.isfinite(target).all():
        raise ValueError('Transparent input must be finite')
    sr = config.internal_sample_rate
    initial = core.calculate_lufs(target, sr)
    if requested_lufs is None or not np.isfinite(requested_lufs):
        raise ValueError('Transparent mastering requires a finite target LUFS')
    if not np.isfinite(initial):
        raise ValueError('Cannot loudness-normalize silent audio')
    up = core.oversample(target, config.oversampling_factor)
    drive = requested_lufs - core.calculate_lufs(up, sr * config.oversampling_factor)
    best = None
    # Always render from the unchanged input, never cascade limiters between trials.
    for attempt in range(12):
        limited, stats = core.linked_lookahead_limiter(up * 10 ** (drive / 20), config)
        result = signal.resample_poly(limited, 1, config.oversampling_factor, axis=-1)
        tp = core.calculate_true_peak(result, sr, config.true_peak_oversampling)
        trim = min(0.0, config.true_peak_ceiling_db - 0.01 - tp)
        result *= 10 ** (trim / 20)
        measured = core.calculate_lufs(result, sr)
        error = requested_lufs - measured
        candidate = (abs(error), result, stats, measured, trim, drive, attempt + 1)
        if best is None or candidate[0] < best[0]:
            best = candidate
        if abs(error) <= 0.1:
            break
        if drive > 30 or (attempt > 0 and abs(error) >= previous_error - 0.005):
            break
        previous_error = abs(error)
        drive += float(np.clip(error, -3, 3))
    error, result, stats, measured, trim, drive, attempts = best
    result = core.apply_dither(result)
    tp = core.calculate_true_peak(result, sr, config.true_peak_oversampling)
    if tp > config.true_peak_ceiling_db:
        raise RuntimeError('Transparent output exceeds true peak ceiling')
    config.last_mastering_stats = {
        'style_mode': 'off', 'target_lufs': float(requested_lufs),
        'input_lufs': float(initial), 'actual_lufs': float(core.calculate_lufs(result, sr)),
        'target_error_lu': float(error), 'target_met': bool(error <= 0.2),
        'applied_gain_db': float(drive), 'safety_trim_db': float(trim),
        'true_peak_dbtp': float(tp), 'limiter': stats, 'iterations': attempts,
        'spectral_processing': False,
    }
    return result
