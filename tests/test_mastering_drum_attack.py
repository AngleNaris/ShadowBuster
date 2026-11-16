import numpy as np

from mastering.drum_attack import detect_attacks, restore_attacks


def test_drum_attack_restore_stays_inside_detected_windows():
    sr = 44100
    stem = np.zeros((2 * sr, 2))
    time = np.arange(sr // 8) / sr
    burst = np.sin(2 * np.pi * 70 * time) * np.exp(-time / .025)
    stem[sr:sr + len(burst)] = burst[:, None]
    hits = detect_attacks(stem, sr)
    assert np.any(abs(hits["kick"] - sr) < .03 * sr)

    compressed = stem * .4
    output = restore_attacks(stem, compressed, sr, hits)
    allowed = np.zeros(len(stem), dtype=bool)
    for band_hits in hits.values():
        for hit in band_hits:
            allowed[hit - round(.004 * sr):hit + round(.029 * sr)] = True
    assert np.max(abs(output[allowed] - compressed[allowed])) > .01
    np.testing.assert_array_equal(output[~allowed], compressed[~allowed])
