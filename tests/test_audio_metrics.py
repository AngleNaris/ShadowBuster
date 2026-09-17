import json

import numpy as np
import soundfile as sf

from audio_metrics import (
    assess_quality,
    measure_audio,
    quality_report_path,
    read_quality_report,
    write_quality_report,
)


def test_measure_audio_reports_dynamic_stereo_and_low_band_metrics():
    sr = 44100
    t = np.arange(sr * 4) / sr
    mono = 0.08 * np.sin(2 * np.pi * 55 * t) + 0.03 * np.sin(2 * np.pi * 440 * t)
    audio = np.column_stack((mono, mono))
    metrics = measure_audio(audio, sr)
    assert metrics["channels"] == 2
    assert metrics["lra_lu"] is not None
    assert metrics["stereo_correlation"] > 0.99
    assert metrics["low_band_side_mid"]["ratio"] < 1e-6
    assert metrics["mono_fold_down_loss_db"] > -0.01
    assert metrics["clipping_samples"] == 0


def test_measure_audio_handles_silence_without_nan():
    metrics = measure_audio(np.zeros((44100, 2)), 44100)
    assert metrics["integrated_lufs"] == float("-inf")
    assert metrics["true_peak_4x_dbtp"] is None
    assert metrics["lra_lu"] is None
    assert metrics["crest_factor_db"] is None
    assert metrics["spectral_centroid_hz"] == 0.0


def test_quality_report_round_trips_actual_output_metrics(tmp_path):
    sr = 44100
    t = np.arange(sr * 2) / sr
    source = np.column_stack((0.1 * np.sin(2 * np.pi * 220 * t),
                              0.08 * np.sin(2 * np.pi * 220 * t)))
    output = source * 0.75
    source_path = tmp_path / "source.wav"
    output_path = tmp_path / "output.wav"
    sf.write(source_path, source.astype(np.float32), sr, subtype="FLOAT")
    sf.write(output_path, output.astype(np.float32), sr, subtype="FLOAT")
    report = write_quality_report(source_path, output_path,
                                  params={"loudness": "dynamic"},
                                  mastering_stats={"target_error_lu": 0.05})
    assert quality_report_path(output_path).is_file()
    loaded = read_quality_report(output_path)
    assert loaded["schema_version"] == 1
    assert loaded["output"]["metrics"] == report["output"]["metrics"]
    assert loaded["processing"]["loudness"] == "dynamic"
    assert loaded["quality"]["overall"] in {"pass", "warn"}
    json.loads(quality_report_path(output_path).read_text(encoding="utf-8"))


def test_quality_assessment_flags_peak_and_mono_loss():
    output = {"true_peak_4x_dbtp": -0.1, "clipping_samples": 2,
              "stereo_correlation": -0.5, "mono_fold_down_loss_db": -4.0}
    result = assess_quality({}, output)
    assert result["overall"] == "fail"
    assert result["constraints"]["true_peak"]["status"] == "fail"
    assert result["constraints"]["sample_clipping"]["status"] == "fail"
    assert result["constraints"]["stereo_correlation"]["status"] == "warn"
    assert result["constraints"]["mono_fold_down"]["status"] == "warn"


def test_spectrum_includes_late_high_band_content():
    sr = 44100
    t = np.arange(sr * 16) / sr
    x = 0.05 * np.sin(2 * np.pi * 1000 * t)
    x[sr * 8:] = 0.05 * np.sin(2 * np.pi * 14000 * t[sr * 8:])
    metrics = measure_audio(x, sr)
    assert 0.48 < metrics['band_energies']['12-16k']['fraction'] < 0.52
    assert 7000 < metrics['spectral_centroid_hz'] < 8000


def test_stage_diagnostics_round_trip_strict_json(tmp_path):
    source, output = tmp_path / 'in.wav', tmp_path / 'out.wav'
    for path in (source, output):
        sf.write(path, np.zeros((44100, 2)), 44100, subtype='FLOAT')
    extra = {'reshape': {'noise': {'mode': 'adaptive_all', 'applied': False,
                                  'stems': {'vocals': {'confidence': None, 'reason': 'silence'}}}}}
    write_quality_report(source, output, stage_reports=extra)
    text = quality_report_path(output).read_text(encoding='utf-8')
    assert 'NaN' not in text and 'Infinity' not in text
    assert json.loads(text)['stages'] == extra
    assert not list(tmp_path.glob('*.tmp'))


def test_quality_report_has_third_octave_compare(tmp_path):
    """报告页主图数据：1/3 倍频程前后对比（各自相对总能量，响度无关）。"""
    import numpy as np
    import soundfile as sf
    sr = 44100
    t = np.arange(sr * 2) / sr
    src = 0.2 * np.sin(2 * np.pi * 220 * t)
    src = np.column_stack((src, src))
    out = src * 0.5 + 0.08 * np.sin(2 * np.pi * 12000 * t)[:, None]   # 高频增亮
    src_w, out_w = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(src_w, src, sr, subtype="FLOAT")
    sf.write(out_w, out, sr, subtype="FLOAT")
    report = write_quality_report(src_w, out_w, params={})
    cmp = report["compare"]
    assert len(cmp["centers"]) >= 25
    assert len(cmp["input_db"]) == len(cmp["output_db"]) == len(cmp["centers"])
    diffs = [b - a for a, b in zip(cmp["input_db"], cmp["output_db"])]
    centers = cmp["centers"]
    high = max(d for c, d in zip(centers, diffs) if c >= 10000)
    low = next(d for c, d in zip(centers, diffs) if c == 200)
    # 高频增亮在 1/3 倍频程差值上清晰可见（合成正弦是窄带能量，量级可以很大）
    assert high > 3.0 and high > abs(low)
    # 相对总能量：两份文件各自归一，响度差异不进入频段分布
    # （纯音合成信号的空频段是频谱泄漏底，可以远低于 -95dB）
    assert all(-170 <= d <= 5 for d in cmp["input_db"] + cmp["output_db"])
