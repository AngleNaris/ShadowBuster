"""参数预设合同（2026-09-28 第二次修订）：采样器式 9 槽方按钮，均分占满整行。

交互：左键空槽 = 保存当前面板参数；左键已存槽 = 一键应用；右键已存槽 =
待确认（槽位显示 ✕）——再右键移除该预设（恢复空槽）、左键用当前参数覆盖；
4 秒未确认自动取消。卡片本体不可点（无手型光标），操作说明写在卡片 tip。
面板参数快照 = collectParams 的面板控件子集（含面板 bypass 开关）；不含文件
队列/输出目录/参考路径——路径依赖具体机器，进预设会在别处失效。
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")

# 面板控件键：与 collectParams 的面板项一一对应（reference/genre/style_mode
# 为派生或路径项，不入预设）。
PRESET_KEYS = ("quality", "guidance", "sub", "sat", "punch", "trans", "vocal",
               "guitar", "space", "denoise", "space_width", "style_blend",
               "loudness", "eq", "bypass")


def test_preset_pads_are_nine_equal_slots():
    files = HTML.split('class="files', 1)[1]
    assert 'id="output-card"' in files and 'id="preset-card"' in files
    assert HTML.index('id="output-card"') < HTML.index('id="preset-card"')
    pads = re.findall(r'class="preset-pad" data-slot="(\d)"', HTML)
    assert pads == [str(i) for i in range(1, 10)]     # 固定 9 槽
    assert re.search(r"\.preset-pads \{[^}]*repeat\(9", CSS, re.S)  # 均分整行
    assert "max-width" not in CSS.split(".preset-pad {")[1].split("}")[0]


def test_preset_keys_cover_panel_params():
    block = APP[APP.index("const PRESET_GETTERS = {"):APP.index("function readSlots")]
    for key in PRESET_KEYS:
        assert re.search(rf"\b{key}:", block), key


def test_preset_save_load_remove_overwrite_contract():
    assert '"sb_presets"' in APP and "version: 2, slots" in APP
    assert "PRESET_GETTERS" in APP and "PRESET_SETTERS" in APP
    # 应用走控件的程序化 setter（与手动调节同一渲染/持久化路径）
    assert APP.count("get.set = (v)") >= 4            # buildDropdown + 三个 bind 组件
    # 右键待确认（✕）：再右键 = 移除（delete slots[n] 恢复空槽）；左键 = 覆盖
    assert 'pad.addEventListener("contextmenu"' in APP
    assert '"✕"' in APP and "delete slots[n]" in APP and "PRESET_ARM_MS" in APP
    assert "armedSlot === n" in APP
    # 操作说明写在组件 tip（卡片 title）里，含移除与覆盖两种待确认动作
    card = HTML[HTML.index('id="preset-card"'):HTML.index('id="preset-pads"')]
    assert "title=" in card and "移除该预设" in card and "覆盖" in card


def test_preset_card_cursor_and_lock():
    # 卡片本体不可点：不出现手型光标误导
    assert "#preset-card { cursor: default; }" in CSS
    # 试听占用期间预设与质量档/参考一同锁定
    assert "$('preset-card').inert=locked;" in APP
    assert re.search(r"\.files \{[^}]*repeat\(2", CSS, re.S)
    # 拟物质感：空槽凹陷 + 已存点亮 + 待确认反色
    assert ".preset-pad.filled" in CSS and ".preset-pad.armed" in CSS
    assert "--inset-well" in CSS.split(".preset-pad {")[1].split("}")[0]
