# 播放器文件名框：整框一个按钮 + 两翼滚动刻度（2026-09-21）

## TODO

- [x] 删掉文件名框右侧的独立刻度按钮（44×44 的尺子图标），整框改成一个 `<button>`。
- [x] 框两侧加刻度带，滚轮换曲时刻度跟着滚动一个周期，做出物理刻度的观感。
- [x] 跑马灯两端各停一拍：名字的头和尾都要在可视区里定住，不是一闪而过。
- [x] 去掉 `#player-file-name` 的原生 `title`（tooltip 顶着窗口边被裁，且和跑马灯重复）。
- [x] 真实浏览器里量过刻度位移相位、跑马灯各段落位移与命中区域，并补回归断言。

只修改权威源码（`ui/`），不更新打包派生副本。

## 起因

上一轮（见 `player_name_and_loop_lane_20260921.md`）跑了马灯和交接动画，用户回看后仍有三条意见：

1. **滚动时名称依然不完整**：旧关键帧是 `0 → -span` 的往返，两端只各闪一帧，
   长名字的第一个字和最后一个字都来不及看清；原生 `title` 的 tooltip 又顶在窗口右边被裁掉。
2. **右侧按钮多余**：`player-file-trigger`（尺子图标）和文件名框在视觉上是一体的，
   却能分别点，命中区域和「整块 = 一个按钮」的直觉不符。
3. **要物理刻度**：文件名框的两翼要有刻度装饰，滚轮切歌时刻度一起滚，
   像旋钮/物理刻度那样有「在档位上走」的反馈。

用户原话：「将文件列表右侧的按钮移除吧，文件列表整体就是一个按钮，此外，
滑动的时候左右两侧有滚动的刻度装饰」；随后补充「给播放器上显示文件名的那个按钮两侧加刻度装饰，
滚轮滑动切换文件的时候刻度也一起滚动，模拟这是一个物理的那种刻度的效果」。

## 行为与接口

### 1. 整框一个按钮

- `ui/index.html`：`.player-file-picker` 由 `<div>` 改为
  `<button type="button" popovertarget="player-file-menu" aria-expanded aria-controls>`，
  里面依次是：左刻度带、`#player-file-name`、右刻度带。
- `player-file-trigger` 整个删除（HTML / CSS / JS 都不再有引用）。
  JS 侧沿用 `id="player-file-picker"`，所以 `placeFileMenu()`、拖拽高亮
  （`drop-hover`）、滚轮监听、托底聚焦（`fileTrigger.focus()`）都不用改。
- 按钮自带 `:focus-visible` 描边；`:hover` 只改文字与边框色，不加位移——框是拖放目标，
  抖一下会让拖拽落点看着不稳。

### 2. 两翼刻度带

- 结构：`<span class="pf-ticks" aria-hidden="true"><i></i></span>`，左右各一。
- CSS：`.pf-ticks` 固定 `width: 9px`、`align-self: stretch`、`overflow: hidden`，
  上下各 8px 用 `mask-image` 淡出——刻度带不能以半截刻度切在框沿上。
- 刻度本身是 `repeating-linear-gradient(to bottom, var(--c-border) 0 1px, transparent 1px 10px)`，
  即 **1px 刻度 / 10px 周期**；内层 `i` 上下各多出一个周期（`top/bottom: -10px`），
  平移时不会露边。hover 时换成 `--c-text-faint`，跟着整框一起提亮。
- `rollTicks(rollDir)`（`ui/app.js`）：`translateY(0) → translateY(-10 × rollDir)px`，
  260ms。位移**正好等于一个周期**，所以动画结束回到 `translateY(0)` 与停在终点像素级等价，
  看起来是刻度连续滚动而不是「弹回去」。方向与列表一致：往下滚（下一首）刻度往上走。
- `rollTicks` 由 `setPlayerFileName(text, rollDir)` 调用，只有滚轮换曲带 `rollDir`；
  点击选中、增删文件不滚刻度。

### 3. 跑马灯两端停留

关键帧由两帧改为五帧，两端各留 18% 的相位不动：

```
0     → translateX(0)      起点停住
0.18  → translateX(0)      停完开始走
0.5   → translateX(-span)  走到尾
0.68  → translateX(-span)  尾巴停住
1     → translateX(0)      走回起点
```

`duration = max(1600ms, span × 12)`、`iterations: Infinity`、`easing: ease-in-out`
（逐段生效）。span 仍是 `scrollWidth − clientWidth`，`≤ 4px` 不起动画。

### 4. 其它

- 去掉 `renderFileList` 里的 `$('player-file-name').title = …`：原生 tooltip 会在窗口
  右缘被裁，且跑马灯已经能读全名。帮助文案「点击刻度按钮展开文件列表」改为
  「点击文件名框展开文件列表」。
- 悬停态用 `el.dataset.hovering` 自己记（`pointerenter` / `pointerleave`），
  不用 `:hover`——Qt WebEngine 里滚轮事件不产生 pointer 事件，`:hover` 可能滞后，
  交接动画结束时判错就会把跑马灯停在第 0 帧。
- 缓存版本 `style.css?v=88` / `app.js?v=84`。

## 验证

真实浏览器（Chrome + 探针页，见 `.zcode/ui_probe`，不进仓库），视口 660×861 与 985×830
两档、深/浅两套主题：

> 视口口径修正：这一轮用应用的窗口截图反推了真实 CSS 宽度——播放/暂停键是
> `--player-control: 44px`，在 1005px 宽的窗口里正好占 44 个设备像素，即 **dpr = 1.0**，
> 应用真实视口约 **985×830**（上一轮记的「660」是把设备像素当成了缩放后的 CSS 像素）。
> 985 档下探针量到的文件名框宽 210px，与应用截图里量到的 212px 一致，此后以 985 档为准。

- **结构**：`#player-file-picker` 的 `tagName` 为 `BUTTON`，内部两个 `.pf-ticks`，
  盒宽 222px（660 档）/ 210px（985 档），两带各 9×42px 分列左右；
  `player-file-trigger` 在 HTML/CSS/JS 里均已不存在。
- **命中区域**：点框中心（376,173）与点左刻度带（270,173）都能开列表，
  `:popover-open` 为真、`aria-expanded="true"`、焦点落在按钮上；列表内容（状态方块、
  逐行 ✕、添加/清空）与上一轮一致，未受影响。
- **刻度滚动**：滚轮向上（`deltaY = -120`）换到第 6 首，两条带各起 1 个动画，
  关键帧为 `translateY(0px) → translateY(10px)`（`rollDir = -1` → `+10px`，往下走），
  时长 260ms；`span` 位移取整为一个 10px 周期。往下滚时相位取反。
  首尾同相，因此循环滚动不会跳。
- **跑马灯停拍**：把动画 `currentTime` 拨到 0.05 / 0.25 / 0.59 / 0.85 / 0.999 个周期，
  计算值依次为 `0 / 0 / -67px / -9.5px / ~0`——两端各停 18%（286ms），
  名字首尾都在可视区里完整出现；交接动画结束后 `dataset.hovering` 仍为 `"1"`，
  跑马灯被接回（1 个 `iterations: Infinity` 的动画在跑）。
- **主题**：深色与浅色截图里刻度都读得出（浅色下 hover 换 `--c-text-faint` 后有对比度），
  名字框与相邻的「原声 / 创建缓存 / 成品」胶囊行对齐，无布局位移。
- **窄容器**：520px 时 `player-source-controls` 换行，第二行放 Δ 与监听旋钮；
  文件名框仍是两带夹名字，靠省略号截断，没有把刻度带挤掉。
- **真机**：重启应用后抓窗口截图（1005×862）——刻度带在两侧、尺子按钮不再出现；
  放大到 3 倍看，每侧 3 条刻度，位置在框的上下沿内收，未被边框切掉。
- **控制台**：`window.__errs` 为空。
- 回归断言更新在 `test_player_name_marquee_and_loop_mask`：整框按钮结构、两带、
  `rollTicks` 与一个周期的位移、五帧关键帧、`title` 与旧按钮的退出、帮助文案；
  缓存版本断言同步到 88/84。
- 全量 `python -m pytest -q`：**670 passed, 11 skipped, 29 subtests passed**（63.14s）。
