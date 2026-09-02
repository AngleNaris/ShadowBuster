# ShadowBuster — AI 音乐修复与母带工坊

面向 AI 生成音乐（Suno 等）与有损音频的一键式修复 + 母带桌面应用。信号链六阶段：

```
Lew 高频重建 → Demucs 四轨分离 → 贝斯增强 → 鼓增强 → 声场重塑/高频降噪 → Soren 母带
```

- **Lew 高频重建**：Apollo 架构超分辨率模型重绘丢失高频（质量档位控制分块/重叠，重建强度 0–2 为 wet/dry 混合）
- **Demucs 四轨分离**：htdemucs 输出 vocals/drums/bass/other（求和≈原曲），为分轨处理提供基础
- **贝斯增强**：30Hz sub 包裹感、<200Hz 谐波饱和（小音箱可闻低频）、RMS 门控
- **鼓增强**：90Hz 鼓身 punch + 全频段瞬态强调，只作用于 drums 轨（实测 kick 起音 ~100% 落在该轨），快 attack 门控
- **声场重塑/高频降噪**：以原混音为基底，对 drums/other 的 side 做宽带增益；独立干湿比控制拓宽强度，并在 other 轨 ≥10kHz 自动估计稳态噪声地板、按强度衰减沙沙伪影，保留高于噪声地板的音乐瞬态
- **Soren 母带**：按曲风档案/参考曲做 RMS 匹配、频谱匹配 EQ、Mid/Side 处理、响度定版（lookahead limiter + 真峰值保护，24bit dither 输出）

界面为 PySide6 + QtWebEngine（DAW 插件质感），支持深/浅色模式、五种预设 + 自定义主题色（整套中性色随色相派生）、文件拖放导入、参数持久化。

## 目录结构

```
main.py               UI 外壳入口（窗口、桥接、拖放、原生标题栏主题）
studio_backend.py     管线编排（子进程调度/进度/取消）+ APP_VERSION 单一来源
ui/                   前端（index.html / app.js / style.css，CSS 变量主题系统）
tests/                pytest 测试（管线路由、守恒门、版本同步等）
experiments/          音频实验脚本（人声端点、残差对比等，不属于安装包）
packaging/            打包：build_shell.ps1 / runtime_sync.ps1 / installer.iss / DEPLOY.md
visual/               视觉规范材料
```

## 重建开发环境

应用本体（本仓库）与 AI 推理环境是分离的。开发一台新机器需要：

### 1. Python（UI 外壳）

Python 3.12+，创建 venv 后安装：

```
pip install PySide6 PyInstaller pytest numpy scipy soundfile
```

### 2. 外部工具链（AI 环境，不在本仓库）

推理子进程依赖三个外部目录（路径可用环境变量覆盖，见下）：

| 目录 | 内容 | 来源 |
|---|---|---|
| `Apollo/` | 上游 [JusperLee/Apollo](https://github.com/JusperLee/Apollo) 源码（`lew_upscale.py`、`look2hear/` 等）+ `ckpts/` Lew 权重 | 上游仓库 + 权重另行获取 |
| `Soren_src/` | Soren 母带链：`core_decrypted.py`、`model/`、`profiles/`、`secured_genres/` | 闭源组件，自行持有 |
| ffmpeg | `ffmpeg.exe` 加入 PATH | 官方构建 |

本仓库的 `apollo_scripts/` 保存 ShadowBuster 自己维护的 `bass_enhance.py`（贝斯增强）、
`drum_enhance.py`（鼓增强）和 `soundstage_reshape.py`（宽带声场重塑与 ≥10kHz 自动噪声地板降噪）。
构建时 `runtime_sync.ps1` 将这些应用源码与外部 Apollo AI 工具链组合进运行时；模型、AI 环境和上游 Apollo 源码不纳入本仓库。

### 3. 环境变量（开发机布局探测）

`studio_backend._resolve_runtime()` 的探测顺序：

1. exe 同级 `runtime/`（安装后的布局）；
2. `SB_ASSETS` 指向装配好的 runtime 根；
3. 开发回退：`SB_PYTHON`（带 torch 的解释器）、`SB_APOLLO`、`SB_SOREN`、`SB_FFMPEG`；
   未设置时使用开发机默认路径（`UniverSR/.venv`、`D:/_3.AI/audio_upscale/{Apollo,Soren_src}`），
   新机器请务必设置这四个变量。

推理运行时（torch cu128 + demucs + librosa/numba/statsmodels 等）的精确依赖清单见
`packaging/runtime_sync.ps1`，开发venv 可按同一清单安装（或直接先跑一次 runtime_sync
复用其产物）。

### 4. 运行与测试

```
python main.py          # 或 start.bat
python -m pytest tests/ -q
```

## 打包构建（Windows）

```
powershell -File packaging\build_shell.ps1     # 1) PyInstaller UI 外壳 + 体积裁剪
powershell -File packaging\runtime_sync.ps1    # 2) 装配 AI runtime（含权重离线预置）
iscc packaging\installer.iss                   # 3) Inno Setup 6 出安装包
```

产物：`packaging/out/ShadowBuster-Setup-<版本>.exe`（外壳 ~510MB + runtime ~6.3GB）。
细节与决策记录见 [packaging/DEPLOY.md](packaging/DEPLOY.md)。

版本号单一来源：`studio_backend.APP_VERSION`，需与 `packaging/installer.iss` 的
`MyAppVersion` 保持一致（`tests/test_app_version.py` 校验）。
