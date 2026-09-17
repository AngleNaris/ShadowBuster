"""批次 5：内部链路 float32 化的格式与精度合同。

锚定契约：
  1. ffmpeg_convert 默认产出 FLOAT（pcm_f32le）并通过采样率/声道校验；
     失败报错，绝不静默回退 PCM16；
  2. mix_wet_dry float64 计算、float32 写出——中间不再有 16-bit 截断台阶；
  3. 缓存身份携带 audio_format 版本（旧 PCM16 产物不混用）；
  4. 质量报告如实记录最终文件 subtype（Soren 旁路不伪装 PCM24）。
"""
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import studio_backend as backend  # noqa: E402
import pipeline_cache  # noqa: E402

SR = 44100


def _write(path, data, subtype="FLOAT"):
    sf.write(path, data, SR, subtype=subtype)
    return path


def _tone(seconds=0.3, amp=0.25):
    t = np.arange(int(SR * seconds)) / SR
    return np.column_stack((amp * np.sin(2 * np.pi * 440 * t),
                            amp * np.sin(2 * np.pi * 441 * t)))


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('SB_PROCESSING_CACHE_DIR', str(tmp_path / 'cache'))


def test_ffmpeg_convert_outputs_float_with_validation(tmp_path):
    src = _write(tmp_path / "in.wav", _tone(), subtype="PCM_16")
    dst = tmp_path / "out.wav"
    backend.ffmpeg_convert(src, dst)
    info = sf.info(dst)
    assert info.subtype == "FLOAT" and info.samplerate == SR and info.channels == 2


def test_ffmpeg_convert_supports_explicit_pcm16_only_when_asked(tmp_path):
    src = _write(tmp_path / "in.wav", _tone())
    dst = tmp_path / "out16.wav"
    backend.ffmpeg_convert(src, dst, subtype="PCM_16")
    assert sf.info(dst).subtype == "PCM_16"


def test_ffmpeg_convert_rejects_unknown_subtype(tmp_path):
    src = _write(tmp_path / "in.wav", _tone())
    with pytest.raises(backend.PipelineError, match="音频格式"):
        backend.ffmpeg_convert(src, tmp_path / "o.wav", subtype="PCM_24")


def test_mix_wet_dry_keeps_sub_16bit_detail(tmp_path):
    """16-bit 台阶（≈3e-5）以下的细节不再被截断：float 混合误差应远小于它。"""
    dry = _tone()
    detail = (np.arange(len(dry)) / len(dry) * 2e-5)[:, None]   # ≈ −94 dBFS 斜坡
    wet = dry * 0.5 + detail
    _write(tmp_path / "dry.wav", dry)
    _write(tmp_path / "wet.wav", wet)
    out = tmp_path / "mixed.wav"
    backend.mix_wet_dry(tmp_path / "dry.wav", tmp_path / "wet.wav", out, 0.5)
    assert sf.info(out).subtype == "FLOAT"
    mixed, _ = sf.read(out, dtype="float64", always_2d=True)
    expected = 0.75 * dry + 0.5 * detail
    # float32 写盘舍入 ~1e-7；旧 PCM16 截断误差 ~3e-5 会让该断言失败
    np.testing.assert_allclose(mixed, expected, atol=1e-6)


def test_mix_wet_dry_peak_protection_and_rate_check(tmp_path):
    loud = _tone(amp=1.2)
    _write(tmp_path / "dry.wav", loud)
    _write(tmp_path / "wet.wav", loud)
    out = tmp_path / "mixed.wav"
    backend.mix_wet_dry(tmp_path / "dry.wav", tmp_path / "wet.wav", out, 1.0)
    assert np.max(np.abs(sf.read(out, always_2d=True)[0])) <= 0.999 + 1e-6
    _write(tmp_path / "wet48.wav", _tone(), subtype="FLOAT")
    import soundfile as _sf
    data, _ = sf.read(tmp_path / "wet48.wav", always_2d=True)
    _sf.write(tmp_path / "wet48.wav", data, SR // 2, subtype="FLOAT")
    with pytest.raises(backend.PipelineError, match="sample rate mismatch"):
        backend.mix_wet_dry(tmp_path / "dry.wav", tmp_path / "wet48.wav",
                            tmp_path / "m2.wav", 0.5)


def test_pipeline_identity_carries_audio_format(tmp_path, monkeypatch):
    identities = []
    real_cache = pipeline_cache.StageCache

    class SpyCache(real_cache):
        def __init__(self, enabled=True, identity=None):
            identities.append(identity)
            super().__init__(enabled, identity)

    monkeypatch.setattr(pipeline_cache, "StageCache", SpyCache)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)

    def fake_stage(*args, **kwargs):
        out = kwargs.get("out_wav") or (args[2] if len(args) >= 3 else args[1])
        sf.write(out, _tone(0.05), SR, subtype="FLOAT")
        return 1.0

    monkeypatch.setattr(backend, "stage_demucs", lambda *a, **k: None)
    for name in ("stage_lew", "stage_bass", "stage_drums", "stage_reshape",
                 "stage_vocals", "stage_soren"):
        monkeypatch.setattr(backend, name, fake_stage)
    src = tmp_path / "song.wav"
    sf.write(src, _tone(0.1), SR, subtype="PCM_16")
    backend.run_pipeline(src, tmp_path / "out", bypass=["lew", "vocals"],
                         balance_mode=None, cache_enabled=False)
    assert identities and identities[0]["audio_format"] == backend.AUDIO_FORMAT_VERSION


def test_quality_report_records_real_output_subtype(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    monkeypatch.setattr(backend, "stage_demucs", lambda *a, **k: None)

    def fake_stage(*args, **kwargs):
        out = kwargs.get("out_wav") or (args[2] if len(args) >= 3 else args[1])
        sf.write(out, _tone(0.05), SR, subtype="FLOAT")
        return 1.0

    for name in ("stage_lew", "stage_bass", "stage_drums", "stage_reshape",
                 "stage_vocals"):
        monkeypatch.setattr(backend, name, fake_stage)

    def copy_soren(input_wav, out_wav, **kwargs):
        import shutil
        shutil.copyfile(input_wav, out_wav)

    monkeypatch.setattr(backend, "stage_soren", copy_soren)
    src = tmp_path / "song.wav"
    sf.write(src, _tone(0.1), SR, subtype="PCM_16")
    final = backend.run_pipeline(src, tmp_path / "out", bypass=["lew", "vocals"],
                                 balance_mode=None, cache_enabled=False)
    report = backend.read_quality_report(final)
    # Soren 被替身替代（未产 PCM24）：最终文件是内部 float 链路产物，
    # 报告必须如实记录 FLOAT，不伪装成 PCM24。
    assert report["processing"]["output_subtype"] == "FLOAT"
