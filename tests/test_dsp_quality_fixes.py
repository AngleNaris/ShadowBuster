"""DSP quality contracts: independent measurements and composed-stage outcomes."""
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from scipy import signal

import audio_metrics as metrics
import prepared_audio
import studio_backend as backend
from mastering.finalizer import finalize

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apollo_scripts"))
import premaster_hygiene as hygiene
import soundstage_reshape as shape
import vocal_adjust as vocals

SR = 44100


def tone(hz, seconds=3, amplitude=.1):
    return amplitude * np.sin(2 * np.pi * hz * np.arange(round(SR * seconds)) / SR)


def stereo(x, ratio=1):
    return np.column_stack((x, x * ratio))


@pytest.mark.parametrize("frequency", (31.5, 63, 100, 1000, 3000, 10000))
@pytest.mark.parametrize("sr", (44100, 48000))
def test_fallback_loudness_agrees_with_pyloudnorm(frequency, sr):
    pyln = pytest.importorskip("pyloudnorm")
    t = np.arange(sr * 3) / sr
    audio = stereo(.1 * np.sin(2 * np.pi * frequency * t))
    expected = pyln.Meter(sr).integrated_loudness(audio)
    with patch.dict(sys.modules, {"pyloudnorm": None}):
        actual, method = metrics.integrated_lufs(audio, sr)
    assert method == "bs1770_fallback"
    assert abs(actual - expected) < .02


def test_lra_keeps_quiet_musical_section():
    x = stereo(tone(1000, 40))
    x[SR * 30:] *= 10 ** (-15 / 20)
    assert metrics.measure_audio(x, SR)["lra_lu"] == pytest.approx(15, abs=.1)


def test_low_cut_evidence_is_independent_of_channel_polarity():
    t = np.arange(SR * 8) / SR
    envelope = np.where(t % 2 < 1, .15 * (.5 + .5 * np.cos(2 * np.pi * t)), 0)
    x = envelope * (np.sin(2 * np.pi * 31.5 * t) + .3 * np.sin(2 * np.pi * 63 * t))
    mid_cut, mid_report = hygiene.choose_low_cut_hz(stereo(x), SR, 40)
    side_cut, side_report = hygiene.choose_low_cut_hz(stereo(x, -1), SR, 40)
    assert mid_cut == side_cut == 0
    assert mid_report["sub_ratio_db"] == pytest.approx(side_report["sub_ratio_db"])


def vocal_phrase():
    t = np.arange(SR * 8) / SR
    envelope = .015 + .35 * (.5 + .5 * np.sin(2 * np.pi * .5 * t)) ** 2
    return stereo(envelope * np.sin(2 * np.pi * 220 * t))


def test_vocal_compression_detects_side_energy_and_preserves_image():
    mid = vocal_phrase()
    side = mid * np.array([1, -1])
    report = {}
    a = vocals.apply_vocal_compression(mid, SR, .4)
    b = vocals.apply_vocal_compression(side, SR, .4, report=report)
    assert report["applied"] and report["gr_max_db"] > .5
    np.testing.assert_allclose(b, a * np.array([1, -1]), atol=1e-14)


@pytest.mark.parametrize("amount,air", ((.4, 0), (1, 0), (.4, 1.5)))
def test_vocal_balance_reports_the_actual_compressed_sources(amount, air):
    v = vocal_phrase()
    backing = stereo(tone(1000, 8, .08))
    mix = v + backing
    out, report = vocals.apply_mid_prominence_control(
        mix, v, SR, comp_amount=amount, air_db=air,
        balance_mode=vocals.REFERENCE_MODE, reference_mix=mix,
        reference_vocals=v, return_report=True)
    effective_v = vocals.apply_vocal_air(vocals.apply_vocal_compression(v, SR, amount), SR, air)
    effective_v *= 10 ** (report["fixed_vocal_mid_gain_db"] / 20)
    effective_a = out - effective_v
    _, vr = vocals.mid_rms_windows(effective_v, SR)
    _, ar = vocals.mid_rms_windows(effective_a, SR)
    valid = np.asarray(report["matched_valid_mask"])
    actual = np.median(20 * np.log10(vr[valid] / ar[valid]))
    assert report["median_output_ratio_db"] == pytest.approx(actual, abs=1e-8)
    assert report["reconstruction_max_error"] < 1e-12


def test_short_peak_does_not_darken_the_rest_of_the_master():
    t = np.arange(SR * 3) / SR
    x = stereo(.06 * np.sin(2 * np.pi * 1000 * t) + .002 * np.sin(2 * np.pi * 20000 * t))
    x[round(.3 * SR)] += .9
    out, stats = finalize(x, SR, "soft")
    assert stats["limiter"]["max_gain_reduction_db"] > 0
    region = slice(round(1.5 * SR), round(2.5 * SR))
    gains = []
    for frequency in (1000, 20000):
        carrier = np.exp(-2j * np.pi * frequency * t[region])
        before = abs(np.dot(x[region, 0], carrier))
        after = abs(np.dot(out[region, 0], carrier))
        gains.append(20 * np.log10(after / before))
    assert abs(gains[1] - gains[0]) < .02


def test_legacy_styled_respects_engagement_budgets():
    from test_soren_original import load, CORE
    core = load("quality_fix_legacy", CORE)
    rng = np.random.default_rng(24)
    x = signal.sosfiltfilt(signal.butter(4, [100, 1200], btype="bandpass", fs=SR,
                                         output="sos"), rng.standard_normal(SR * 4))
    x *= .55 / max(abs(x))
    side = np.roll(x, 37) * .25
    audio = np.column_stack((x + side, x - side))
    config = core.Config()
    config.loudness_option = "loud"
    core.process_audio(audio.T, audio.T, 5, config, {"genre": "Pop", "lufs": -9.2})
    stats = config.last_mastering_stats
    assert stats["limiter"]["active_fraction"] <= .70 + 1e-6
    assert stats["limiter"]["gain_reduction_p50_db"] <= 2 + 1e-6
    assert stats["target_status"] == "below_target"


def write_stems(directory, x, name="guitar"):
    directory.mkdir()
    for key in ("vocals", "drums", "bass", "other", "guitar", "piano"):
        sf.write(directory / f"{key}.wav", x if key == name else np.zeros_like(x),
                 SR, subtype="FLOAT")


def test_partial_noise_budget_is_applied_to_working_stems(tmp_path, monkeypatch):
    raw = stereo(tone(1000))
    inputs, outputs = tmp_path / "stems", tmp_path / "working"
    write_stems(inputs, raw, "other")
    source, target = tmp_path / "source.wav", tmp_path / "target.wav"
    sf.write(source, raw * .4, SR, subtype="FLOAT")
    monkeypatch.setattr(shape, "adaptive_denoise", lambda x, *a, **kw: x * .5)
    monkeypatch.setattr(shape, "constrain_noise_delta", lambda x, d, *a:
                        (d * .25, {"applied": True, "admitted_delta_gain": .25}))
    monkeypatch.setattr(sys, "argv", ["shape", "--in-mix", str(source),
        "--out-wav", str(target), "--stems-dir", str(inputs), "--wet", "0",
        "--space-amount", "0", "--noise-mode", "adaptive_all",
        "--other-denoise-amount", "1", "--stem-scale", ".4",
        "--working-stems-dir", str(outputs)])
    shape.main()
    actual, _ = sf.read(target, always_2d=True)
    working, _ = sf.read(outputs / "other.wav", always_2d=True)
    np.testing.assert_allclose(actual, raw * .4 * .875, atol=1e-8)
    np.testing.assert_allclose(working * .4, actual, atol=1e-8)


def test_width_stem_deltas_sum_to_the_admitted_mix_delta():
    t = np.arange(SR * 2) / SR
    mid = .1 * np.sin(2 * np.pi * 3000 * t)
    side = .03 * np.cos(2 * np.pi * 3000 * t)
    mix = np.column_stack((mid + side, mid - side))
    request = np.column_stack((side * 3, -side * 3))
    parts = {"other": request * .4, "drums": request * .6}
    admitted, _ = shape.constrain_width_delta(mix, request, SR, 12, stem_deltas=parts)
    np.testing.assert_allclose(sum(parts.values()), admitted, atol=1e-12)


def test_pipeline_reuses_denoised_stems_and_restores_them_from_cache(tmp_path, monkeypatch):
    rng = np.random.default_rng(17)
    hiss = signal.sosfiltfilt(signal.butter(4, [8500, 19500], btype="bandpass", fs=SR,
                                            output="sos"), rng.standard_normal(SR * 3)) * .05
    raw = stereo(hiss + tone(1000))
    source, inputs = tmp_path / "song.wav", tmp_path / "stems"
    sf.write(source, raw, SR, subtype="FLOAT")
    write_stems(inputs, raw)
    monkeypatch.setattr(backend, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(prepared_audio, "endpoints", lambda *a, **kw:
                        (source, source, inputs, inputs))
    calls = []
    real_reshape = backend.stage_reshape

    def reshape(in_mix, stems_dir, out_wav, **kwargs):
        calls.append(1)
        return real_reshape(in_mix, stems_dir, out_wav, **kwargs)

    reshape.writes_working_stems = True
    reshape.writes_stage_report = True
    reshape.report_input_arg = "in_mix"
    monkeypatch.setattr(backend, "stage_reshape", reshape)
    consumed = []
    real_guitar = backend.stage_guitar

    class GuitarConsumer:
        # Keep this consumer uncached so it observes the restored working stems.
        def __call__(self, stem_dir, in_mix, out_wav, **kwargs):
            shaped, _ = sf.read(in_mix, always_2d=True)
            working, _ = sf.read(Path(stem_dir) / "guitar.wav", always_2d=True)
            consumed.append((shaped, working))
            return real_guitar(stem_dir, in_mix, out_wav, **kwargs)

    monkeypatch.setattr(backend, "stage_guitar", GuitarConsumer())
    for name in ("first", "cached"):
        work = tmp_path / name / "work"
        result = backend.run_pipeline(source, tmp_path / name / "out", work_dir=work,
            bypass=["lew", "bass", "drums", "vocals", "soren"],
            noise_mode="adaptive_all", space_denoise=1, space_wet=0,
            space_amount=0, guitar_gain_db=-6)
        rendered, _ = sf.read(result, always_2d=True)
        shaped, working = consumed[-1]
        np.testing.assert_allclose(rendered, shaped * 10 ** (-6 / 20), atol=2e-8)
        assert np.max(abs(working - raw)) > 1e-4
    assert len(calls) == 1
    assert len(consumed) == 2
