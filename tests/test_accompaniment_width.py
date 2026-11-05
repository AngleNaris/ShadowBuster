"""User contract: expand narrow accompaniment; retain wide/panned/protected parts."""
import json
from pathlib import Path
import sys

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apollo_scripts"))
import ambience_width as ambience
import soundstage_reshape as reshape

SR = 44100


def stem(ratio=0, seconds=2, frequency=997):
    t = np.arange(round(SR * seconds)) / SR
    m = .1 * np.sin(2 * np.pi * frequency * t)
    s = .1 * ratio * np.cos(2 * np.pi * frequency * t)
    return np.column_stack((m + s, m - s))


def rms(x):
    return np.sqrt(np.mean(x ** 2))


def test_mono_accompaniment_gains_space_without_changing_its_mid():
    x = stem()
    delta, detail = ambience.accompaniment_delta(x, SR, .6, 6)
    accepted, _ = reshape.constrain_width_delta(x, delta, SR, allow_generated=True)
    assert np.isfinite(accepted).all() and rms(accepted[8192:-8192]) > .001
    np.testing.assert_allclose((x + accepted).mean(axis=1), x.mean(axis=1), atol=1e-16)
    assert detail["applied"]


@pytest.mark.parametrize("wet,width", [(0, 6), (.6, 0)])
def test_zero_controls_are_identity(wet, width):
    delta, detail = ambience.accompaniment_delta(stem(), SR, wet, width)
    assert not np.any(delta) and not detail["applied"]


def test_already_wide_and_hard_panned_content_is_retained():
    for x in (stem(.8), np.column_stack((stem()[:, 0], np.zeros(SR * 2)))):
        delta, _ = ambience.accompaniment_delta(x, SR, 1, 12)
        assert rms(delta[8192:-8192]) < 1e-7


def test_narrow_and_wide_sections_are_decided_locally():
    x = np.vstack((stem(0), stem(.8)))
    delta, _ = ambience.accompaniment_delta(x, SR, .6, 6)
    assert rms(delta[SR // 2:SR * 3 // 2]) > .001
    assert rms(delta[SR * 5 // 2:SR * 7 // 2]) < 1e-7


def test_subbass_is_not_used_to_generate_width():
    delta, _ = ambience.accompaniment_delta(stem(frequency=60), SR, 1, 12)
    assert rms(delta[8192:-8192]) < 1e-6


def test_chunked_processing_has_no_seams(monkeypatch):
    x = stem(seconds=3)
    monkeypatch.setattr(ambience, "CHUNK", 1 << 30)
    whole, _ = ambience.accompaniment_delta(x, SR, .6, 6)
    monkeypatch.setattr(ambience, "CHUNK", 1 << 15)
    chunked, _ = ambience.accompaniment_delta(x, SR, .6, 6)
    np.testing.assert_allclose(chunked, whole, atol=1e-7)


def test_generated_width_obeys_local_mix_phase_boundary():
    x = np.vstack((stem(0), stem(.8)))
    delta, _ = ambience.accompaniment_delta(np.vstack((stem(), stem())), SR, 1, 12)
    accepted, _ = reshape.constrain_width_delta(x, delta, SR, allow_generated=True)
    for lo, hi in ((SR // 2, SR * 3 // 2), (SR * 5 // 2, SR * 7 // 2)):
        assert np.corrcoef((x + accepted)[lo:hi].T)[0, 1] >= -1e-3


def test_stage_changes_only_target_working_stems_and_preserves_residual(tmp_path, monkeypatch):
    sources = {"other": stem(), "guitar": stem(frequency=1703) * .3,
               "piano": stem(frequency=2309) * .2,
               "bass": stem(frequency=60) * .2,
               "drums": stem(frequency=90) * .2,
               "vocals": stem(frequency=443) * .2}
    for name, value in sources.items():
        sf.write(tmp_path / f"{name}.wav", value, SR, subtype="FLOAT")
    sources = {name: sf.read(tmp_path / f"{name}.wav", always_2d=True)[0] for name in sources}
    residual = stem(frequency=3203) * .1
    sf.write(tmp_path / "mix.wav", sum(sources.values()) + residual, SR, subtype="FLOAT")
    monkeypatch.setattr(sys, "argv", ["reshape", "--in-mix", str(tmp_path / "mix.wav"),
        "--out-wav", str(tmp_path / "out.wav"), "--stems-dir", str(tmp_path),
        "--mode", "adaptive", "--wet", ".6", "--side-gain-db", "6",
        "--space-amount", "0", "--other-denoise-amount", "0",
        "--working-stems-dir", str(tmp_path / "working"),
        "--report-json", str(tmp_path / "report.json")])
    reshape.main()
    output, _ = sf.read(tmp_path / "out.wav", always_2d=True)
    working = {name: sf.read(tmp_path / "working" / f"{name}.wav", always_2d=True)[0]
               for name in sources}
    for name in ("bass", "drums", "vocals"):
        assert (tmp_path / f"{name}.wav").read_bytes() == (tmp_path / "working" / f"{name}.wav").read_bytes()
    for name in ambience.TARGET_STEMS:
        assert rms(working[name] - sources[name]) > .0001
    mix, _ = sf.read(tmp_path / "mix.wav", always_2d=True)
    np.testing.assert_allclose(output, mix + sum(working[k] - sources[k] for k in sources), atol=1e-7)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["extra"]["width"]["accompaniment"]["scope"] == list(ambience.TARGET_STEMS)
