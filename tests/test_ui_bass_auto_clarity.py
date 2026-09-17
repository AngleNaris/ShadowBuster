# -*- coding: utf-8 -*-
"""低频自动清晰：恒开契约（20260915 起移除 UI 开关）。

发闷必然有害，且该功能有界（±2dB 封顶）且带置信度门控（不闷不动、
不提亮已明亮音色、弱谐波不提噪声）——UI 不再提供开关，GUI 映射恒开；
CLI 保持 opt-in（后端默认关），行为差异记录在 docs/CLI.md。
"""
from pathlib import Path
import inspect
import re

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def bass_rack_html():
    m = re.search(r'data-rack="bass"(.*?)data-rack="space"', HTML, re.S)
    assert m, "bass rack section"
    return m.group(1)


def test_toggle_removed_from_ui():
    rack = bass_rack_html()
    assert "opt-bass-clarity" not in rack
    assert "opt-bass-clarity" not in APP
    assert "getBassClarity" not in APP
    assert "bass_auto_clarity" not in APP   # GUI 不发送该键，恒开由映射层决定


def test_mapping_forces_clarity_on_with_explicit_override():
    m = re.search(r"# 自动清晰恒开[\s\S]*?bass_auto_clarity = True if "
                  r"bass_auto_clarity is None else bool\(bass_auto_clarity\)", MAIN)
    assert m, "GUI 映射恒开（显式隐藏参数可覆盖）"
    assert '"bass_auto_clarity": bass_auto_clarity' in MAIN


def test_cli_default_stays_off():
    import studio_backend as backend
    default = inspect.signature(backend.run_pipeline).parameters["bass_auto_clarity"].default
    assert default is False   # CLI 契约不变：opt-in
    assert "--bass-auto-clarity" in (ROOT / "processing_cli.py").read_text(encoding="utf-8")


def test_help_documents_always_on_with_bounds():
    assert "自动清晰" in APP
    assert "默认始终开启" in APP
    assert "±2dB" in APP        # 有界声明随恒开一并呈现
