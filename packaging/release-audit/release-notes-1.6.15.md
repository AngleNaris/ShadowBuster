# ShadowBuster v1.6.15

本版包含自 GitHub v1.6.12 以来的声场、低频、母带和界面更新。

- **声场**：优先为窄中置的伴奏增加空间感，减少对已有宽度的区域的处理。只处理 `other`、`guitar`、`piano`。加入局部相位检查，减小新增侧声道对原有侧声道的抵消。
- **低频**：校准饱和器的小信号增益，避免开启饱和时连带抬高低频底色。
- **母带**：密度压缩使用 ZL Compressor 0.6.1，提供关闭、轻、标准三档。独立母带的「响亮」档使用 ZL Limiter 0.0.1，目标区间为 −8 至 −6 LUFS，输出真峰值上限为 −0.5 dBTP。柔和段保留响度起伏，开启密度压缩且有对齐鼓轨时保护强鼓击的起音。其余独立母带档保留 −1.0 dBTP 上限。
- **报告和试听**：简化报告页布局与频谱切换，更新草稿试听的声场近似和参数提示。

## 下载与升级

Windows x64 使用 `ShadowBuster-Setup-1.6.15.exe` 安装或覆盖升级。已有 GPU 1.6.11 的用户可以继续使用。macOS 本次提供源代码和更新后的装配脚本。

Windows 安装包的 `licenses/mastering/` 包含上游许可证文本和对应版本的源码链接。 [母带依赖清单](https://github.com/AngleNaris/ShadowBuster/tree/v1.6.15/packaging/third-party/mastering)

## 验收

- 全量回归 810 项通过、1 项跳过，29 项子测试通过。
- Windows 独立安装后，实际 GUI、ZL Compressor、ZL Limiter 和运行时导入检查通过。1010 项运行时关键文件与构建清单的 SHA-256 一致。四轨和六轨模型均通过离线加载检查。
- 安装版处理 30 秒音乐，启用标准密度压缩和「响亮」母带。质量报告通过，鼓起音保护已生效，PCM24 成品回读为 −7.97 LUFS、−0.515 dBTP。

## 安装包校验

`ShadowBuster-Setup-1.6.15.exe`，903,942,881 字节。

SHA-256 `63f4f28933c4abef888aed15c01b8d545fab051c3288d81a824adedcf0ce6290`，也可下载 `SHA256SUMS.txt` 核对。
