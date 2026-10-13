"""The frozen Qt shell excludes SciPy; reports must use the audio interpreter."""
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pytest
import soundfile as sf

import studio_backend as backend


@pytest.fixture
def frozen_audio_runtime(tmp_path, monkeypatch):
    shutil.copyfile(Path(backend.__file__).parent / "audio_metrics.py", tmp_path / "audio_metrics.py")
    monkeypatch.setattr(backend, "MASTERING_ROOT", tmp_path)
    monkeypatch.setattr(backend, "PYTHON", sys.executable)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def unavailable_shell_writer(*args, **kwargs):
        raise AssertionError("The shell has no SciPy")

    monkeypatch.setattr(backend, "write_quality_report", unavailable_shell_writer)
    return tmp_path


def test_frozen_shell_generates_real_quality_sidecar(frozen_audio_runtime):
    directory = frozen_audio_runtime
    sr = 44100
    t = np.arange(sr * 2) / sr
    audio = np.column_stack((.1 * np.sin(2 * np.pi * 440 * t),
                             .1 * np.sin(2 * np.pi * 440 * t + .1)))
    source, target = directory / "source.wav", directory / "target.wav"
    sf.write(source, audio, sr, subtype="FLOAT")
    sf.write(target, audio * 2, sr, subtype="PCM_24")
    report = backend._write_pipeline_quality_report(source, target, params={"loudness": "normal"})
    persisted = json.loads(backend.quality_report_path(target).read_text(encoding="utf-8"))
    assert persisted == report
    assert report["processing"]["loudness"] == "normal"
    metrics = report["output"]["metrics"]
    assert np.isfinite(metrics["integrated_lufs"])
    assert metrics["true_peak_4x_dbtp"] < -10
    assert metrics["clipping_samples"] == 0


def test_audio_runtime_report_error_reaches_the_caller(frozen_audio_runtime):
    directory = frozen_audio_runtime
    with pytest.raises(backend.PipelineError, match="质量报告生成失败"):
        backend._write_pipeline_quality_report(directory / "missing.wav", directory / "target.wav")
