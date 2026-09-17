"""§10.4 参考模式响度契约（开发规格 v2 §10.4）。

有效参考覆盖流派的**音色**目标，但**不覆盖用户的响度选择**：参考模式 +
--reference-tone-only 时，响度目标 = 流派 profile 基准（genre 或 Pop 回退）
+ 响度档偏移；不带该标志的旧路径（参考实测响度作基准）作为兼容行为保留，
仅供给直接调用引擎的旧脚本。

能力级证据（黑盒实测，2026-09-17）：
  旧路径：--reference pop_ref(−11.76 LUFS) --style-mode styled → 目标
  −11.7584（= 参考响度 + 0.0 档偏移），Pop profile（−9.20）完全未参与。
  tone-only（本测试的 mocked 等价 + 真实探针复核）：目标回到 profile 基准。
"""
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.soren_resources import SOREN_RESOURCE_DIR  # noqa: E402
sys.path.insert(0, str(SOREN_RESOURCE_DIR))

import studio_backend as backend  # noqa: E402

spec = importlib.util.spec_from_file_location(
    'tone_only_core', ROOT / 'packaging' / 'soren_core.py')
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)


class _Capture(Exception):
    def __init__(self, requested_lufs):
        self.requested_lufs = requested_lufs


@pytest.fixture()
def stereo():
    rng = np.random.default_rng(20260918)
    x = 0.3 * rng.standard_normal((2, 44100 * 2))
    return x


def _run_styled_with_reference(monkeypatch, stereo, reference_lufs,
                               reference_tone_only, genre=None,
                               profile_lufs=-9.20, loudness="normal"):
    """跑 process_original_styled 的响度目标解析（其余环节全部替身）。"""
    cfg = core.Config()
    cfg.style_mode = "styled"
    cfg.style_blend = 0.5
    cfg.loudness_option = loudness
    cfg.genre = genre
    cfg.reference_tone_only = reference_tone_only
    cfg.last_mastering_stats = {}

    monkeypatch.setattr(core, 'load_genre_profile',
                        lambda g: {'genre': g, 'lufs': profile_lufs})
    monkeypatch.setattr(core, 'calculate_lufs', lambda *a, **k: reference_lufs)
    monkeypatch.setattr(core, 'calculate_true_peak', lambda *a, **k: -6.0)

    class _Orig:
        class Config:
            pass

        @staticmethod
        def process_audio(audio, reference, step, config, profile):
            return audio

    monkeypatch.setattr(core, 'load_soren_original_module', lambda: _Orig)

    def fake_transparent(target, config, requested_lufs, module):
        raise _Capture(requested_lufs)

    monkeypatch.setattr(core, 'process_transparent', fake_transparent)
    reference = stereo * 0.5
    with pytest.raises(_Capture) as exc:
        core.process_original_styled(stereo, reference, 5, cfg, None)
    return exc.value.requested_lufs, cfg


def test_tone_only_keeps_loudness_on_genre_profile(monkeypatch, stereo):
    """tone-only：目标 = 流派基准 + 档偏移，与参考实测响度无关。"""
    # 参考响度 −20（很远）：tone-only 下完全不影响目标
    lufs, cfg = _run_styled_with_reference(
        monkeypatch, stereo, reference_lufs=-20.0,
        reference_tone_only=True, genre=None, profile_lufs=-9.20,
        loudness="normal")
    assert lufs == pytest.approx(-9.20)
    assert cfg.loudness_target_source == "genre_profile:Pop"

    # 响度档仍生效（软 −3.10）：用户响度选择未被参考覆盖
    lufs_soft, _ = _run_styled_with_reference(
        monkeypatch, stereo, reference_lufs=-20.0,
        reference_tone_only=True, genre=None, profile_lufs=-9.20,
        loudness="soft")
    assert lufs_soft == pytest.approx(-9.20 - 3.10)

    # 显式 genre 作基准
    lufs_g, cfg_g = _run_styled_with_reference(
        monkeypatch, stereo, reference_lufs=-20.0,
        reference_tone_only=True, genre="Orchestral", profile_lufs=-19.88)
    assert lufs_g == pytest.approx(-19.88)
    assert cfg_g.loudness_target_source == "genre_profile:Orchestral"


def test_legacy_reference_loudness_behavior_preserved(monkeypatch, stereo):
    """不带 tone-only 的旧路径保持：目标 = 参考实测响度 + 档偏移。"""
    lufs, cfg = _run_styled_with_reference(
        monkeypatch, stereo, reference_lufs=-11.76,
        reference_tone_only=False, genre=None, profile_lufs=-9.20)
    assert lufs == pytest.approx(-11.76)
    assert cfg.loudness_target_source == "reference_measured"


def test_tone_only_dispatch_uses_reference_file(monkeypatch, tmp_path, stereo):
    """CLI 分发：tone-only + 参考文件（即使同时有 genre）走参考分支，
    genre 不再挤掉参考（旧分发 genre 优先于 reference）。"""
    # 造最小输入/参考文件（load_audio 替身只看路径）
    import soundfile as sf
    inp = tmp_path / "in.wav"
    ref = tmp_path / "ref.wav"
    sf.write(inp, stereo.T.astype(np.float32), 44100, subtype="FLOAT")
    sf.write(ref, stereo.T.astype(np.float32) * 0.5, 44100, subtype="FLOAT")

    seen = {}
    real_load = core.load_audio

    def fake_load(path, config):
        if str(path) == str(ref):
            seen['reference_loaded'] = True
            return stereo * 0.5, 44100
        return real_load(path, config)

    monkeypatch.setattr(core, 'load_audio', fake_load)
    monkeypatch.setattr(core, 'log_audio_metrics', lambda *a, **k: None)
    monkeypatch.setattr(core, 'load_genre_profile',
                        lambda g: {'genre': g, 'lufs': -9.20})

    def fake_process(target, reference, step, config, profile):
        seen['genre_profile_arg'] = profile
        seen['reference_arg_shape'] = None if reference is None else reference.shape
        config.last_mastering_stats = {"style_mode": "styled"}  # 替身初始化统计
        return target * 0.1    # 替身输出降到安全电平，让 TP 回读门通过

    monkeypatch.setattr(core, 'process_audio', fake_process)
    cfg = core.Config()
    cfg.style_mode = "styled"
    cfg.genre = "Pop"                     # 同时给了 genre——tone-only 下不得挤掉参考
    cfg.reference_file = str(ref)
    cfg.reference_tone_only = True
    core.master_audio(str(inp), str(tmp_path / "out.wav"), cfg, "Neutral")
    assert seen.get('reference_loaded') is True
    assert seen['reference_arg_shape'] == stereo.shape       # 真实参考进了管线
    assert seen['genre_profile_arg'] is None                # 音色走参考


def test_shell_passes_tone_only_flag_with_reference(monkeypatch, tmp_path):
    """stage_soren：参考模式命令带 --reference-tone-only（+真值 genre）；
    无参考时不带。"""
    from pathlib import Path as P
    import soundfile as sf
    inp = tmp_path / "in.wav"
    t = np.arange(44100) / 44100
    sf.write(inp, np.column_stack((0.2 * np.sin(2 * np.pi * 440 * t),) * 2),
             44100, subtype="PCM_16")
    ref = tmp_path / "ref.wav"
    sf.write(ref, np.column_stack((0.1 * np.sin(2 * np.pi * 440 * t),) * 2),
             44100, subtype="PCM_16")
    seen = []
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: P(str(tmp_path)))
    monkeypatch.setattr(backend, "_run_stream",
                        lambda cmd, cwd, **k: seen.append([str(c) for c in cmd]))
    backend.stage_soren(inp, tmp_path / "o.wav", genre="Pop", reference=ref)
    cmd = seen[-1]
    assert "--reference" in cmd and "--reference-tone-only" in cmd
    assert cmd[cmd.index("--genre") + 1] == "Pop"
    # 参考与 tone-only 相邻出现
    assert abs(cmd.index("--reference-tone-only") - cmd.index("--reference")) <= 2

    seen.clear()
    backend.stage_soren(inp, tmp_path / "o2.wav", genre="EDM", reference=None)
    cmd = seen[-1]
    assert "--reference" not in cmd and "--reference-tone-only" not in cmd
    assert cmd[cmd.index("--genre") + 1] == "EDM"
