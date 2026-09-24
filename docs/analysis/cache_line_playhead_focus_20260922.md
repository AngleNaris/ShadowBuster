# 进度线收窄到 3px、指针贯穿整条时间轴、键盘焦点不留外框（20260922）

## TODO

- [x] 扫光只出现在缓存进度线所在的那一行：`.player-timeline::after` 由 `inset: 0` 收到 `bottom: 0; height: 3px`
- [x] 收回上一轮的整高淡紫进度底，进度只留底部 3px 线；260ms 过渡改挂在线的宽度上
- [x] 播放指针贯穿整条时间轴：从范围选择带顶（时间轴内边框顶）一直画到轨道底，三角随之坐到最上面
- [x] 键盘操作后不再在任何元素上出现外框（全站 `:focus-visible`、播放器焦点环、卡片 `:focus-within`）
- [x] 回归断言改写 + 新增、缓存版本递增、全量 `pytest` 通过
- [x] 探针浏览器：真实「创建缓存 → 进度推送」链路、深浅主题、33 个 Tab 停留点差分审计
- [x] 重启应用后窗口复核

## 起因

用户反馈（附播放条截图两张）：

> 扫光效果应该只出现在缓存进度条的那一行所在的范围。
> 我之前说的是让这个指针占满高度，现在上面不是空出来了么，
> 还有键盘操作以后禁止显示任何的外框在元素上。

三条都能对上：

1. **扫光铺满整条时间轴**：上一轮把扫光画在 `.player-timeline::after` 上（`inset: 0`），
   亮带会横穿波形区与范围选择带。用户的期望是**只扫进度条那一行**。
2. **指针上方空着**：上一轮把「占满高度」理解成了缓存进度底（见
   `cache_progress_and_playhead_20260922.md` 的口径说明），而用户指的是**指针**——
   `#player-playhead` 在 `.player-track` 内且 `top: 0`，只覆盖 44px 轨道，
   范围选择带那 20px 一直是空的。
3. **键盘没管焦点样式**：全站约 20 条 `.class:focus-visible { outline: 2px solid … }`，
   播放器里还额外换成了一圈 `box-shadow` 焦点环。Tab / 方向键操作后就到处冒外框。

### 口径确认

用选择题向用户确认过，无歧义：

- 「缓存进度条的那一行」= **底部那条 3px 进度线**（不是整条时间轴）；
- 上一轮的整高淡紫底色 = **收回，只留底部进度线**。

## 行为与接口

### 1. 进度只长在底部 3px 线上（`ui/style.css`）

- 删掉 `.player-timeline::before`（16% / 就绪 8% 的整高进度底）与其就绪浓度规则：
  铺满会让波形罩上一层色，读数反而更糊。
- 底线拆成两层，位置不变（都在轨道底 3px 内）：

  ```css
  .player-track::before { …; left: 0; right: 0; bottom: 0; height: 3px; background: var(--c-border); }
  .player-track::after  { …; left: 0;          bottom: 0; height: 3px; width: 0; background: var(--c-accent-hi);
                          transition: width 260ms var(--ease), background-color 260ms var(--ease); }
  .player[data-cache="preparing"] .player-track::after { width: var(--cache-progress); }
  .player[data-cache="ready"]     .player-track::after { width: 100%; }
  .player[data-cache="stale"]/["error"] .player-track::after { width: 100%; background: var(--c-err); }
  ```

  为什么改成宽度而不是继续用渐变：`background-image` 不可过渡（离散跳变），
  上一轮「进度变化要有过渡」的要求只有靠可动画的 `width` 才留得住。
- JS 不动：`--cache-progress` 仍由 `cacheState()` 写在 `#player` 上，只是消费方从伪元素换成底线。

### 2. 扫光收进同一行（`ui/style.css`）

```css
.player[data-cache-sweep="true"] .player-timeline::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; z-index: 1; … }
```

- `inset: 0` → `left/right/bottom: 0; height: 3px`。时间轴的内边距底边正是轨道底，
  所以这一行与进度线**完全重合**；扫光 z=1 压在底线之上，沿着线扫过去。
- 带子本身不变（100° 斜向渐变、`background-size: 260% 100%`、`95% → 5%` 1.5s 线性循环，
  带心 x ≈ `W·(1.3 − 1.6P)`）；浓度由 38% 提到 55%——
 面积从 72px 高缩到 3px 高，不提一档读不出来。
- `prefers-reduced-motion` 分支改为 `display: none`（原来同时关 `::before` 的过渡，那条已删）。

### 3. 指针贯穿整条时间轴（`ui/style.css`）

```css
#player-playhead { position: absolute; top: calc(-3px - 20px - var(--s-1)); bottom: -2px; width: 1px;
                   background: var(--c-text); pointer-events: none; z-index: 4; }
```

- `top: 0` → `calc(-3px - 20px - var(--s-1))` = −27px，即轨道顶再往上
  「3px 轨道外边距 + 20px 范围带 + 4px 时间轴内边距」，正好落在时间轴内边框顶（页面 y=208）。
- `z-index: 2 → 4`：范围带底部那条 1px 分界线是 z=3，压在指针上的话竖线穿过它时会被割断 1px。
- 三角仍在盒子顶部（`top: 0`），于是坐到整条时间轴的最上沿；`bottom: -2px` 不变。

### 4. 键盘不留外框（`ui/style.css`）

- 文件末尾新增一条兜底，与上面各条 `.class:focus-visible` **同权重（0,2,0）**，靠源序取胜：

  ```css
  :root :focus-visible { outline: none; }
  ```

- 删掉 `.player :focus-visible { outline: none; box-shadow: 0 0 0 1px accent, 0 0 8px … }`
  ——它把描边换成了焦点环，同样是一层框。
- 删掉 `:focus-visible { outline-color: var(--c-accent-hi); }`（描边已全局关闭，留着是死代码）。
- `.file-card:hover, .file-card:focus-within` → 只留 `:hover`：
  卡片自身带 `tabindex`，用键盘进卡片或点进路径框都会给整卡描一圈边。
- 两处**非焦点**描边必须保留：`.btn-process.stop:hover` 的红描边（806 行）与
  `.spec-lane.active canvas`（1420 行）。选中/激活态另有底色与内描边
  （`aria-pressed`、`is-selected`），不靠焦点环表态。

界面接口（id / 属性 / 信号）不变；本轮只动 `ui/style.css`，`ui/app.js` 未改
（缓存版本递增为 `style.css?v=93`，`app.js?v=88` 保持）。

## 验证

探针浏览器（`.zcode/ui_probe`，视口 836×861）。几何：时间轴矩形 `{x:31, y:207, w:774, h:72}`、
内边框顶 y=208、范围带 `{y:212, h:20}`、轨道 `{y:235, h:44}`、底线行 y=276..279。

- **真实链路**：点「添加歌曲」→ 首行选中 → 切「试听缓存」源 → 真实 `draftPrepare` 调用
  （id=3、时长 217s、参数齐全）→ `data-cache="preparing"`、`data-cache-sweep="true"`、
  `--cache-progress: 0%`、文案「正在准备试听缓存」。
- **扫光只画那一行**（把手：同进度 34% 下只切 `data-cache-sweep`，并把动画钉在
  `currentTime=750ms` 的固定相位，逐像素差分）：

  | 项 | 实测 |
  |---|---|
  | 变化行 | capture 213..221 = **CSS y 276.0..278.67**（底线行 276..279） |
  | 越出底线 3px 的行 | **0** |
  | 变化列 | CSS x 263..532（宽 269px；渐变带理论全宽 322px，阈值截掉两端淡尾） |
  | 亮带位置 | 相位 250/750ms → 峰在 0.019W / **0.499W**，自左向右走 |
  | 可见度 | 底线底色 `(48,39,41)` → 带峰 `(77,42,51)`，峰值差 **59/255** |
  | 相位 0 / 1250ms | 带心出画（`W·(1.3−1.6P)`），故该相位下无差 |

  深浅主题各测一遍，越出底线行数均为 0。
- **进度线宽度与过渡**（扫光关闭，差分取最右变化列）：

  | 进度 | 实测宽度 | 期望 `frac × 774` |
  |---|---|---|
  | 10% | 77.00 | 77.40 |
  | 34% | 263.00 | 263.16 |
  | 50% | 387.00 | 387.00 |
  | 90% | 697.00 | 696.60 |

  残差 ≤ 0.4px = 3 倍采样下半个设备像素的取整。10% → 90% 跳变采样
  `t(ms):宽度` = `56: 207 → 112: 482 → 167: 621 → 210: 671`，稳定 697，
  260ms 缓动仍在 ✓。整高底色差分：34% 帧减空态帧，越出底线 3px 的行 **0**（上一轮的整高染色已消失）。
- **指针贯穿**：`getBoundingClientRect()` = y **208..281**（高 73px = 时间轴内边框顶到轨道底 +2）；
  x=418 处逐行取墨迹，`y 208..280` **连续无断口**——竖线穿过范围带、带底线与底线都不被割断
  （207 行为时间轴上边框，非指针）。三角同轴复测（隐去波形，逐行墨迹重心，
  底边 / 中段 / 顶点 / 带内竖线四点）：

  | left | 底边中心 | 中段 | 顶点 | 带内竖线 | 顶点−竖线 |
  |---|---|---|---|---|---|
  | 0% | 25.000 | 25.000 | 25.000 | 25.000 | **0.000 px** |
  | 33.333% | 25.000 | 25.000 | 25.000 | 25.000 | **0.000 px** |
  | 66.667% | 25.000 | 25.000 | 25.000 | 25.000 | **0.000 px** |
  | 100% | 25.000 | 25.000 | 25.000 | 25.000 | **0.000 px** |

- **键盘外框**：连按 Tab 走完 45 步 / 33 个停留点（含 `#player-loop-lane`、`#draft-seek`、
  `.file-card`、旋钮、推子、下拉、段控、设置与处理键）——`getComputedStyle` 的
  `outline-style` 全为 `none`。再做**聚焦前后差分**（元素本身 + 4 层祖先的
  outline / box-shadow / border-color / background-color）：**有差异的元素 0**
  （修 `.file-card:focus-within` 之前是 4 个：输出目录卡与参考音频卡及其路径框）。
- **主题**：深色 + 浅色各复核进度/扫光两态；浅色下底线与扫光同样只占那一行。
- **回归**：`python -m pytest -q` → **675 passed, 11 skipped, 29 subtests passed (58.46s)**；
  `test_cache_progress_lives_on_the_bottom_line_with_sweep` 钉住底线两层结构、四态宽度、
  扫光的行内定位与「整高底不许回潮」；`test_playhead_spans_timeline_and_triangle_is_concentric`
  钉住 `top: calc(-3px - 20px - var(--s-1))`、z=4 与 clip-path 三角；
  `test_keyboard_focus_draws_no_outline_anywhere` 钉住末尾那条全局关闭、
  播放器焦点环已删、两处非焦点描边仍在；`test_ui_cache` 同步 `style.css?v=93`。
- **真机**：重启应用后窗口复核。
