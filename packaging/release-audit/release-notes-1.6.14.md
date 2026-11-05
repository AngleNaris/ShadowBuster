# ShadowBuster v1.6.14

## 变更

- 集成 ZL Compressor 0.6.1 Optical 多频段密度压缩，界面提供关闭、轻、标准档。
- 校准低频饱和小信号增益，独立母带加入快慢恢复与局部介入预算，真峰值天花板为 −1.0 dBTP。
- 声场只为空间不足的窄中置伴奏生成左右差异：处理 other/guitar/piano，已有宽/偏置区域减少处理，鼓/bass/人声不参与拓宽；最终母带不再全混音重复拓宽。
- 同步草稿近似、缓存身份、报告页交互与布局、控件禁用状态。
- pedalboard 0.9.26 随内置 CPU 运行时分发，母带固定使用该解释器；现有 GPU 1.6.11 继续用于 AI/分轨，无需重装。安装后实际加载 VST3 验证。

## TODO

- [x] 用户试听反馈有改善，授权全部提交、打包和覆盖升级。
- [x] 提交全部功能改动：`edb277a`。
- [x] 版本与安装依赖检查：73 项相关测试通过，PowerShell 语法检查通过。
- [ ] 重建 UI 外壳、完整 CPU 运行时及 Windows 安装器。
- [ ] 独立安装验证、实际 GUI 与 CPU/GPU VST 探针。
- [ ] 覆盖升级本机原安装，验证版本、GUI、DSP 与 GPU 环境复用。
- [ ] 记录安装器大小/SHA-256，提交构建与验收记录。

应用版本 1.6.14，DSP `dsp-v7-20261105`，独立母带 `shadowbuster-mastering-v7`；GPU 环境继续使用 1.6.11。本次按用户要求完成本地构建与安装，不自动发布远程 Release。

## 既有验证

DSP 全量回归 803 passed / 1 skipped / 29 subtests passed；后续 UI 缓存与草稿检查 13 passed。用户已反馈改善；具体素材与等响对照见 `docs/analysis/dsp_improvements_20261105.md`。

构建、安装与哈希数据待实际完成后填写。
