"""UI 旋钮 → 处理语义固定映射（main.map_ui_params）的合同测试。

映射原则（不新增 UI 控件，面向非专业用户）：
  1. 高频降噪 fader > 0 → adaptive_all（跨分轨置信度降噪）；=0 → 兼容 other；
  2. 鼓身 punch → Kick/Bass 侧链 amount = 0.5×punch/10（有界）；
  3. 质量档 2 → 六轨（权重可用时）；权重缺失 → 明确回退四轨并给出提示；
  4. 显式隐藏参数（noise_mode/sidechain_amount/demucs_model）一律优先。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6.QtWebEngineWidgets")
import main as app_main  # noqa: E402


def test_denoise_fader_drives_adaptive_mode():
    mapping, notices = app_main.map_ui_params({"denoise": 0.2})
    assert mapping["noise_mode"] == "adaptive_all"
    assert notices == []


def test_zero_denoise_keeps_legacy_mode():
    mapping, _ = app_main.map_ui_params({"denoise": 0.0})
    assert mapping["noise_mode"] == "other"


def test_punch_drives_bounded_sidechain():
    assert app_main.map_ui_params({"punch": 0})[0]["sidechain_amount"] == 0.0
    assert app_main.map_ui_params({})[0]["sidechain_amount"] == 0.1      # 默认鼓身 2
    assert app_main.map_ui_params({"punch": 10})[0]["sidechain_amount"] == 0.5


def test_explicit_hidden_params_win():
    mapping, _ = app_main.map_ui_params(
        {"denoise": 0.5, "punch": 10, "noise_mode": "other", "sidechain_amount": 0.0})
    assert mapping["noise_mode"] == "other"
    assert mapping["sidechain_amount"] == 0.0


def test_quality_two_uses_six_stem_when_available():
    mapping, notices = app_main.map_ui_params({"quality": 2}, lambda: True)
    assert mapping["demucs_model"] == "htdemucs_6s"
    assert notices == []


def test_quality_two_falls_back_without_weights_with_notice():
    mapping, notices = app_main.map_ui_params({"quality": 2}, lambda: False)
    assert mapping["demucs_model"] == "htdemucs"
    assert notices and "四轨" in notices[0]


def test_standard_quality_stays_four_stem():
    mapping, notices = app_main.map_ui_params({"quality": 1}, lambda: False)
    assert mapping["demucs_model"] == "htdemucs"
    assert notices == []


def test_explicit_model_wins_and_skips_fallback_notice():
    mapping, notices = app_main.map_ui_params(
        {"quality": 2, "demucs_model": "htdemucs"}, lambda: False)
    assert mapping["demucs_model"] == "htdemucs"
    assert notices == []


def test_mapping_carries_full_noise_and_sidechain_contract():
    mapping, _ = app_main.map_ui_params({})
    assert mapping["noise_low_hz"] == 8000.0
    assert mapping["noise_high_hz"] == 20000.0
    assert mapping["noise_max_attenuation_db"] == 6.0
    assert mapping["sidechain_attack_ms"] == 5.0
    assert mapping["sidechain_release_ms"] == 150.0
    assert mapping["sidechain_max_duck_db"] == 6.0


def test_vocal_comp_and_air_default_on_with_mastering_mirror():
    mapping, _ = app_main.map_ui_params({})
    assert mapping["vocal_comp_amount"] == 0.4
    # 镜像母带 8kHz 高架衰减（−1.5dB → 人声 +1.5dB，只补不削，封顶 2dB）
    assert mapping["vocal_air_db"] == 1.5
    assert mapping["vocal_air_db"] <= 2.0


def test_explicit_vocal_params_win():
    mapping, _ = app_main.map_ui_params({"vocal_comp_amount": 0.0, "vocal_air_db": 0.5})
    assert mapping["vocal_comp_amount"] == 0.0
    assert mapping["vocal_air_db"] == 0.5


def test_guitar_fader_zero_is_neutral_and_four_stem():
    mapping, notices = app_main.map_ui_params({"guitar": 0.0}, lambda: True)
    assert "guitar_gain_db" not in mapping
    assert mapping["demucs_model"] == "htdemucs"
    assert notices == []


def test_guitar_fader_drives_bounded_controls_and_six_stem():
    mapping, notices = app_main.map_ui_params({"guitar": 1.0}, lambda: True)
    assert mapping["demucs_model"] == "htdemucs_6s"
    assert mapping["guitar_gain_db"] == 0.75
    assert mapping["guitar_mud_cut_db"] == 2.5
    assert mapping["guitar_presence_db"] == 4.0
    assert mapping["guitar_harsh_cut_db"] == 1.5
    assert mapping["guitar_width_db"] == 0.0
    assert notices == []
    half, _ = app_main.map_ui_params({"guitar": 0.5}, lambda: True)
    assert half["guitar_presence_db"] == 2.0 and half["guitar_gain_db"] == 0.0


def test_guitar_without_weights_falls_back_with_explicit_notice():
    mapping, notices = app_main.map_ui_params({"guitar": 0.5}, lambda: False)
    assert mapping["demucs_model"] == "htdemucs"
    assert notices and "吉他" in notices[0]


def test_explicit_guitar_stem_params_win():
    mapping, _ = app_main.map_ui_params(
        {"guitar": 1.0, "guitar_gain_db": -1.0}, lambda: True)
    assert mapping["guitar_gain_db"] == -1.0
    assert mapping["guitar_presence_db"] == 4.0   # 其余分项仍由推子驱动


def test_auto_clarity_always_on_for_gui():
    """发闷必然有害：GUI 映射恒开自动清晰（有界 ±2dB、置信度门控）。
    显式隐藏参数 False 仍可关闭；CLI 默认行为不受影响（后端默认仍为关）。"""
    mapping, _ = app_main.map_ui_params({})
    assert mapping["bass_auto_clarity"] is True
    explicit, _ = app_main.map_ui_params({"bass_auto_clarity": False})
    assert explicit["bass_auto_clarity"] is False
    import studio_backend as backend
    import inspect
    default_off = inspect.signature(backend.run_pipeline).parameters["bass_auto_clarity"].default
    assert default_off is False
