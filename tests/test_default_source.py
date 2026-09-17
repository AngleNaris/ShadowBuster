"""处理默认值单一来源合同（开发规格 v2 §5.2/§12.1，P0-03；ENG-01）。

GUI（main.collect_pipeline_kwargs）、CLI（processing_cli）与后端
（studio_backend.run_pipeline / stage_reshape）不传参时必须解析出同一套
产品默认。此前三层各自维护：run_pipeline 的 space_wet/space_denoise 默认
0.0 与 UI/CLI 的 0.6/0.2 不一致（靠 GUI 恒传参掩盖）、stage_reshape 的
wet=1.0 是第三种取值——不传全参的任何调用方都会拿到与产品默认不同的处理。
"""
import argparse
import inspect
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import processing_cli  # noqa: E402
import studio_backend as backend  # noqa: E402

D = backend.DEFAULTS

# GUI 前端 index.html 的旋钮 data-default（HTML 值 → 后端键）。
HTML_DEFAULTS = {
    "knob-quality": ("quality", D["quality"]),
    "knob-guidance": ("guidance", D["guidance"]),
    "knob-vocal": ("vocal", D["vocal_gain_db"]),
    "knob-sub": ("sub", D["sub_db"]),
    "knob-punch": ("punch", D["punch_db"]),
    "knob-trans": ("trans", D["trans"]),
    "knob-sat": ("sat", D["sat"]),
    "width-meter": ("space_width", D["space_width_db"]),
    "fader-space": ("space", D["space_wet"]),
    "fader-denoise": ("denoise", D["space_denoise"]),
    "fader-blend": ("style_blend", D["style_blend"]),
}


def _html_default(key):
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    i = html.find(f'id="{key}"')
    assert i > 0, f"{key} not found in index.html"
    j = html.find('data-default="', i)
    return html[j + len('data-default="'):html.find('"', j + len('data-default="'))]


def test_html_knob_defaults_match_single_source():
    for key, (param, expected) in HTML_DEFAULTS.items():
        assert float(_html_default(key)) == float(expected), \
            f"{key} HTML default != DEFAULTS[{param}]"


def test_run_pipeline_defaults_resolve_from_table():
    """run_pipeline 不传参时的有效默认 = DEFAULTS（用 inspect 解析签名填充）。"""
    sig = inspect.signature(backend.run_pipeline)
    bound = sig.bind(_DUMMY := "x.wav", "out")
    bound.apply_defaults()
    for name in D:
        assert bound.arguments.get(name) in (None, D[name]) or \
            bound.arguments.get(name) == D[name], name
    # None 语义：函数体内替换为 DEFAULTS；这里只验证签名默认不再是
    # 与 DEFAULTS 冲突的第三种取值（None 或表值二选一）。
    for name, value in bound.arguments.items():
        if name in D and value is not None:
            assert value == D[name], f"run_pipeline 默认 {name}={value} != DEFAULTS"


def test_stage_reshape_defaults_resolve_from_table():
    sig = inspect.signature(backend.stage_reshape)
    bound = sig.bind("in.wav", "stems", "out.wav")
    bound.apply_defaults()
    assert bound.arguments["wet"] in (None, D["space_wet"])
    assert bound.arguments["denoise"] in (None, D["space_denoise"])
    assert bound.arguments["width_db"] in (None, D["space_width_db"])
    # 实际解析行为：不传参 → 产品默认（此前 wet=1.0 是第三种取值）
    with patch.object(backend, "_run_stream") as run, \
            patch.object(backend, "validate_noise_options"):
        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as td:
            root = P(td)
            root.joinpath("drums.wav").write_bytes(b"x")
            root.joinpath("other.wav").write_bytes(b"x")
            backend.stage_reshape(root / "in.wav", root, root / "out.wav")
    cmd = [str(c) for c in run.call_args[0][0]]
    assert cmd[cmd.index("--wet") + 1] == str(D["space_wet"])
    assert cmd[cmd.index("--side-gain-db") + 1] == str(D["space_width_db"])


def test_cli_defaults_match_single_source(monkeypatch, tmp_path):
    """CLI 解析默认与 DEFAULTS 一致（-h 构建解析器不跑批处理）。

    style_mode 是已知的 CLI 差异：headless 历史默认 'off'（opt-in 风格），
    与 GUI/后端默认 'styled' 不同——行为保持不变，单独锚定。"""
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")
    captured = {}

    def fake_run_batch(inputs, output, **kwargs):
        captured.update(kwargs)
        return [(str(inputs[0]), "done.wav", None)]

    monkeypatch.setattr(backend, "run_batch", fake_run_batch)
    assert processing_cli.main(["-i", str(source), "-o", str(tmp_path / "o")]) == 0
    for name, expected in D.items():
        if name == "style_mode":
            assert captured[name] == "off"     # CLI 保守 opt-in（历史行为）
        else:
            assert captured[name] == expected, f"CLI 默认 {name} != DEFAULTS"


def test_gui_collected_defaults_match_single_source():
    """GUI 不传面板参数时的组装结果 = 产品默认（PySide6 缺失时跳过）。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    import main as app_main
    kwargs, notices = app_main.collect_pipeline_kwargs({})
    assert notices == []
    assert kwargs["sub_db"] == D["sub_db"]
    assert kwargs["sat"] == D["sat"]
    assert kwargs["punch_db"] == D["punch_db"]
    assert kwargs["trans"] == D["trans"]
    assert kwargs["space_wet"] == D["space_wet"]
    assert kwargs["space_denoise"] == D["space_denoise"]
    assert kwargs["space_width_db"] == D["space_width_db"]
    assert kwargs["vocal_gain_db"] == D["vocal_gain_db"]
    assert kwargs["quality"] == D["quality"]
    assert kwargs["guidance"] == D["guidance"]
    assert kwargs["genre"] == D["genre"]
    assert kwargs["loudness"] == D["loudness"]
    assert kwargs["eq_profile"] == D["eq_profile"]
    assert kwargs["style_mode"] == D["style_mode"]
    assert kwargs["style_blend"] == D["style_blend"]


def test_defaults_table_is_frozen_contract():
    """表内容 = 当前产品默认。低频四旋钮为两轮听音校准定版（2026-09-17，
    listening_pack/ROUND1_RESULTS.md + ROUND2_RESULTS.md，规格 §13）：
    sub 6/9 被判偏多 → 2；弹性档 rc1e（punch 3 / trans 0.4）在真实歌曲
    上最好；sat 0.2。改动这些默认值必须是有意识的 profile 决策并附听音
    记录——改这里会同时改 GUI/CLI/后端三层。"""
    assert D["sub_db"] == 2.0 and D["punch_db"] == 3.0
    assert D["trans"] == 0.4 and D["sat"] == 0.2
    assert D["space_wet"] == 0.6 and D["space_denoise"] == 0.2
    assert D["space_width_db"] == 6.0 and D["vocal_gain_db"] == 0.0
    assert D["guidance"] == 1.5 and D["quality"] == 1
    assert D["style_blend"] == 0.85 and D["style_mode"] == "styled"
    assert D["noise_mode"] == "other"       # CLI/后端保守默认；GUI 由映射层升级
