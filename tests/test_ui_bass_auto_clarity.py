"""低频自动清晰 opt-in 开关：UI 静态契约测试（不触碰 DSP/后端/CLI）。"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def bass_rack_html():
    m = re.search(r'data-rack="bass"(.*?)data-rack="space"', HTML, re.S)
    assert m, "bass rack section"
    return m.group(1)


def test_toggle_is_opt_in_with_default_off():
    rack = bass_rack_html()
    m = re.search(r'<button class="rack-bypass rack-bypass--hardware" id="opt-bass-clarity"[^>]*', rack)
    assert m, "opt-bass-clarity toggle present in bass rack"
    tag = m.group()
    assert 'role="switch"' in tag
    assert 'aria-checked="false"' in tag          # 默认关闭
    assert 'type="button"' in tag                 # 原生按钮：Enter/Space 可操作
    assert 'aria-labelledby="label-bass-clarity"' in tag
    assert '<span class="bp-track"><span class="bp-dot"></span></span>' in rack


def test_toggle_reuses_shared_switch_contract_and_alignment():
    rack = bass_rack_html()
    # 共享拨杆组件契约：与面板 bypass 开关同款 chrome，而非页面私有实现
    assert re.search(r'id="bp-bass-drums"[^>]*role="switch"', rack)
    # 列对齐：开关列复用 knob-wrap 节奏，标签/读数与旋钮列基线一致
    assert re.search(
        r'<span class="opt-slot">.*?id="opt-bass-clarity".*?'
        r'id="label-bass-clarity">自动清晰</span>.*?'
        r'id="val-bass-clarity">关</output>', rack, re.S)
    # 槽位与旋钮同高，保证五个控件列标签基线对齐
    assert ".opt-slot { height: 64px;" in CSS
    # 键盘可达性来自共享契约的 focus-visible
    assert ".rack-bypass:focus-visible" in CSS
    # 无遮罩劫持：特效层不拦截指针事件
    assert re.search(r"\.fx-canvas \{[^}]*pointer-events: none", CSS, re.S)


def test_no_global_switch_loop_captures_the_shared_class():
    # 开关行为只允许 id 作用域绑定：不存在按 .rack-bypass / role=switch
    # 的全局循环（否则会把新开关误当成 bypass 面板处理）。
    assert 'querySelectorAll(".rack-bypass")' not in APP
    assert "querySelectorAll('[role=\"switch\"]')" not in APP
    assert 'querySelectorAll("[role=switch]")' not in APP
    # BYPASS_CONTROLS 只覆盖四个头部 bp-* 开关；opt 开关不属于 bypass 列表
    keys = re.findall(r'^\s{4}"(bp-[\w-]+)":', APP, re.M)
    assert sorted(keys) == ["bp-bass-drums", "bp-lew", "bp-reshape", "bp-soren"]
    assert "opt-bass-clarity" not in APP.split("BYPASS_CONTROLS")[1].split("};")[0]
    # 任何代码都不得禁用/隐藏 opt 开关
    assert '$("opt-bass-clarity").disabled' not in APP
    assert 'id="opt-bass-clarity"[^>]*disabled' not in HTML
    assert 'id="opt-bass-clarity"[^>]*hidden' not in HTML


def test_binding_is_registered_exactly_once():
    # 单一绑定：多次 addEventListener 会在一次点击里翻转两次（点击无效）
    assert APP.count('$("opt-bass-clarity")') == 1
    opt_section = APP.split("低频自动清晰 opt-in 开关")[1].split("自定义下拉组件")[0]
    assert opt_section.count('btn.addEventListener("click"') == 1


def test_hardware_variant_matches_knob_material_and_is_scoped():
    # 命名变体只落在 opt 开关上；头部 bypass 保持原扁平拨杆（非全局改版）
    for head_id in ("bp-lew", "bp-bass-drums", "bp-reshape", "bp-soren"):
        m = re.search(rf'<button class="rack-bypass" id="{head_id}"', HTML)
        assert m, f"{head_id} keeps plain shared switch"
    css_h = CSS.split(".rack-bypass--hardware {", 1)[1]
    css_h = css_h.split("/* 旁路关闭", 1)[0]
    assert "--rocker-face-on:" in css_h and "inset 0 7px 5px" in css_h
    assert "repeating-linear-gradient" not in css_h
    assert "width: 48px; height: 64px" in css_h
    assert "width: 34px; height: 58px" in css_h
    assert 'html .rack-bypass--hardware[aria-checked] .bp-dot' in css_h
    assert "background: var(--rocker-face)" in css_h
    assert "background: none; box-shadow: none" in css_h
    assert '.rack-bypass--hardware[aria-checked="true"] .bp-dot::before' in css_h
    assert 'html[data-mode="light"] .rack-bypass--hardware[aria-checked] .bp-dot' in css_h
    assert 'prefers-reduced-motion: reduce' in css_h


def test_app_persists_and_sends_payload_default_false():
    assert 'loadValue("bass_auto_clarity", "0") === "1"' in APP   # 默认关
    assert 'saveValue("bass_auto_clarity", enabled ? "1" : "0")' in APP
    assert "return () => enabled;" in APP                          # getter 返回布尔
    assert "bass_auto_clarity: getBassClarity()" in APP            # 处理载荷
    # 状态不单靠颜色：读数列显示 开/关
    assert 'val.textContent = enabled ? "开" : "关"' in APP


def test_help_is_short_and_plain_language():
    help_copy = APP.split("const HELP_CONTENT = {", 1)[1].split("const helpDialog", 1)[0]
    assert "自动清晰（默认关）" in help_copy
    assert "不好听就关掉" in help_copy
    assert "不会改动 Sub 提升的数值" in help_copy
    assert "更响不等于更好听" in help_copy
    for jargon in ("low-shelf", "side", "Mid", "门控", "残差", "kHz", "噪声地板"):
        assert jargon not in help_copy
    for section in help_copy.split('title:')[1:]:
        assert len(re.sub(r'<[^>]+>', '', section)) < 700
        assert '<b>' in section



def test_bridge_forwards_bass_auto_clarity_to_backend():
    assert 'bass_auto_clarity = bool(params.get("bass_auto_clarity", False))' in MAIN
    assert "bass_auto_clarity=bass_auto_clarity," in MAIN
    # 仅桥接层透传；main.py 不得自行实现 DSP
    assert MAIN.count("bass_auto_clarity") == 4


def test_asset_versions_bumped_for_cache_busting():
    assert 'style.css?v=48' in HTML
    assert 'app.js?v=46' in HTML
