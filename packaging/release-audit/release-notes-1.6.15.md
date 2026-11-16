# ShadowBuster v1.6.15

本版包含自 GitHub v1.6.12 以来的声场、低频、母带和界面更新。

- **声场**：优先为窄中置的伴奏增加空间感，减少对已有宽度的区域的处理。只处理 `other`、`guitar`、`piano`。加入局部相位检查，减小新增侧声道对原有侧声道的抵消。
- **低频**：校准饱和器的小信号增益，避免开启饱和时连带抬高低频底色。
- **母带**：密度压缩使用 ZL Compressor 0.6.1，提供关闭、轻、标准三档。独立母带的「响亮」档使用 ZL Limiter 0.0.1，目标区间为 −8 至 −6 LUFS，输出真峰值上限为 −0.5 dBTP。柔和段保留响度起伏，开启密度压缩且有对齐鼓轨时保护强鼓击的起音。其余独立母带档保留 −1.0 dBTP 上限。
- **报告和试听**：简化报告页布局与频谱切换，更新草稿试听的声场近似和参数提示。

## 下载与升级

Windows x64 使用 `ShadowBuster-Setup-1.6.15.exe` 安装或覆盖升级。已有 GPU 1.6.11 的用户可以继续使用。macOS 本次提供源代码和更新后的装配脚本。

上游固定版本的许可证与对应源码随 Windows 安装包附在 `licenses/mastering/`。 [母带依赖清单](https://github.com/AngleNaris/ShadowBuster/tree/v1.6.15/packaging/third-party/mastering)
