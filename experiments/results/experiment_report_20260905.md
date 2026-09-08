# 实际素材基线与人声路由修复实验（2026-09-05）

## 执行范围

- 源素材目录 `D:/_4.Projects/_MY/中二病晚期患者` 仅读取，未写回、未覆盖。
- 既有算法/UI 文件未修改；新增工具 `experiments/measure_baseline.py`。
- 未试听，也不对听感作结论。

## 执行命令

```bash
python -m experiments.measure_baseline --source 'D:/_4.Projects/_MY/中二病晚期患者' --out experiments/results/baseline_20260905.json
ffmpeg -y -v error -ss 0 -t 30 -i 'D:/_4.Projects/_MY/中二病晚期患者/1.假想末日.wav' -ar 44100 -ac 2 -c:a pcm_f32le experiments/results/real_20260905/clip30.wav
TORCH_HOME=... HF_HOME=... HF_HUB_OFFLINE=1 D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe -m demucs --float32 --clip-mode=none -n htdemucs -o experiments/results/real_20260905/demucs experiments/results/real_20260905/clip30.wav
D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe experiments/results/real_20260905/run_variants.py
```

## 运行环境/可用性

- pytest Python：`D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe`；该环境为 torch 2.7.1+cu128，CUDA 可用，demucs、pyloudnorm、librosa、soundfile 可导入。
- 系统 Python 3.14 有 numpy/scipy/soundfile/pyloudnorm，但无 pytest、torch、demucs。
- 已确认本地 Lew checkpoint：`D:/_3.AI/audio_upscale/Apollo/ckpts/lew/apollo_model_uni.ckpt`。本次为避免不必要耗时，真实片段实验运行了本地 Demucs；未下载模型、未联网。
- 本地 Soren 模型/Pop profile 可用，实际母带运行成功。日志有 sklearn 版本警告（模型来自 1.5.1，当前 1.9.0），不是阻塞。

## 基线测量

扫描到 12 个 WAV（六首原始/ShadowBuster 配对）。每个文件记录：integrated LUFS、4x true peak、sample peak、M/S side-mid ratio/dB、立体声相关、RMS、crest、0–4/4–8/8–12/12–16/16–20/20–22 kHz 相对能量、谱质心、采样率/通道/帧数/时长及路径。完整 JSON：

`D:/_3.AI/audio_upscale/SorenStudio/experiments/results/baseline_20260905.json`

## 实际片段实验

输入为第一首真实素材开头 30 秒；Demucs htdemucs 离线成功。对同一分离结果做 vocal 0/+4/+5 dB、真实声场 broadband、真实 Soren Pop/normal/Neutral 母带。输出与指标：

`D:/_3.AI/audio_upscale/SorenStudio/experiments/results/real_20260905/`

| 变体 | LUFS | TP dBTP | side/mid dB |
|---|---:|---:|---:|
| vocal +0 reshape | -17.66 | -4.73 | -12.83 |
| vocal +4 reshape | -16.53 | -4.73 | -13.26 |
| vocal +5 reshape | -16.12 | -3.99 | -13.40 |
| vocal +0 master | -11.66 | -0.47 | -12.92 |
| vocal +4 master | -10.64 | -0.47 | -13.29 |
| vocal +5 master | -10.88 | -0.48 | -13.38 |

母带三组均真实执行，输出为 PCM24；真实声场阶段 mono fold-down 能量变化约 0 dB、相关 1.0000（日志）。

## 路由说明/阻塞

旧版 `run_pipeline` 的代码路径在 `studio_backend.py:591-598`：vocal gain=0 时 `vocal_out` 位级 copy `drum_out`；非 0 时调用 `vocal_adjust.py`。因此不能把旧路由的 +4/+5 与 0 声场输入称为完全同样；它们共享同一 drum 基底，但 +4/+5 增加人声分离残差。主 agent 已修复并通过真实 DSP 的 16 组合路由测试；本报告保留的变体用于实际 DSP 基线，不声称旧路由等价。

本次未执行 Lew，以免将未要求的超分阶段混入声场/人声变量；其 checkpoint 已确认存在。未试听。
