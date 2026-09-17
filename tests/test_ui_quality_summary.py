"""输出质量展示契约（20260915 起：输出检查并入报告 TAB）。

锚定契约：
  1. 主界面不再有独立"输出检查"面板；质量数据以指标卡片 + 图表并入报告页；
  2. 顶部三 TAB：效果器（默认）/ 预览 / 报告，页面切换而非弹窗；
  3. 质量模块（载荷存储/取值函数）保留：不可判定值、防伪造判定等约束不变；
  4. 报告数据跨会话可得：从输出目录扫描质量报告文件；
  5. 预览为频谱图（非波形），播放器集成在频谱面板上。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")


def quality_module():
    start = APP.index("─── 输出质量摘要：")
    end = APP.index("─── 输出 / 参考音频 ───", start)
    return APP[start:end]


def report_pane_html():
    m = re.search(r'id="pane-report"(.*?)</section>', HTML, re.S)
    assert m, "report pane section"
    return m.group(1)


def test_bridge_restores_file_finished_and_guards_quality_signal():
    # 曾发生 qualitySummary 处理器错误替换 fileFinished.connect 开头的语法事故，
    # 这里锁死：fileFinished 处理器完整、qualitySummary 为守卫连接且不改状态。
    assert "api.fileFinished.connect((fi, ftotal, fname, succeeded, error) => {" in APP
    assert 'setFileStatus(fi, succeeded ? "done" : "fail");' in APP
    assert "state.fi = fi; state.ftotal = ftotal;" in APP
    assert "if (api.qualitySummary) api.qualitySummary.connect((raw) => onQualitySummary(raw));" in APP
    assert "function onQualitySummary(raw)" in APP


def test_quality_panel_removed_and_merged_into_report():
    assert 'id="quality-summary"' not in HTML
    assert "输出检查" not in HTML
    pane = report_pane_html()
    for anchor in ('id="rp-cards"', 'id="rp-bands"', 'id="rp-width"',
                   'id="rp-constraints"', 'id="rp-file-label"', 'id="rp-config"'):
        assert anchor in pane
    # 指标卡片用 DOM API 构建，数据值绝不经过 innerHTML
    module = APP[APP.index("/* ─── 报告：前后指标卡片"):]
    assert "card.appendChild(title);" in module and "card.appendChild(vals);" in module
    assert ".mc-vals" in CSS


def test_three_tabs_with_effector_default():
    assert 'id="tab-process"' in HTML and ">效果器<" in HTML
    assert 'id="tab-preview"' in HTML and 'id="tab-report"' in HTML
    assert re.search(r'id="tab-process"[^>]*aria-pressed="true"', HTML)
    assert re.search(r'id="tab-preview"[^>]*aria-pressed="false"', HTML)
    # 页面切换（非弹窗）：pane 用 hidden 切换，无 modal/backdrop 结构
    assert 'id="pane-preview" hidden' in HTML and 'id="pane-report" hidden' in HTML
    assert "files-modal" not in HTML and "files-backdrop" not in HTML


def test_undecidable_values_never_render_null_or_nan():
    module = quality_module()
    # 防御式数值归一：只接受有限数值/数字字符串
    assert "function qualityNumber(value)" in module
    assert "Number.isFinite" in module
    # 输出面永不出现字面 null/NaN 文案
    for bad in ('"null"', '"NaN"', '"undefined"'):
        assert bad not in module


def test_no_false_compliance_and_no_mastering_claim():
    module = quality_module()
    # 达标/未达标只能来自后端 overall 映射，禁止前端自造判定
    assert module.count('"达标"') == 1 and module.count('"未达标"') == 1
    for fabricated in ('target_met', '全部达标', '母带级'):
        assert fabricated not in module


def test_new_batch_clears_previous_reports():
    start = APP.index("async function startProcess")
    end = APP.index("/* ─── 参数说明浮层", start)
    assert "clearQualityReports();   // 新批次：清空上一批的逐文件质量报告" in APP[start:end]
    module = quality_module()
    assert "function clearQualityReports()" in module
    assert "qualityReports = [];" in module and "qualitySelectedKey = null;" in module


def test_report_loads_from_disk_scan():
    """报告数据跨会话可得：从输出目录扫描质量报告（reportsScan/reportLoad）。"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def reportsScan" in main_src
    assert "def reportLoad" in main_src
    assert "function selectedReport()" in APP
    assert "api.reportsScan(state.output)" in APP
    assert "api.reportLoad(hit.path)" in APP


def test_selection_follows_queue_clicks():
    """预览/报告不再有独立文件选择控件：跟随待处理列表的点击选中。"""
    assert 'id="pv-file"' not in HTML and 'id="rp-file"' not in HTML
    assert "state.selectedFile = f;" in APP
    assert 'if (ev.target.closest(".fi-x")) return;' in APP
    assert "selectedFile: null," in APP
    assert "state.selectedFile = additions[additions.length - 1];" in APP


def test_preview_uses_spectrogram_with_integrated_player():
    assert 'id="pv-spec-src"' in HTML and 'id="pv-spec-out"' in HTML
    for stale in ("drawWave", 'id="pv-wave"', 'id="pv-render"'):
        assert stale not in HTML and stale not in APP
    module = APP[APP.index("/* ─── 预览：频谱选段"):]
    assert "createImageData" in module and "drawImage" in module   # 频谱成像
    # 播放器集成在频谱面板上：单播放键 + 轨道切换 + 空格
    assert 'id="pv-play"' in HTML and 'id="pv-lane-src"' in HTML and 'id="pv-lane-out"' in HTML
    assert "function pvPlayPause()" in module and "function pvSelectTrack(" in module
    assert 'e.code !== "Space"' in module
    assert "previewProgress = Signal(float)" in (ROOT / "main.py").read_text(encoding="utf-8")
    # dock 按钮随视图切换：预览页 PREVIEW，报告页隐藏；独立渲染按钮不再存在
    assert "processLabel.textContent = pv.busy ? \"渲染中…\" : \"PREVIEW\";" in APP
    assert "dock.hidden = which === \"report\";" in APP
    # 大段说明移入问号帮助
    assert 'data-help="preview"' in HTML and 'data-help="report"' in HTML
    for sub in ("pane-sub", "快速渲染试听（与正式成品同一套处理）"):
        assert sub not in HTML
    assert "preview: {" in APP and "report: {" in APP


def test_no_emoji_icons_and_no_new_animation():
    pane = report_pane_html()
    assert re.search(r"[🀀-🫿☀-➿]", pane) is None
    assert "@keyframes" not in pane and "animation:" not in pane


def test_app_js_syntax_via_node():
    """旧输出检查面板的行为套件随面板退役（quality_summary_state.cjs 已删）；
    保留 node 语法门禁：任何前端改动至少要能通过解析。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    check = subprocess.run([node, "--check", "ui/app.js"], cwd=ROOT,
                           capture_output=True, text=True)
    assert check.returncode == 0, f"node --check failed: {check.stderr}"


def test_main_emits_full_quality_payload_for_nested_lookups():
    """质量摘要信号必须携带完整报告：output.metrics / mastering 嵌套结构是
    界面取数函数（qualityMetric / qualityMastering）的数据来源；把 output
    覆盖成字符串路径曾导致采样峰值/削波/目标响度全部"无法判定"。"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "payload = {**report, **summary," in main_src
    assert 'summary["output"] = str(out)' not in main_src
    assert 'self.qualitySummary.emit(_json.dumps(payload' in main_src
    # report["input"] 是含 metrics 的对象：绝不能被字符串路径覆盖
    # （曾导致报告页"原始"值全部无法判定、频段对比图为空）
    assert '"input_path": str(src)' in main_src
    assert '"input": str(src)' not in main_src
    # 报告主图：优先 1/3 倍频程 compare，旧报告回退粗频段
    assert "const cmp = p.compare || null;" in APP
    assert "p.compare || null" in APP or "cmp.input_db" in APP
