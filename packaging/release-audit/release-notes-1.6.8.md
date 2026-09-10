# ShadowBuster v1.6.8

## 更新内容
- 安装器改为 **per-user**：无需管理员权限，默认安装到 `%LOCALAPPDATA%\Programs\ShadowBuster`（可自选目录）。应用目录对当前用户可写，「设置 → GPU 环境」下载的 CUDA 运行时可就地安装；用户自选受限目录（如 Program Files）会被检测并提示。历史 per-machine 安装（v1.6.x 早期）不会被自动卸载，升级需手动卸载或向导中选回原目录。
- Lew 高频重建在 CUDA 上默认使用 **FP16 计算**（`torch.autocast`）：模型内 FFT 经守卫保持 FP32，规避 cuFFT 半精度仅支持 2 的幂长度的限制（20ms 窗口 882 点）。实测 RTX 5060 Ti、10 秒双声道片段：提速约 1.65 倍，显存峰值 6245 MB → 3966 MB，输出与 FP32 相关性 0.999999、分频段能量差 ≤ 0.01 dB。CPU 环境恒为 FP32，行为不变。
- Lew 脚本纳入处理缓存指纹：`lew_upscale.py` 变更后旧阶段缓存自动失效，不再出现旧精度结果被复用。
- 界面 logo 更新为当前红边设计。

## 安装
关闭旧客户端后覆盖安装。安装包未签名，请核对 SHA-256。本版起默认 per-user 安装，无需管理员。

GPU 环境版本继续使用 **1.5.0**，已有环境复用。本 Release 不重复上传 GPU 环境；新安装由客户端获取 [v1.5.0 GPU 资源](https://github.com/AngleNaris/ShadowBuster/releases/tag/v1.5.0)。

## 验证范围
- 371 项测试通过、1 项跳过、29 项子测试通过（版本号同步校验含在内）。
- 三步构建全部重跑：外壳 PyInstaller、运行时装配（runtime 导入 OK 2.7.1+cpu，关键文件 manifest 写入）、Inno 编译 `ShadowBuster-Setup-1.6.8.exe`；FP16 版 `lew_upscale.py` 已确认进入打包运行时。
- `install_test.ps1 -Version 1.6.8` 静默安装通过，安装目录 runtime 导入探针通过（13 个必需模块，numpy 2.5.2）。
- FP16 数值验证基于开发机 RTX 5060 Ti 单片段 A/B 实测；整首歌端到端与多 GPU 型号未验收，安装包 per-user 行为未做独立安装器交互验证。
- 不包含私人音频、缓存、repo.bundle 或本地实验结果。

## 安装包校验
文件：`ShadowBuster-Setup-1.6.8.exe`

大小：794,927,365 bytes

SHA-256：`a749ef8416ed3b321469d3bbbb55052733e6094e5f4d70690a98edbcbfb4d098`
