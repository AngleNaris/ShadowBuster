"""母带统计旁车（<out>.mastering.json）透传、缓存恢复与最终复制回归。

锚定五条保证：
  1. stage_soren 原样透传引擎旁车统计（返回 dict）；引擎没产出统计时返回
     None，绝不编造；
  2. run_pipeline 把统计复制为 <最终wav>.mastering.json；缓存命中（引擎未跑）
     同样恢复并复制；母带旁路时移除同名过期旁车；
  3. 统计里的 NaN 原样保留（不丢弃、不因 NaN 失败）；
  4. 引擎源码内容参与缓存身份：改 core_decrypted.py 使母带缓存自动失效；
  5. GUI 既有进度日志通道展示 target / actual / target_status。
"""
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import scipy.io.wavfile as wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import processing_cli
import studio_backend as backend

STATS = {"engine": "soren_bounded_v2", "style_mode": "off",
         "target_lufs": -14.0, "actual_lufs": -14.08,
         "target_error_lu": 0.08, "target_signed_error_lu": -0.08,
         "target_status": "met", "true_peak_dbtp": -0.41}


def _tone_wav(path, sr=44100):
    t = np.arange(int(sr * 0.05)) / sr
    data = np.column_stack((0.02 * np.sin(2 * np.pi * 440 * t),) * 2)
    wavfile.write(path, sr, data.astype(np.float32))
    return path


@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    """把引擎子进程换成“写成品+旁车”的假实现；返回可变的统计与调用记录。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "core_decrypted.py").write_text("# engine stub\n", encoding="utf-8")
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    state = {"stats": dict(STATS), "calls": []}

    def run_stream(cmd, cwd, env=None, cancel=None, on_progress=None):
        state["calls"].append(list(cmd))
        out = Path(cmd[3])
        out.write_bytes(b"mastered")
        Path(str(out) + backend.MASTERING_REPORT_SUFFIX).write_text(
            json.dumps(state["stats"]), encoding="utf-8")

    monkeypatch.setattr(backend, "_run_stream", run_stream)
    return state


def test_stage_soren_returns_engine_stats(fake_engine, tmp_path):
    out = tmp_path / "out.wav"
    stats = backend.stage_soren(_tone_wav(tmp_path / "in.wav"), out, style_mode="off")
    assert stats["engine"] == "soren_bounded_v2"
    assert stats["target_lufs"] == -14.0 and stats["target_status"] == "met"
    assert Path(str(out) + backend.MASTERING_REPORT_SUFFIX).is_file()


def test_stage_soren_without_sidecar_returns_none(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    monkeypatch.setattr(backend, "_run_stream",
                        lambda cmd, *a, **k: Path(cmd[3]).write_bytes(b"x"))
    out = tmp_path / "out.wav"
    assert backend.stage_soren(_tone_wav(tmp_path / "in.wav"), out) is None
    assert not Path(str(out) + backend.MASTERING_REPORT_SUFFIX).exists()


def test_report_copied_and_restored_on_cache_hit(fake_engine, tmp_path):
    src = _tone_wav(tmp_path / "song.wav")
    out_dir = tmp_path / "out"
    final_report = out_dir / "song_shadowbuster.wav.mastering.json"
    labels = []
    kwargs = dict(bypass=["lew", "bass", "drums", "reshape", "vocals"],
                  style_mode="off", loudness="normal")

    backend.run_batch([src], out_dir,
                      progress=lambda *args: labels.append(args[4]), **kwargs)
    assert len(fake_engine["calls"]) == 1
    first = final_report.read_text(encoding="utf-8")
    assert json.loads(first)["target_status"] == "met"
    assert any("目标" in lb and "实测" in lb and "达标" in lb for lb in labels)

    labels.clear()
    backend.run_batch([src], out_dir,
                      progress=lambda *args: labels.append(args[4]), **kwargs)
    assert len(fake_engine["calls"]) == 1            # 引擎未再运行：缓存命中
    assert final_report.read_text(encoding="utf-8") == first   # 统计恢复并复制
    assert any("目标" in lb and "实测" in lb and "达标" in lb for lb in labels)


def test_bypassed_master_leaves_no_stale_report(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "SOREN_DIR", tmp_path / "runtime")
    src = _tone_wav(tmp_path / "song.wav")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = out_dir / "song_shadowbuster.wav.mastering.json"
    stale.write_text('{"target_lufs": -99.0}', encoding="utf-8")
    backend.run_pipeline(src, out_dir, cache_enabled=False,
                         bypass=["lew", "bass", "drums", "reshape", "vocals", "soren"])
    assert not stale.exists()                        # 无统计 → 过期旁车移除，不伪造
    final = out_dir / "song_shadowbuster.wav"
    assert final.read_bytes() == src.read_bytes()    # 全旁路逐字节透传不变


def test_nan_stats_survive_to_final_report(fake_engine, tmp_path):
    src = _tone_wav(tmp_path / "song.wav")
    fake_engine["stats"] = {"target_lufs": -14.0, "actual_lufs": float("nan"),
                            "target_status": "met"}
    labels = []
    out_dir = tmp_path / "out"
    final_report = out_dir / "song_shadowbuster.wav.mastering.json"
    backend.run_batch([src], out_dir,
                      progress=lambda *args: labels.append(args[4]),
                      bypass=["lew", "bass", "drums", "reshape", "vocals"],
                      style_mode="off")
    text = final_report.read_text(encoding="utf-8")
    assert "NaN" in text                             # 字节复制：NaN 不丢弃
    stats = json.loads(text)
    assert math.isnan(stats["actual_lufs"])
    assert any("实测 NaN LUFS" in lb for lb in labels)


def test_engine_source_changes_invalidate_cache(tmp_path, monkeypatch):
    src = _tone_wav(tmp_path / "song.wav")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "core_decrypted.py").write_text("# v1\n", encoding="utf-8")
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    calls = []

    def soren(input_wav, out_wav, **kwargs):         # 无旁车的测试替身：仍可缓存
        calls.append(1)
        shutil.copyfile(input_wav, out_wav)

    monkeypatch.setattr(backend, "stage_soren", soren)
    kwargs = dict(bypass=["lew", "bass", "drums", "reshape", "vocals"],
                  style_mode="off")
    backend.run_pipeline(src, tmp_path / "out", cache_enabled=True, **kwargs)
    backend.run_pipeline(src, tmp_path / "out", cache_enabled=True, **kwargs)
    assert len(calls) == 1                           # 引擎不变 → 母带命中缓存
    (runtime / "core_decrypted.py").write_text("# v2 算法变更\n", encoding="utf-8")
    backend.run_pipeline(src, tmp_path / "out", cache_enabled=True, **kwargs)
    assert len(calls) == 2                           # 引擎源码在缓存身份里 → 失效


def test_mastering_log_label_variants():
    assert backend._mastering_log_label(
        {"target_lufs": -14.0, "actual_lufs": -14.08, "target_status": "met"}) == \
        "母带统计（目标 -14.0 LUFS / 实测 -14.1 LUFS / 达标）"
    assert backend._mastering_log_label({"target_status": "below_target"}) == \
        "母带统计（低于目标）"
    assert backend._mastering_log_label({"actual_lufs": -12.0, "target_met": False}) == \
        "母带统计（实测 -12.0 LUFS / 未达标）"
    assert backend._mastering_log_label({"actual_lufs": float("nan")}) == \
        "母带统计（实测 NaN LUFS）"
    assert backend._mastering_log_label({"foo": 1}) is None
    assert backend._mastering_log_label(None) is None


def test_cli_help_describes_processing_intensity(capsys):
    with pytest.raises(SystemExit):
        processing_cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert "processing intensity" in help_text
    assert "bit-identical" in help_text
    assert "dry/wet" not in help_text
