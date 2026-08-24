from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from experiments import objective_compare as comparison


SR = 48_000


def _tone(amplitude=0.1, frequency=997.0, seconds=1.2, stereo=True):
    time = np.arange(round(SR * seconds), dtype=np.float64) / SR
    mono = amplitude * np.sin(2 * np.pi * frequency * time)
    if not stereo:
        return mono[:, None].astype(np.float32)
    return np.column_stack((mono, 0.8 * mono)).astype(np.float32)


def _write(path: Path, audio: np.ndarray, *, rate=SR, subtype="FLOAT"):
    sf.write(path, audio, rate, format="WAV", subtype=subtype)
    return path


def _sources(tmp_path, candidate_amplitude=0.05):
    return (
        _write(tmp_path / "dry.wav", _tone(0.1)),
        {"restored": _write(tmp_path / "restored.wav", _tone(candidate_amplitude))},
    )


def test_rejects_non_float_and_shape_or_rate_mismatch(tmp_path):
    dry = _write(tmp_path / "dry.wav", _tone())
    pcm = _write(tmp_path / "pcm.wav", _tone(), subtype="PCM_16")
    with pytest.raises(ValueError, match="FLOAT WAV"):
        comparison.load_float_candidates(dry, {"pcm": pcm})

    wrong_shape = _write(tmp_path / "mono.wav", _tone(stereo=False))
    with pytest.raises(ValueError, match="does not match"):
        comparison.load_float_candidates(dry, {"mono": wrong_shape})

    wrong_rate = _write(tmp_path / "rate.wav", _tone(), rate=44_100)
    with pytest.raises(ValueError, match="does not match"):
        comparison.load_float_candidates(dry, {"rate": wrong_rate})


def test_metrics_cover_required_objective_measures(monkeypatch):
    # Force and exercise the dependency-free robust loudness path.
    monkeypatch.setattr(comparison, "integrated_loudness",
                        lambda audio, rate: (comparison._fallback_loudness(audio, rate),
                                             "bs1770_fallback"))
    metrics = comparison.measure_audio(_tone(), SR)
    assert np.isfinite(metrics["integrated_lufs"])
    assert metrics["loudness_backend"] == "bs1770_fallback"
    assert metrics["sample_peak"] == pytest.approx(0.1, rel=1e-4)
    assert metrics["true_peak_4x"] >= metrics["sample_peak"] * 0.999
    assert set(metrics["band_energies"]) == {
        "0-4k", "4-8k", "8-12k", "12-16k", "16-20k", "20-22k"
    }
    assert metrics["crest_factor_db"] > 0
    assert metrics["stereo_correlation"] == pytest.approx(1.0, abs=1e-12)
    assert metrics["side_mid"]["ratio"] > 0


def test_generation_is_deterministic_anonymous_float_and_private(tmp_path):
    dry = _write(tmp_path / "dry.wav", _tone(0.10))
    candidates = {
        "alpha": _write(tmp_path / "alpha.wav", _tone(0.05, 997)),
        "beta": _write(tmp_path / "beta.wav", _tone(0.08, 3000)),
        "gamma": _write(tmp_path / "gamma.wav", _tone(0.06, 7000)),
    }
    source_hashes = {path: comparison.sha256_file(path)
                     for path in (dry, *candidates.values())}
    key1_path = tmp_path / "private-1.json"
    key2_path = tmp_path / "private-2.json"
    key1 = comparison.generate_blind_comparison(
        dry, candidates, tmp_path / "blind-1", key1_path, seed=9127
    )
    key2 = comparison.generate_blind_comparison(
        dry, candidates, tmp_path / "blind-2", key2_path, seed=9127
    )

    assert [entry["identity"] for entry in key1["entries"]] == [
        entry["identity"] for entry in key2["entries"]
    ]
    assert [entry["anonymous_wav"] for entry in key1["entries"]] == [
        entry["anonymous_wav"] for entry in key2["entries"]
    ]
    assert [entry["output_sha256"] for entry in key1["entries"]] == [
        entry["output_sha256"] for entry in key2["entries"]
    ]
    assert all(comparison.sha256_file(path) == digest
               for path, digest in source_hashes.items())

    blind_files = sorted((tmp_path / "blind-1").iterdir())
    assert [path.name for path in blind_files] == [
        f"candidate_{index:03d}.wav" for index in range(1, 5)
    ]
    assert all(path.suffix == ".wav" for path in blind_files)
    assert key1_path.parent != (tmp_path / "blind-1")
    persisted = json.loads(key1_path.read_text(encoding="utf-8"))
    assert persisted["seed"] == 9127
    assert persisted["parameters"]["gain"] == "static scalar only"
    assert all(len(entry["source_sha256"]) == 64 for entry in persisted["entries"])
    assert all(len(entry["output_sha256"]) == 64 for entry in persisted["entries"])
    for path in blind_files:
        info = sf.info(path)
        assert info.format == "WAV"
        assert info.subtype == "FLOAT"
        assert info.samplerate == SR
        assert info.frames == len(_tone())
        assert info.channels == 2

    levels = []
    for path in blind_files:
        audio, rate = sf.read(path, always_2d=True, dtype="float64")
        levels.append(comparison.integrated_loudness(audio, rate)[0])
        assert np.max(np.abs(audio)) <= 1.0
    assert max(levels) - min(levels) < 2e-4


def test_static_gain_clipping_is_rejected_without_outputs_or_source_changes(tmp_path):
    # Dry's crest is low enough that matching a peaky candidate drives it over full scale.
    dry_audio = _tone(0.5, seconds=1.0)
    candidate_audio = np.zeros_like(dry_audio)
    candidate_audio[::100] = 0.99
    dry = _write(tmp_path / "dry.wav", dry_audio)
    candidate = _write(tmp_path / "candidate.wav", candidate_audio)
    before = {path: comparison.sha256_file(path) for path in (dry, candidate)}
    blind = tmp_path / "blind"
    key = tmp_path / "private.json"

    with pytest.raises(comparison.ComparisonError, match="post-gain clipping rejected"):
        comparison.generate_blind_comparison(
            dry, {"peaky": candidate}, blind, key, seed=1
        )
    assert not blind.exists()
    assert not key.exists()
    assert all(comparison.sha256_file(path) == digest for path, digest in before.items())


def test_overwrite_protection_and_key_must_be_private(tmp_path):
    dry, candidates = _sources(tmp_path)
    blind = tmp_path / "blind"
    key = tmp_path / "private.json"
    comparison.generate_blind_comparison(dry, candidates, blind, key, seed=3)

    with pytest.raises(FileExistsError, match="blind directory"):
        comparison.generate_blind_comparison(dry, candidates, blind,
                                             tmp_path / "another.json", seed=3)
    with pytest.raises(FileExistsError, match="private key"):
        comparison.generate_blind_comparison(dry, candidates,
                                             tmp_path / "another-blind", key, seed=3)
    with pytest.raises(ValueError, match="outside"):
        comparison.generate_blind_comparison(
            dry, candidates, tmp_path / "new-blind",
            tmp_path / "new-blind" / "key.json", seed=3
        )


def test_source_hash_immutability_gate_detects_concurrent_change(tmp_path, monkeypatch):
    dry, candidates = _sources(tmp_path)
    candidate = candidates["restored"]
    original_write = comparison.sf.write
    changed = False

    def write_then_mutate(*args, **kwargs):
        nonlocal changed
        result = original_write(*args, **kwargs)
        if not changed:
            changed = True
            with candidate.open("ab") as handle:
                handle.write(b"concurrent-change")
        return result

    monkeypatch.setattr(comparison.sf, "write", write_then_mutate)
    blind = tmp_path / "blind"
    with pytest.raises(comparison.ComparisonError, match="source hash changed"):
        comparison.generate_blind_comparison(
            dry, candidates, blind, tmp_path / "private.json", seed=10
        )
    assert not blind.exists()
    assert not (tmp_path / "private.json").exists()
