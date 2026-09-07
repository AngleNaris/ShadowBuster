# ShadowBuster v1.6.5

## 声场与母带错误修正
- 修正 styled 母带链在 4x 过采样域中错误使用内部采样率设计 Side 滤波器的问题。
- 修正 Side 低架实现方向：低频 Side 温和收束，高频不再被错误削减。
- 修正频率相关混合曲线被错误应用到时域样本索引的问题，改为在频域应用。
- 母带目标响度、压缩、限幅和 styled `soren_original` 响度路径保持不变。
- 母带前声场仍使用 broadband 全域拓宽；仅对新增 Side 增量施加保守的约 70 Hz / −2 dB 低频保护，以保留鼓和 Bass 的低中频包裹感。

## 安装
关闭旧客户端后覆盖安装。

GPU 环境版本仍为 **1.5.0**，已安装环境继续复用，不重复下载。本 Release 不上传 GPU 环境；新安装由客户端获取 [v1.5.0 GPU 资源](https://github.com/AngleNaris/ShadowBuster/releases/tag/v1.5.0)。

## 验证
- 全量测试：277 passed、29 subtests passed。
- 低频保护与四项 DSP 修复回归测试通过。
- 运行时从源码重新同步，关键 manifest 已刷新。
- 1.6.5 安装包独立安装验证通过，安装后的 runtime imports OK（torch 2.5.2）。
- 安装包未签名，请核对 SHA-256。私人音频和实验结果不包含在发布内容中。

大小：797,307,477 bytes

SHA-256：`f00ab8619272f0b6ff7ca60fdb703e0bc033b3f08ef953c36dae9b0781dc92b1`
