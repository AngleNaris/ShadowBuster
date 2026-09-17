# Soren 母带引擎能力清单（capability manifest，2026-09-17）

开发规格 P4-01：验证风格、参考、响度、输出采样率、True Peak、限幅、dither 的真实控制方式。本文不靠猜测命令行含义——每条能力都给出黑盒探针实测数字或引擎源码行号。

- 引擎入口：`dev_runtime/Soren_src/core_decrypted.py`（与 `packaging/soren_core.py` 逐字节一致，filecmp 校验通过）；styled 子引擎 `soren_original.py`（与 `packaging/soren_original.py` 一致）。`model@`/`profiles@`/`secured_genres@` junction 均可解析。
- 调用方式与 `studio_backend.stage_soren`（studio_backend.py:952-968）完全一致：cwd=PYTHONPATH=soren_dir，子进程运行。
- 探针工具：`tools/probe_soren_capabilities.py`（可重跑，输出到仓库外 `_e2e_tmp/soren_capability_probe/`）。原始数据：`_e2e_tmp/soren_capability_probe/soren_capability_probe_20260917_125235.json`；机器可读清单：`docs/analysis/soren_capability_manifest_20260917.json`。
- 探针素材：输入 9.16 s 44.1k 立体声 PCM16（`_e2e_tmp/inference_samples/set_2/input.wav`，实测 −7.83 LUFS / −0.08 dBTP）；参考轨 200 s PCM16（`_e2e_tmp/pop_ref.wav`，实测 −11.76 LUFS）。共 9 次引擎运行（8 成功 + 1 预期失败）。
- 状态标记：**实测** = 本次黑盒探针测得；**代码** = 引擎源码确认；**假设** = 未验证。

## 结论

1. **响度目标精确可信（实测）**：profile JSON 基准 + 响度档偏移的预测值与实测综合 LUFS 在所有达标运行中误差 ≤0.002 LU。`loud` 档在本素材上差 −0.38 LU 未达标（见下），且统计如实标记 `below_target`/`dynamic_budget`——与 docs/CLI.md 的声明一致，无矛盾。
2. **True Peak 天花板 −0.4 dBTP 成立（实测）**：9 次运行最大实测 TP −0.405 dBTP（repo 表，引擎自测 −0.420），无任何一次越界，零 clipping 采样。styled/loud 素材按设计贴着 −0.4 运行（−0.42 ~ −0.47），quiet 目标（soft / Orchestral / 参考）远离天花板。**不存在输出削波超限问题**；−1 dBTP 以上是 styled/loud 的有意行为，天花板是 −0.4 而非 −1。
3. **24-bit TPDF dither 确认施加（代码）**：`core_decrypted.py:114-120`（two-LSB peak-to-peak TPDF，`lsb=1/2^23`），在 `process_transparent` 的 TP 安全裁剪之后、PCM24 量化之前施加（:938），dither 后重新校验 TP ≤ −0.4 dBTP（:939-941），写出前对 PCM24 回读再测一次并硬性把关（:1493-1504）。三种模式共用该终段，全部带 dither。
4. **参考模式优先于流派，且参考直接决定响度目标（实测）**：`--reference` 不带 `--genre` 时，目标 LUFS = 实测参考综合响度 + 响度档偏移（实测 target −11.7584 = pop_ref 实测 −11.7584 + 0.0），Pop profile（−9.20）完全未参与。频谱风格目标同样换成参考音频本身。
5. **`--style-blend` 是逐处理器处理强度，不是干湿混合（实测）**：0.85 vs 0.35 两次运行，旁车中每个处理器额度随强度线性缩放（RMS 匹配 0.2125=0.25×0.85 vs 0.0875=0.25×0.35；频谱下限 −0.8925=−1.05×0.85 vs −0.3675=−1.05×0.35；宽度 0.2187 vs 0.0907），输出 TP 随之不同（−0.466 vs −0.886 dBTP）而 LUFS 都精确压在目标上。
6. **限幅器**：单一 stereo-linked lookahead limiter（5 ms lookahead / 1 ms attack / 天花板 −0.5 dBFS @4x 过采样域 / 越界即 RuntimeError）。release 实测 soft/dynamic/normal = 150 ms，**loud = 80 ms**（旁车 `limiter_release_ms`），与 CLI.md:20 一致。源码里的多级限幅器（`multi_stage_limiter` 等）是死代码，生产路径不走。
7. **输出格式（实测）**：所有运行一律 PCM_24 / 44100 Hz / 立体声，时长与输入逐样本一致（9.160 s）。引擎硬性要求 44.1 kHz 输入（:1435-1439 抛 ValueError），非 44.1k 由后端先行重采样。
8. **流派名必须精确匹配 profile 文件名（实测）**：`--genre Orchestra` 1.3 s 内 rc=1 失败（`FileNotFoundError: ...Orchestra_profile.json`），有效集合是 `Orchestral` 等 10 个 profile 名，无模糊匹配。
9. **与仓库现有文档（docs/CLI.md、mastering_strength_20260911.md 等）无矛盾**：loud +2.5 LU、release 80/150 ms、blend=处理强度、PCM24+TPDF dither、旁车统计语义、eq_only 忽略强度，全部得到实测/代码证实。

## 证据表（黑盒探针，9 次运行）

目标预测 = profile JSON `lufs`（或参考实测响度）+ 响度档偏移 {soft −3.10, dynamic −1.94, normal 0.0, loud +2.50}（core_decrypted.py:834-845）。TP 列为 repo `audio_metrics`（4× resample_poly kaiser 8.6）测量；括号内为引擎旁车自测值（resample_poly 默认窗，dither 后）。

| # | 命令 | 目标预测 LUFS | 实测 LUFS | 误差 | TP dBTP | release | 达标 |
|---|---|---:|---:|---:|---|---:|---|
| 1 | `--style-mode off --loudness normal` | −9.1996 | −9.198 | +0.002 | −1.291 (−0.617) | 150 | met |
| 2 | `--style-mode styled --genre Pop --style-blend 0.85` | −9.1996 | −9.199 | +0.001 | −0.466 (−0.471) | 150 | met |
| 3 | `--style-mode styled --genre Pop --style-blend 0.35` | −9.1996 | −9.198 | +0.002 | −0.886 (−0.694) | 150 | met |
| 4 | `--style-mode eq_only --eq-profile Bright` | −9.1996 | −9.198 | +0.002 | −0.762 (−0.757) | 150 | met |
| 5 | `--style-mode off --loudness soft` | −12.2996 | −12.298 | +0.002 | −4.391 (−3.717) | 150 | met |
| 6 | `--style-mode off --loudness loud` | −6.6996 | **−7.081** | **−0.381** | −0.405 (−0.420) | **80** | **below_target** |
| 7 | `--reference pop_ref.wav --style-mode styled --style-blend 0.85` | −11.7584（参考响度+0） | −11.757 | +0.002 | −3.008 (−3.001) | 150 | met |
| 8 | `--style-mode styled --genre Orchestral --style-blend 0.85` | −19.8783 | −19.877 | +0.001 | −11.187 (−11.196) | 150 | met |
| 9 | `--genre Orchestra`（无效名） | — | — | — | — | — | **失败 rc=1，1.3 s** |

全部输出：PCM_24 / 44100 Hz / 2 ch / 9.160 s / clipping_samples=0。运行 1–8 总耗时约 48 s（CPU）。

### loud 未达标细节（运行 6）

- 旁车：`stop_reason=dynamic_budget`、`dynamic_budget_limited=true`、13 次迭代、`safety_trim_db=−0.036`。
- 限幅器：max GR 8.82 dB、p95 7.99 dB——p95 预算（loud 档 8 dB，core_decrypted.py:883-890）是实际绑定约束；TP 被钉在 −0.42 dBTP。
- 引擎行为符合设计：二分搜索 drive，目标不可安全达成时保留最安全候选并如实报告，不无限压缩。CLI.md:26「Pop 的目标约 −6.70 LUFS……超过动态预算优先保留安全输出并标记未达标」实测重现。

### blend 强度缩放证据（运行 2 vs 3，旁车 `style_processing`）

| 处理器 | blend 0.85 | blend 0.35 | 代码缩放关系 |
|---|---:|---:|---|
| rms_match_mid/side_db | 0.2125 | 0.0875 | ±0.25 × strength（soren_original.py:253-254） |
| spectral_mid_min_db | −0.8925 | −0.3675 | 曲线 = clip(spec×0.2×strength, ±1.25×strength)（:325-326） |
| level_correction_db | 0.1170 | 0.0490 | clip(req×0.1×s, ±0.25×s)（:380） |
| stereo_widening_db | 0.2187 | 0.0907 | max_adjustment = 0.03 × strength（:438） |
| style_width_change_db | 0.0371 | 0.0155 | ±0.35 × strength（:771） |

## 能力逐项

| 能力 | 状态 | 证据 |
|---|---|---|
| style_mode 三态分发（styled/off/eq_only） | 实测 | 三种模式全部跑通；off 无频谱处理（旁车 `spectral_processing=false`）；分发点 core_decrypted.py:1012-1024。三种模式共用同一终段（响度二分 + 限幅 + TP 裁剪 + dither），响度/TP 行为一致，只有频谱内容不同 |
| style_blend 语义 = 处理强度 | 实测+代码 | 见上表；无任何干湿波形混合（CLI.md:20 成立） |
| 参考优先于流派；参考设定响度目标 | 实测+代码 | 运行 7 target = 参考实测 LUFS + 偏移（core_decrypted.py:1551-1556 CLI 优先级、:1470-1479 加载、:994-997 目标合成） |
| 响度目标映射 | 实测 | 见证据表；偏移表 core_decrypted.py:834-845；10 个 profile 的 `lufs` 基准逐一读取核对（Pop −9.1996 / EDM −9.6082 / Rock −9.4172 / Dance −10.3272 / Hiphop −11.6370 / Ambient −13.8848 / Chillout −13.5136 / Orchestral −19.8783 / Speech −16.4962 / Piano −17.2159） |
| 输出采样率/位深/subtype | 实测+代码 | 全部 PCM_24 / 44.1k / 2ch / 时长不变；写入 core_decrypted.py:112；44.1k 硬门槛 :1435-1439 |
| True Peak 天花板 −0.4 dBTP | 实测+代码 | 最大实测 −0.405 dBTP；Config :70、dither 后校验 :939-941、PCM24 回读把关 :1493-1504（越界 RuntimeError） |
| 限幅器特征 | 实测+代码 | stereo-linked lookahead：5 ms lookahead / 1 ms attack / 天花板 −0.5 dBFS @176.4 kHz 域（:752-802）；release 150 ms（loud 80 ms，:886-887）；旁车输出 GR 统计（max/p50/p95/active_fraction） |
| 24-bit dither | 代码 | TPDF two-LSB p-p，`lsb=1/2^23`，:114-120；在 process_transparent:938 施加（全模式），dither 后 TP 复检 :939-941。写入文件层面无法直接区分 dither 噪声，以代码路径为准 |
| `--upstream-delta` | 代码 | CLI :1528-1546（JSON 需含 freqs/mid_db/side_db/rms_mid/rms_side）；styled 匹配目标 = 流派曲线 × 上游频谱 delta（soren_original.py:312-315），delta 的 M/S 宽带 RMS 比缩放参考电平（:756-759）。探针未实测（预算内未列） |
| `--eq-profile` | 实测+代码 | Bright 曲线生效（运行 4 TP/LUFS 与运行 1 可区分）；曲线唯一定义于 soren_original.py:944-972（Warm/Bright/Fusion，RBJ 最小相位），eq_only 经 core_decrypted.py:1421-1424 复用同一函数，与 styled 永不漂移；eq_only 忽略 style-blend |
| `--lowpass-cutoff` | 代码 | 默认 None = 不施加低通（soren_original.py:778-779 仅 `explicit_lowpass` 时执行 butter(10, cutoff)）；core_decrypted.py:1525-1550、:988-990。探针未实测（预算内未列） |
| 流派名校验 | 实测 | 运行 9：无效名在任何 DSP 前快速失败（FileNotFoundError），rc=1 |
| styled 输入保护 | 实测+代码 | 输入 TP +0.72 dBTP → 预增益 −0.8245 dB（压到 −0.1 dBTP，core_decrypted.py:969-974；旁车 `input_protection`），只衰减不增益 |

## 已知限制

- 探针只覆盖一个 9.2 s 立体声 44.1k PCM16 素材 + 一个 Pop 参考；单声道/静音/非 44.1k 行为仅有代码证据（近单声道跳过 side 匹配 soren_original.py:270-273、静音输入拒绝 core_decrypted.py:970-971、非 44.1k 拒绝 :1435-1439）。
- `--lowpass-cutoff` 与 `--upstream-delta` 仅代码级验证，未进探针矩阵（时间预算）。
- TP 测量方法差异：引擎用 resample_poly 默认窗（kaiser 5.0）在 float64 上测，repo 表用 kaiser 8.6 + padtype=line，本素材上差异可达 ~0.7 dB（运行 1：−1.291 vs −0.617）。引擎自己的 PCM24 回读闸门才是其 −0.4 dBTP 承诺的绑定方；repo 测量最坏 −0.405 dBTP 也未越界。
- `--preview`（30 s 截断，:1446-1454）存在于引擎 CLI，但 stage_soren 从不发送；未探针。
- core_decrypted.py:1027-1273 的旧版 styled 全流程及旧多级限幅器为不可达死代码，随发行文件携带但不影响行为；读代码审计时不要把它们当成现行路径。
- 本清单全部为自动指标与代码证据，不含听感验收；不构成音质宣称。
