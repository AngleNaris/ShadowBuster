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


def test_normal_mode_forwards_drums_source_when_density_active(monkeypatch, tmp_path):
    import studio_backend as backend
    calls = []
    drums = tmp_path / "drums.wav"
    monkeypatch.setattr(backend, "_run_stream", lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(backend, "read_mastering_stats", lambda path: {})
    backend.stage_mastering(tmp_path / "in.wav", tmp_path / "out.wav", loudness="normal",
                            density="light", drums_source=drums)
    assert "--drums-source" in calls[-1]
    assert calls[-1][calls[-1].index("--drums-source") + 1] == str(drums.resolve())
    backend.stage_mastering(tmp_path / "in.wav", tmp_path / "out.wav", loudness="normal",
                            density="off", drums_source=drums)
    assert "--drums-source" not in calls[-1]
