# 第 4 批次：htdemucs_6s 六轨能力评估（20260913）

## 结论（能力探测 + 小规模离线评估）

**六轨模型在当前生产运行时技术可用，打包成本很小；但"吉他可独立调整"的产品宣称仍被听感验证门槛拦住。** 本轮只回答可用性、资源成本和结构性风险，不做音质宣称。

## 探测环境与方式

- 运行时解释器：`D:/_3.AI/audio_upscale/UniverSR/.venv`（与 `studio_backend.PYTHON` 一致），demucs 4.1.0、torch 2.7.1+cu128。
- GPU：RTX 5060 Ti 16GB。评估脚本：`experiments/eval_htdemucs_6s.py`（进程内计时、显存峰值、残差、分轨能量与交叉相关）。
- 素材：两首真实 AI 歌曲（《5.愚蠢人类观察家》《6.想象在彼方》）的 Lew 输出中段各 60 秒；四轨基线与六轨用完全相同片段。
- 评估产物：`.zcode/eval/six-stem-20260913/`（分轨 WAV + results.json，含 GPU 与 CPU 两轮）。该目录不入库、不上传。

## 权重分发链（重要修正）

- demucs 4.1.0 实际从 **HuggingFace** 解析 `htdemucs_6s`（`models--adefossez--HTDemucs-6s`，diffq 量化 safetensors），不是 `remote/files.txt` 里的 fbaipublicfiles 直链；`HT_HOME/torch_home` 路径已过时。
- 生产离线模式（`HF_HOME` 指向预置目录 + `HF_HUB_OFFLINE=1`）对本机缓存验证可用；四轨权重已预置进 `packaging/stage/runtime/hf_home`，同一路径加一份 `models--adefossez--HTDemucs-6s` 即可支持六轨。
- 打包增量：**+53 MB**（HTDemucs-6s HF 量化缓存；对照四轨 81 MB）。不是此前按 .th 估的 ~280 MB。

## 模型资源对比（同一 60 秒片段，GPU，热缓存）

| 模型 | 参数量 | GPU 峰值显存 | 60s 片段耗时 | 输出 |
|---|---:|---:|---:|---|
| htdemucs（四轨，生产基线） | 42.0M | 0.65 GB | 2.6 s | drums/bass/other/vocals |
| htdemucs_6s（六轨） | 27.4M | 0.75 GB | 1.4 s | + guitar/piano |

六轨更小（更激进 diffq 量化）因此反而更快；显存多 0.1 GB，可忽略。CPU 数据见下节。

## 质量信号（仅测量，不是听感结论）

两首歌一致的观察：

1. **guitar 轨有实质内容**：song1 −10.2 dB、song2 −16.8 dB；四轨里这些内容原本大致都在 other（song1 other −8.5→−23.1 dB）。
2. **piano 轨接近空或很小**：song1 −61.1 dB（基本无钢琴）、song2 −35.4 dB。空轨是正常输出，不是故障； guitar/piano 处理阶段必须允许空轨且恒等通过。
3. **混合残差变大**：四轨残差 −29.0/−31.1 dB → 六轨 −21.3/−21.2 dB。六轨分解对混音的解释更不完整。在现有 delta-add 架构下残差保留在混音里，结构性安全，但意味着六轨 stem 的"处理差值"覆盖面变小，且 **4 轨与 6 轨的 stem 产物不能混用在同一缓存身份里**（拓扑变化必须进缓存 identity）。
4. **guitar 与 other/vocals/piano 的高频相关 0.10–0.27**（低相关）。低相关只说明内容可区分，不能证明"分出来的是吉他"——合成器/弦乐被标成 guitar 的风险只能靠听测排除。

## CPU 数据（song1 前 20 秒片段，`eval/six-stem-20260913/cpu-song1/results.json`）

| 模型 | 20s 片段耗时 | 残差 |
|---|---:|---:|
| htdemucs | 6.9 s | −30.6 dB |
| htdemucs_6s | 4.3 s | −23.3 dB |

六轨在 CPU 上同样更快（模型更小）。按此外推：整首 ~204 秒歌曲 CPU 分离四轨约 70 秒、六轨约 44 秒；六轨对 CPU 用户也是降本而非成本。另注意前 20 秒片段的 guitar/other 高频相关 0.45，高于中段——分段内容不同会显著改变该指标，进一步说明它只能当筛查信号，不能当隔离质量证明。

## 发布门槛（未过）

- 未做 ABX / 专业监听：guitar 轨是否"像吉他"、piano 空轨是否引入伪影、六轨残差变化是否可闻，全部未验证。
- 仅 2 首 × 60 秒，不是跨风格基准集。
- `stage_demucs` 仍硬编码 `-n htdemucs`；六轨接入需要：模型参数进缓存 identity、stem 拓扑驱动的阶段路由、guitar/piano 独立 delta-add 阶段（默认中性）、runtime_sync hf_home 清单增量、CPU/降级路径与产品文案。这些属于第 4 批次的实施工作，未开始。
- 若最终决定不引入六轨：产品边界维持"vocals/drums/bass 可调、other 只能整体处理、不提供吉他独立控制"，四轨路径不变。

## 实施记录（20260913，评估通过后推进）

产品决策：推进六轨 opt-in 实施。已落地：

- `apollo_scripts/stem_enhance.py`：guitar/piano 各自的有界 delta-add 增强阶段。五个控制（gain −6..6 / mud 300Hz 削减 / presence 3kHz 高架 / harsh 7kHz 收敛 / width side 增益，后四者 0..6）全部默认 0 → 位级透传；滤波用与 bass_enhance 同族的模拟原型 shelf（filtfilt 零相位、恒稳定）+ RBJ peaking；缺 stem 透传并报告 unavailable/missing_stem；峰值保护与其他阶段同约定。
- `studio_backend.py`：`stage_demucs` 增加 `model` 参数（默认 htdemucs，命令与旧行为一致）；`htdemucs_6s` 时 stem 产物目录换为 `stems/htdemucs_6s/<stem>`，并在 reshape 之后、vocals 之前路由 guitar→piano 两个默认中性阶段；模型名同时进入缓存身份（4/6 轨产物不混用）与参考人声分离；模型与控制参数在任何推理前校验；四个分轨消费阶段全旁路时附加阶段与分离一起跳过；六轨运行才在质量报告 `processing` 记录 `demucs_model` 与分轨控制，`stages.guitar/piano` 承载诊断 extras。
- CLI `--demucs-model` 与每分轨五个 `--guitar-*` / `--piano-*` 旗标（解析期拒绝越界）；GUI 桥接以隐藏参数转发，无可见 UI 变更。
- `packaging/runtime_sync.ps1`：构建期加 `-n htdemucs_6s` 权重种子（HF 量化 +53MB）、离线加载验证、`stem_enhance.py` 进断言/复制/manifest。
- 测试：`tests/test_stem_enhance.py`（9 项 DSP 合同）+ `tests/test_six_stem_pipeline.py`（9 项路由/缓存/CLI），全量回归 **503 passed, 1 skipped, 29 subtests**。
- 听感门槛仍未过：本实施只保证结构安全与中性默认，不改变"六轨 ≠ 吉他隔离"的边界；guitar/piano 旋钮的效果需用户以真实歌曲试听后再决定是否进入 UI 可见面。

## 拓扑修订（20260913，评估试听后）

用户试听确认分离可用后提出：多数 AI 歌曲没有钢琴，独立 piano 轨常为空。产品拓扑据此修订：

- **synth（合成器/键盘层）= other + piano 求和**，作为一条独立处理组（合成器、铺底、效果器、钢琴等和声性内容），替代原 piano 独立阶段；guitar 保持独立。
- `stem_enhance.py` 增加 `--stem2` 双源合并（组 = 两轨精确相加后再处理）；缺任一源透传并报告 `missing`；源长度不匹配拒绝。
- 后端 `stage_piano` → `stage_synth`（`STEM_SOURCES = {"guitar": ("guitar",), "synth": ("other", "piano")}`），CLI `--piano-*` → `--synth-*`，质量报告 `stages.synth` 承载诊断（含 `sources`）。
- 全量回归：**506 passed, 1 skipped, 29 subtests**。
