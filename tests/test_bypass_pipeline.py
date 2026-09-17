"""lew/分轨旁路契约：非 44.1k 输入先统一采样率、全旁路逐字节透传、分轨按需跳过。

回归背景：旁路 Lew 时原输入直入 Demucs，而 Demucs 分轨保留输入采样率，
48k 源的中间产物全部保持 48k，Soren 母带只接受 44.1k 直接报错；同时四个
分轨消费阶段全部旁路时分离阶段仍照跑，最重的计算被白白执行。
"""
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import studio_backend as backend


def _tone_wav(path, sr):
    t = np.arange(int(sr * 0.1)) / sr
    data = np.column_stack((0.02 * np.sin(2 * np.pi * 440 * t),) * 2)
    sf.write(path, data, sr, subtype="PCM_16")
    return path


@pytest.fixture
def stub_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: tmp_path / "runtime")
    return tmp_path


def test_lew_bypass_resamples_48k_and_skips_useless_demucs(stub_runtime, tmp_path, monkeypatch):
    """48k 源 + 旁路 Lew：进链路前统一 44.1k；分轨消费阶段全旁路时不跑分离。"""
    src = _tone_wav(tmp_path / "song.wav", sr=48000)
    calls = {"convert": [], "demucs": 0, "soren": []}

    def convert(src, dst, sr=44100):
        calls["convert"].append((Path(src).name, Path(dst).name, sr))
        _tone_wav(dst, sr)

    def demucs(input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None):
        calls["demucs"] += 1

    def soren(input_wav, out_wav, **kwargs):
        # 工作目录在 run_pipeline 结束时清理，mock 内当场读取，不能留到断言。
        calls["soren"].append(sf.info(input_wav).samplerate)
        shutil.copyfile(input_wav, out_wav)

    monkeypatch.setattr(backend, "ffmpeg_convert", convert)
    monkeypatch.setattr(backend, "stage_demucs", demucs)
    monkeypatch.setattr(backend, "stage_soren", soren)
    backend.run_pipeline(src, tmp_path / "out",
                         bypass=["lew", "bass", "drums", "reshape", "vocals"],
                         cache_enabled=False)

    assert len(calls["convert"]) == 1
    assert calls["convert"][0][2] == 44100
    assert calls["convert"][0][1].endswith("_lew.wav")
    assert calls["demucs"] == 0
    assert calls["soren"] == [44100]


def test_lew_bypass_feeds_44k_mix_into_demucs_when_stem_stage_enabled(stub_runtime, tmp_path, monkeypatch):
    """任一分轨消费阶段启用时分离照常执行，且拿到的是统一后的 44.1k 混音。"""
    src = _tone_wav(tmp_path / "song.wav", sr=48000)
    demucs_inputs = []

    def convert(src, dst, sr=44100):
        _tone_wav(dst, sr)

    def demucs(input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None):
        demucs_inputs.append((Path(input_wav).name, sf.info(input_wav).samplerate))

    def passthrough(stem_dir, in_mix, out_wav, **kwargs):
        shutil.copyfile(in_mix, out_wav)
        return 1.0

    monkeypatch.setattr(backend, "ffmpeg_convert", convert)
    monkeypatch.setattr(backend, "stage_demucs", demucs)
    monkeypatch.setattr(backend, "stage_bass", passthrough)
    monkeypatch.setattr(backend, "stage_drums", passthrough)
    monkeypatch.setattr(backend, "stage_reshape",
                        lambda in_mix, stems_dir, out_wav, **kwargs: (shutil.copyfile(in_mix, out_wav), 1.0)[1])
    monkeypatch.setattr(backend, "stage_vocals",
                        lambda stem_dir, in_mix, out_wav, **kwargs: shutil.copyfile(in_mix, out_wav))
    monkeypatch.setattr(backend, "stage_soren",
                        lambda input_wav, out_wav, **kwargs: shutil.copyfile(input_wav, out_wav))
    backend.run_pipeline(src, tmp_path / "out", bypass=["lew"], balance_mode=None,
                         cache_enabled=False)

    assert len(demucs_inputs) == 1
    assert demucs_inputs[0][0].endswith("_lew.wav")
    assert demucs_inputs[0][1] == 44100
    assert (tmp_path / "out" / "song_shadowbuster.wav").exists()


def test_lew_bypass_keeps_44k_input_bit_exact(stub_runtime, tmp_path, monkeypatch):
    """输入已是 44.1k 时不重编码：旁路 Lew + 全部分轨旁路，母带收到原文件字节。"""
    src = _tone_wav(tmp_path / "song.wav", sr=44100)
    calls = {"convert": 0, "soren": []}

    def convert(src, dst, sr=44100):
        calls["convert"] += 1

    def soren(input_wav, out_wav, **kwargs):
        calls["soren"].append(Path(input_wav).read_bytes())
        shutil.copyfile(input_wav, out_wav)

    monkeypatch.setattr(backend, "ffmpeg_convert", convert)
    monkeypatch.setattr(backend, "stage_soren", soren)
    backend.run_pipeline(src, tmp_path / "out",
                         bypass=["lew", "bass", "drums", "reshape", "vocals"],
                         cache_enabled=False)

    assert calls["convert"] == 0
    assert len(calls["soren"]) == 1
    assert calls["soren"][0] == src.read_bytes()
    assert (tmp_path / "out" / "song_shadowbuster.wav").read_bytes() == src.read_bytes()
