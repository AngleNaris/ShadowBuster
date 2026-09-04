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
# → packaging\out\ShadowBuster-Setup-1.5.0.exe
```

发布前必须完成三层校验：

- `build_shell.ps1` 会在构建前检查外壳 Python 可导入 `PyInstaller` 与 `numpy`，并在构建后确认 onedir 产物内存在 numpy 目录、原生 `.pyd` 扩展，且 PyInstaller warning 文件没有报告 numpy 缺失。
- `runtime_sync.ps1` 会把所有第三方依赖统一安装到 `runtime\env\Lib\site-packages`，验证 `numpy`、torch、Demucs、音频与母带依赖，并把便携解释器与 numpy 文件写入 `critical-manifest.sha256`。Apollo 的 `look2hear` 通过 `PYTHONPATH` 单独验证。
- `install_test.ps1` 默认使用 v1.5.0，静默安装后会直接调用安装目录的 `runtime\env\python.exe` 重跑导入探针；安装器退出码为 0 但探针失败时，测试仍然失败。

截图中出现 `Apollo\\New_upscale.py` 的安装包属于旧版入口，当前 v1.5.0 装配流程使用 `Apollo\\lew_upscale.py`。修复依赖后必须重新执行上述三步并重新编译安装器，不能只替换 UI 外壳或复用旧的 `stage` 目录。

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
- v1.4.1 变更（窗口紧凑化：可调宽度下限 840、自适应默认 1000×860、高度不足时内容区滚动且底部 BUSTER 按钮固定、几何记忆；声场新增「宽度」上限正方形扇形控件——高度与推子列总高对齐、满扇形张角 100°、向右拖动扩大/向左缩小且到 0 不反向——及 `--side-gain-db` 参数；高频面板新增「人声」增益旋钮，管线新增 `vocal_adjust.py` 阶段与 `vocals` bypass 名；修复盲听对比 FLOAT WAV 的 PEAK 时间戳确定性缺陷）已通过 132 项自动测试与多档几何截图验证。
- 1.4.1 最终构建：runtime 从空目录重建（`bass_enhance.py`/`drum_enhance.py`/`soundstage_reshape.py`/`vocal_adjust.py` 四份脚本源码与 stage 哈希一致，关键 manifest 已覆盖），PyInstaller 外壳 + Inno Setup 编译通过。安装器 `ShadowBuster-Setup-1.4.1.exe` 大小 `2747732575` bytes，SHA-256：`1dfb35a24ecca6c8be03c3820a0bd01bf69f6537d89004807a4c28ab81b8d2c9`。
- `packaging/install_test.ps1` 静默安装 `EXIT=0`；安装目录内四份 DSP 脚本与源码 SHA-256 完全一致，便携 Python 导入 torch 2.7.1+cu128（CUDA 12.8）/ scipy 1.18.0；从安装目录启动应用视觉确认：正方形宽度扇形与推子列等高对齐、人声旋钮、六阶段条（声场·人声）与 BUSTER 按钮完整可见、默认 1000×860 无滚动。
- v1.4.2 变更（所有图标替换为新红色 `shadowbuster.svg` logo；默认主题色改为红色 ember；宽度扇形 hover 改为单向雷达扫描（左→右匀速、末端拖尾最长、发射点渐隐、首尾淡入淡出）；宽度正方形顶边与「声场」滑轨顶边严格对齐、「宽度」铭牌移到方块外左上角与「声场」标签同行、扇形顶点贴底；母带面板流派/响度同行、EQ 独行且与声场面板等高；输出目录/参考音频合并为双列卡片——文案入框、点击选择、支持拖入（Qt 层按落点路由目录/参考/队列）；旁路关闭时面板熄灭主题色高亮并整体变暗；下拉菜单改为 fixed 浮层不再撑高页面；修复滚动区幽灵滚动条/底部空白；设置弹窗「软件更新」接入 GitHub Releases 检查（后台线程 + 结果回显 + 打开页面）。注意：仓库为私有，检查更新需仓库公开或内置 token 才能返回真实结果。
- 1.4.2 最终构建：runtime 从空目录重建（四份 DSP 脚本哈希一致、关键 manifest 覆盖），PyInstaller 外壳 + Inno Setup 6.7.3 编译通过。安装器 `ShadowBuster-Setup-1.4.2.exe` 大小 `2747639857` bytes，SHA-256：`fdfbede8d399a2f037760d91695be098086f0277431819f805120bff2aa34cb6`。
- 1.4.2 图标修复：`ui/logo.ico` 手工写为 7 尺寸 PNG 条目 ICO（16×16 … 256×256，37,926 bytes），重建 PyInstaller 外壳并以 PE 资源解析验证 `ShadowBuster.exe` 内嵌全部 7 档（256×256 为 PNG 压缩条目）；安装包已按此重新编译，上一条哈希即为新构建。
- v1.5.0 变更：**分发方式重构**——安装包改为 CPU 版瘦身运行时（`runtime_sync.ps1 -Flavor cpu`，torch 2.7.1+cpu，整包约 1GB 可直接上传 GitHub Release，绕过单资产 2GiB 上限）；GPU 加速改为应用内「设置 → GPU 环境」下载 CUDA 运行时（`runtime_sync.ps1 -Flavor gpu` → `build_gpu_env.ps1` 产出 `gpu-env-<ver>.zip` 分卷 + 清单），下载引擎（`gpu_env.py`：清单校验/断点续传/分卷 SHA-256/组装/整包校验/解压/原子切换）安装到 `%LOCALAPPDATA%\ShadowBuster\runtime-gpu\env`，`_resolve_runtime()` 优先采用（仅覆盖解释器，Apollo/Soren/权重仍读安装目录），重启应用后 CUDA 生效。
- 1.5.0 最终构建：CPU runtime 从空目录重建（`-Flavor cpu`，torch/torchaudio 2.7.1+cpu，DSP 脚本与 critical-manifest 校验通过），PyInstaller 外壳 + Inno Setup 编译通过。安装器 `ShadowBuster-Setup-1.5.0.exe` 大小 `796272797` bytes，SHA-256：`f07b2bde3183d66c372bc686fe7f119aeb2850f1c94fc319e03c8f8e2d597a8b`。
- 1.5.0 发布修正：`releases/tags/{tag}/assets` 并非 GitHub 合法 REST 路由（匿名恒 404），清单拉取改为优先 `releases/latest` 的 id 型 `assets_url`、仅旧版本回退 `tags/{tag}` 对象路由；安装包已按修复后外壳重建并替换 release 资产（上一条哈希即新构建）。
- 1.5.0 GPU 环境包：`build_gpu_env.ps1` 产出 `gpu-env-1.5.0.zip`（SHA-256 `57be784d2084e78b008c401025cf950e67de290eafa6c66885bc0a3f5050d714`，3,619,194,850 bytes）+ 3 个分卷 + `gpu-env-1.5.0.json` 清单，全部上传 v1.5.0 Release；最终 ZIP central directory 已确认包含便携 Python、NumPy `__init__.py` 与原生 `.pyd`、soundfile、CUDA torch/torchaudio metadata，下载后会运行完整依赖探针并原子切换用户级 runtime。
- 测试：180 passed, 13 subtests passed（含新增 `tests/test_gpu_env.py`：清单校验/续传/组装/哈希/解压/切换/旧环境复用，无网络依赖）。
