"""面板失效态合同（2026-10-20）：旁路 / 缺参考 → 统一变淡且不可调。

- 旁路面板：rack-body 加 .is-disabled + inert，内部全部 slider/button 标记
  aria-disabled（button 同时 disabled），头部旁路开关不进入失效区。
- 风格强度以参考音频为前提：无参考时整行 .is-disabled，推子 inert。
- 母带字段顺序：参考音频 / 风格强度 / EQ 风格 / 响度。
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")


def test_soren_field_order_swaps_loudness_and_blend():
    block = HTML[HTML.index('data-rack="soren"'):HTML.index("<!-- ═══ 阶段流水线")]
    labels = re.findall(r'class="(?:field-label|fader-label)"[^>]*>([^<]+)<', block)
    # 压缩范围收进「密度」背面原位翻转卡，母带恢复干净两行布局
    assert labels == ["参考音频", "风格强度", "EQ 风格", "密度", "响度"]
    assert 'id="btn-density-flip"' in block
    assert 'id="density-scope-face"' in block
    assert 'class="seg density-scope"' in block
    for scope in ("global", "low", "mid", "high"):
        assert f'data-scope="{scope}"' in block
    painter = APP[APP.index("function paintParamAvailability()"):APP.index("/* ─── 自定义下拉组件")]
    assert "if (scopeOff && state.densityFlipped) setDensityFlipped(false, false);" in painter
    assert "flipBtn.disabled = scopeOff;" in painter


def test_bypassed_rack_disables_controls_but_keeps_head_switch():
    painter = APP[APP.index("function paintParamAvailability()"):APP.index("/* ─── 自定义下拉组件")]
    assert 'body.classList.toggle("is-disabled", off);' in painter
    assert "body.inert = off;" in painter
    assert "el.disabled = off;" in painter
    # 失效态只作用于 rack-body；开关在 rack-head，保持可点
    assert 'body.inert' in painter and "rack-head" not in painter


def test_missing_reference_disables_style_blend():
    painter = APP[APP.index("function paintParamAvailability()"):APP.index("/* ─── 自定义下拉组件")]
    assert 'const noRef = !state.reference;' in painter
    assert 'blendField.classList.toggle("is-disabled", noRef && panelOn.soren);' in painter
    assert '$("fader-blend").inert = noRef;' in painter
    assert "选择参考音频后可用" in painter
    # 选择 / 拖入参考后立即重绘可用性
    assert APP.count("paintParamAvailability();") >= 4


def test_unified_disabled_visual_and_input_guards():
    # 一个 .is-disabled 规则同时覆盖旁路面板与依赖未满足的字段
    assert ".is-disabled {" in CSS and "opacity: 0.45;" in CSS
    assert '.is-disabled [aria-disabled="true"] { opacity: 1; }' in CSS
    assert '.rack[data-bypassed="1"] .rack-body' not in CSS
    # 自定义 slider 在指针/滚轮/双击/键盘路径上都先看 aria-disabled
    fader = APP[APP.index("function bindFader("):APP.index("/* ─── 声场宽度")]
    meter = APP[APP.index("function bindWidthMeter("):APP.index("const getQuality")]
    guard = re.compile(r"""getAttribute\(['"]aria-disabled['"]\)\s*===\s*['"]true['"]""")
    for block in (fader, meter):
        assert len(guard.findall(block)) >= 4
