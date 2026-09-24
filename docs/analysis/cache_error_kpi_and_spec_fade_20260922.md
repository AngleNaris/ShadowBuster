# 缓存失败提示、报告 KPI 图标/箭头与频谱切换动效（20260922）

## TODO

- [x] 缓存取消/失败：不再弹「用户取消 / 失败原因」文字（清空 `#draft-status`）
- [x] 缓存取消/失败：时间轴底部红线只显示 1 秒就收回，恢复中性状态
- [x] 报告页顶部四张 KPI 卡片：图标不再跟数值变色，统一取标签（字体）色
- [x] 报告页 KPI 读数右侧的 ▲/▼ 箭头删除（方向仍由数字正负号 + 读数颜色表达）
- [x] 频谱变化（原始 / 处理后 / 变化 Δ）切换一律整幅渐变，去掉左右擦除
- [x] 回归断言（新增 `test_draft_failure_flashes_timeline_without_message` 并改写 KPI/频谱断言）、缓存版本递增、全量测试通过
- [x] 探针浏览器：取消/失败两条路径 + 深浅主题复核，`window.__errs` 为空
- [x] 重启应用后窗口截图复核

## 起因

用户反馈（附播放条红线截图 + 报告页截图）：

> 用户取消创建缓存，或者缓存创建失败，不要这个文字提示，红线也应该显示1秒就消失掉。
> 报告界面，顶部四个卡片的 icon 不要跟着一起变色，icon 统一和字体一个颜色。
> 删除数字右侧的上下箭头。频谱变化切换的时候动画全都用渐变

四条都能在代码里对上：

1. **失败提示**：`onDraftFailed()` 里 `cacheState('error','缓存准备失败'); playerError(p.error)`
   ——`playerError` 把后端异常原文（取消时就是 `用户取消`，见 `draft_preview.py` 的
   `PipelineError('用户取消')`）写进 `#draft-status`；`data-cache="error"` 又让
   `#player-track::after` 那条 3px 红线一直挂着，直到用户自己去做下一个动作。
2. **图标跟着变色**：`.rp-kpi.up .rp-kpi-ic, .rp-kpi.up .rp-kpi-num, … { color: var(--rp-boost) }`
   ——图标、数字、箭头三件套同色，读数一变色图标就跟着绿/粉。
3. **箭头**：`renderSummary()` 给每张卡拼了一个 `<span class="rp-kpi-arrow">▲/▼/—</span>`。
4. **擦除式切换**：频谱三个视图的 tab 点击走 `rpWipe($("rp-spec"), 240)`——`clip-path`
   从左往右揭开，读起来是「又画了一遍」，不是「换了一幅」。

## 行为与接口

### 缓存失败/取消（`ui/app.js`）

- `onDraftFailed()` 改为 `playerError(''); flashCacheError();`——只清文字、不再写错误原文，
  两条路径（用户取消、准备真失败，后端都经 `draftFailed` 信号）行为一致。
- 新增 `flashCacheError()`：`cacheState('error',…)` 后 `setTimeout(…, 1000)` 收回为
  `cacheState('empty','缓存尚未准备')`（红线即 `--c-err`，按钮同时回到「创建缓存」）。
- `cacheState()` 增加自增 `cacheStateToken`，任何一次状态更新都作废尚未到期的回收；
  闪回期间用户再点「创建缓存」不会被 1 秒前的旧定时器打回空态。
- 播放键时间轴的状态机（`empty / preparing / ready / stale / error`）不变；
  `stale`（输入或精度已变化）仍是常驻红线，本轮不动。

### 报告 KPI 卡片（`ui/app.js` + `ui/style.css`）

- `renderSummary()` 删掉箭头元素，读数仍然是 `rpSigned()`（带正负号）。
- `dir`（up/down/flat）继续写在卡片的 class 上，但只驱动读数颜色：
  `.rp-kpi.up .rp-kpi-num { color: var(--rp-boost) }`、`.rp-kpi.down … var(--rp-cut)`、
  `.rp-kpi.flat … var(--c-text-dim)`。
- `.rp-kpi-ic` 固定 `color: var(--c-text-dim)`（与标签同色），顺带去掉原来的 `opacity: .9`
  ——不然「同色」还要再打九折；`.rp-kpi-arrow` 规则随元素一并删除。

### 频谱变化切换（`ui/app.js` + `ui/style.css`）

- 新增 `rpSpecFade(apply, ms = 240)`：把当前画布 `drawImage` 拍成等尺寸快照、
  `position:absolute` 盖在 `#rp-spec` 正上方（按 `offsetLeft+clientLeft` 对齐内容盒），
  执行 `apply()` 画出新视图，再让快照 240ms 淡出——整幅 dissolve，不做裁剪。
  连点两次会先移除上一张快照，不会叠层。
- 两个入口都改用它：tab 点击（240ms）与频谱载荷晚到（260ms，`onPreviewOutputPeaks`）。
  `rpWipe()` 因此无调用者，删除；`rpClipAt()` 仍供进报告页的「画出来」揭示动画使用，
  该揭示动画（数字增长 + 图形绘出）不在本轮改动范围。
- `.rp-spec-wrap` 加 `position: relative` 作为快照的定位上下文；`prefers-reduced-motion`
  下跳过动画，直接落终态。

界面接口（id/结构）不变；改动集中在 `ui/app.js`、`ui/style.css`、缓存版本号与测试断言。

## 验证

- **探针浏览器**（`.zcode/ui_probe`，1280×900，真实频谱/报告载荷，伪造 QWebChannel 桥）：
  - KPI：四张卡的图标计算色 = 标签色（深色 `rgb(154,145,168)`，浅色 `rgb(99,95,102)`），
    `.rp-kpi-arrow` 元素数为 0；读数仍按方向取色（提升 `rgb(59,168,139)`、衰减 `rgb(176,80,106)`）。
  - 频谱切换：点击「原始」的瞬间 `.rp-spec-wrap` 内两枚画布（`#rp-spec` + 快照，
    快照 `left/top = 1px` 对齐描边），60ms 时快照 `opacity ≈ 0.58`，500ms 后回到 1 枚；
    `#rp-spec.style.clipPath` 为空（擦除路径已不存在）；连点两次不叠层、`aria-pressed` 正确。
    截帧比对：70ms 中间帧 99.8% 的像素落在「变化 Δ」与「原始」两帧之间——是真正的溶解而非换图。
  - 缓存失败：`__probeFire('draftFailed', {error:'用户取消'})` → `data-cache='error'`、
    红线 `rgb(255,77,79)`、`#draft-status` 保持 hidden（无文字）、按钮回「创建缓存」；
    0.55s 仍是红线，1.3s 后 `data-cache='empty'`、线条回到 `rgb(62,50,53)`。
    非取消类失败（资源占用文案）表现相同；失败后 0.3s 内重新点「创建缓存」，
    1.3s 后仍是 `preparing`（旧定时器已被 token 作废）。
  - 浅色主题复核通过；`window.__errs` 为空。
- **回归**：`python -m pytest -q` → **672 passed, 11 skipped, 29 subtests passed (66.32s)**；
  新增 `test_draft_failure_flashes_timeline_without_message` 钉住「不写文字 + 清 `#draft-status` +
  1 秒闪回 + token 作废」；`test_report_contract` 改写为「无箭头、图标中性色、读数取方向色、
  `rpSpecFade` 两个入口、`rpWipe` 不得回潮」；`test_ui_cache` 同步
  `style.css?v=91`、`app.js?v=87`。
- **真机**：重启应用后窗口截图复核。
