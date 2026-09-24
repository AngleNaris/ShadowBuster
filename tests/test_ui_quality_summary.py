"""视图/预览/报告 UI 契约（20260918 起：播放器与报告全面改版）。

锚定契约：
  1. 顶部三 TAB 为连体段控（共用外框 + 滑动指示块 tab-glider），页面切换而非弹窗；
  2. 预览播放器：上下频谱同缩放（同一时间轴），选段外压暗，选段顶部拖饼整体移动，
     点击任一频谱直接切轨并定位；无参数/指标文字列表；
  3. 预览渲染走后端缓存（同片段同参数直接命中），已完成文件可切换输出版本对比；
  4. 报告：无冗余文字面板，用处理摘要 KPI / 频率响应曲线（含 Δ 差异曲线）/ 响度哑铃行 /
     3 频段声场钻石图 / 原始·处理后·变化(Δ) 频谱图做前后直观对比，支持多版本勾选；
     每张图的含义只写在右上角图例，图下不留说明；量程按被比较的取值动态定标；
  5. 成品命名：重复处理自动 _v2/_v3，绝不静默覆盖；
  6. WebView 存储与预览产物集中在缓存目录。
"""
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
BACKEND = (ROOT / "studio_backend.py").read_text(encoding="utf-8")
METRICS = (ROOT / "audio_metrics.py").read_text(encoding="utf-8")


def report_pane_html():
    m = re.search(r'id="pane-report"(.*?)</section>', HTML, re.S)
    assert m, "report pane section"
    return m.group(1)


def preview_pane_html():
    m = re.search(r'id="pane-preview"(.*?)</section>', HTML, re.S)
    assert m, "preview pane section"
    return m.group(1)


def test_bridge_wiring_and_guards():
    # fileFinished 处理器完整性（曾因信号连接改写出过语法事故，锁死）。
    assert "api.fileFinished.connect((fi, ftotal, fname, succeeded, error) => {" in APP
    assert 'setFileStatus(fi, succeeded ? "done" : "fail");' in APP
    assert "state.fi = fi; state.ftotal = ftotal;" in APP
    # 频谱/预览信号守卫连接
    assert "if (api.previewOutputPeaks) api.previewOutputPeaks.connect((raw) => onPreviewOutputPeaks(raw));" in APP
    assert "api.audioInfoReady.connect(onAudioInfo)" in APP


def test_joined_tab_segment_with_glider():
    assert 'id="tab-process"' in HTML and ">效果器<" in HTML
    assert 'id="tab-preview"' not in HTML and 'id="tab-report"' in HTML
    assert re.search(r'id="tab-process"[^>]*aria-pressed="true"', HTML)
    assert 'id="pane-preview"' not in HTML and 'id="pane-report" hidden' in HTML
    # 连体段控：共用外框 + 滑动指示块，切换动画走 WAAPI
    assert 'class="topbar-nav"' in HTML and 'id="tab-glider"' in HTML
    assert ".tab-glider" in CSS
    assert "function moveTabGlider(animate)" in APP
    assert "tabGlider.animate(" in APP
    # 分隔线压在滑动块的描边上：谁盖住谁取决于 DPI 取整，会让块少一条竖边。
    # 选中项自己的分隔线改用同色，右侧那枚让位为透明。
    assert ".tab-btn[aria-pressed=\"true\"] {\n  color: #fff;" in CSS
    assert "border-left-color: var(--c-accent-hi);" in CSS
    assert ".tab-btn[aria-pressed=\"true\"] + .tab-btn { border-left-color: transparent; }" in CSS
    # 旧式独立三按钮/徽标样式退役
    assert ".tab-badge" not in CSS
    # 面板入场动画 + 页面切换（非弹窗）
    assert "pane.animate(" in APP
    assert "files-modal" not in HTML and "files-backdrop" not in HTML


def test_scrollbar_contract():
    # 标准属性一旦声明，Chromium 就改画系统滚动条并整套忽略 ::-webkit-scrollbar：
    # 上下箭头与圆角正是这么回来的，所以只允许伪元素一套。
    assert "scrollbar-width:" not in CSS and "scrollbar-color:" not in CSS
    assert "::-webkit-scrollbar { width: 8px; height: 8px; }" in CSS
    assert "::-webkit-scrollbar-button { display: none; width: 0; height: 0; }" in CSS
    # 轨道要读得出凹槽，滑条与轨道都不许圆角
    assert "::-webkit-scrollbar-track { background: var(--c-bg); border-radius: 0; }" in CSS
    assert "background: var(--c-border); border-radius: 0;" in CSS
    assert "::-webkit-scrollbar-corner { background: var(--c-bg); }" in CSS
    # 全局一套规则覆盖所有滚动面，不再逐面板重复（重复处曾各自换了种颜色）
    assert ".queue-list::-webkit-scrollbar" not in CSS
    assert ".dd-panel::-webkit-scrollbar" not in CSS


def test_player_name_marquee_and_loop_mask():
    # 选中反馈只落在整行底色上：名称按钮再自描 1px 就成了双层框
    assert '.player-file-menu .fi-name[aria-pressed="true"] { box-shadow: none; }' in CSS
    # 整框一个按钮：右侧独立的刻度按钮删掉，两翼刻度带直接做进按钮里
    assert '<button class="player-file-picker" id="player-file-picker" type="button" popovertarget="player-file-menu"' in HTML
    assert HTML.count('<span class="pf-ticks" aria-hidden="true"><i></i></span>') == 2
    assert 'player-file-trigger' not in HTML and 'player-file-trigger' not in CSS and 'player-file-trigger' not in APP
    # 刻度带：固定 9px（宽度供带子定位复用）、两端淡出；内层用一个周期的重复渐变滚动
    assert "flex: 0 0 auto; align-self: stretch; position: relative; width: var(--pf-tick-w); overflow: hidden;" in CSS
    assert "--pf-tick-w: 9px;" in CSS
    assert "repeating-linear-gradient(to bottom, var(--c-border) 0 1px, transparent 1px 10px)" in CSS
    assert "mask-image: linear-gradient(to bottom, transparent 0, #000 8px, #000 calc(100% - 8px), transparent 100%);" in CSS
    assert "function rollTicks(rollDir)" in APP
    assert "document.querySelectorAll(\".pf-ticks i\")" in APP
    assert "translateY(${-10 * rollDir}px)" in APP
    assert "mqStop(host);\n    rollTicks(rollDir);" in APP
    # 换曲是“带子滚动”而不是淡入淡出：新旧两行贴成一条带子平移一个框高
    assert "left: calc(var(--pf-tick-w) + var(--s-1)); right: calc(var(--pf-tick-w) + var(--s-1));" in CSS
    assert ".mq-roll > span { display: flex; align-items: center; height: 100%; white-space: nowrap; overflow: hidden; }" in CSS
    assert 'roll.className = "mq-roll"' in APP
    assert "const from = rollDir > 0 ? 0 : -100, to = rollDir > 0 ? -100 : 0;" in APP
    assert "if (rollDir < 0) roll.prepend(roll.lastElementChild);" in APP
    # 位移里不许再带 opacity——透明度一变就回成“淡入淡出”
    assert "opacity: 0" not in APP[APP.index("function setPlayerFileName"):APP.index("function setPlayerFileName") + 1400]
    # 内层承载位移：外层裁切、内层出省略号，滚动中改 clip 免得省略号飘到窗口中间
    assert "text-overflow: ellipsis; white-space: nowrap;" in CSS
    assert ".mq.running { text-overflow: clip; }" in CSS
    assert '<span class="mq">${escapeHtml(name)}</span>' in APP
    assert "function mqStart(" in APP and "function mqStop(" in APP
    assert "function bindNameMarquee(" in APP and "bindNameMarquee($('player-file-name'))" in APP
    assert "bindNameMarquee(item.querySelector(\".fi-name\"));" in APP
    # 两端各停一拍：名字的头和尾都要在屏幕中央定住，否则只是各闪一下
    assert '{ transform: "translateX(0)", offset: 0.18 }' in APP
    assert '{ transform: `translateX(${-span}px)`, offset: 0.68 }' in APP
    # 悬停态自己记：滚轮不产生 pointer 事件，:hover 在 WebEngine 里可能滞后
    assert 'el.dataset.hovering = "1"' in APP and "delete el.dataset.hovering" in APP
    assert "host.dataset.hovering" in APP
    # 旧的原生 tooltip 会顶着窗口边被裁掉，去掉；说明文字改指整框按钮
    assert 'title="滚轮切换文件"' not in HTML and "$('player-file-name').title=" not in APP
    assert "点击文件名框展开文件列表" in APP and "点击刻度按钮展开文件列表" not in APP
    # 滚轮换曲：文件名上下滚动交接，且只有滚轮那条路径带方向
    assert "function setPlayerFileName(text, rollDir)" in APP
    assert "renderFileList(e.deltaY>0?1:-1)" in APP
    assert "$('player-file-name').textContent=" not in APP
    # 循环带遮罩：78% 会把整条波形压成黑块，只保留一层可分辨的轻纱
    assert "color-mix(in srgb, var(--c-bg) 78%, transparent)" not in CSS
    assert "color-mix(in srgb, var(--c-bg) 38%, transparent)" in CSS


def test_integrated_player_contract():
    for control in ('player-sources', 'draft-play', 'draft-seek', 'player-a', 'player-b', 'player-volume'):
        assert f'id="{control}"' in HTML
    assert 'id="draft-title"' not in HTML
    assert 'id="pv-spec-src"' not in HTML
    assert "api.audioInfo(file)" in APP
    assert 'data-cache="empty"' in HTML
    assert "specCache.get(normPath(" in APP  # Reports still have spectral analysis.


def test_preview_version_switching():
    assert "async function refreshPreviewOutputs()" in APP
    assert "async function chooseSource(" in APP
    assert "api.listOutputs" in APP
    assert "api.draftPrepare(state.selectedFile,0,transport.duration,collectParams(),draft.id)" in APP


def test_draft_failure_flashes_timeline_without_message():
    # 取消准备 / 准备失败都不留文字提示：清掉 #draft-status，只让时间轴红线闪 1 秒
    assert "draft.busy=false;draft.ready=false;playerError('');flashCacheError();" in APP
    assert "cacheState('error','缓存准备失败');playerError(p.error);" not in APP
    assert "function flashCacheError(){" in APP
    assert "setTimeout(()=>{if(token===cacheStateToken)cacheState('empty','缓存尚未准备');},1000);" in APP
    # 闪回是「一次性读数」：任何新的状态更新都要作废尚未到期的恢复
    assert "cacheStateToken++;   // 任何一次状态更新都作废尚未到期的「错误闪回」" in APP
    assert "  cacheStateToken++;" in APP.split("function cacheState(")[1].split("}")[0]


def test_cache_progress_lives_on_the_bottom_line_with_sweep():
    # 缓存进度只长在轨道底部那 3px 线上（整高淡色底已收回）：::before 是常驻暗槽，
    # ::after 是已准备的长度，260ms 宽度过渡让每一批进度是「推」过去而不是「跳」过去。
    assert "--cache-progress: 0%; container-type: inline-size;" in CSS
    assert ".player-track { position: relative; height: var(--player-control); margin-top: 3px; background: var(--c-bg); }" in CSS
    assert ".player-track::before { content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: var(--c-border); pointer-events: none; }" in CSS
    assert ".player-track::after { content: ''; position: absolute; left: 0; bottom: 0; height: 3px; width: 0; background: var(--c-accent-hi); pointer-events: none; transition: width 260ms var(--ease), background-color 260ms var(--ease); }" in CSS
    assert '.player[data-cache="preparing"] .player-track::after { width: var(--cache-progress); }' in CSS
    assert '.player[data-cache="ready"] .player-track::after { width: 100%; }' in CSS
    assert '.player[data-cache="stale"] .player-track::after, .player[data-cache="error"] .player-track::after { width: 100%; background: var(--c-err); }' in CSS
    # 整高铺底不许回潮：时间轴上没有别的进度底
    assert ".player-timeline::before" not in CSS
    assert "$('player').style.setProperty('--cache-progress'" in APP
    assert "$('player-track').style.setProperty('--cache-progress'" not in APP
    # 第一批进度到达前（fraction 仍为 0）扫光；扫光只占进度线所在的那一行，进度一来就让位
    assert ('.player[data-cache-sweep="true"] .player-timeline::after { content: \'\'; position: absolute; '
            "left: 0; right: 0; bottom: 0; height: 3px; z-index: 1;") in CSS
    assert "@keyframes player-cache-sweep" in CSS
    assert "animation: player-cache-sweep 1.5s linear infinite;" in CSS
    assert "$('player').dataset.cacheSweep=String(status==='preparing' && cacheFraction<=0);" in APP
    assert '@media (prefers-reduced-motion: reduce) { .player-track::after { transition: none; } .player[data-cache-sweep="true"] .player-timeline::after { display: none; } }' in CSS
    # 取消时保留已到达的进度：否则进度条当着用户的面归零、还重新开始扫光
    assert "cacheState('preparing','正在取消准备',cacheFraction);" in APP


def test_playhead_spans_timeline_and_triangle_is_concentric():
    # 指针贯穿整条时间轴：向上要盖到范围选择带顶（3px 轨道外边距 + 20px 带高 + 4px 时间轴内边距），
    # 不在带上留一段空白；z-index 压过带底那条 1px 分界线，竖线穿过去不许被割断。
    assert ("#player-playhead { position: absolute; top: calc(-3px - 20px - var(--s-1)); bottom: -2px; "
            "width: 1px; background: var(--c-text); pointer-events: none; z-index: 4; }") in CSS
    # 三角必须与 1px 竖线同轴。border 画不出这种三角：Blink 打底把 border 宽度取整，
    # 三角恒为偶数宽，偶数宽盒子对不齐 L+0.5 的中轴（实测 left:-4px 与 left:50% 都差 0.5px）。
    # 9px 奇数宽 + clip-path 才行，顶点 50% 落在竖线中轴上。
    assert ("#player-playhead::before { content: ''; position: absolute; top: 0; left: -4px; "
            "width: 9px; height: 6px; background: var(--c-text); "
            "clip-path: polygon(50% 100%, 0 0, 100% 0); }") in CSS
    assert "border-top: 6px solid var(--c-text)" not in CSS


def test_keyboard_focus_draws_no_outline_anywhere():
    # 键盘操作不在任何元素上留外框：文件末尾一条 :root :focus-visible 关掉全站描边
    # （与上面各条 .class:focus-visible 同权重，靠源序取胜），播放器里那条 box-shadow 焦点环也撤掉。
    assert CSS.rstrip().endswith(":root :focus-visible { outline: none; }")
    assert ".player :focus-visible" not in CSS
    # 两处非焦点描边必须留着：停止键的红色悬停描边、激活频段的画布描边
    assert "outline: 1.5px solid var(--c-err);" in CSS
    assert ".spec-lane.active canvas { outline: 1px solid var(--c-accent-hi); }" in CSS


def test_source_pills_and_play_button_states():
    # 播放键平时是普通按键，只有 .player-play[data-playing="true"] 才点亮；
    # 全局 aria-pressed 高亮不许再无条件罩在播放键上
    assert ':root .player-play,' not in CSS and ':root .player-play {' not in CSS
    assert '.player-play[data-playing="true"] { color: #fff; background: var(--c-accent); border-color: var(--c-accent-hi);' in CSS
    assert '.player-play[data-playing="true"]:hover:not(:disabled) { background: color-mix(in srgb, var(--c-accent) 82%, #fff); }' in CSS
    assert ".player-play { width: var(--player-control); color: #fff" not in CSS
    # 源胶囊：邻接不再让出左边描边，而是各留整圈描边重叠 1px；
    # 悬停/选中那枚抬到邻居之上，四边描边才完整（左侧描边消失的根因）
    assert ".player-source + .player-source { border-left: 0; }" not in CSS
    assert ".player-source + .player-source { margin-left: -1px; }" in CSS
    assert '.player-source:hover:not(:disabled), .player-source[aria-pressed="true"], .player-source.is-selected { position: relative; z-index: 1; }' in CSS
    assert ':root .player-source[aria-pressed="true"], :root .player-source.is-selected { color: var(--c-text); border-color: var(--c-accent-hi);' in CSS
    # 版本菜单箭头改用声明式 popover invoker：重复点击由浏览器收放，
    # JS togglePopover 会被 light-dismiss 抢先关掉再重开，永远关不上
    assert "arrow.onclick=()=>versionMenu.togglePopover();" not in APP
    assert "arrow.setAttribute('popovertarget','player-version-menu');" in APP
    assert "arrow.onkeydown=e=>{if(e.key==='ArrowDown')" in APP


def test_report_contract():
    pane = report_pane_html()
    for anchor in ('id="rp-summary"',
                   'id="rp-spectrum"', 'id="rp-dyn"', 'id="rp-fans"',
                   'id="rp-spec"', 'id="rp-spec-tabs"',
                   # 全站一份色块图例，放在分析时间区域
                   'id="rp-legend"'):
        assert anchor in pane
    assert 'id="rp-chips"' not in pane
    # 冗余文字面板全部退役：文件行 / 指标卡 / 频段条 / 配置行 / 约束列表 /
    # 重复版本徽标 / 处理链行 / 英文副标题 / 缩放徽标 / 图下说明段
    for stale in ('id="rp-file-label"', 'id="rp-cards"', 'id="rp-bands"',
                  'id="rp-width"', 'id="rp-config"', 'id="rp-constraints"',
                  'id="rp-badge"', 'id="rp-chain"', 'id="rp-freq-zoom"',
                  'id="rp-spec-note"', "rp-hint", "rp-cap-en", "rp-zoom",
                  "rp-spec-note",
                  # 每卡各自的图例并入工具栏那一份
                  'id="rp-freq-legend"', 'id="rp-dyn-legend"',
                  'id="rp-stereo-legend"', 'id="rp-spec-legend"',
                  # 分组条形图与堆叠频谱 lanes 被哑铃行 / 3 频段钻石图 / Δ 频谱图取代
                  'id="rp-levels"', 'id="rp-stereo"', 'id="rp-specs"',
                  # 频谱右侧色标（渐变条 + 数字）整块移除：读的是图里的色块，不是色标
                  'id="rp-cbar"'):
        assert stale not in pane
    module = APP[APP.index("/* ── 报告画布 hover 提示"):]
    assert "function drawSpectrumChart(canvas, anim)" in module
    assert "function renderSummary()" in module
    assert "function renderDynamics()" in module
    assert "function renderStereoFans()" in module
    assert "function drawFieldDiamond(canvas" in module
    assert "function renderSpectral()" in module
    assert "function drawSpecBitmap(canvas, off, fHi, duration, note, anim)" in module
    assert "function renderReportLegend()" in module
    assert "function drawGroupedBars" not in module
    assert "function drawStereoChart" not in module
    assert "function renderReportSpecs" not in module
    assert "function setSpecNote" not in module
    assert "function reportCurves()" in APP
    # 曲线数据来自 compare（1/3 倍频程 input_db/output_db）
    assert "cmp.input_db" in APP and "cmp.output_db" in APP
    # 摘要/差异条按频段聚合 compare 能量，而非整曲聚合指标
    assert "function rpBandDelta(lo, hi)" in module
    # 分频段宽度来自后端新增的 band_widths（整曲 S/M 被低频主导，会误判变窄）
    assert "function rpBandWidth(m, key)" in module
    assert '"band_widths": _band_widths(data, sample_rate)' in METRICS
    # 总宽度 KPI 剔除低频：取中/高频各自 S/M 比（dB）等权平均。能量加权的
    # >250Hz 总宽只跟中频走，高频的实际拓宽会被吃掉
    assert "const smDb = (m, key)" in module and '["mid", "high"].map' in module
    assert "wide_gt250" not in module
    # 分频段 S/M 电平比（dB）是听感宽度的直接读数，宽度比 sideE/(midE+sideE)
    # 只是它的非线性压缩：同一件事 0.03→0.02 只有 −1.4 dB，写成 −23% 会把
    # 轻微收窄读成大幅收窄。优先读后端字段，旧成品缺字段时由宽度比反推
    assert "function rpBandSmDb(m, key)" in module
    assert "b.side_mid_db" in module and "10 * Math.log10(w / (1 - w))" in module
    # 配色只剩三个语义色：原始=中性色、处理后/衰减=主题色、提升=主题的对比色。
    # 不再直接取 --c-ok / --c-support——那对五种 accent 是同一组固定色，等于逐主题凑色。
    assert "function rpThemeColors()" in module
    assert 'cssVar("--c-ok"' not in module and 'cssVar("--c-support"' not in module
    assert "function hueRotate(rgb, deg)" in module and "function mixRgb(a, b, t)" in module
    assert "boost: sep(co), cut: cutHex" in module
    # 处理后与衰减本来就是同一件事（成品偏低就是衰减），同值；两个弱化槽位无人读，删
    assert "after: cutHex" in module
    assert "themeSoft" not in module and "contrastSoft" not in module
    # 写死的版本色轮一并删掉：报告里每条处理后曲线都按方向取色
    assert "versionColor" not in APP
    assert "DELTA_LUT = buildDeltaLut(rpThemeColors())" in module
    # 衰减改成饱和主题色后，Δ 色标必须有死区：0.4dB 以内的噪声底不落色，
    # 否则浅色模式（近白面板）整张频谱糊成淡红，「没变」与「微降」分不出
    assert "const DELTA_DEADBAND = 0.4;" in module
    assert "Math.abs(db) - DELTA_DEADBAND" in module
    # 推导色还要与所在表面对比度达标：色相旋转沿用主题明度，浅色模式下
    # indigo 的对比色 gold 在 #fafafa 上只有 2.1:1，Δ 上色会整片看不见
    assert "function ensureSep(c, face, ink, minCr)" in module
    assert "function contrastRatio(a, b)" in module
    assert 'ensureSep(c, bg, ink, 2.6)' not in module   # 参照面是面板，不是页面底色
    assert "ensureSep(c, panel, ink, 2.6)" in module
    assert 'cssVar("--c-panel"' in module
    # DOM 侧（KPI 读数）与画布同源：推导色回写 CSS 变量，否则主题一换读数仍是写死的绿/蓝
    assert 'setProperty("--rp-boost"' in module and 'setProperty("--rp-cut"' in module
    assert "var(--rp-boost)" in CSS and "var(--rp-cut)" in CSS
    assert ".rp-kpi.up .rp-kpi-arrow { color: var(--c-ok); }" not in CSS
    # 方向只由读数颜色表态：数字右侧不再挂 ▲/▼（正负号已经说清方向），
    # 图标是定位用的，跟标签同为中性色，不跟着数值变红变绿
    assert "rp-kpi-arrow" not in module and "rp-kpi-arrow" not in CSS
    assert ".rp-kpi.up .rp-kpi-num { color: var(--rp-boost); }" in CSS
    assert ".rp-kpi.down .rp-kpi-num { color: var(--rp-cut); }" in CSS
    assert ".rp-kpi-ic { width: 24px; height: 24px; flex: none; color: var(--c-text-dim); }" in CSS
    assert ".rp-kpi-num { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }" in CSS
    # 报告配色全来自 CSS 变量，切换主题后必须重绘
    assert 'document.addEventListener("sb-theme"' in module
    # 版本改单选：不再支持同时对比多版
    assert "const rp = { versions: [], sel: null, reports: new Map() };" in APP
    assert "rp.selected" not in APP
    # 曲线可缩放：滚轮/拖拽/双击复位；缩放一律经 setView 对称钳位
    # （旧 clampZoom 在定义域两端非对称压缩跨度，视野被单向拖走后缩不回去）
    assert "const rpZoom = { f0: 20, f1: 20000 };" in module
    assert 'canvas.addEventListener("wheel"' in module
    assert 'canvas.addEventListener("dblclick"' in module
    assert "const setView = (c0, c1, instant)" in module
    assert "clampZoom" not in module
    # 缩放锚点按当前视野反解（固定 20–20000 映射会让视野单向漂移）
    assert "Math.exp(a + (b - a) * frac)" in module
    # 视野变化有缓动，拖拽平移保持跟手
    assert "zoomAnim = requestAnimationFrame(step)" in module
    assert "Math.log(rpZoom.f1) - dLog, true)" in module
    # 缩放态不再挂徽标（视野变化本身可读，提示反而占位）
    # 视野外的频点记 null 而非钳到画布边缘（钳位会在两端画出假垂直线）
    assert "pts.push(null); return;" in module
    assert "if (!p || !q || (keep && !keep(i))) continue;" in module
    # 一律实线（原始不再画虚线）；处理后高出原始的段落再叠一遍对比色
    assert "ctx.setLineDash([5 * dpr, 4 * dpr])" not in module
    assert "strokeSegments(pts, (i) => (pts[i - 1][2] + pts[i][2]) / 2 > EPS_DB)" in module
    # 差异不再单独成图：直接填在原始与处理后两条曲线之间，按符号取提升/衰减色
    assert "const pts = [];" in module and "ctx.fillStyle = (d0 + d1) / 2 >= 0 ? pal.boost : pal.cut" in module
    assert "差异 Δ (dB)" not in module and "stripMid" not in module
    # 纵轴非线性：核心区按可见采样百分位划定，两端离群段按 1/8 倍率折进窄带，
    # 于是每格 dB 高度不相等，1~2dB 的差异读得出来
    assert "coreHi = at(0.96); coreLo = at(0.04);" in module
    assert "plotH / (1 + (upTail + dnTail) / (coreSpan * TAIL))" in module
    # 改了比例就必须看得见：分段边界画成较亮横线并标注压缩比
    assert 'if (dnPx > 0) edge(coreLo);' in module and '"1:8"' in module
    # 压缩段刻度按窄带可用高度反推档距，且不贴着分段边界
    assert "px > 14 * dpr" in module and "-36, -45" not in module
    # 哑铃行前后贴近时合并成一个居中读数，避免两个上标叠字
    assert 'class="rp-dyn-num both' in module
    # 行方向写进 class：处理后一侧（圆点/连线/读数）按升=对比色、降=主题色取色
    assert 'row.className = "rp-dyn-row " + dir' in module
    assert "Math.abs(dv) < halfLsb" in module
    assert ".rp-dyn-row.up .rp-dyn-dot.after" in CSS
    assert ".rp-dyn-dot.after { color: var(--rp-cut); background: currentColor" in CSS
    # 每行用该属性的常用总量程（跨文件可比），只有取值超出才外扩；
    # 不再跟着本行前后两值缩放
    assert "const rpNiceStep = (range, want)" in module
    assert "let lo = d.ref[0], hi = d.ref[1];" in module
    assert "Math.min(lo, Math.min(...vals) - margin)" in module
    assert "(hi - lo) * 0.5" not in module
    # 数值只标在轨道上，右侧不再重复一列
    assert "rp-dyn-nums" not in module and "rp-dyn-nums" not in CSS
    assert "grid-template-columns: 92px minmax(0, 1fr); align-items: center" in CSS
    # 副标题只留单位，不再用另一种说法重复行标题（响度 / 整体响度…）
    assert 'sub: "LUFS"' in module and 'sub: "dBTP"' in module
    assert "整体响度 LUFS" not in module and "相位相关 Correlation" not in module
    # 刻度与圆点同值同位时只画刻度线，不在轨道上下各印一次同一个数
    assert "const dup = marks0.some(" in module
    # 默认量程取成品母带的常见区间，典型值落在轨道中段
    assert "ref: [-20, -6]" in module and "ref: [0, 12]" in module and "ref: [6, 18]" in module
    # 层级：卡片主标题白，卡内小标题（KPI 名 / 动态行名 / 频段名）灰；KPI 数字按方向取色
    assert ".rp-cap-zh { font-size: 12.5px; font-weight: 700; letter-spacing: 0.5px; color: var(--c-text)" in CSS
    assert ".rp-kpi-label { font-size: 12px; font-weight: 600; color: var(--c-text-dim)" in CSS
    assert ".rp-dyn-label b { font-size: 11.5px; font-weight: 600; color: var(--c-text-dim)" in CSS
    assert ".rp-fan-hd b { font-size: 11px; font-weight: 600; color: var(--c-text-dim)" in CSS
    assert ".rp-kpi-num { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }" in CSS
    assert ".rp-kpi.down .rp-kpi-num" in CSS and ".rp-kpi.flat .rp-kpi-num" in CSS
    # 钻石图：角度域按本频段取值取档（画布内不再印那一排角度文字）
    assert "const RP_FAN_DOMAINS = [15, 20, 25, 30, 40, 45];" in module
    assert "function rpHalfAngle(wd)" in module
    assert "rp-fan-cap" not in module and "rp-fan-cap" not in CSS
    # 标准钻石比例：顶角—底角定高、L/R 落在半高（旧 UP/DN 参数把形状拉成上窄下长，
    # 顶角标签被画布上沿裁掉）
    assert "midY = top + boxH / 2" in module and "const UP = 0.52" not in module
    # 宽度尽量张满可用宽度：按 tan(域角)×高度定宽时 15° 一档只有二十来 px
    assert "let halfW = w / 2 - padX;" in module
    assert "Math.tan(Math.min(theta, dom)) / tanDom" in module
    # 钻石框保持正方形：高度跟不上宽度时让宽度收回来。宽窗口下按域角张满宽度
    # 会把图拉成扁宽一只（宽近高的两倍），两侧留白好过声场形状失真
    assert "if (availH < halfW * 2) halfW = availH / 2;" in module
    assert "Math.min(availH, halfW * 2)" in module and "halfW * 3" not in module
    # 画布底行（角度域 / 百分比 / 实测角）整体移除，变化数值改由卡片底部一行承担
    assert "角度域 ±" not in module and "实测 ${" not in module
    assert 'class="rp-fan-ft"' in module and ".rp-fan-ft { text-align: center" in CSS
    assert ".rp-fan.up .rp-fan-ft { color: var(--rp-boost); }" in CSS
    assert "footH" not in module
    # 底部读数是该频段 S/M 电平变化 dB，不是宽度比百分比（−1.4 dB 被印成 −23%
    # 正是「处理后声场反而收窄」的来源）。±0.5 dB 内读作持平=灰色，
    # 标题行的前后宽度比与钻石几何不变
    assert "const rpFanDbText = (d)" in module and "const RP_FAN_FLAT_DB = 0.5;" in module
    assert "const rpFanDb = (di, dok)" in module
    assert "rpFanText" not in module
    assert "rpFanPctText" in module                        # 旧成品无 dB 字段时的兜底
    # 图下不再有文本行：前后读数移到频段标题行右侧
    assert "rp-fan-val" not in module and "rp-fan-val" not in CSS
    assert 'class="rp-fan-num"' in module and ".rp-fan-num { margin-left: auto" in CSS
    # 同一行两张卡等高且内容铺满：图区随卡片伸展，频谱与声场最小高度一致
    assert ".rp-fig {\n  position: relative" in CSS and "display: flex; flex-direction: column" in CSS
    # 第二排限高：窗口拉高时钻石图不能变成针、频谱不能变成超长条。
    # 画布用 flex-basis 0 参与分配——height:auto 会按 canvas 属性的固有宽高比定高，
    # 频谱画布因此锁在 340 上下并把整排顶开。240 = 钻石正方形框 + 标题行 + 底部变化值。
    assert ".rp-fig--stereo, .rp-fig--spec { max-height: 240px; }" in CSS
    assert ".rp-fan canvas { width: 100%; flex: 1 1 0; height: auto; min-height: 112px" in CSS
    assert "min-height: 112px; border: var(--border)" in CSS
    # 色标整块退役（渐变条、数字刻度、竖排单位），频谱图自己承担读数
    assert "rp-cbar" not in module and "renderCbar" not in module
    assert ".rp-cbar" not in CSS
    # 四张卡各自的色块图例合并为工具栏右侧一份
    assert 'fillLegend("rp-legend", [["原始", t.before], ["处理后 / 衰减", t.after], ["提升", t.boost]])' in module
    assert "renderSpecLegend" not in module and "renderInlineLegends" not in module
    # 图例不再承载状态提示，频谱画布自己说明为什么是空的
    assert "前后频谱网格不一致，无法逐格求差" in module and "处理完成后可见逐格变化" in module
    # 动效改在数据与图形本身：进页面数字从 0 长到显示值、图从左侧画出来，
    # 换版本则从上一次真正画出的状态插值过去（整块卡片套淡入读不出数值变化）
    assert "function rpFadeIn" not in module and '".rp-kpi, .rp-fig"' not in module
    assert "let rpShown = null;" in module and "let rpPending = null;" in module
    assert "function rpHook(fn)" in module and "function rpRunAnim(mode)" in module
    assert "function rpClipAt(el, u)" in module
    assert 'renderReport("reveal")' in APP and 'refreshReportVersions("reveal")' in APP
    assert 'renderReport("morph")' in APP
    assert "async function renderReport(mode)" in module
    assert "rpShown = rpPending;" in module and "if (mode) rpRunAnim(mode);" in module
    # 每个渲染器各自登记重放：KPI 数字、哑铃圆点、钻石张角、曲线、频谱位图
    assert module.count("rpHook((u, reveal) =>") == 5
    assert "rpPending.kpi" in module and "rpPending.dyn" in module
    assert "rpPending.bands" in module and "rpPending.outDb" in module
    assert "rpPending.specOff" in module
    # 钻石动画期间角度档位锁死在最终档，否则框线会边长边换档地跳
    assert "const domDeg = fixedDom ||" in module
    # 空态一律画骨架而不是漂浮文案；骨架与实图共用内边距与横轴刻度，数据到达不跳位
    assert "function drawFreqSkeleton(ctx, w, h, dpr)" in module
    assert "const rpPlotPad = (dpr) =>" in module
    assert "function rpFreqTicks(ctx, X, dpr, h, faint)" in module
    assert module.count("rpPlotPad(dpr)") == 2          # 骨架 + 实图各一次
    assert module.count("rpFreqTicks(ctx, X, dpr, h, faint)") == 3   # 定义 + 两处调用
    assert "const tickPool = [" not in module
    # 响度与动态：无数据也画满五行固定量程刻度，读数槽用中性「— → —」
    assert 'if (!pair) {' not in module.split("function renderStereoFans")[0].split("function renderDynamics")[1]
    assert '<span class="rp-dyn-num empty">— → —</span>' in module
    assert ".rp-dyn-num.empty { left: 50%; color: var(--c-text-faint); font-size: 9px; }" in CSS
    assert "if (pair) rpHook((u, reveal) =>" in module     # 骨架行没有圆点可插值
    # 频谱变化：没有位图也先画频率标尺
    assert "drawFreqScale(ctx, w, h, dpr, fHi);\n    ctx.fillStyle" in module
    # 换频谱视图（原始/处理后/变化）整幅渐变：拍下当前画面盖在新画面上淡出，
    # 不做左右擦除。载荷晚到走同一条路，两种入口动画一致。
    assert "function rpSpecFade(apply, ms = 240)" in module
    assert "rpWipe" not in module
    assert "rpSpecFade(() => renderSpectral(), 240)" in module
    assert "rpSpecFade(() => renderSpectral(), 260)" in APP
    assert ".rp-spec-wrap { position: relative; display: flex;" in CSS
    # 重放必须有收尾兜底：窗口被遮挡时 rAF 可以整段不推进，否则报告停在起点
    assert "let rpGuard = 0;" in module
    assert "rpGuard = setTimeout(finish, ms + 250);" in module
    assert "clearTimeout(rpGuard);" in module
    # 报告页最外层框移除（预览页仍保留面板框），左右内缩一并归零才对齐队列面板
    assert "#pane-report.pane-view { border: none; background: none; padding: var(--s-3) 0; }" in CSS
    # 视图切换按钮原先靠左侧图例顶到右边，图例走后要自己 margin-left:auto
    assert ".rp-spec-tabs { display: inline-flex; margin-left: auto" in CSS
    assert 'block.className = "rp-fan " + dir' in module
    assert ".rp-fan.up .rp-fan-num .after { color: var(--rp-boost); }" in CSS
    assert ".rp-spec-wrap canvas { flex: 1 1 0;" in CSS
    assert ".rp-fans { display: grid" in CSS and "; flex: 1 1 auto; min-height: 0; }" in CSS
    assert ".rp-fan canvas { width: 100%; flex: 1 1 0;" in CSS
    # 说明文案统一后补，图例先只留色块
    assert "只反映频谱平衡" not in module
    # KPI 名称必须名副其实：读数就是该频段能量变化，不是「清理」
    assert 'label: "低频能量"' in module and 'label: "空气感能量"' in module
    assert "低频清理" not in module and "人声空气感" not in module
    # 指标 hover 提示（含义 + 数值）
    assert "function attachRpTip(canvas)" in module
    assert "立体声相关性" in module
    # 版本号一律大写 V
    assert re.search(r'`v\$\{', APP) is None
    # 标题不重复版本号：播放器统一选择成品版本
    assert "name.textContent = pair ? stemOf(pair.path)" in module
    assert "`V${pair.ver}" not in APP
    # 跨会话数据：报告从输出目录读取（只读选中的那一版）
    assert 'api.reportLoad(path + ".quality.json")' in APP
    assert 'rp.reports.set(path, report)' in APP


def test_no_emoji_icons_and_no_pane_keyframes():
    pane = report_pane_html()
    assert re.search(r"[🀀-🫿☀-➿]", pane) is None
    assert "@keyframes" not in pane and "animation:" not in pane


def test_app_js_syntax_via_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    check = subprocess.run([node, "--check", "ui/app.js"], cwd=ROOT,
                           capture_output=True, text=True)
    assert check.returncode == 0, f"node --check failed: {check.stderr}"


def test_main_versioned_output_and_preview_cache():
    # 重复处理自动 _v2：成品存在时绝不静默覆盖
    assert "def _versioned_output_path" in BACKEND
    assert "out_final = _versioned_output_path(output_dir, stem)" in BACKEND
    # 预览缓存：键含文件身份/片段/参数/引擎版本，命中直接回放
    assert "def _preview_cache_key" in MAIN
    assert "cache_dir = pipeline_cache.preview_dir()" in MAIN
    assert 'payload["cached"] = True' in MAIN
    # 成品版本列表 + 任意文件频谱通道
    assert "def listOutputs" in MAIN
    assert "def previewLoadOutput" in MAIN
    assert "previewOutputPeaks = Signal(str)" in MAIN
    # 质量摘要仍携带完整报告（磁盘报告的数据与后端信号同源）
    assert "payload = {**report, **summary," in MAIN
    assert '"input_path": str(src)' in MAIN


def test_report_spectrogram_rows_are_log_spaced(tmp_path):
    """频率行由后端按对数下发（载荷带 fmin）。旧版下发线性 bin 让前端重排，
    100 Hz 以下一个 bin 要铺几十行，报告频谱图糊成马赛克。"""
    import base64
    from math import log

    import main as app_main

    sr, rows_n, fmin = 44100, 256, 30.0
    t = np.arange(sr * 3) / sr
    tone = sum(0.4 * np.sin(2 * np.pi * f * t) for f in (60.0, 3000.0, 12000.0))
    path = tmp_path / "tones.wav"
    sf.write(path, np.column_stack((tone, tone)).astype("float32"), sr)

    payload = app_main._spectrogram_payload(str(path), time_frames=400,
                                            freq_rows=rows_n, fmin=fmin)
    assert payload["h"] == rows_n and payload["fmin"] == fmin
    grid = np.frombuffer(base64.b64decode(payload["b64"]), dtype=np.uint8)
    grid = grid.reshape(payload["w"], rows_n).mean(axis=0).astype(float)
    nyq = sr / 2.0
    for f in (60.0, 3000.0, 12000.0):
        want = int(rows_n * log(f / fmin) / log(nyq / fmin))
        band = grid[max(0, want - 3): want + 4]
        assert band.max() >= grid.max() * 0.8, f"{f} Hz 未落在预测行 {want}"
    # 低频不再是几行台阶：30–120 Hz 之间的行应逐行连续变化（相邻行差值有限）
    low = grid[int(rows_n * log(30 / fmin) / log(nyq / fmin)):
               int(rows_n * log(120 / fmin) / log(nyq / fmin))]
    assert np.max(np.abs(np.diff(low))) < 40


def test_storage_in_cache_dir():
    # WebView 持久存储与预览产物集中在缓存目录；旧目录一次性搬迁
    assert 'app_cache_dir() / "webview-storage"' in MAIN
    assert "def _migrate_legacy_storage" in MAIN
    assert 'ROOT / "webview_storage" / "preview"' not in MAIN


def test_versioned_output_path(tmp_path):
    import studio_backend
    first = studio_backend._versioned_output_path(tmp_path, "song")
    assert first.name == "song_shadowbuster.wav"
    first.touch()
    second = studio_backend._versioned_output_path(tmp_path, "song")
    assert second.name == "song_shadowbuster_v2.wav"
    second.touch()
    third = studio_backend._versioned_output_path(tmp_path, "song")
    assert third.name == "song_shadowbuster_v3.wav"


def test_selection_follows_queue_clicks():
    """预览/报告跟随待处理列表的点击选中（无独立文件选择控件）。"""
    assert 'id="pv-file"' not in HTML and 'id="rp-file"' not in HTML
    assert "state.selectedFile = f;" in APP
    assert 'if (ev.target.closest(".fi-x")) return;' in APP
    assert "selectedFile: null," in APP
    assert "state.selectedFile = additions[additions.length - 1];" in APP
