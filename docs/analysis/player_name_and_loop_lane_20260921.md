# 播放器：文件名的滚动交接与循环带遮罩（2026-09-21）

## TODO

- [x] 选中行去掉名称按钮自描的那一圈 1px，只留整行底色。
- [x] 名称显示不全时，鼠标 hover 快速往返滚动。
- [x] 滚轮换曲时文件名做上下滚动交接。
- [x] 时间轴循环带遮罩从 78% 降到 38%，波形不再被压成黑块。
- [x] 真实浏览器量过跑马灯位移、滚轮交接过程与两档遮罩的逐行亮度，并补回归断言。

只修改权威源码（`ui/`），不更新打包派生副本。

## 起因

四个都是看得到的判读问题：

1. **选中文件多一层框**：队列行的名称是 `<button class="fi-name" aria-pressed>`，
   全局那条 `:root button[aria-pressed="true"] { box-shadow: inset 0 0 0 1px … }`
   把选中态又画了一遍，和 `.file-item.selected` 的行底色叠成双层框。
2. **名称截断后读不到全名**：`#player-file-name` 与队列行都只给 `text-overflow: ellipsis`，
   鼠标停上去也没有下文，长名字（`…_缩混_FINAL_shadowbuster_320k.wav`）只能靠 tooltip。
3. **滚轮换曲时名字瞬切**：滚轮本来是最快的换曲手势，但名字是 `textContent` 直替换，
   连续滚轮时看不出换到了哪一首、也不知道方向。
4. **循环带遮罩太黑**：`.player-timeline .player-loop-lane` 的背景是
   `color-mix(--c-bg 78%, transparent)`，盖在波形画布上等于给整条波形压了一块黑板，
   范围选择器这一带基本读不出内容。

## 行为与接口

- `.player-file-menu .fi-name[aria-pressed="true"] { box-shadow: none; }`：
  选中反馈只落在行的 `color-mix(--c-accent 18%, --c-panel)` 底色上。
  键盘 `:focus-visible` 的描边保留——那是可达性反馈，不是选中态。
- 名称结构统一为「外层裁切 + 内层位移」：`<span class="mq">`。
  `.mq` 自己吃 `overflow: hidden` 与省略号，外层只负责框；滚动期间加 `.running`
  把省略号换成 `clip`——内层带着省略号位移会把「…」拖到窗口中间。
- `#player-file-name` 改为 `display: flex; align-items: center`，让 `.mq` 成为
  flex 子项而不是 inline-block：截断内联块会把基线顶到底边，文字在 44px 的框里会下沉。
- `mqStart(el)` / `mqStop(el)`：超宽（`scrollWidth − clientWidth > 4px`）才起动画，
  `translateX(0) → translateX(-span)`，`duration = max(600ms, span × 7px/ms)`、
  `direction: alternate`、`easing: ease-in-out`，即约 140px/s 的快速往返。
  `pointerenter` / `pointerleave` 绑定（`bindNameMarquee`），队列行在
  `renderFileList` 重建后逐行绑定；未超宽时一次动画都不起。
- `setPlayerFileName(text, rollDir)`：`rollDir` 只在滚轮换曲时传入（`±1`）。
  旧名字按 `translateY(-100% × rollDir)` 交出（110ms，`fill: forwards`），
  落地后换字、新名字从 `+100% × rollDir` 进入（150ms，`--ease` 同款曲线）。
  交接完成后若指针还在框上，把跑马灯接回去（滚轮就是在悬停状态下用的）。
  `prefers-reduced-motion` 或没有 `rollDir`（点击选中、增删文件）时直接换字。
- `renderFileList(rollDir)` 成为唯一入口，滚轮那条路径传 `e.deltaY > 0 ? 1 : -1`
  （往下滚 = 列表往下走，新名字从下方进来）。
- 循环带遮罩 `78% → 38%`：仍与主轨有色差（下缘另有 1px 分隔线），
  但波形在这条带里读得出形状。

## 验证

真实浏览器（Chrome 150 + 探针页，见 `.zcode/ui_probe`，不进仓库）：

- **选中行**：第 8 行（`5.未寄出的信_…`）`.fi-name` 的 `box-shadow` 计算值为 `none`，
  行底色仍是 `color(srgb 0.215 0.130 0.155)`；放大截图里只剩底色，没有描边。
- **跑马灯**：把 picker 压到 170px 后 `scrollWidth − clientWidth = 159px`；
  `pointerenter` 后 `.mq` 带 `running`、`getAnimations().length = 1`、
  `text-overflow: clip`；500ms 内 `matrix(1,0,0,1,-3.12,0) → matrix(1,0,0,1,-92.89,0)`，
  即位移约 180px/s，单程 1113ms；`pointerleave` 后动画清零、省略号恢复。
- **滚轮交接**：`deltaY = -120` 时第 7 首 → 第 6 首。70ms 采样到
  `opacity 0.564 / translateY +7.85px / 旧名字仍在`（交出中），
  ~450ms 后 `transform: none / opacity 1 / 0 个动画`，文本与 `title` 都换成
  `4.风向标与旧站台_…`。
- **遮罩**：同一滚动位置分别用 38% 与 78% 截图，逐行均值比对——
  循环带对应的 y=212..230 行差 2.5 → 13.6（越靠下越大），
  该区均值 27.0 → 41.0；放大对比图里 78% 只剩一条黑影，38% 能读出包络形状。
  浅色主题同位置也量过：遮罩变成一层浅纱，与主轨仍可分辨。
- 新增 `test_player_name_marquee_and_loop_mask`：钉住去边框规则、`.mq` 双层结构、
  滚轮方向参数、`$('player-file-name').textContent=` 不得回归，以及 78% 遮罩的退出。
- 全量 `python -m pytest -q`：**670 passed, 11 skipped, 29 subtests passed**（74.93s）。
- 缓存版本递增为 `style.css?v=87` / `app.js?v=83`。

### 探针侧的坑（与本次改动无关，但会误导判断）

探针页缺 `ui/draft_audio.js` 时，`const draft = { … new DraftPlayer() }` 会抛
`DraftPlayer is not defined`，整个播放器 IIFE 直接中断：队列能渲染、播放器却是死的
（`#player-sources` 空、`addPaths` 走到 `draft.busy` 时撞 TDZ 报
`Cannot access 'draft' before initialization`）。build.py 现已一并复制该文件，
并在页面里预置 `window.__errs` 接住载入期异常——控制台里这些报错会被资源 404 淹掉。
