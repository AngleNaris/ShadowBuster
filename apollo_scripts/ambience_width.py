"""Generate Side only for locally narrow accompaniment; retain its Mid exactly."""
import numpy as np
from numba import njit
from scipy import ndimage, signal

TARGET_STEMS = ("other", "guitar", "piano")
NARROW_SIDE_MID_RATIO = .5
WINDOW = 4096
CHUNK = 1 << 19
CONTEXT = 8192


@njit(cache=True)
def _allpass(x, delay, coefficient):
    y = np.empty_like(x)
    for i in range(len(x)):
        y[i] = coefficient * x[i]
        if i >= delay:
            y[i] += x[i - delay] - coefficient * y[i - delay]
    return y


def diffuse_mid(mid, sr):
    """Two short allpass cascades generate a deterministic diffuse tail."""
    a = _allpass(_allpass(mid, max(1, round(sr * .0031)), .55),
                 max(1, round(sr * .0073)), -.45)
    b = _allpass(_allpass(mid, max(1, round(sr * .0047)), .55),
                 max(1, round(sr * .0113)), -.45)
    return (a - b) * .5


def accompaniment_delta(stem, sr, wet, width_db):
    """Create a bounded, side-only ambience delta, never amplify existing Side.

    Each local time/frequency region admits generation only while S/M < 0.5.
    The caller bounds the sum against the actual mix and distributes that same
    accepted mask back to the working stems. Wide/anti-phase regions are retained.
    """
    report = {"applied": False, "method": "narrow_accompaniment_allpass",
              "narrow_side_mid_ratio": NARROW_SIDE_MID_RATIO,
              "narrow_coverage": 0.0}
    if wet <= 0 or width_db <= 0 or len(stem) < WINDOW:
        return np.zeros_like(stem), report
    mid = np.asarray(stem, dtype=np.float64).mean(axis=1)
    side = (stem[:, 0] - stem[:, 1]) * .5
    diffuse = diffuse_mid(mid, sr)
    added = np.zeros_like(mid)
    strength = .5 * wet * np.expm1(np.log(10) * width_db / 20)
    covered = total = 0
    for start in range(0, len(mid), CHUNK):
        end = min(start + CHUNK, len(mid))
        lo, hi = max(0, start - CONTEXT), min(len(mid), end + CONTEXT)
        def stft(x):
            return signal.stft(x[lo:hi], sr, nperseg=WINDOW,
                               noverlap=WINDOW * 3 // 4, boundary="zeros", padded=True)
        frequencies, times, m = stft(mid)
        _, _, s = stft(side)
        _, _, d = stft(diffuse)
        em = ndimage.uniform_filter(abs(m) ** 2, size=(3, 5))
        es = ndimage.uniform_filter(abs(s) ** 2, size=(3, 5))
        ratio = np.sqrt(np.maximum(es, 0) / np.maximum(em, 1e-24))
        narrow = np.clip(1 - ratio / NARROW_SIDE_MID_RATIO, 0, 1)
        cross = ndimage.uniform_filter((m * s.conj()).real, size=(3, 5))
        pan = abs(2 * cross) / np.maximum(em + es, 1e-24)
        narrow *= np.clip(1 - pan / .5, 0, 1)
        active = em > max(float(em.max()) * 1e-8, 1e-24)
        narrow *= active
        # Smooth admission without allowing a wide region to inherit its neighbour's boost.
        narrow = np.minimum(narrow, ndimage.uniform_filter(narrow, size=(3, 5)))
        highpass = .5 - .5 * np.cos(np.pi * np.clip((frequencies - 120) / 120, 0, 1))
        _, result = signal.istft(d * narrow * highpass[:, None] * strength,
                                  sr, nperseg=WINDOW, noverlap=WINDOW * 3 // 4,
                                  input_onesided=True, boundary=True)
        added[start:end] = result[start - lo:end - lo]
        central = (times * sr >= start - lo) & (times * sr < end - lo)
        audible = active[:, central] & (frequencies[:, None] >= 240)
        covered += int(np.count_nonzero((narrow[:, central] > .05) & audible))
        total += int(np.count_nonzero(audible))
    report.update(applied=bool(np.any(added)), narrow_coverage=covered / max(total, 1),
                  requested_wet=wet, requested_width_db=width_db)
    return np.column_stack((added, -added)), report


def protect_side_cancellation(mix, delta, sr, stem_deltas=None):
    """Use the mix's original Side as the reference for new ambience Side."""
    def side(audio):
        return (audio[:, 0] - audio[:, 1]) * .5

    source_side, total_side = side(mix), side(delta)
    parts = ({name: side(value) for name, value in stem_deltas.items()}
             if stem_deltas else {None: total_side})
    corrected = {name: part.copy() for name, part in parts.items()}
    phase_cells = active_cells = 0

    for start in range(0, len(mix), CHUNK):
        end = min(start + CHUNK, len(mix))
        lo, hi = max(0, start - CONTEXT), min(len(mix), end + CONTEXT)

        def stft(value):
            return signal.stft(value[lo:hi], sr, nperseg=WINDOW,
                               noverlap=WINDOW * 3 // 4)

        frequencies, times, reference = stft(source_side)
        taper = .5 - .5 * np.cos(np.pi * np.clip((frequencies - 120) / 120, 0, 1))
        requested = stft(total_side)[2] * taper[:, None]
        es, ed = abs(reference) ** 2, abs(requested) ** 2
        cross = (reference.conj() * requested).real
        # Include any intermediate gain that the subsequent width budget can admit.
        minimum_gain = np.clip(-cross / np.maximum(ed, 1e-30), 0, 1)
        lowest_energy = es + 2 * minimum_gain * cross + minimum_gain ** 2 * ed
        loss = -10 * np.log10(np.maximum(lowest_energy / np.maximum(es, 1e-30), 1e-12))
        active = (es > max(float(es.max()) * 1e-8, 1e-24)) & (taper[:, None] > 0)
        weight = np.where(active, np.clip((loss - .5) / .5, 0, 1), 0)
        central = (times * sr >= start - lo) & (times * sr < end - lo)
        phase_cells += int(np.count_nonzero(weight[:, central]))
        active_cells += int(np.count_nonzero(active[:, central]))
        if not np.any(weight):
            continue

        weight = np.maximum(weight, ndimage.uniform_filter(weight, size=(3, 5)))
        weight *= active & (cross < 0)
        direction = weight * reference / np.maximum(es, 1e-30)
        # A common linear projection keeps the working stems and mix in agreement.
        for name, part in parts.items():
            projection = (reference.conj() * stft(part)[2]).real
            _, correction = signal.istft(-direction * projection, sr, nperseg=WINDOW,
                                         noverlap=WINDOW * 3 // 4)
            corrected[name][start:end] += correction[start - lo:end - lo]

    if stem_deltas:
        for name, part in corrected.items():
            stem_deltas[name] = np.column_stack((part, -part))
    result = sum(corrected.values())
    return np.column_stack((result, -result)), {"phase_cells": phase_cells, "active_cells": active_cells}
