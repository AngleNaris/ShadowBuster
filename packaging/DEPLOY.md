# ShadowBuster 部署方案（目标：陌生环境一键安装即用）

## 现状与决策

- 界面是 PySide6 + QtWebEngine（UI 外壳）。**已可独立打包**。
- 处理管线包含 Lew、Demucs、贝斯、鼓、声场/高频降噪、Soren 六阶段。AI 工具链在开发机 `D:\_3.AI\audio_upscale\{Apollo, Soren_src}`，
  应用维护的贝斯/鼓/声场 DSP 源码在仓库 `apollo_scripts/`；运行时依赖 Python 环境（含 torch），**不能**把这些搬进外壳 exe（体积/兼容双输）。
- 因此采用**"外壳 + runtime 双目录"**结构：UI exe 小，AI 运行时由安装器整体装配。

  | 组件 | 内容 | 大小量级 |
  |---|---|---|
  | ShadowBuster.exe (+PySide6/QtWebEngine) | UI + 桥接 | ~350–500 MB |
  | runtime/env/            | 便携推理解释器（torch **CUDA cu128** + demucs + librosa 等） | ~6 GB |
  | runtime/Apollo/         | lew_upscale.py、bass_enhance.py、drum_enhance.py、soundstage_reshape.py、look2hear/、ckpts/ | ~1 GB（模型） |
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
# → packaging\out\ShadowBuster-Setup-1.4.0.exe
```

## 安装器（installer.iss）

- per-machine（`PrivilegesRequired=admin`），默认装 `C:\Program Files\ShadowBuster`
  （`DefaultDirName={autopf}`），向导显示目录选择页（`DisableDirPage=no`），用户可自选。
- 完成页"立即启动"用 `runasoriginaluser`，避免应用带着管理员令牌跑。

## 已验证（2026-09，v1.4.0 最终发布构建）

- 自动测试：`124 passed, 13 subtests passed`；包含非零 bass/drums 整体增益的 delta-add 回归、精确旁路、立体声联动高频降噪与单声道兼容性验证。
- 开发环境完整六阶段端到端处理通过：Lew 高频重建、Demucs 四轨分离、贝斯增强、鼓增强、声场重塑/高频降噪、Soren 母带均执行完成。
- 实测输入 `xianshi_44k.wav`，声场干湿比 0.60、高频降噪 0.20。输出为 44.1 kHz 双声道，shape=`(8131905, 2)`，全部采样有限，峰值 `0.946060419`，RMS `0.237280827`；曲首高频不存在旧控制率滤波造成的异常衰减。
- `runtime_sync.ps1` 从空目录重建运行时，验证 CUDA torch 2.7.1+cu128、SciPy 1.18.0、离线 htdemucs 权重、依赖导入和关键文件哈希，并生成 `critical-manifest.sha256`。
- PyInstaller onedir 外壳与 Inno Setup 1.4.0 安装包构建通过。最终安装器大小 `2747962406` bytes，SHA-256：`d2e4ff5510024e470b00ae11617298bb77c4cb89042f5743f22ddd59c7401116`。
- `packaging/install_test.ps1` 静默安装返回 `EXIT=0`；安装目录内 `soundstage_reshape.py`、`bass_enhance.py`、`drum_enhance.py` 与源码及 stage 的 SHA-256 完全一致，便携 Python 可成功导入 torch 2.7.1+cu128 和 scipy 1.18.0。
- 已从修正版测试安装目录启动应用并视觉验证：1100×950 界面完整加载；文件队列为 158px 高，列表下方“添加 / 清空”按钮均保持 30px 完整高度；四个效果面板、两个声场推子、六阶段条和处理按钮均可见且未受挤压。
- v1.4.1 窗口紧凑化（可调宽度下限 840、自适应默认 1000×860、高度不足时内容区滚动且底部 BUSTER 按钮固定、几何记忆）已通过 124 项自动测试与默认/最小宽度/矮窗三档截图验证；安装包待下一次构建。
