# 滚动条样式统一（2026-09-19）

## TODO

- [x] 去掉滚动条上下箭头。
- [x] 轨道要有颜色，不再是一片透明。
- [x] 滑条与轨道一律直角。
- [x] 全站一套规则，补回归断言并在真实浏览器两种主题下量过。

只修改权威源码 `ui/style.css`，不更新打包派生副本。

## 起因

原先 CSS 里已经写了整套 `::-webkit-scrollbar`（含 `scrollbar-button { display: none }`），
看起来箭头早该没了，实际却还在，而且滑条是圆的。根因是同一份 CSS 里还写了标准属性：

```css
* { scrollbar-width: thin; scrollbar-color: var(--c-panel-2) transparent; }
```

Chromium 一旦看到 `scrollbar-color` / `scrollbar-width` 取非 `auto` 值，就改用**系统外观**
绘制滚动条，并把该元素上整套 `::-webkit-scrollbar` 伪元素规则**整体忽略**。
于是 Windows 的原生细滚动条带着两端的箭头和 Win11 的圆角滑条回来了，
下面那 6 条伪元素规则一行都没生效。`.queue-list / .dd-panel / .help-body / .content`
又各自重复声明了一遍标准属性（还把滑条颜色换成另一个 token），空态与实态不一致。

实测证据（Chrome 150）：`.content` 的滚动槽宽 10px、`.queue-list` 12px，
而不是 `::-webkit-scrollbar { width: 8px }` 要求的 8px；
`getComputedStyle(el).scrollbarColor` 返回 `rgb(214,213,216) rgba(0,0,0,0)`。

## 行为与接口

- 删掉全部 `scrollbar-width` / `scrollbar-color` 声明（含逐面板那一组），只保留伪元素一套；
  改后滚动槽实测宽度回到 8px，说明自定义通道重新生效。
- 轨道与转角用 `--c-bg`（全站最暗的一档，读得出凹槽），滑条用 `--c-border`
  （比轨道亮一档，深/浅两种主题下都与轨道同向拉开对比）。
- 滑条保留 `border: 2px solid transparent; background-clip: padding-box` 的内缩画法，
  所以 8px 槽里是 4px 的滑条，两侧露出轨道色。
- 轨道与滑条都显式 `border-radius: 0`；`::-webkit-scrollbar-button { display: none }`
  现在才真正生效，上下箭头消失。
- 悬停仍是 `--c-accent`。
- 逐面板的重复规则整块删除，`.queue-list / .dd-panel / .help-body / .content` 以及
  `.modal-body / .settings-body` 等所有滚动面统一由全局一套覆盖。

## 验证

- 真实浏览器（Chrome 150）逐像素与几何核对：
  - 合成滚动盒的槽宽从 10/12px 变为 **8px**，`scrollbarWidth/scrollbarColor` 计算值为
    `auto / auto`——伪元素规则重新接管。
  - 浅色主题截图放大：轨道为浅灰凹槽、滑条为较深灰，两端是平头，轨道上下端点无箭头按钮。
  - 深色主题同一位置截图：滑条 `#35323f` 落在 `#141218` 轨道上，同样直角、无箭头。
- 新增 `test_scrollbar_contract`：禁止标准属性声明回归、钉住 8px 槽宽、箭头隐藏、
  轨道/转角着色、滑条与轨道 `border-radius: 0`，并断言逐面板重复规则已清除。
- 缓存版本递增为 `style.css?v=77`（本轮未改 `ui/app.js`）。
