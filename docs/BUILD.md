# ShadowBuster 构建与发布指南

面向开发机的三步构建流程、三层校验与版本号同步清单。产品代码之外的组件（Soren 母带组件、Apollo/Lew 上游源码与权重、FFmpeg）不属于本仓库，需另行准备。

## 架构与目录

- 界面是 PySide6 + QtWebEngine（UI 外壳），独立打包为 onedir。
- 处理链：Lew 高频重建 → Demucs 四轨分离 → 贝斯增强 → 鼓增强 → 人声调整 → 声场重塑/高频降噪 → Soren 母带。
- 采用**“外壳 + runtime 双目录”**结构：UI exe 小，AI 运行时由安装器整体装配。

| 组件 | 内容 | 大小量级 |
|---|---|---|
| ShadowBuster.exe (+PySide6/QtWebEngine) | UI + 桥接 | ~350–500 MB |
| runtime/env/ | 便携推理解释器（torch CPU + demucs + librosa 等） | ~2 GB |
| runtime/Apollo/ | lew_upscale.py、bass/drum/声场 DSP、look2hear/、ckpts/ | ~1 GB（模型） |
| runtime/Soren_src/ | core_decrypted.py、soren_original.py、model/、profiles/、secured_genres/ | ~1 GB（模型） |
| runtime/ffmpeg/bin/ | ffmpeg.exe（后端自动探测） | ~80 MB |

- v1.5.0 起安装包为 CPU 瘦身运行时（整包约 1 GB），CUDA torch 环境改为应用内“设置 → GPU 环境”按需下载（`build_gpu_env.ps1` 产出分卷与清单，`gpu_env.py` 负责校验、续传、解压与原子切换，安装到**应用安装目录** `runtime-gpu\env`；v1.6.x 旧版安装在 `%LOCALAPPDATA%\ShadowBuster\runtime-gpu\env` 的环境仍被检测识别，无需重新下载）。安装器为 **per-user**（`PrivilegesRequired=lowest`，无需管理员，默认装到 `%LOCALAPPDATA%\Programs\ShadowBuster`），应用目录对当前用户可写，GPU 环境可就地安装；用户自选受限目录（如 Program Files）时会被检测并提示不可写。
- **运行时必须可重定位**：不用 venv。`runtime_sync.ps1` 用 python-build-standalone 整目录拷贝 + `pip --target` 安装 site-packages，依赖清单见 [`RUNTIME_DEPENDENCIES.md`](RUNTIME_DEPENDENCIES.md)。
- **离线优先**：Lew 权重（`runtime/Apollo/ckpts/`）、Soren 模型（`runtime/Soren_src/model/`）、demucs 的 htdemucs 权重（`runtime/hf_home/`）全部内置，装完即可离线处理。

## 构建步骤（开发机）

```powershell
# 1) 打 UI 外壳（PyInstaller onedir）
powershell -File packaging\build_shell.ps1

# 2) 装配 runtime（拷贝工具链+模型，便携解释器 + CPU torch + 全量依赖）
powershell -File packaging\runtime_sync.ps1

# 3) 生成安装包（需先装 Inno Setup 6；winget install JRSoftware.InnoSetup）
& "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
# → packaging\out\ShadowBuster-Setup-<ver>.exe
```

GPU 环境包另跑：

```powershell
powershell -File packaging\build_gpu_env.ps1
```

## 三层校验（发布前必须全过）

- `build_shell.ps1`：构建前检查外壳 Python 可导入 `PyInstaller` 与 `numpy`；构建后确认 onedir 产物内存在 numpy 目录、原生 `.pyd` 扩展，且 PyInstaller warning 未报告 numpy 缺失。
- `runtime_sync.ps1`：所有第三方依赖统一装入 `runtime\env\Lib\site-packages`，验证 torch/demucs/numpy/soundfile/scipy/librosa/numba/statsmodels/pyloudnorm/joblib/cryptography 及 `look2hear.models`（Lew 真实导入链），并把关键文件 SHA-256 写入 `critical-manifest.sha256`。
- `install_test.ps1 -Version <ver>`：静默安装到 `packaging\test_install`，并调用安装目录的 `runtime\env\python.exe` 重跑导入探针；安装器退出码为 0 但探针失败时测试仍失败。

修复依赖后必须重新执行三步并重新编译安装器，不能只替换 UI 外壳或复用旧的 `stage` 目录。

## 版本号同步清单

| 位置 | 字段 |
|---|---|
| `studio_backend.py:17` | `APP_VERSION`（单一来源） |
| `packaging/installer.iss:14` | `MyAppVersion`（由 `tests/test_app_version.py` 校验一致） |
| `tests/test_gpu_release_reuse.py` | `APP_VERSION` 断言 |
| `packaging/release-audit/release-notes-<ver>.md` | 大小 + SHA-256 + GPU 环境是否上传 |

`GPU_ENV_VERSION` 独立演进；GPU 环境未变更时，新 Release 不上传 GPU 包，客户端继续复用旧版本资源。

## 发布流程

1. 确认全量测试通过：`python -m pytest tests/ -q`。
2. 跑完三步构建 + `install_test.ps1 -Version <ver>`。
3. 计算安装器大小与 SHA-256，写入 `packaging/release-audit/release-notes-<ver>.md`。
4. 提交发布提交 → 打 tag `v<ver>` → 推送分支与 tag。
5. `gh release create v<ver> packaging\out\ShadowBuster-Setup-<ver>.exe --title ... --notes-file ...`。

历史版本的验证记录与哈希见 `packaging/release-audit/` 各版本说明与 [`archive/DEPLOY-2025-validated.md`](archive/DEPLOY-2025-validated.md)。
