"""第 4 批次六轨（htdemucs_6s / guitar / piano）后端与 CLI 路由回归。

锚定契约：
  1. 默认 htdemucs 四轨：guitar/piano 阶段不执行，质量报告不记录新参数；
  2. demucs_model="htdemucs_6s"（opt-in）：stem 产物目录换模型名、guitar/piano
     两个中性阶段按 reshape→guitar→piano→vocals 顺序路由，模型名进缓存身份；
  3. 模型与分轨控制在跑任何推理前校验；
  4. 阶段报告 extras 进入最终质量报告 stages；
  5. CLI 解析期拒绝非法模型/范围（exit 2）并转发默认值。
"""
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import processing_cli  # noqa: E402
import studio_backend as backend  # noqa: E402
from apollo_scripts.stage_metadata import write_report  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    # 测试不得读写开发者真实处理缓存：替身产物会污染真实身份条目。
    monkeypatch.setenv('SB_PROCESSING_CACHE_DIR', str(tmp_path / 'cache'))


def _tone_wav(path, seconds=0.05, sr=44100):
    t = np.arange(int(sr * seconds)) / sr
    data = np.column_stack((0.02 * np.sin(2 * np.pi * 440 * t),) * 2)
    sf.write(path, data, sr, subtype="FLOAT")
    return path


class StemFakes:
    """替身：demucs 记 model，bass/drums/reshape 透传，guitar/piano 记调用并写真
    报告，vocals 记输入，soren 透传。"""

    def __init__(self, monkeypatch, tmp_path):
        self.calls = []
        self.demucs_models = []
        self.vocal_inputs = []
        runtime = tmp_path / "runtime"
        runtime.mkdir(exist_ok=True)
        monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)

        def demucs(input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None):
            self.demucs_models.append(model)
            # 真实 demucs 会创建 out_dir/<model>/<stem>/；替身至少建 out_dir，
            # 否则 StageCache 因产物不存在而永不缓存该阶段。
            Path(out_dir).mkdir(parents=True, exist_ok=True)

        def stem_fake(kind):
            def fake(stem_dir, in_mix, out_wav, gain_db=0.0, mud_cut_db=0.0,
                     presence_db=0.0, harsh_cut_db=0.0, width_db=0.0,
                     progress=None, cancel=None, report_json=None):
                self.calls.append((kind, Path(stem_dir).parent.name, Path(stem_dir).name,
                                   gain_db, mud_cut_db, presence_db, harsh_cut_db,
                                   width_db, Path(in_mix).name))
                shutil.copyfile(in_mix, out_wav)
                if report_json is not None:
                    write_report(report_json, stage=kind, scale=1.0,
                                 input_path=in_mix, output_path=out_wav,
                                 extra={"stem": kind, "applied": True, "status": "applied",
                                        "gains_db": {"gain": gain_db}})
                return 1.0
            fake.writes_stage_report = True
            fake.report_input_arg = "in_mix"
            return fake

        def vocals(stem_dir, in_mix, out_wav, **kwargs):
            self.vocal_inputs.append(Path(in_mix).name)
            shutil.copyfile(in_mix, out_wav)

        def soren(input_wav, out_wav, **kwargs):
            shutil.copyfile(input_wav, out_wav)

        def _copy1(src, dst):
            shutil.copyfile(src, dst)
            return 1.0

        monkeypatch.setattr(backend, "stage_demucs", demucs)
        monkeypatch.setattr(backend, "stage_bass",
                            lambda stem_dir, in_mix, out_wav, **k: _copy1(in_mix, out_wav))
        monkeypatch.setattr(backend, "stage_drums",
                            lambda stem_dir, rest_wav, out_wav, **k: _copy1(rest_wav, out_wav))
        monkeypatch.setattr(backend, "stage_reshape",
                            lambda in_mix, stems_dir, out_wav, **k: _copy1(in_mix, out_wav))
        monkeypatch.setattr(backend, "stage_guitar", stem_fake("guitar"))
        monkeypatch.setattr(backend, "stage_synth", stem_fake("synth"))
        monkeypatch.setattr(backend, "stage_vocals", vocals)
        monkeypatch.setattr(backend, "stage_soren", soren)


def _run_kwargs(**overrides):
    kwargs = dict(bypass=["lew"], balance_mode=None, cache_enabled=False)
    kwargs.update(overrides)
    return kwargs


# ── 1. 校验先于一切 ──────────────────────────────────────────────────

def test_invalid_model_and_controls_rejected_before_anything(tmp_path):
    missing = tmp_path / "missing.wav"
    with pytest.raises(backend.PipelineError, match="分离模型"):
        backend.run_pipeline(missing, tmp_path / "o", demucs_model="bogus")
    with pytest.raises(backend.PipelineError, match="guitar.gain_db"):
        backend.run_pipeline(missing, tmp_path / "o", guitar_gain_db=9)
    with pytest.raises(backend.PipelineError, match="synth.width_db"):
        backend.run_pipeline(missing, tmp_path / "o", synth_width_db=-1)


# ── 2. 四轨默认不路由新阶段 ──────────────────────────────────────────

def test_default_four_stem_skips_extra_stages(tmp_path, monkeypatch):
    src = _tone_wav(tmp_path / "song.wav")
    fakes = StemFakes(monkeypatch, tmp_path)
    final = backend.run_pipeline(src, tmp_path / "out", **_run_kwargs())
    assert fakes.calls == [] and fakes.demucs_models == ["htdemucs"]
    report = backend.read_quality_report(final)
    assert "demucs_model" not in report["processing"]
    assert "guitar" not in report["stages"] and "piano" not in report["stages"]


# ── 3. 六轨路由：拓扑 / 顺序 / 中性参数 / 报告 ────────────────────────

def test_six_stem_routes_extra_stages(tmp_path, monkeypatch):
    src = _tone_wav(tmp_path / "song.wav")
    fakes = StemFakes(monkeypatch, tmp_path)
    final = backend.run_pipeline(src, tmp_path / "out", **_run_kwargs(),
                                 demucs_model="htdemucs_6s",
                                 guitar_gain_db=1.5, synth_presence_db=2.0)
    assert fakes.demucs_models == ["htdemucs_6s"]
    kinds = [c[0] for c in fakes.calls]
    assert kinds == ["guitar", "synth"]           # reshape 之后、vocals 之前
    for kind, model_dir, stem_name, gain, mud, presence, harsh, width, vocal_in in fakes.calls:
        assert model_dir == "htdemucs_6s" and stem_name == "song"
        if kind == "guitar":
            assert (gain, mud, presence, harsh, width) == (1.5, 0, 0, 0, 0)
            assert vocal_in == "song_shapemix.wav"
        else:
            assert (gain, mud, presence, harsh, width) == (0, 0, 2.0, 0, 0)
            assert vocal_in == "song_guitarmix.wav"
    assert fakes.vocal_inputs == ["song_synthmix.wav"]
    report = backend.read_quality_report(final)
    assert report["processing"]["demucs_model"] == "htdemucs_6s"
    assert report["processing"]["stems"]["guitar"]["gain_db"] == 1.5
    assert report["stages"]["guitar"]["gains_db"]["gain"] == 1.5
    assert report["stages"]["synth"]["applied"] is True


# ── 4. 模型名进缓存身份 ──────────────────────────────────────────────

def test_demucs_model_changes_cache_identity(tmp_path, monkeypatch):
    src = _tone_wav(tmp_path / "song.wav")
    fakes = StemFakes(monkeypatch, tmp_path)
    kwargs = _run_kwargs(cache_enabled=True, bypass=["lew", "vocals"])
    backend.run_pipeline(src, tmp_path / "out", **kwargs)
    backend.run_pipeline(src, tmp_path / "out", **kwargs)
    assert fakes.demucs_models == ["htdemucs"]           # 第二次命中缓存
    backend.run_pipeline(src, tmp_path / "out", **kwargs, demucs_model="htdemucs_6s")
    assert fakes.demucs_models == ["htdemucs", "htdemucs_6s"]  # 模型变化 → 重算


# ── 5. stage_demucs / stage_stem 命令选择模型与源分轨 ─────────────────

def test_stage_demucs_cmd_selects_model(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(backend, "_run_stream",
                        lambda cmd, cwd, **k: seen.append([str(c) for c in cmd]))
    src = _tone_wav(tmp_path / "in.wav")
    backend.stage_demucs(src, tmp_path / "o")
    assert seen[0][seen[0].index("-n") + 1] == "htdemucs"
    backend.stage_demucs(src, tmp_path / "o", model="htdemucs_6s")
    assert seen[1][seen[1].index("-n") + 1] == "htdemucs_6s"


def test_stage_synth_cmd_merges_other_and_piano(tmp_path, monkeypatch):
    """synth 组阶段把 other+piano 两轨作为源传给 DSP（--stem + --stem2）。"""
    seen = []
    monkeypatch.setattr(backend, "_run_reported",
                        lambda cmd, *a, **k: seen.append([str(c) for c in cmd]) or 1.0)
    src = _tone_wav(tmp_path / "in.wav")
    backend.stage_synth(tmp_path / "stems" / "htdemucs_6s" / "song",
                        src, tmp_path / "out.wav", gain_db=2.0)
    cmd = seen[-1]
    assert cmd[cmd.index("--kind") + 1] == "synth"
    assert cmd[cmd.index("--stem") + 1].endswith("other.wav")
    assert cmd[cmd.index("--stem2") + 1].endswith("piano.wav")
    assert cmd[cmd.index("--gain-db") + 1] == "2.0"
    backend.stage_guitar(tmp_path / "stems" / "htdemucs_6s" / "song",
                         src, tmp_path / "out2.wav")
    cmd = seen[-1]
    assert cmd[cmd.index("--stem") + 1].endswith("guitar.wav")
    assert "--stem2" not in cmd


# ── 6. CLI 转发与解析期拒绝 ──────────────────────────────────────────

def test_cli_forwards_demucs_and_stem_options(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")
    captured = {}

    def fake_run_batch(inputs, output, **kwargs):
        captured.update(kwargs)
        return [(str(inputs[0]), "done.wav", None)]

    monkeypatch.setattr(backend, "run_batch", fake_run_batch)
    assert processing_cli.main(["-i", str(source), "-o", str(tmp_path / "o1")]) == 0
    assert captured["demucs_model"] == "htdemucs"
    assert all(captured[f"guitar_{k}"] == 0.0 for k in
               ("gain_db", "mud_cut_db", "presence_db", "harsh_cut_db", "width_db"))
    assert processing_cli.main(
        ["-i", str(source), "-o", str(tmp_path / "o2"),
         "--demucs-model", "htdemucs_6s",
         "--guitar-gain-db", "3", "--synth-harsh-cut-db", "2"]) == 0
    assert captured["demucs_model"] == "htdemucs_6s"
    assert captured["guitar_gain_db"] == 3.0
    assert captured["synth_harsh_cut_db"] == 2.0


@pytest.mark.parametrize("args", [
    ["--demucs-model", "bogus"],
    ["--guitar-gain-db", "7"],
    ["--synth-width-db", "-0.5"],
])
def test_cli_rejects_invalid_model_and_stem_ranges_at_parse(tmp_path, args):
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")
    with pytest.raises(SystemExit) as exc:
        processing_cli.main(["-i", str(source), "-o", str(tmp_path), *args])
    assert exc.value.code == 2
