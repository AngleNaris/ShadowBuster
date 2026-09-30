"""Conservative, versioned engineering budgets; not a subjective quality score."""
import numpy as np
from scipy import ndimage, signal

from audio_metrics import guard_metrics

POLICY_VERSION = "reference-guard-v1"


def snapshot(audio, sr=44100):
    """Guard-decision metrics only (audit P1-2): the lightweight subset that
    `violations()` reads. The persisted quality report still uses the full
    `measure_audio`; this snapshot no longer pays for 4x true-peak resampling,
    LUFS gating, or band_widths that no guard ever inspects."""
    return guard_metrics(audio, sr)


def violations(before, after):
    # 声场保护语义（2026-09-30 裁决，与 widen() 相位 guard 统一）：只防
    # 「本次新增的损伤」——非预期收窄（丢失已授权宽度）与新引入的相位翻转。
    # 变宽本身、band width 变化与 mono fold-down 损失不再是违规依据：
    # 单声道兼容性不是缺陷，保护不得抑制用户的调整意志。
    reasons = []
    a, b = before["side_mid"]["db"], after["side_mid"]["db"]
    if a is not None and b is not None and b - a < -.5:
        reasons.append("side_mid_change")
    a, b = before["stereo_correlation"], after["stereo_correlation"]
    if a is not None and b is not None and a >= 0 and b < 0:
        reasons.append("stereo_correlation")
    a, b = before["crest_factor_db"], after["crest_factor_db"]
    if a is not None and b is not None and b - a < -1.0:
        reasons.append("crest_factor_db")
    for key, band in before["band_energies"].items():
        other = after["band_energies"][key]
        if band["relative_energy_db"] > -60 and abs(
                other["relative_energy_db"] - band["relative_energy_db"]) > 3.5:
            reasons.append("tone_budget:" + key)
    return reasons


def matching_curve(source, candidate, bandwidth, sr=44100):
    """Project candidate tone to a bounded shared L/R filter; discard its phase/MS gain."""
    nfft = min(8192, len(source))
    f, before = signal.welch(source, sr, nperseg=nfft, axis=0)
    _, after = signal.welch(candidate, sr, nperseg=nfft, axis=0)
    before, after = before.mean(axis=1), after.mean(axis=1)
    reliable = (before > max(float(np.max(before)) * 1e-6, 1e-20)) & (after > 1e-20)
    db = 10 * np.log10(np.maximum(after, 1e-30) / np.maximum(before, 1e-30))
    # Remove candidate loudness; only its broad tonal direction is relevant.
    weights = before[reliable]
    center = float(np.average(db[reliable], weights=weights)) if weights.size else 0.0
    grid = np.geomspace(20, sr / 2, 192)
    shape = np.interp(grid, f, np.where(reliable, db - center, 0.0))
    shape = ndimage.gaussian_filter1d(shape, 4, mode="nearest")
    # Missing reference bandwidth must never be interpreted as a high-frequency cut.
    fade = np.clip((bandwidth - grid) / max(500, bandwidth * .15), 0, 1)
    low = np.clip((grid - 20) / 60, 0, 1)
    shape = np.clip(shape, -3, 3) * fade * low
    return np.r_[0, grid], np.r_[0, shape]


def apply_curve(audio, frequencies, db, strength, sr=44100):
    if strength == 0 or not np.any(db):
        return audio.copy()
    # Odd FIR + centered convolution: no relative delay or raw-candidate blending.
    kernel = signal.firwin2(2049, frequencies, 10 ** (db * strength / 20), fs=sr)
    padded = np.pad(audio, ((1024, 1024), (0, 0)), mode="reflect")
    result = signal.fftconvolve(padded, kernel[:, None], mode="valid", axes=0)
    source_rms, result_rms = np.sqrt(np.mean(audio ** 2)), np.sqrt(np.mean(result ** 2))
    if result_rms:
        result *= source_rms / result_rms
    return result


def protected_match(audio, source, candidate, bandwidth, strength):
    frequencies, db = matching_curve(source, candidate, bandwidth)
    return protected_match_curve(audio, frequencies, db, strength)


def protected_match_curve(audio, frequencies, db, strength):
    """Guard the strength bisection against a precomputed tonal target curve.

    Split out of `protected_match` (audit §9.8) so the expensive `matching_curve`
    can be cached independently of downstream loudness / width / EQ; the guard
    itself always recomputes against the current `audio`."""
    before = snapshot(audio)
    attempts = []
    for amount in (strength, strength / 2, strength / 4, strength / 8):
        result = apply_curve(audio, frequencies, db, amount)
        after = snapshot(result)
        reasons = violations(before, after)
        attempts.append({"strength": amount, "violations": reasons})
        if not reasons:
            return result, {"policy": POLICY_VERSION, "requested_strength": strength,
                "accepted_strength": amount, "status": "accepted", "attempts": attempts,
                "curve_frequencies_hz": frequencies.tolist(), "curve_db": (db * amount).tolist(),
                "before": before, "after": after}
    return audio.copy(), {"policy": POLICY_VERSION, "requested_strength": strength,
        "accepted_strength": 0.0, "status": "fallback", "reason": "matching_budget",
        "attempts": attempts, "before": before, "after": before}
