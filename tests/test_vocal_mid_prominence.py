from pathlib import Path
import importlib.util
import sys

import numpy as np
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "apollo_scripts"
sys.path.insert(0, str(SOURCE))
spec = importlib.util.spec_from_file_location("current_vocal_control", SOURCE / "vocal_adjust.py")
vocal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vocal)
SR = 8000


def tracks(seconds=2):
    t = np.arange(int(SR * seconds)) / SR
    vm = 0.05 * np.sin(2 * np.pi * 400 * t)
    vs = 0.01 * np.sin(2 * np.pi * 900 * t)
    a = np.repeat((0.08 * np.sin(2 * np.pi * 200 * t))[:, None], 2, axis=1)
    v = np.column_stack((vm + vs, vm - vs))
    return v, a


@pytest.mark.parametrize("gain", [-6, -3, 0, 4, 5, 6])
def test_same_reference_is_exact_mid_offset(gain):
    v, a = tracks()
    mix = v + a
    out, report = vocal.apply_mid_prominence_control(mix, v, SR, gain, mix, v, return_report=True)
    delta = v.mean(axis=1) * (10 ** (gain / 20) - 1)
    np.testing.assert_allclose(out, mix + delta[:, None], atol=1e-12)
    np.testing.assert_allclose(out[:, 0] - out[:, 1], mix[:, 0] - mix[:, 1], atol=1e-15)
    assert report["absolute_error_p95_db"] < 1e-8
    if gain == 0:
        np.testing.assert_array_equal(out, mix)


@pytest.mark.parametrize("scale", [1, 0.5, 0.07])
def test_scaled_mix_vocal_accounting_and_two_db_accompaniment(scale):
    v, a = tracks()
    current = scale * (v + a * 10 ** (2 / 20))
    out, report = vocal.apply_mid_prominence_control(current, v, SR, 4, v + a, v,
                                                     vocal_scale=scale, return_report=True)
    expected = current + (v.mean(axis=1) * scale * (10 ** (6 / 20) - 1))[:, None]
    np.testing.assert_allclose(out, expected, atol=1e-12)
    assert report["absolute_error_p95_db"] < 1e-7


def test_reference_sections_keep_their_own_balance():
    v, a = tracks(4)
    v[2 * SR:] *= 0.4
    a[2 * SR:] *= 1.4
    out, report = vocal.apply_mid_prominence_control(v + a, v, SR, 4, v + a, v, return_report=True)
    windows = report["windows"]
    reference = np.array(windows["reference_ratio_db"])
    after = np.array(windows["after_ratio_db"])
    assert np.ptp(reference) > 8
    np.testing.assert_allclose(after - reference, 4, atol=1e-7)


@pytest.mark.parametrize("gain,boost", [(0.5, -12), (-0.5, 12), (6, 12), (-6, -12)])
def test_safety_cap_and_no_direction_reversal(gain, boost):
    v, a = tracks()
    out, report = vocal.apply_mid_prominence_control(v + a * 10 ** (boost / 20), v, SR,
                                                     gain, v + a, v, return_report=True)
    assert report["actual_gain_min_db"] >= -9
    assert report["actual_gain_max_db"] <= 9
    if gain > 0:
        assert report["actual_gain_min_db"] >= 0
    else:
        assert report["actual_gain_max_db"] <= 0
    assert report["limited_window_count"] > 0
    assert report["absolute_error_p50_db"] > 0


@pytest.mark.parametrize("kind", ["silence", "acapella", "side", "tiny_denominator", "no_vocal"])
def test_unreliable_ratio_falls_back_without_side_modification(kind):
    v, a = tracks()
    if kind == "silence":
        v[:] = 0
        a[:] = 0
    elif kind == "acapella":
        a[:] = 0
    elif kind == "side":
        v[:, 1] = -v[:, 0]
    elif kind == "tiny_denominator":
        a *= 1e-8
    else:
        v[:] = 0
    mix = v + a
    out, report = vocal.apply_mid_prominence_control(mix, v, SR, 4, mix, v, return_report=True)
    assert report["valid_window_count"] == 0
    assert report["absolute_error_p50_db"] is None
    np.testing.assert_allclose(out, mix + (v.mean(axis=1) * (10 ** (4 / 20) - 1))[:, None], atol=1e-12)


@pytest.mark.parametrize("samples", [1, 20, 799, 801, 1001])
def test_short_tail_and_mono(samples):
    v = np.ones((samples, 1)) * .02
    mix = v * 3
    centers, rms = vocal.mid_rms_windows(v, SR)
    assert np.isfinite(rms).all()
    assert (np.diff(centers) > 0).all()
    out = vocal.apply_mid_prominence_control(mix, v, SR, 4, mix, v)
    assert out.shape == mix.shape
    np.testing.assert_allclose(out, mix + v * (10 ** (4 / 20) - 1))


def test_smoothed_transition():
    v, a = tracks(4)
    enhanced = a.copy()
    enhanced[2 * SR:] *= 1.4
    _, report = vocal.apply_mid_prominence_control(v + enhanced, v, SR, 4, v + a, v, return_report=True)
    assert report["gain_max_step_db"] < 0.01
    assert 4 <= report["actual_gain_max_db"] <= 7


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 7])
def test_bad_target_rejected(bad):
    v, a = tracks()
    with pytest.raises(ValueError):
        vocal.apply_mid_prominence_control(v + a, v, SR, bad, v + a, v)


def test_input_validation_and_legacy():
    v, a = tracks()
    for rv in (v[:-1], np.ones((len(v), 1)), v * np.nan):
        with pytest.raises(ValueError):
            vocal.apply_mid_prominence_control(v + a, v, SR, 4, v + a, rv)
    out = vocal.apply_mid_prominence_control(v + a, v, SR, 12)
    np.testing.assert_allclose(out, a + v * 10 ** (12 / 20), atol=1e-12)
