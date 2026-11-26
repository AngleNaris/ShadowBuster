"""Final mix width: amplify existing Side above 120 Hz, never sum old stem deltas."""
import math

import numpy as np
from scipy import fft, signal

from audio_metrics import guard_metrics

# 相位翻转保护（2026-09-30 裁决）：宽度增长由用户请求全额授权，唯一保护是
# 「本次处理新引入的相位翻转」——输入 broadband correlation 非负、而请求会把它
# 推成负值时，二分收回至 correlation ≥ 0 所需的最小增益。输入本就负相关的素材
# （部分歌曲存在单声道内容或超宽混音）不是损伤，不做任何干预；mono fold-down
# 与 Side 能量占比（旧 GLOBAL_SIDE_FRACTION / WIDTH_BANDS 封顶）不再作为保护
# 依据——单声道兼容性不是缺陷，保护不得抑制用户的调整意志。


def _side_correlation(em, es, ed, cross, cms, cm, gain):
    """Broadband L/R correlation after side' = side + gain*added (DC-centered).

    Matches audio_metrics.measure_audio's stereo_correlation definition:
    corr = E[LR] / sqrt(E[L^2] E[R^2]) with L = mid+side', R = mid-side'.
    """
    es_after = es + 2 * gain * cross + gain * gain * ed
    cms_after = cms + gain * cm
    total = em + es_after
    variance = (total + 2 * cms_after) * (total - 2 * cms_after)
    if variance <= 0:
        return 0.0
    return (em - es_after) / math.sqrt(variance)


def widen(audio, wet=0.0, width_db=0.0, sr=44100):
    if not np.isfinite([wet, width_db]).all() or not 0 <= wet <= 1 or not 0 <= width_db <= 12:
        raise ValueError("Invalid final soundstage parameters")
    report = {"requested_wet": wet, "requested_width_db": width_db,
              "applied": False, "accepted_delta_gain": 0.0}
    if wet == 0 or width_db == 0 or len(audio) < 32:
        return audio.copy(), report
    mid, side = audio.mean(axis=1), (audio[:, 0] - audio[:, 1]) / 2
    if float(np.mean(side ** 2)) < 1e-16:
        report["reason"] = "no_existing_side"
        return audio.copy(), report
    padded = np.pad(side, (2048, 2048), mode="reflect")
    n = fft.next_fast_len(len(padded))
    freq = fft.rfftfreq(n, 1 / sr)
    mask = .5 - .5 * np.cos(np.pi * np.clip((freq - 120) / 120, 0, 1))
    added = fft.irfft(fft.rfft(padded, n) * mask, n)[2048:2048 + len(side)]
    if float(np.mean(side * added)) <= 0:
        report["reason"] = "nonconstructive_side_delta"
        return audio.copy(), report
    requested = wet * (10 ** (width_db / 20) - 1)
    m = mid - mid.mean()
    s = side - side.mean()
    d = added - added.mean()
    moments = (float(np.mean(m * m)), float(np.mean(s * s)), float(np.mean(d * d)),
               float(np.mean(s * d)), float(np.mean(m * s)), float(np.mean(m * d)))
    corr = lambda gain: _side_correlation(*moments, gain)
    admitted = requested
    input_corr, unlimited_corr = corr(0.0), corr(requested)
    if input_corr >= 0 and unlimited_corr < 0:
        low, high = 0.0, requested
        for _ in range(40):
            probe = (low + high) / 2
            if corr(probe) >= 0:
                low = probe
            else:
                high = probe
        admitted = low
        report["phase_guard"] = {"input_correlation": input_corr,
                                 "unlimited_correlation": unlimited_corr,
                                 "admitted_correlation": corr(admitted)}
    if admitted <= 0:
        report["reason"] = "phase_inversion_guard"
        return audio.copy(), report
    new_side = side + admitted * added
    result = np.column_stack((mid + new_side, mid - new_side))
    # widen() only guards the broadband Mid/Side ratio (width_decreased check below),
    # so request the band-free guard metrics: no whole-file spectrum, no true-peak
    # resample, no LUFS. Full measure_audio still runs once for the quality report.
    before = guard_metrics(audio, sr, bands=False)
    after = guard_metrics(result, sr, bands=False)
    # Reconstruction boundaries must not turn an intended increase into a loss.
    a, b = before["side_mid"]["db"], after["side_mid"]["db"]
    if a is not None and b is not None and b < a - .05:
        report.update(reason="width_decreased", before=before, after=before)
        return audio.copy(), report
    report.update(applied=True, accepted_delta_gain=admitted, before=before, after=after)
    return result, report


DEFAULT_LOW_SIDE_HZ = 90.0
DEFAULT_LOW_SIDE_AMOUNT = 0.7


def tighten_low_side(audio, sr=44100, cutoff_hz=DEFAULT_LOW_SIDE_HZ, amount=0.0):
    """Gentle 12 dB/oct zero-phase Side high-pass (Elliptical Mono-Maker).

    Tightens excessive sub-bass Side spread below ``cutoff_hz`` without touching
    Mid (bit-exact preserved) or narrowing stereo width above ~2.5*cutoff_hz.
    ``amount == 0`` or ``cutoff_hz == 0`` returns a copy unchanged.
    """
    if not np.isfinite([cutoff_hz, amount]).all() or not 0.0 <= amount <= 1.0:
        raise ValueError("Invalid low-side tightening parameters")
    if cutoff_hz < 0.0 or cutoff_hz > 250.0:
        raise ValueError(f"low_side_hz must be within [0, 250], got {cutoff_hz!r}")
    data = np.asarray(audio, dtype=np.float64)
    report = {"applied": False, "cutoff_hz": float(cutoff_hz), "amount": float(amount)}
    if amount == 0.0 or cutoff_hz == 0.0 or data.ndim != 2 or data.shape[1] != 2 or len(data) < 32:
        return data.copy(), report
    mid = (data[:, 0] + data[:, 1]) * 0.5
    side = (data[:, 0] - data[:, 1]) * 0.5
    if float(np.mean(side * side)) < 1e-16:
        report["reason"] = "no_existing_side"
        return data.copy(), report
    # 1st-order Butterworth + sosfiltfilt = smooth 12 dB/oct zero-phase HPF (minimal pre-ringing).
    sos = signal.butter(1, float(cutoff_hz), btype="highpass", fs=sr, output="sos")
    pad = min(len(side) - 1, int(round(0.25 * sr)))
    hp_side = signal.sosfiltfilt(sos, side, padlen=pad)
    new_side = (1.0 - float(amount)) * side + float(amount) * hp_side
    out = np.column_stack((mid + new_side, mid - new_side))
    lo_sos = signal.butter(2, [20.0, max(25.0, float(cutoff_hz))], btype="bandpass", fs=sr, output="sos")
    s_before = signal.sosfiltfilt(lo_sos, side, padlen=0)
    s_after = signal.sosfiltfilt(lo_sos, new_side, padlen=0)
    eb, ea = float(np.mean(s_before ** 2)), float(np.mean(s_after ** 2))
    att_db = float(10.0 * math.log10(ea / eb)) if eb > 1e-24 and ea > 1e-24 else 0.0
    report.update(applied=True, low_side_change_db=round(att_db, 3))
    return out, report
