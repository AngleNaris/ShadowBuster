# 播放键与音源胶囊的状态修正（20260921）

## TODO

- [x] 播放键：平时恢复成普通按键，只有正在播放时才点亮（主题色底 + 光晕）
- [x] 播放键：补上悬停反馈（悬停描边转主题色；播放中悬停底色再亮一档）
- [x] 版本选择菜单：同一按钮重复点击可以收回菜单
- [x] 音源胶囊（原声 / 创建缓存 / 成品 / ▼）：悬停与选中时四边描边完整，不再缺左边
- [x] 回归断言（新增 `test_source_pills_and_play_button_states`）、缓存版本递增、全量测试通过
- [x] 探针浏览器：深/浅主题 + 窄宽度（620/480）复核，`window.__errs` 为空
- [x] 重启应用后窗口截图复核

## 起因

用户反馈（附播放条截图）：

> 目前播放按钮无hover效果，且只有正在播放时该按钮才应该处于高亮状态。
> 成品 旁边的版本选择菜单按钮目前无法通过重复点击收回菜单。
> 这几个按钮存在左侧描边消失的问题

三条都能在代码里对上：

1. **播放键常亮 + 无悬停**：`.player-play` 自己常驻 `color:#fff; background:var(--c-accent);
   border-color:var(--c-accent-hi)`，全局 `:root ... :root .player-play { box-shadow: inset 0 0 0 1px
   accent-hi, 0 0 8px ... }` 又把内描边 + 光晕无条件罩在它身上；而悬停只改 `border-color`
   （本来就是 accent-hi）——探针里「静止 vs 悬停」两张截图逐像素 diff 为 0，确实一点反馈都没有。
2. **菜单收不回**：箭头是 `arrow.onclick=()=>versionMenu.togglePopover()`（`popover="auto"`）。
   第二次按下时 light-dismiss 先把菜单关掉，click 处理器再 `togglePopover()` 又把它开回来，
   于是永远开。对照组：文件名框用的是声明式 `popovertarget`，浏览器处理后重复点击能正常收放
   （探针实测 `true → false`）。
3. **左侧描边消失**：`.player-source + .player-source { border-left: 0; }` 把左边那 1px 描边
   让给了前一枚的分隔线；悬停只改 `border-color`，于是被悬停的胶囊只有上/右/下三边转主题色，
   左边还是邻居的灰线——整框看起来缺一条边（探针截图复现：悬停「成品」「▼」均是三边框）。

## 行为与接口

### 播放键（`ui/style.css`）

- `.player-play` 只保留尺寸；点亮态整体挂到 `.player-play[data-playing="true"]` 上
  （底色/描边/白色图标/光晕），`data-playing` 由 `paintTransport()` 按 `playerPlaying()` 写入。
- 悬停：暂停态走 `.player-key:hover` 的描边转主题色；播放态额外
  `background: color-mix(in srgb, var(--c-accent) 82%, #fff)`（亮一档，白图标不变）。
- 全局 `:root button[aria-pressed="true"] …` 规则里删掉 `:root .player-play`，
  光晕不再常驻。禁用态（没选歌）现在是普通的 40% 灰键，不再是一颗「灰掉的主题色按钮」。

### 版本选择菜单（`ui/app.js`）

- 箭头改为声明式 popover invoker：`arrow.setAttribute('popovertarget','player-version-menu')`，
  删掉 `onclick=togglePopover`。开/关、light-dismiss、`aria-expanded`（toggle 事件里同步）
  都交给浏览器；ArrowDown 打开、Escape 收回并交还焦点的键盘路径不变。

### 音源胶囊（`ui/style.css`）

- `.player-source + .player-source` 从「让出左边描边」改为「整圈描边、重叠 1px」
  （`margin-left: -1px`）：静止时分隔线仍是一条（后一枚的左描边压在前一枚右描边上，同色不可辨）。
- 悬停/选中那枚 `position: relative; z-index: 1` 抬到邻居之上，自己的四边描边都完整——
  这是「左侧描边消失」的根因修法（只补颜色不补层级的话，右边又会被邻居盖掉）。
- 选中态不再吃全局 `aria-pressed` 的内描边，改为点亮自身描边：
  `:root .player-source[aria-pressed="true"] { border-color: var(--c-accent-hi);
  background: color-mix(...24%...); box-shadow: 0 0 8px rgba(accent,.35) }`。
  框与整行的格子线共用同一条边，四边齐整、不叠线（与播放条既有的循环/差值开关语言一致）。

界面接口（id/属性）不变；纯 `ui/` 三个权威文件 + 测试断言 + 缓存版本号。

## 验证

- **探针浏览器**（`.zcode/ui_probe`，视口 985×830，暗紫 + 深色，与用户应用同款配色）：
  - 播放键四态截屏：暂停（灰键）/ 暂停悬停（描边转主题色，diff 由 0 变为有差异）/
    播放（主题色底 + 白 ‖ + 光晕）/ 播放悬停（底色再亮一档）。
  - 胶囊：悬停「成品」「创建缓存」「▼」四边框完整；选中「成品」左边缘像素剖面为
    单条 1px 主题色线（旧版是「灰分隔线 + 内描边」两条）；未悬停时行内分隔线仍是一条。
  - 菜单：箭头连点三次 `open → closed → open`，`aria-expanded` 同步；
    点选项收起菜单并切轨（`player-mode` = 成品 V1）；ArrowDown 打开、Escape 收回且焦点回箭头。
  - 布局：620/480px 宽下胶囊行换行排布正常；空状态（未选歌）四枚胶囊与播放键禁用态正常。
  - 浅色主题复核通过；`window.__errs` 为空。
  - 已知遗留：toolbar 在窄宽下 `scrollWidth` 比 `clientWidth` 大 3px——注入旧规则复测同值，
    属改动前既有现象，本轮不动。
- **回归**：`python -m pytest -q` → **671 passed, 11 skipped, 29 subtests passed (61.13s)**；
  新增 `test_source_pills_and_play_button_states` 钉住：播放键点亮受 `data-playing` 约束、
  全局规则不再罩播放键、胶囊重叠描边与 z-index 抬升、箭头声明式 invoker（`togglePopover` 不得回潮）；
  `test_ui_cache` 同步 `style.css?v=90`、`app.js?v=86`。
- **真机**：重启应用后窗口截图复核。
