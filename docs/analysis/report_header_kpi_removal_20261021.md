# 报告页：移除顶部信息行与四张摘要 KPI 卡（2026-10-21）

## TODO

- [x] 删除报告页顶部信息行 `#rp-head`：文件名 / 格式胶囊
      （WAV·44.1 kHz·24 bit·立体声·时长）/ 色块图例 / 分析时间。
- [x] 删除四张摘要 KPI 卡 `#rp-summary`：低频能量、空气感能量、
      动态范围、立体声宽度。
- [x] 同步删除只服务于这两块的 JS（`renderHead`、`renderSummary`、
      `rpBandDelta`、`RP_KPI_ICONS`、`fillLegend`、`renderReportLegend`）
      与 CSS（`.rp-head*`、`.rp-mchip*`、`.rp-summary`、`.rp-kpi*`、
      `.rp-legend-item`、`.rp-swatch`、`.rp-legend-inline`）。
- [x] 报告页从频率响应曲线网格直接开始；四张图与频谱视图切换不变。
- [x] 契约测试改写：移除断言、`rpHook` 计数 4→3、版本号
      style.css v94→v95、app.js v90→v91。

## 起因

用户截图圈出报告页顶部整块并反馈「移除报告页面中的截图中这部分」。
经确认选择「整块移除」：文件信息行、图例、分析时间与四张摘要 KPI
卡全部退役，报告页直接从频率曲线开始。理由与既有的「无冗余文字面板」
方向一致——这些信息在播放器与各图自身读数里已有，顶部再排一遍只是
挤占第一屏，且 KPI 与哑铃行 / 钻石图 / 曲线读的是同一批数据。

## 行为与接口

- **HTML**：`#pane-report` 下只剩一个 `.pane-body`，其唯一子元素是
  `.rp-grid`（四张 `.rp-fig`：频率响应、响度与动态、立体声场、频谱变化）。
  连同已无规则的 `.rp-body` 钩子一并去掉。
- **JS 删除**：
  - `renderHead()`：写入 `#rp-name` / `#rp-mchips` / `#rp-time`，
    读取报告 `meta.output_format` 与 `generated_at`；
  - `renderSummary()`：构造四张 `.rp-kpi`，登记 KPI 数字的重放钩子，
    并向 `rpPending` 写入 `kpi` 数组；
  - `rpBandDelta(lo, hi)`：1/3 倍频程能量求和的频段差，唯一消费者是
    摘要卡；
  - `RP_KPI_ICONS`：四张卡的 SVG 图标表；
  - `fillLegend()` / `renderReportLegend()`：色块图例渲染，图上已无
    图例容器；
  - `renderReport(mode)` 中对应的三处调用。
- **保留**：`rpPair()`（曲线/哑铃/钻石仍要用）、`rpBandSmDb()`（钻石
  读数）、`rpDuration()`（频谱位图时间标尺）、`rpThemeColors()` 及
  `--rp-boost` / `--rp-cut` 回写（哑铃与钻石的 DOM 读数仍在用这两个
  变量）、全部图表渲染与 `rpSpecFade` 三处淡入淡出。
- **重放机制**：`rpHook` 登记点由 4 处降为 3 处（曲线、哑铃圆点、
  钻石张角），`rpPending.kpi` / `rpShown.kpi` 字段随之退役；入场裁剪与
  换版本插值逻辑不变。
- **CSS 删除**：成品元信息行整段（含 900px 断点）、处理摘要 KPI 整段
  （含 900px 断点与方向取色规则）、图例项与色块、标题行右侧
  `.rp-legend-inline`。`--rp-boost` / `--rp-cut` 在哑铃与钻石规则中
  的用法保留。
- **顶部间距对齐卡片间距**：报告页没有外框，`.pane-view` 的
  `padding: var(--s-3) 0` 与内容区 8px 行距叠加后，播放器到首张图是
  18px，比图与图之间的 12px 宽。改为 `padding: var(--s-1) 0 var(--s-3)`：
  顶部 8 + 4 = 12px，与 `.rp-grid` 的 `gap` 一致；底部仍留 10px。
- **静态资源**：`style.css?v=96`、`app.js?v=91`，旧缓存立即失效。

## 验证

- `node --check ui/app.js` 通过；`pytest tests/test_ui_quality_summary.py
  tests/test_ui_cache.py -q` 31 项全绿。
- 浏览器探针（836×861 无头 Chrome + 真实载荷 QWebChannel 桩）：
  - 添加文件后切报告页：`#pane-report` 首个元素为 `div.rp-grid`，
    `#rp-head` / `#rp-summary` / `#rp-legend` 均不存在，
    `window.__errs` 为空；
  - 布局量测：`.pane-body` 只有 `.rp-grid` 一个子元素，内边距 0，
    网格间距 12px，无残留空行；
  - 截图确认：顶部是「频率响应对比」全宽曲线，下接「响度与动态」；
    向下滚动依次为三张声场钻石图与「频谱变化」Δ 图，全部正常绘制；
  - 频谱视图切换（原始 → 处理后）：画布更新、`aria-pressed` 落位，
    无报错；切换主题触发重绘后 `--rp-boost` 仍由 `rpThemeColors()`
    正确回写。
