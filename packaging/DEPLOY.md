# ShadowBuster 部署方案（目标：陌生环境一键安装即用）

## 现状与决策

- 界面是 PySide6 + QtWebEngine（UI 外壳）。**已可独立打包**。
- 处理管线是 4 个子进程阶段，工具链在开发机 `D:\_3.AI\audio_upscale\{Apollo, Soren_src}`，
  依赖 Python 环境（含 torch）。**不能**把这些搬进外壳 exe（体积/兼容双输）。
- 因此采用**"外壳 + runtime 双目录"**结构：UI exe 小，AI 运行时由安装器整体装配。

  | 组件 | 内容 | 大小量级 |
  |---|---|---|
  | ShadowBuster.exe (+PySide6/QtWebEngine) | UI + 桥接 | ~350–500 MB |
  | runtime/env/            | 便携推理解释器（torch **CUDA cu128** + demucs + librosa 等） | ~6 GB |
  | runtime/Apollo/         | lew_upscale.py、bass_enhance.py、look2hear/、ckpts/ | ~1 GB（模型） |
  | runtime/Soren_src/      | core_decrypted.py、test_model.py、model/、profiles/、secured_genres/ | ~1 GB（模型） |
  | runtime/ffmpeg/bin/     | ffmpeg.exe（后端自动探测） | ~80 MB |

- 推理用 **CUDA 版 torch 2.7.1+cu128**（与开发环境同版本）：有 NVIDIA 卡自动走 GPU，
  没有则 `auto_device()` 探测失败自动回退 CPU，一套运行时覆盖两种机器。
  CUDA 运行库随轮子内置，用户只需较新显卡驱动。安装包约 2.5 GB。
- **运行时必须可重定位**：不用 venv！venv 的 `pyvenv.cfg` 硬编码构建机解释器绝对路径，
  拷到陌生机器起不来。`runtime_sync.ps1` 改用 python-build-standalone 整目录拷贝 +
  `pip --target` 安装 site-packages（依赖清单与开发 venv 同版本，含 Soren 需要的
  librosa/scipy/numba/statsmodels/pyloudnorm/cryptography 和 look2hear 需要的
  pytorch_lightning/omegaconf/rich/torch-complex；omegaconf 2.0.6 元数据老旧需 pip24 安装）。
- **离线优先（安装即用，禁止联网下载）**：所有模型全部内置——
  Lew 权重（`runtime/Apollo/ckpts/`）、Soren 模型（`runtime/Soren_src/model/`）、
  demucs 的 htdemucs 权重（`runtime/torch_home/hub/checkpoints/`，由 `runtime_sync` 预置，
  后端 `stage_demucs` 把 `TORCH_HOME` 指向这里）。用户在陌生环境装完即可离线处理。

## 目录探测逻辑（studio_backend.py `_resolve_runtime`）

1. 优先：exe 同级 `runtime/`（含 `Apollo/`、`Soren_src/`、`env/python.exe`、`ffmpeg/`）→ 安装即用；
   注意冻结态必须用 `sys.executable` 定位（`__file__` 在 `_internal` 里，会指错）；
2. 其次：环境变量 `SB_ASSETS`；
3. 开发回退：`SB_PYTHON/SB_APOLLO/SB_SOREN` → 开发机硬编码路径。

## 子进程行为约定（studio_backend.py）

- 所有子进程带 `CREATE_NO_WINDOW`：GUI 外壳无控制台，否则每个推理/ffmpeg 子进程
  都会弹一个 python 黑框。
- 取消 = `taskkill /PID x /T /F` 杀整棵进程树（demucs 的 DataLoader worker 是孙进程，
  只 kill 直接子进程杀不干净）；管道读取在独立线程 + 超时轮询，子进程静默时取消也能即时生效。
- 全局进程注册表 `_ACTIVE` + `terminate_all()`：窗口关闭时兜底清树。

## 构建步骤（开发机）

```
# 1) 打 UI 外壳（PyInstaller onedir）
powershell -File packaging\build_shell.ps1

# 2) 装配 runtime（拷贝工具链+模型，便携解释器 + CUDA torch + 全量依赖）
powershell -File packaging\runtime_sync.ps1

# 3) 生成安装包（需先装 Inno Setup 6；winget install JRSoftware.InnoSetup）
iscc packaging\installer.iss
# → packaging\out\ShadowBuster-Setup-1.2.0.exe
```

## 安装器（installer.iss）

- per-machine（`PrivilegesRequired=admin`），默认装 `C:\Program Files\ShadowBuster`
  （`DefaultDirName={autopf}`），向导显示目录选择页（`DisableDirPage=no`），用户可自选。
- 完成页"立即启动"用 `runasoriginaluser`，避免应用带着管理员令牌跑。

## 已验证（2026-08）

- stage runtime 直接跑通 4 阶段端到端（15s 音频 23s 完成，GPU）。
- 静默安装到 `C:\Program Files\ShadowBuster` 成功；安装版真实任务 trace 显示
  `auto_device: runtime probe -> 1`（走自带 runtime 且探测到 CUDA）。
- 任务运行中点取消 → 数秒内无任何残留 python/ffmpeg 进程。
