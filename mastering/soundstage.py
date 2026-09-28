"""Final mix width: amplify existing Side above 120 Hz, never sum old stem deltas."""
import math
import numpy as np
from scipy import fft, signal

from .guard import snapshot

# 单声道兼容占比上限（Side 能量相对 Mid 能量的宽松封顶），是唯一不随请求
# 变化的固定保护；增长量本身由用户的宽度请求授权。旧固定增长阶梯
# （2026-09-22 的 +3/+4dB 与全局 S/M ≤ 0dB）在默认档位就已封顶，用户向上
# 调整听不到变化，2026-09-28 起废弃。
GLOBAL_SIDE_FRACTION = 0.70
WIDTH_BANDS = ((120, 2000, 0.60), (2000, 8000, 0.75), (8000, None, 0.75))


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
    # A request-independent cap makes the admitted gain monotonic in the knob.
    em, es = float(np.mean(mid ** 2)), float(np.mean(side ** 2))
    ed, cross = float(np.mean(added ** 2)), float(np.mean(side * added))
    if cross <= 0:
        report["reason"] = "nonconstructive_side_delta"
        return audio.copy(), report
    growth = 10 ** (width_db / 10)
    allowed = max(0.0, min(em * GLOBAL_SIDE_FRACTION / (1 - GLOBAL_SIDE_FRACTION),
                           es * growth) - es)
    cap = max(0.0, (-cross + math.sqrt(max(0.0, cross ** 2 + ed * allowed))) / max(ed, 1e-30))
    nperseg = min(8192, len(mid))
    f, pm = signal.welch(mid, sr, nperseg=nperseg)
    _, ps = signal.welch(side, sr, nperseg=nperseg)
    _, pd = signal.welch(added, sr, nperseg=nperseg)
    _, pc = signal.csd(side, added, sr, nperseg=nperseg)
    band_caps = []
    for lo, hi, fraction in WIDTH_BANDS:
        band = (f >= lo) & ((f < hi) if hi is not None else (f <= sr / 2))
        m, s, d, c = (float(np.sum(p[band])) for p in (pm, ps, pd, pc.real))
        if s < float(np.sum(ps)) * 1e-8:
            continue
        budget = max(0.0, min(m * fraction / (1 - fraction), s * growth) - s)
        limit = max(0.0, (-c + math.sqrt(max(0.0, c*c + d*budget))) / max(d, 1e-30))
        cap = min(cap, limit)
        band_caps.append({"low_hz": lo, "high_hz": min(hi or sr / 2, sr / 2),
                          "side_fraction_cap": fraction, "delta_cap": limit})
    report["band_caps"] = band_caps
    admitted = min(wet * (10 ** (width_db / 20) - 1), cap)
    if admitted <= 0:
        report["reason"] = "existing_width_at_budget"
        return audio.copy(), report
    new_side = side + admitted * added
    result = np.column_stack((mid + new_side, mid - new_side))
    before, after = snapshot(audio), snapshot(result)
    # Reconstruction boundaries must not turn an intended increase into a loss.
    a, b = before["side_mid"]["db"], after["side_mid"]["db"]
    if a is not None and b is not None and b < a - .05:
        report.update(reason="width_decreased", before=before, after=before)
        return audio.copy(), report
    report.update(applied=True, accepted_delta_gain=admitted, before=before, after=after)
    return result, report
