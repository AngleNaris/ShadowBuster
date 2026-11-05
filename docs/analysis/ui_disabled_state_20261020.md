# 面板失效态统一与母带字段换位（2026-10-20）

## TODO

- [x] 旁路面板：`rack-body` 统一加 `.is-disabled` + `inert`，内部 slider
      标记 `aria-disabled`、按钮设 `disabled`；头部旁路开关不进入失效区，
      始终可点。
- [x] 无效组件：未选择参考音频时，风格强度整行 `.is-disabled`、推子
      `inert`，点击/拖入参考后立即恢复可调。
- [x] 自定义推子与宽度计量表补齐 `pointerdown` / `pointermove` /
      `keydown` 的 `aria-disabled` 守卫（原先只挡滚轮与双击）。
- [x] 母带面板字段顺序改为：参考音频 / 风格强度 / EQ 风格 / 响度，
      帮助文案同步换序。
- [x] 新增 `tests/test_ui_disabled_state.py` 合同测试，缓存版本号递增
      （style.css v94 / app.js v89）。

## 起因

用户反馈三处：
1. 关闭卡片右上角开关后，面板只变淡，旋钮/推子仍能继续修改，容易误以为
   改的是生效参数；
2. 无效组件没有失效提示——未选参考音频时，风格强度滑块看起来仍和有效
   控件一样；
3. 要求把母带面板里的“响度”和“风格强度”互换位置。

## 行为与接口

- **统一失效态**：新增 `.is-disabled` 规则（opacity 0.45、
  `saturate(0.25) brightness(0.82)`），旁路面板作用于整个 `rack-body`，
  无效组件作用于对应 `.field`。两者使用同一视觉，不再分别维护。
- **旁路面板禁止修改**：`paintParamAvailability()` 在旁路关闭时给
  `rack-body` 设 `inert`，并对内部 `[role=slider]` / `[role=button]`
  设 `aria-disabled="true"`、对原生按钮设 `disabled`。因此指针、滚轮、
  键盘都无法改变参数；参数值原样保留，重新打开面板后继续生效。
  `rack-head` 内的旁路开关与帮助按钮不受影响。
- **风格强度依赖参考音频**：`noRef = !state.reference` 时，风格强度
  所在的 `.field` 加 `.is-disabled`，推子设 `inert` 与
  `aria-disabled`，`title` 提示“选择参考音频后可用”。选择或拖入参考后
  `paintParamAvailability()` 立即重绘，恢复可调；EQ 风格不依赖参考，
  无参考时仍可单独使用（`eq_only` 语义不变）。
- **状态汇聚**：`state.draftLocked` 由 `paintTransport()` 统一写入，
  `paintParamAvailability()` 同时处理“面板旁路 / 草稿锁定 / 缺参考”
  三类锁；旁路切换、预设应用 bypass、参考变更、播放传输状态变化都会
  触发重绘。
- **布局**：母带面板两行四格变为上排「参考音频 | 风格强度」、下排
  「EQ 风格 | 响度」；控件尺寸与行距沿用原栅格，和左侧声场推子列对齐。
- 仅前端改动，不涉及 DSP 或后端参数契约；旧缓存仍有效，只递增静态资源
  查询版本以强制刷新 UI。

## 验证

- 浏览器探针（836×861，真实 HTTP 页面 + QWebChannel 桩）：
  - 初始无参考：风格强度 `.field` 为 `is-disabled`，计算 opacity 0.45；
    推子 `aria-disabled=true`、`inert=true`。合成 wheel / ArrowRight /
    pointerdown+pointermove 后数值保持 0.85。
  - 拖入参考：字段恢复 `class="field"`、opacity 1；滚轮一次后 0.85 → 0.90。
  - 关闭母带面板：`rack-body` 为 `rack-body is-disabled`、`inert=true`、
    opacity 0.45；参考卡 `inert=true`，四个响度按钮 `disabled=true`；
    点击“响亮”后仍为“标准”。真实滚轮悬停在旁路旋钮上数值不变。
  - 真实点击头部旁路开关可恢复：`aria-checked=true`，body opacity 1。
  - 关闭高频面板后，`#knob-guidance` 的 `aria-disabled=true`，合成
    wheel / ArrowUp / pointer 事件后 1.5 不变；开关仍可点。
  - 深色 / 浅色主题各截图检查：失效态、有效态与两行四格对齐均正常。
- `node --check ui/app.js` 通过。
- `python -m pytest -q`：757 passed, 11 skipped, 29 subtests passed
  （175.94s）。新增 `tests/test_ui_disabled_state.py` 4 项包含在内。
