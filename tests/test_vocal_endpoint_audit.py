from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from experiments import vocal_endpoint_audit as audit


SR = 8_000


def _vocal(*, stereo=False, dtype=np.float32):
    rng = np.random.default_rng(714)
    value = np.zeros(SR, dtype=dtype)
    value[800:3200] = rng.normal(0, .08, 2400)
    value[4000:7200] = rng.normal(0, .06, 3200)
    if stereo:
        return np.column_stack((value, value * .75 + np.roll(value, 1) * .1)).astype(dtype)
    return value


def _delay(value, samples):
    positions = np.arange(len(value), dtype=np.float64) - samples
    if value.ndim == 1:
        return np.interp(positions, np.arange(len(value)), value, left=0, right=0).astype(value.dtype)
    return np.column_stack([np.interp(positions, np.arange(len(value)), value[:, c], left=0, right=0) for c in range(value.shape[1])]).astype(value.dtype)


@pytest.mark.parametrize("bad", [
    np.array([], dtype=np.float32), np.zeros((5, 3), dtype=np.float32),
    np.zeros((2, 2, 2), dtype=np.float32), np.zeros(8, dtype=np.int16),
    np.array([0, np.nan], dtype=np.float32),
])
def test_strict_audio_validation(bad):
    with pytest.raises((TypeError, ValueError)):
        audit.audit_vocal_endpoint(_vocal(), bad, SR)


def test_active_100ms_least_squares_matching_and_fractional_lag():
    reference = _vocal()
    endpoint = _delay(reference / np.float32(1.5), 2.25)
    mask, activity = audit.active_mask(reference, SR)
    level = audit.least_squares_level(reference, reference / np.float32(1.5), mask)
    lag = audit.lag_diagnostics(reference, endpoint, mask, max_lag=12)
    assert activity["frame_ms"] == 100
    assert level["gain_linear"] == pytest.approx(1.5, rel=.03)
    assert lag["median_lag_samples"] == pytest.approx(2.25, abs=.2)
    assert all(isinstance(item["integer_samples"], int) for item in lag["segments"])
    assert all(abs(item["fractional_samples"]) <= .5 for item in lag["segments"])


def test_optional_static_correction_is_verified():
    reference = _vocal()
    endpoint = _delay(reference / np.float32(1.25), 2.0)
    with pytest.raises(audit.VocalEndpointAuditError, match="audit gates"):
        audit.audit_vocal_endpoint(reference, endpoint, SR, max_lag=12)
    result = audit.audit_vocal_endpoint(
        reference, endpoint, SR, correct=True, max_lag=12,
        max_abs_lag=.35, gain_tolerance_db=.2,
    )
    assert result.report["correction"]["applied"] is True
    assert "constant" in result.report["correction"]["method"]
    assert result.report["after"]["passed"] is True
    assert result.report["after"]["correlation"]["minimum_required"] == pytest.approx(.80)
    assert result.report["after"]["correlation"]["minimum_observed"] >= .80
    assert abs(result.report["after"]["lag"]["median_lag_samples"]) < .35
    assert abs(result.report["after"]["level"]["gain_db"]) < .2


def test_unrelated_active_audio_cannot_pass_lag_level_and_peak_gates():
    reference = _vocal()
    rng = np.random.default_rng(991)
    unrelated = np.zeros_like(reference)
    unrelated[800:3200] = rng.normal(0, .08, 2400)
    unrelated[4000:7200] = rng.normal(0, .06, 3200)
    candidate = .25 * reference + np.sqrt(1 - .25 ** 2) * unrelated
    mask, _ = audit.active_mask(reference, SR)
    gain = audit.least_squares_level(reference, candidate, mask)["gain_linear"]
    candidate = np.multiply(candidate, gain, dtype=reference.dtype)

    permissive = audit.audit_vocal_endpoint(
        reference, candidate, SR, min_segment_correlation=-1.0,
        max_abs_lag=1.0, max_drift=1.0, gain_tolerance_db=.25,
    )
    assert permissive.report["after"]["level"]["gain_db"] == pytest.approx(0, abs=.25)
    assert permissive.report["after"]["peaks"]["passed"] is True
    assert permissive.report["after"]["correlation"]["minimum_observed"] < .80
    with pytest.raises(audit.VocalEndpointAuditError, match="audit gates"):
        audit.audit_vocal_endpoint(
            reference, candidate, SR, max_abs_lag=1.0, max_drift=1.0,
            gain_tolerance_db=.25,
        )


@pytest.mark.parametrize("bad", [True, False, np.bool_(True), np.nan, np.inf, -np.inf, -1.01, 1.01, "0.8"])
def test_minimum_segment_correlation_validation(bad):
    with pytest.raises((TypeError, ValueError), match="min_segment_correlation"):
        audit.audit_vocal_endpoint(_vocal(), _vocal(), SR, min_segment_correlation=bad)


def test_stereo_mid_side_and_inactive_delta_audit():
    reference = _vocal(stereo=True)
    endpoint = reference.copy()
    endpoint[:400] += np.array([.001, -.001], dtype=np.float32)
    result = audit.audit_vocal_endpoint(reference, endpoint, SR)
    spatial = result.report["after"]["spatial_and_inactive_delta"]
    assert spatial["inactive_delta_rms"] > 0
    assert spatial["inactive_samples"] > 0
    assert set(spatial["reference"]) == {"stereo_correlation", "mid_rms", "side_rms"}
    assert spatial["delta"]["side_rms"] > 0


def test_exact_shape_policy_rejects_unexplained_and_allows_documented_tail_trim():
    reference = _vocal()
    longer = np.pad(reference, (0, 8))
    with pytest.raises(audit.VocalEndpointAuditError, match="unexplained"):
        audit.audit_vocal_endpoint(reference, longer, SR)
    with pytest.raises(audit.VocalEndpointAuditError, match="unexplained"):
        audit.reconcile_target_shape(longer, reference.shape, policy="trim_tail")
    result = audit.audit_vocal_endpoint(
        reference, longer, SR, shape_policy="trim_tail",
        shape_reason="endpoint contract includes eight transport tail samples",
    )
    assert result.report["shape_reconciliation"]["policy"] == "trim_tail"
    assert result.audited.shape == reference.shape


def test_peak_helper_has_sample_and_4x_true_peak_gates():
    impulse = np.zeros(256, dtype=np.float32)
    impulse[128] = .9
    passed = audit.peak_gate(impulse, sample_peak_limit=1, true_peak_limit=2)
    assert passed["sample_peak"] == pytest.approx(.9)
    assert passed["true_peak_4x"] >= passed["sample_peak"]
    assert passed["passed"] is True
    assert audit.peak_gate(impulse, sample_peak_limit=.8, true_peak_limit=2)["passed"] is False


def test_float_artifacts_hash_report_and_overwrite_protection(tmp_path):
    reference = _vocal(stereo=True)
    result = audit.audit_vocal_endpoint(reference, reference.copy(), SR)
    paths = audit.save_audit(result, tmp_path / "audit", SR)
    persisted = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert set(persisted["artifacts"]) == {"reference", "endpoint", "audited"}
    for name in persisted["artifacts"]:
        info = sf.info(paths[name])
        assert (info.format, info.subtype, info.samplerate) == ("WAV", "FLOAT", SR)
        assert len(persisted["artifacts"][name]["samples_sha256"]) == 64
        assert len(persisted["artifacts"][name]["wav_sha256"]) == 64
    assert len(persisted["hashes"]["audited_samples_sha256"]) == 64
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        audit.save_audit(result, tmp_path / "audit", SR)
    audit.save_audit(result, tmp_path / "audit", SR, overwrite=True)


def test_declared_target_dtype_rate_and_empty_output_rejected(tmp_path):
    reference = _vocal()
    with pytest.raises(audit.VocalEndpointAuditError, match="declared"):
        audit.audit_vocal_endpoint(reference, reference, SR, target_shape=(len(reference) - 1,))
    with pytest.raises(TypeError, match="same float dtype"):
        audit.audit_vocal_endpoint(reference, reference.astype(np.float64), SR)
    result = audit.audit_vocal_endpoint(reference, reference, SR)
    with pytest.raises(ValueError, match="explicit"):
        audit.save_audit(result, "", SR)
    with pytest.raises(ValueError, match="does not match"):
        audit.save_audit(result, tmp_path / "x", 44_100)
