"""Restore a bounded amount of density compression around strong drum attacks."""
import numpy as np
from scipy import ndimage, signal

BANDS = {"kick": (42, 125), "stick": (1800, 6200)}
RESTORE_FRACTION = .15


def _bandpass(audio, limits, sample_rate, *, centered=False):
    sos = signal.butter(2, limits, "bandpass", fs=sample_rate, output="sos")
    process = signal.sosfiltfilt if centered else signal.sosfilt
    return process(sos, audio, axis=0)


def detect_attacks(drums, sample_rate):
    """Use strong local rises in the drum stem to locate each band's attacks."""
    hop = round(.010 * sample_rate)
    edge = sample_rate // 4
    block = 30 * sample_rate
    hits = {}
    for name, limits in BANDS.items():
        filtered = _bandpass(drums, limits, sample_rate)
        energy = ndimage.uniform_filter1d(np.mean(filtered ** 2, axis=1),
                                         round(.008 * sample_rate))
        rise = np.maximum(energy[hop:] - energy[:-hop], 0)
        peaks, _ = signal.find_peaks(rise, distance=round(.15 * sample_rate))
        peaks = peaks[(peaks >= edge) & (peaks + edge < len(rise)) & (rise[peaks] > 0)]
        selected = []
        for start in range(0, len(rise), block):
            local = peaks[(peaks >= start) & (peaks < start + block)]
            if local.size:
                selected.extend(local[rise[local] >= np.percentile(rise[local], 75)])
        hits[name] = np.asarray(selected, dtype=int) + hop // 2
    return hits


def restore_attacks(before_density, compressed, sample_rate, hits):
    """Blend only the band-limited compression difference into the attack window."""
    delta = before_density - compressed
    output = compressed.copy()
    offsets = np.arange(-round(.004 * sample_rate), round(.029 * sample_rate))
    pulse = np.interp(offsets / sample_rate, [-.004, 0, .011, .029], [0, 1, 1, 0])
    for name, limits in BANDS.items():
        if not len(hits[name]):
            continue
        envelope = np.zeros(len(output))
        for hit in hits[name]:
            indices = hit + offsets
            envelope[indices] = np.maximum(envelope[indices], pulse)
        output += RESTORE_FRACTION * _bandpass(delta, limits, sample_rate, centered=True) * envelope[:, None]
    return output
