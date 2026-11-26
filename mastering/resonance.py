"""Bounded dynamic resonance suppression for mastering (200-600 Hz mud, 2-5.5 kHz glare).

Inspired by dynamic EQ / spectral resonance taming (Pro-Q 3 / Soothe2), designed for
offline mastering with conservative engineering caps:
  - Stereo-linked power spectrum (single attenuation mask for L and R -> zero image shift).
  - Detects narrowband spectral peaks standing well above the local spectral envelope
    (median baseline) AND persisting across frames (transient hits are preserved).
  - 2-D Gaussian smoothing in time and frequency avoids watery/musical-noise artifacts.
  - Delta reconstruction leaves frequencies outside the target bands untouched.
  - amount == 0 is a bit-exact identity.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage, signal

FFT_SIZE = 4096
HOP = 1024
BLOCK_SECONDS = 8.0
CONTEXT_SECONDS = 1.0
MAX_ATTENUATION_DB = 2.5

# (name, low_hz, high_hz, baseline_bins, excess_threshold_db, slope)
# At 44.1 kHz / 4096 FFT, 1 bin = 10.77 Hz:
#   - mud band (200-600 Hz): 17 bins (~183 Hz baseline window), threshold +6.0 dB
#   - glare band (2000-5500 Hz): 35 bins (~377 Hz baseline window), threshold +5.5 dB
RESONANCE_BANDS = (
    ("low_mid_mud", 200.0, 600.0, 17, 6.0, 0.35),
    ("upper_mid_glare", 2000.0, 5500.0, 35, 5.5, 0.35),
)


def _process_chunk(chunk: np.ndarray, sr: int, amount: float):
    n_ch = chunk.shape[1]
    nperseg = min(FFT_SIZE, len(chunk))
    if nperseg < 64:
        return chunk.copy(), {name: 0.0 for name, *_ in RESONANCE_BANDS}
    noverlap = min(nperseg - HOP, nperseg * 3 // 4)
    hop = max(1, nperseg - noverlap)
    noverlap = nperseg - hop

    spectra = [signal.stft(chunk[:, c], sr, nperseg=nperseg, noverlap=noverlap)[2]
               for c in range(n_ch)]
    f = np.fft.rfftfreq(nperseg, 1.0 / sr)
    power = np.mean([np.abs(Z) ** 2 for Z in spectra], axis=0)
    # Time-smoothed power preserves fast drum/pluck attacks and targets sustained resonances.
    power_smooth = ndimage.gaussian_filter1d(power, sigma=1.5, axis=1, mode="nearest")
    power_db = 10.0 * np.log10(np.maximum(power_smooth, 1e-24))

    active_e = np.sum(power[(f >= 60.0) & (f < min(12000.0, sr * 0.48))], axis=0)
    active_floor = max(1e-12, float(np.percentile(active_e, 95)) * 1e-4)
    active = (active_e > active_floor).astype(np.float64)

    att_db = np.zeros_like(power_db)
    band_peaks = {}
    cap_db = MAX_ATTENUATION_DB * amount

    for name, lo_hz, hi_hz, med_bins, thr_db, slope in RESONANCE_BANDS:
        hi_eff = min(hi_hz, sr * 0.45)
        if hi_eff <= lo_hz:
            band_peaks[name] = 0.0
            continue
        # Include margin bins so the local spectral median has accurate context at edges.
        margin_lo = max(40.0, lo_hz * 0.65)
        margin_hi = min(sr * 0.48, hi_eff * 1.35)
        ext = (f >= margin_lo) & (f <= margin_hi)
        if np.count_nonzero(ext) < max(7, med_bins // 2):
            band_peaks[name] = 0.0
            continue
        sub_db = power_db[ext]
        baseline_db = ndimage.median_filter(sub_db, size=(med_bins, 1), mode="nearest")
        excess_db = sub_db - baseline_db
        raw_att = np.clip((excess_db - thr_db) * slope * amount, 0.0, cap_db) * active[None, :]

        # Smooth transition at band boundaries so outside frequencies stay untouched.
        f_ext = f[ext]
        lo_span = max(30.0, (hi_eff - lo_hz) * 0.15)
        hi_span = max(50.0, (hi_eff - lo_hz) * 0.15)
        lo_taper = 0.5 - 0.5 * np.cos(np.pi * np.clip((f_ext - (lo_hz - lo_span)) / lo_span, 0.0, 1.0))
        hi_taper = 0.5 - 0.5 * np.cos(np.pi * np.clip(((hi_eff + hi_span) - f_ext) / hi_span, 0.0, 1.0))
        taper = (lo_taper * hi_taper)[:, None]

        raw_att = ndimage.gaussian_filter(raw_att * taper, sigma=(1.5, 2.0), mode="nearest")
        raw_att = np.clip(raw_att * taper, 0.0, cap_db)
        att_db[ext] = np.maximum(att_db[ext], raw_att)
        core = (f_ext >= lo_hz) & (f_ext <= hi_eff)
        band_peaks[name] = float(np.max(raw_att[core])) if np.any(core) else 0.0

    if not np.any(att_db > 1e-6):
        return chunk.copy(), band_peaks

    multiplier = np.expm1(-att_db * (math.log(10.0) / 20.0))
    outs = []
    for c, Z in enumerate(spectra):
        _, delta = signal.istft(Z * multiplier, sr, nperseg=nperseg, noverlap=noverlap)
        if len(delta) < len(chunk):
            delta = np.pad(delta, (0, len(chunk) - len(delta)))
        outs.append(chunk[:, c] + delta[:len(chunk)])
    return np.column_stack(outs), band_peaks


def tame_resonances(audio: np.ndarray, sr: int = 44100, amount: float = 0.0):
    """Apply bounded dynamic resonance suppression; returns (processed, report)."""
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim != 2 or data.shape[1] not in (1, 2) or not len(data):
        raise ValueError("Resonance suppressor requires nonempty mono/stereo 2-D audio")
    if not np.isfinite(data).all():
        raise ValueError("Resonance suppressor input contains non-finite samples")
    if not math.isfinite(amount) or not 0.0 <= amount <= 1.0:
        raise ValueError(f"resonance amount must be finite within [0, 1], got {amount!r}")

    report = {
        "applied": False,
        "amount": float(amount),
        "max_cap_db": round(MAX_ATTENUATION_DB * float(amount), 4),
        "bands": {},
    }
    if amount == 0.0 or len(data) < 64 or not np.any(data):
        return data.copy(), report

    block = max(FFT_SIZE, int(BLOCK_SECONDS * sr) // HOP * HOP)
    context = int(CONTEXT_SECONDS * sr) // HOP * HOP
    out = np.empty_like(data)
    peaks = {name: 0.0 for name, *_ in RESONANCE_BANDS}

    for start in range(0, len(data), block):
        end = min(start + block, len(data))
        lo = max(0, start - context)
        hi = min(len(data), end + context)
        processed, c_peaks = _process_chunk(data[lo:hi], sr, float(amount))
        out[start:end] = processed[start - lo:end - lo]
        for k, v in c_peaks.items():
            peaks[k] = max(peaks[k], v)

    if not np.isfinite(out).all():
        raise RuntimeError("Resonance suppression produced non-finite samples")

    applied = any(v > 0.05 for v in peaks.values())
    report.update(
        applied=bool(applied),
        bands={name: {"range_hz": [lo, hi], "max_attenuation_db": round(peaks[name], 3)}
               for name, lo, hi, *_ in RESONANCE_BANDS},
    )
    return (out if applied else data.copy()), report
