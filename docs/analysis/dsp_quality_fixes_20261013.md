# DSP 修复与真实歌曲验收 2026 年 10 月 13 日

审查发现的六类问题已修正，并完成《翻开第四面墙》198.85 秒和《太虚引》228 秒的完整处理及同参数前后比较。《太虚引》原先持续限幅的问题在真实素材上复现，修复后限幅介入比例从 96.98% 降到 69.86%，中位减益从 5.40 dB 降到 0.68 dB。两首成品通过格式、长度、有限值、采样削波和独立真峰值复核。

完整回归为 **762 passed、1 skipped、29 subtests passed**。工程结果与主观音质判断分开：本次提供等响试听片段，不宣称已进行人工盲听或保证所有歌曲的听感。

## TODO

- [x] 保存修复前权威源码和同参数真实歌曲基线。
- [x] 修正六类 DSP 和测量问题，更新引擎版本与缓存身份。
- [x] 补充 23 项音质回归，验证工作分轨及缓存恢复。
- [x] 完成完整回归及 Python 编译检查。
- [x] 处理两首完整原曲并交叉测量真实成品。
- [x] 准备等响试听、峰值变化图与原始验证数据。

## 修复内容

| 问题 | 修改 | 权威源码 |
| --- | --- | --- |
| 旧 styled 持续限幅 | 与独立终段一样约束介入比例 ≤0.70、中位减益 ≤2.0 dB；不可达目标如实报告 dynamic_budget | `packaging/soren_core.py` |
| 人声分析基底错误 | 压缩/空气 EQ 差值先进入有效混音，活动、伴奏残差、平衡和重构报告共用该基底 | `apollo_scripts/vocal_adjust.py` |
| 重采样暗化高频 | 4 倍采样使用 257 抽头 Kaiser FIR，独立和旧 styled 路径共用相同设计；无减益时保留原始采样 | `audio_metrics.py`、`mastering/finalizer.py`、`packaging/soren_core.py`、`packaging/soren_original.py` |
| 检测忽略侧向能量 | 低频音乐性与人声压缩按声道功率平均检测；人声平衡的 Mid 指标保留原用途 | `apollo_scripts/premaster_hygiene.py`、`apollo_scripts/vocal_adjust.py` |
| 跨阶段分轨基底错误 | reshape 导出工作分轨，后续 guitar/synth/vocal 从该分轨计算差值；混音继续保留原分离残差 | `apollo_scripts/soundstage_reshape.py`、`studio_backend.py` |
| 响度/LRA 有偏差 | 使用与 pyloudnorm 默认表一致的数字 K 加权；LRA 采用能量平均及 −20 LU 门限，短时采样提升至 10 Hz | `audio_metrics.py` |

降噪的混音预算可能缩小单轨请求，因此工作分轨也按实际准许的系数更新。宽度预算的 STFT 掩模分别施加到各轨差值，保证它们之和仍是实际加进混音的差值。工作分轨按原始分轨的单位保存，后续仍使用累计全局缩放，并与阶段音频和报告一起缓存；热缓存恢复通过真实 DSP 调用测试。

零人声增益时，显式请求的压缩或空气 EQ 现在照常进入处理，修正了后端提前旁路这些控制的分支。

引擎身份更新为 `dsp-v5-20261013`、`shadowbuster-mastering-v4` 和 `soren_bounded_v3`。DSP 缓存自然失效，AI 准备缓存继续按模型/推理条件复用。

## 合成信号回归结果

- 31.5、63、100、1000、3000、10000 Hz 在 44.1/48 kHz 下，后备 LUFS 与 pyloudnorm 偏差低于 0.02 LU；原 31.5 Hz 的 5.54 LU 偏差已消除。
- 30 秒较响、随后 10 秒降低 15 dB 的素材，LRA 由错误的约 0 LU 变为 15 LU。
- 单个脉冲触发限幅后，远离脉冲的 20 kHz 与 1 kHz 相对增益误差低于 0.02 dB；此前约 −2.72 dB 的额外暗化已消除。
- 左右反相后的低频音乐性裁决和人声压缩与同相信号一致，保留一条联动控制曲线。
- 开启压缩/空气 EQ 后，声部重构误差低于 1e−12，报告的人声比例与独立计算相符。
- 验证降噪预算只准许 25% 请求时，工作分轨与实际混音保持一致；两次运行中后一次从缓存恢复工作分轨，后续 −6 dB 增益正确作用于已降噪信号。

新测试位于 `tests/test_dsp_quality_fixes.py`。既有两项母带测试改为验证“可达目标正常收敛、不可达目标遵守动态预算”和“输入降低 40 dB 后处理结果保持一致”，避免要求每份素材都以持续限幅方式强行达标。另修正已有 CSS 测试的末尾位置断言：后续无关按钮样式可以追加，全局焦点 outline 规则仍须存在且未被覆盖；产品 CSS 未修改。

## 两首完整歌曲的处理条件

| 曲目 | 输入 | 时长 | 母带设置 |
| --- | --- | --- | --- |
| 翻开第四面墙 | `D:/_4.Projects/_MY/造梦双子星/FINAL/11.翻开第四面墙_缩混_FINAL.wav`，48 kHz 双声道 FLOAT | 198.853 秒 | eq_only / Bright / normal |
| 太虚引 | `D:/_4.Projects/_MY/万象婆娑/1.太虚引.wav`，48 kHz 双声道 PCM16 | 228 秒 | styled / Pop / Neutral / loud / strength 0.85 |

两版使用同一原曲和显式验证参数：CUDA、Lew quality=1、guidance=1.5、htdemucs_6s 六轨，adaptive_all 降噪请求 0.6、宽度 wet=0.6/6 dB、人声压缩 0.4。额外开启 guitar gain=0.25/mud=1.25/presence=2/harsh=0.75 dB，以及 synth gain=−1/presence=1 dB，覆盖跨阶段同轨控制。低频参数取后端默认。这是用于覆盖修复点的验证设置，不是 GUI 参数完整回放。

修复前使用冻结的 `before_source` 和独立 `before_soren`，避免渲染期间源码变动混入基线。原始 AI 端点完成真实推理；后续同输入/同推理条件运行复用相同 AI 缓存，DSP 和母带使用各自版本源码。

## 成品检查结果

| 指标 | 第四面墙 修复前 | 第四面墙 修复后 | 太虚引 修复前 | 太虚引 修复后 |
| --- | --- | --- | --- | --- |
| 集成响度 LUFS | −9.154 | −9.154 | −7.144 | **−8.561** |
| FFmpeg LRA LU | 3.2 | 3.2 | 1.7 | **3.5** |
| 限幅介入比例 | 4.85% | 4.93% | **96.98%** | **69.86%** |
| 中位减益 dB | 约 0 | 约 0 | **5.40** | **0.68** |
| P95 减益 dB | 0.096 | 0.098 | 7.99 | **2.44** |
| 50 ms 块峰值 IQR dB | 1.848 | 1.856 | **0.018** | **1.785** |
| 接近 −0.5 dBFS 天花板的块比例 | 5.33% | 5.33% | **92.04%** | **57.63%** |
| 人声阶段重构最大误差 | 0.0505 | **2.22e−16** | 0.0406 | **1.11e−16** |

接近天花板定义为 50 ms 块峰值高于 −1.0 dBFS。块峰值 IQR 是第 75 与第 25 百分位的差。《太虚引》从几乎恒定的峰值恢复到有变化的分布；这证明持续钳制的工程指纹得到缓解，不等同于已经证明所有“锯齿感”的主观来源均消失。

两首修复后成品均为 **44.1 kHz、双声道、PCM24**，帧数分别为 8,769,432 和 10,054,800，与输入时长及修复前成品完全一致；所有样本有限，采样削波计数为 **0**。

| 真峰值复核 | 第四面墙 | 太虚引 |
| --- | --- | --- |
| 引擎 4 倍测量 dBTP | −0.500 | −0.499 |
| 8 倍复核 dBTP | −0.465 | −0.442 |
| 16 倍复核 dBTP | **−0.465** | **−0.442** |
| FFmpeg loudnorm input_tp dBTP | **−0.47** | **−0.45** |

16 倍复核使用带上下文的分块插值，FFmpeg 再提供独立实现的核对。两首成品都低于 −0.4 dBTP 天花板，FFmpeg 集成响度与引擎差异低于 0.05 LU。4 倍与更密插值的结果不完全相同，不能只用四倍值代表理想连续信号的精确峰值。

人声阶段的声部分离估计中，两首修复后的中位平衡目标误差分别为 0.0118 和 0.0122 dB。这是母带前、基于分离轨和伴奏残差的估计，不代表最终成品中真实独立人声的语义测量。

《太虚引》响亮目标仍为 −6.6996 LUFS，实际 −8.5612 LUFS，旁车如实标记 `below_target / dynamic_budget`。降低约 1.42 LU 是减少持续限幅的结果，没有修改预置响度目标。两首质量报告均为 warn：第四面墙仅因保守降噪未实际动作而没有降噪声明；太虚引另有动态预算阻止响度达标的提示。有限值、采样削波、真峰值和立体声相关度检查均 pass。真实素材不能证明降噪移除了噪声时，报告继续保留 unknown。

## 成品与等响试听

完整修复后成品：

- [翻开第四面墙 完整成品](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/after/fourth_wall/output/11.翻开第四面墙_缩混_FINAL_shadowbuster_v2.wav)
- [太虚引 完整成品](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/after/taixu/output/1.太虚引_shadowbuster_v2.wav)

截取同一 60–90 秒区间，加入相同 10 ms 边缘淡化，并分别匹配至 −12.00 LUFS，实测残差低于 0.01 LU。试听文件为 FLOAT WAV，避免试听增益再引入整型量化。以下文件展示修改前后差异，而非原声/成品比较。

| 曲目 | 修复前 | 修复后 |
| --- | --- | --- |
| 第四面墙 | [等响片段](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/listening/fourth_wall_before_60-90s.wav) | [等响片段](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/listening/fourth_wall_after_60-90s.wav) |
| 太虚引 | [等响片段](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/listening/taixu_before_60-90s.wav) | [等响片段](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/listening/taixu_after_60-90s.wav) |

![太虚引的块峰值变化与分布](D:/_3.AI/audio_upscale/SorenStudio/experiments/results/dsp_fix_20261013/taixu_peak_comparison.png)

图中是未做试听响度匹配的正式成品，前后峰值天花板相同；绿色线保留更多峰值变化。试听文件另行做等响匹配，不能将图中的音量差直接作为主观音质评分。

## 复现与工程检查

```powershell
& 'D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe' -m pytest tests -q --tb=short --maxfail=3 -rs --junitxml=experiments/results/dsp_fix_20261013/tests_final.xml
& 'D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe' experiments/results/dsp_fix_20261013/render_real.py after
& 'D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe' experiments/results/dsp_fix_20261013/inspect_real.py
```

最终完整回归耗时 124.51 秒，762 项通过、1 项因缺失历史实验音频而跳过、29 个子测试通过。仅保留 Matchering 上游的 SciPy 弃用警告。修改过的 Python 文件逐批通过 `py_compile`，`git diff --check` 通过；当前没有可调用的 IDE/cclsp diagnostics 工具。

原始参数、母带/质量/人声报告、FFmpeg 复核和试听信息位于 `experiments/results/dsp_fix_20261013/verification.json` 与各版本/曲目下的 `result.json`、`vocal.json`。测试结果保存在 `tests_final.xml` 和 `tests_final.log`。该目录属于本地忽略产物，原歌曲没有被覆盖。

开发态验收阶段：Soren runtime 通过现有生成器安全同步；本地 CPU/GPU 构建 staging 仅同步已修改的权威源码，旧代码已备份，外部模型和流派资源保持只读。当时未重新装配依赖、重打安装包或提交 Git。随后用户授权打包并发布 v1.6.11，已重建外壳与 CPU/CUDA 运行时、通过最终安装器及实际 EXE 的处理验收；详见 [v1.6.11 发布记录](../../packaging/release-audit/release-notes-1.6.11.md)。

## 当前验收与后续待办

2026-10-13 用户反馈：“目前试听感觉正常”。本轮定位的六类问题均已修复，完整回归无失败项，两首整曲客观检查通过；当前没有已确认、仍影响这两首成品的 DSP 缺陷。试听反馈仅记录当前已试听结果，不扩展为全部素材、实时草稿或参考匹配的验收。

- [x] 本轮 DSP 修复、完整回归与两首真实歌曲整曲检查。
- [x] 收到用户当前试听正常的反馈。
- [ ] 扩大素材覆盖，并验证长曲、最低 16 GB 设备的资源占用及用户参考匹配听感；两首歌未实际触发降噪，真实降噪效果仍需有噪素材验证。
- [ ] 使用修复后的导出结果复验实时草稿的参数方向与听感差异；草稿继续采用既定近似设计，不能替代成品响度与真峰值验收。
- [x] 重建 v1.6.11 安装包，在本机独立安装目录完成 CPU/GPU 端到端处理验收，并验证实际生成质量报告；未替代其他物理设备的验证。

质量报告中“降噪未实际动作”和“动态预算阻止响度达标”的提示属于当前策略状态，不列为未修复音质缺陷。参考匹配保护阈值的听感标定、旧母带引擎的后续迁移另见现有迁移计划，不由本轮两首歌的验收自动关闭。
