# ShadowBuster macOS 构建与运行指南（Apple Silicon）

面向 MacBook（Apple Silicon）的开发运行与打包流程，与 [`BUILD.md`](BUILD.md)（Windows）同构。macOS 版不做 Inno 安装包，产物为自包含的 `ShadowBuster.app`；无 CUDA，推理设备按 **MPS → CPU** 自动探测（`SB_DEVICE` 可强制覆盖）。

## GPU 加速（MPS）

两个神经网络阶段都走 Apple Silicon GPU（MPS），由 `auto_device()` 自动探测启用；Lew 阶段在 MPS 上默认启用 **fp16 autocast**（`lew_upscale.py --precision` 可选 `auto`/`fp32`）——该模型多小算子、Metal 每算子调度开销高，fp16 实测大幅提速且数值差异极小：

| 阶段 | 设备 | 实测（M2 Pro 16GB） |
|---|---|---|
| Lew 高频重建 | MPS + fp16 autocast | 10s 音频 89s → **25s**；30s 音频 12.5min → **36s**（fp32 对比） |
| Demucs 四轨分离 | MPS | `-d mps`；分轨与 CPU 数值一致（RMS 误差 <0.1%） |
| DSP / Soren 母带 | CPU | 纯 numpy/scipy/sklearn 信号处理，无 GPU 版本 |

fp16 与 fp32 输出差异：30s 音频 SNR≈58.7dB、相关 0.99999937（对 fp32 参考全精度跑同样内容）。`--precision fp32` 可回到全精度。全链路 10s 音频（fp32 时代）118s → fp16 后预计约 1 分钟内。

注意：MPS 分配器按块缓存显存，16GB 机型建议处理时关闭大型应用；分块批处理（`--chunk-batch-size >1`）在 16GB 上会触发 MPS OOM（单层卷积需一次性分配 8GB+），保持默认 batch=1。

## 与 Windows 版的差异

| 项 | Windows | macOS |
|---|---|---|
| 推理设备 | CUDA（GPU 环境包按需下载，`gpu_env.py`） | MPS/CPU（系统内置，无需下载；设置页的下载按钮被隐藏） |
| 推理 torch | `2.7.1+cu128`（自建 index） | PyPI 原生 `2.7.1`（macOS 无 CUDA 轮子，即 CPU 版，MPS 随系统） |
| 运行时解释器 | python-build-standalone 整目录拷贝 | uv 托管 Python 3.12 + venv（本机分发够用；跨机分发注意 `pyvenv.cfg` 路径） |
| ffmpeg | 随安装包内置 `ffmpeg.exe` | 内置 runtime 自带，或 `brew install ffmpeg`（自动探测 PATH） |
| 进程树清理 | `taskkill /T /F` | `start_new_session` + `os.killpg` |
| 打包产物 | Inno Setup 安装包 | PyInstaller `.app`（`ShadowBuster_macos.spec`，图标 `.icns`） |
| 组件级改动 | — | 仅 `lew_upscale.py`：`--device` 增加 `mps`，`auto` 优先 MPS（`select_device`） |

其余链路（DSP 脚本、Soren 母带、批处理、UI）与 Windows 完全同一套代码；`studio_backend.py` / `main.py` 的平台差异均已做成运行时分支。

## 环境准备（一次性）

```bash
# 1) uv + Python 3.12
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12

# 2) ffmpeg
brew install ffmpeg

# 3) 推理环境（离线 wheels；迁移包 wheels-macos-arm64/ 与本仓库同级）
uv venv --python 3.12 .venv-infer
uv pip install --python .venv-infer/bin/python --no-index \
    --find-links ../wheels-macos-arm64 -r ../notes/requirements-macos-arm64.txt

# 4) 桌面外壳 + 测试环境（PySide6/PyInstaller/pytest + Soren 分析栈）
uv venv --python 3.12 .venv-shell
uv pip install --python .venv-shell/bin/python PySide6 PyInstaller pytest numpy scipy soundfile
uv pip install --python .venv-shell/bin/python --no-index \
    --find-links ../wheels-macos-arm64 pyloudnorm librosa numba llvmlite statsmodels cryptography joblib
```

注意：`huggingface_hub==0.36.2` 在 arm64 上硬依赖 `hf-xet`，迁移包 wheels 缺失时从 PyPI 补一个 `hf_xet-<x.y.z>-cp38-abi3-macosx_11_0_arm64.whl` 进 `wheels-macos-arm64/` 即可继续离线安装。

## 开发运行

```bash
# 装配 Soren 装配件到组件目录 + 最小测试 stage（幂等）
bash packaging/runtime_sync_macos.sh

# 启动 GUI（自动设置 SB_PYTHON/SB_APOLLO/SB_SOREN，可用环境变量覆盖）
./start.sh
```

`SB_*` 环境变量（与 Windows 语义一致）：`SB_PYTHON` 推理解释器、`SB_APOLLO` Apollo 组件根、`SB_SOREN` Soren 组件根、`SB_FFMPEG` ffmpeg 路径、`SB_DEVICE` 强制 `mps/cuda/cpu`、`SB_TRACE=1` 诊断打点（`SB_TRACE_FILE`，默认 `/tmp/sb_trace.log`）。

## 无头 CLI（v1.6.6）

GUI 之外可用官方无头入口批处理（自动探测 MPS，输出 JSON 结果）：

```bash
SB_PYTHON="$PWD/.venv-infer/bin/python" SB_APOLLO=... SB_SOREN=... \
  .venv-infer/bin/python processing_cli.py -i 输入.wav -o 输出目录 [--result-json 结果.json]
```

处理缓存（`pipeline_cache.py`）跨平台可用：macOS 上落在 `~/ShadowBuster/processing-cache`（或 `SB_PROCESSING_CACHE_DIR`），用 fcntl 文件锁互斥；同输入+参数+设备重跑时各阶段直接复用缓存工件（10s 音频实测全量 118s → 命中缓存 1.2s）。

## 测试

```bash
bash packaging/runtime_sync_macos.sh        # 先落位 stage（4 个测试读取其中的 Soren 副本）
export SB_PYTHON="$PWD/.venv-infer/bin/python"
export SB_APOLLO=<component_Apollo 目录>
export SB_SOREN=<component_Soren_src 目录>
.venv-shell/bin/python -m pytest tests/ -q
```

`tests/test_gpu_offline_reuse.py` 模拟"已装 GPU 环境"这一 Windows 专属前提，已按移植惯例补 `os.name` 模拟；其余测试跨平台直接通过。

## 打包 .app

```bash
bash packaging/build_macos.sh
# → dist/ShadowBuster.app（自包含 runtime：env + Apollo + Soren_src + ffmpeg + 权重）
```

- runtime 未装配时 `build_macos.sh` 自动先跑 `runtime_sync_macos.sh --runtime`（离线 wheels + 预置 htdemucs 权重，权重首次需联网）。
- 图标由 `packaging/make_icns.sh` 生成 `ui/logo.icns`：母版优先从矢量源 `ui/shadowbuster.svg`（红边设计，与 `logo.ico` 同源）渲染，回退 `logo.ico`/`logo.png`。生成后按 Apple 图标模板预制（824/1024 居中 + 烘焙透明四角），使 Dock 不再对图标套用系统圆角遮罩，直角红边设计按原样显示；圆角半径可用 `SB_ICON_RADIUS` 调整（默认 0 = 完全直角）。
- runtime 被拷入 `Contents/MacOS/runtime`，由 `studio_backend._resolve_runtime()` 的 `ROOT/runtime` 候选发现；开发布局（`SB_*` 变量）不受影响。
- 开源项目：**不做签名与公证**。各二进制自带 ad-hoc 签名，可直接运行；若未来需要对外分发再补 Developer ID 签名。

## DMG 安装镜像（分发形态）

```bash
bash packaging/make_dmg.sh
# → dist/ShadowBuster-<ver>-macOS-arm64.dmg
```

打开即见经典的拖拽安装窗口：品牌背景（暗色主题 + 红边 logo + 拖拽提示）+ 应用图标 + Applications 链接 + 首次打开说明。布局由 `packaging/dmg_settings.py` 定义。两个坑已修复并固化在脚本里：dmgbuild 的 `size` 必须是 hdiutil 格式字符串（如 `"3g"`，int 会崩）；其自动尺寸估算对 1.9G 的 .app 会低估约 700M 导致写入 ENOSPC，必须显式指定。

## 安装测试（本机）

```bash
cp -R dist/ShadowBuster.app /Applications/
open /Applications/ShadowBuster.app
```

安装版自包含，无需任何环境变量。手工复验要点：

- 启动后 `~/Library/Application Support/ShadowBuster/webview_storage` 生成（冻结版存储路径生效）。
- 内置 runtime 全部产物可用离线 GPU 链路独立验证：

```bash
APP=/Applications/ShadowBuster.app/Contents/MacOS
export SB_PYTHON="$APP/runtime/env/bin/python" SB_APOLLO="$APP/runtime/Apollo" \
       SB_SOREN="$APP/runtime/Soren_src" SB_DSP="$APP/runtime/Apollo" \
       TORCH_HOME="$APP/runtime/torch_home" HF_HOME="$APP/runtime/hf_home" HF_HUB_OFFLINE=1
<dev venv 的 python> studio_backend.py -i <输入.wav> -o <输出目录>
```

外层 bundle 未做 Developer ID 签名（开源项目，无需签名；本地/同机分发可正常运行）。

## 版本号同步清单

与 [`BUILD.md`](BUILD.md) 相同（`APP_VERSION` 单一来源；`installer.iss` 校验项在 macOS 上不适用）。新增：`ShadowBuster_macos.spec` 与 `ShadowBuster.spec` 保持 `hiddenimports`/`excludes` 契约一致（`tests/test_shell_dependencies.py` 针对 Windows spec，mac spec 请同步修改）。
