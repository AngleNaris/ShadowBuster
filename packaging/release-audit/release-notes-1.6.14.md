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
- [x] 重建 UI 外壳、完整 CPU 运行时及 Windows 安装器。
- [x] 独立安装验证、实际 GUI、CPU VST 与 GPU 复用探针。
- [x] 覆盖升级本机原安装，验证版本、GUI、DSP 与 GPU 环境复用。
- [x] 记录安装器大小/SHA-256，提交构建与验收记录。

应用版本 1.6.14，DSP `dsp-v7-20261105`，独立母带 `shadowbuster-mastering-v7`；GPU 环境继续使用 1.6.11。本次按用户要求完成本地构建与安装，不自动发布远程 Release。

## 既有验证

DSP 全量回归 803 passed / 1 skipped / 29 subtests passed；后续 UI 缓存与草稿检查 13 passed。用户已反馈改善；具体素材与等响对照见 `docs/analysis/dsp_improvements_20261105.md`。

## 构建与本机验收

- 功能提交 `edb277a`，版本准备提交 `950f893`；先提交功能，再构建安装包。
- PyInstaller 外壳构建通过；冻结 GUI 使用 Qt 6.11.2，435 个界面元素，页面与 QWebChannel 桥接就绪，应用/桥接版本均为 1.6.14。
- 完整重建 CPU 运行时，torch/torchaudio 2.7.1+cpu、numpy 2.5.2、pedalboard 0.9.26 等依赖导入通过；htdemucs 与 htdemucs_6s 均完成预置、分离及离线加载检查，关键源码与 stage 一致。
- Inno 安装器编译通过；独立安装至 `packaging/test_install_1.6.14_verify`，安装器退出码 0，实际 GUI、依赖导入及 ZL Compressor VST3 处理探针全部通过。
- 本机从 1.6.13 就地升级至 1.6.14，原安装位置为 `D:\_3.AI\audio_upscale\SorenStudio\packaging\test_install`。未删除原安装目录；安装登记、桌面和开始菜单快捷方式均指向该目录，升级后实际 GUI 检查通过。
- 安装版 EXE 处理 30 秒真实音乐：旁路 AI/分轨消费阶段，启用无风格母带、标准密度档；报告确认 `shadowbuster-mastering-v7` 和 `ZL Compressor 0.6.1` 实际生效，质量结果 `pass`，响度 −9.266 LUFS（normal 目标 −9.2），4 倍采样真峰值 −1.099 dBTP；WAV、母带和质量旁车均生成。
- 保留的 GPU 1.6.11 运行时完成 CUDA 计算（torch 2.7.1+cu128，RTX 5060 Ti），并运行安装版声场 DSP 处理真实六轨素材；鼓、bass、人声工作分轨与源文件逐字节一致。
- 升级后 1006 项关键文件 SHA-256 与构建 manifest 全部一致；GPU 版本标记、Python 和 torch 入口的哈希与升级前一致。

本地日志、GUI 报告、安装登记基线、真实音频及完整探针结果保存在 `experiments/results/release_1_6_14/`（忽略生成物，不纳入 Git）。

## 安装包校验

文件：`packaging/out/ShadowBuster-Setup-1.6.14.exe`。

大小：**903,269,074 bytes**。

SHA-256：`14f35fa07032201c8d9e3776cc3bfc74aec21f4744842a681e30b324e02d2b10`。

同目录校验文件：`ShadowBuster-Setup-1.6.14.exe.sha256`。本次未重新构建 GPU 环境包。
