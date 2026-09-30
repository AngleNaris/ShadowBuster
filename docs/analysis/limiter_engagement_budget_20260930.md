# 响度母带介入频率预算（2026-09-30）

## TODO

- [x] 定量定位"类削波 / 锯齿感"来源，并排除采样域缺陷
- [x] `finalize` 可行性判据加入限制器介入频率与中位减益预算
- [x] 母带旁车记录两项新预算
- [x] `DSP_ENGINE_VERSION` → `dsp-v4-20260930`，mastering `ENGINE_VERSION` → v3
- [x] 回归测试：闸门生效 / 关掉闸门复现缺陷 / 目标可达时不干预
- [ ] 下次发布执行 `packaging/runtime_sync.*`，把 new `mastering/finalizer.py` 落到运行时
- [ ] 用《太虚引》全曲复跑，确认整曲 50 ms 块峰值钉死比例由 83.9% 回落

## 起因

用户报告 `1.太虚引_shadowbuster.wav`（响亮 preset）"感觉存在类似削波的效果，一些地方听着有明显的
锯齿感"；自行复渲的 v2（常规）、v3（常规）分别为"不太明显""有微弱"。

先按假设逐项排除采样域缺陷（三版成品 + 48k 源同口径测量）：

| 假设 | 实测 | 结论 |
| --- | --- | --- |
| 样本削波 | `sample_clipping: 0`，真峰 −0.438 dBTP，≥−0.004 dBFS 样本 0 个 | 否 |
| 平顶（连续同值） | ≥3 连续同值样本 run：0（三版皆无） | 否 |
| 限制器台阶 / 爆裂 | 1 ms 峰值包络"钉死在天花板"的 ≥8 ms 平顶 run：0；最长 4 ms | 否 |
| 宽带点击 | v1/v2/v3/源 的 HF(14–22k)/MID 突峰窗口时刻完全重合（0.0、55.7、68.5、86.2、120.1、121.0、124.3 s） | 均为音乐瞬态，非缺陷 |
| 饱和混叠 | `saturation_wet` 先限带 <500 Hz，再 4× 过采样 tanh → 抗混叠下采样（`apollo_scripts/bass_enhance.py:94-108`） | 无可听混叠 |
| 滤波预振铃 | `widen()` 全曲 FFT side mask 与 hygiene 1025-tap / 20 kHz FIR 的脉冲响应，能量落在 ±20 ms 之外的比例 0.0% | 否 |
| 高频变暗致硬 | 16 k −2.67 / −1.32 / −2.64 dB、20 k −5.47 / −4.08 / −5.32 dB 三版同向 | 共享链特征，非 preset |

真正定位到限制器，指纹是**块峰值被钉死在同一天花板值上**：

| | 50 ms 块峰值落在 −0.5 dBFS 天花板 0.5 dB 内 | 块峰值 IQR |
| --- | --- | --- |
| 源（48 k） | 0.0% | 0.132（≈1.3 dB） |
| **v1 响亮** | **83.9%** | **0.0047（≈0.04 dB）** |
| v2 常规 | 18.9% | 0.268 |
| v3 常规 | 25.5% | 0.261 |

v1 限制器：`active_fraction 0.960`、`gain_reduction_p50 5.09 dB`、`p95 7.99`（预算 8.0）、
`max 10.49`、`release 80 ms`、`applied_gain 10.27 dB`；整曲 LRA 5.66 → 2.28 LU，
全局 crest 13.22 → 8.87 dB。用产品自身的 `linked_limiter` / `_release_envelope` 重建增益包络
（复现统计逐位一致：p50 2.38 / max 5.13 / active 0.967）可见增益以约 **41 次/秒、~1 ms 下落、
80 ms 回升**的方式调制整曲 —— 一个锯齿形增益波。"像削波"来自峰值被恒定钳住，而不是样本折叠，
所以质量报告的 `sample_clipping: pass` 与听感并不矛盾。

根因：`finalize` 的 `feasible` 只约束**减益多深**（`p95 ≤ 8.0`、`max ≤ 20`），不约束**多常动手**。
v1 的 p95 = 7.988 恰在预算内，于是被判"可行"并交付了一个 96% 时间都在限幅、峰值轮廓只剩 0.04 dB
起伏的母带。`active_fraction` 早已被测量并写进旁车，只是没参与判定。

## 行为与接口

- `mastering/finalizer.py` 新增 `ACTIVE_BUDGET_FRACTION = 0.70`、`MEDIAN_GR_BUDGET_DB = 2.0`，
  并入 `feasible`；搜索/二分/收尾逻辑不变。
- 首个试用 drive `lower = min(target − initial, ceiling − peak_db − .02)` 保证不触顶，
  故恒可行 → 新闸门不会引入 `No safe mastering candidate` 这条新失败路径。
- 旁车 `.mastering.json` 增加 `limiter_active_budget_fraction`、`limiter_median_gr_budget_db`。
  质量报告的 `constraints.dynamic_budget` / `loudness_target`（`audio_metrics.py:367-372`）已存在，
  因此收敛结果在前端可见，无需前端改动。
- `DSP_ENGINE_VERSION` `dsp-v3-20260930` → `dsp-v4-20260930`，mastering `ENGINE_VERSION`
  `shadowbuster-mastering-v2` → `-v3`：本改动改变输出语义（同一请求可能交付更低响度的母带）。
- 边界：**不改任何 preset 数值、不改前端、不压制用户意图**。响亮仍是响亮，只是收敛到
  "该曲能承受的最响且不持续钳制的母带"，并诚实标注 `stop_reason = dynamic_budget`、
  `dynamic_budget_limited = true`。目标本可达到的素材不受影响（实测 `normal` 与低峰值密度素材同旧结果一致）。

## 验证

同一首《太虚引》40 s 密集段（100–140 s）单独跑 `finalize`：

| preset | 结果 |
| --- | --- |
| loud（新闸门） | `applied_gain 7.00 dB`、`active 0.693`、`p50 GR 0.23 dB`、`max 2.46 dB`、块峰值 IQR 0.1271、交付 −8.58 LUFS、`dynamic_budget` |
| normal | `target_met`、`active 0.273`、`p50 GR 0.03 dB`、IQR 0.1449 → 闸门不介入 |

峰值轮廓由 0.0047 恢复到 0.127（27×），且 `stop_reason` 如实标记预算受限。

合成对照（`dense_noise`，100–1200 Hz，crest 15.1 dB，目标 −6.7）在旧判据下**合规却完全钉死**，
新判据下退回可持续的候选：

| 判据 | applied_gain | active | p50 GR | p95 GR | 块峰值 IQR |
| --- | --- | --- | --- | --- | --- |
| 仅深度预算（旧） | 16.63 dB | **1.000** | 5.99 dB | 7.99（预算 8.0 内） | **0.0003** |
| 加入介入预算（新） | 9.09 dB | 0.699 | 0.21 dB | 1.24 | 0.1331 |

测试：`tests/test_independent_mastering.py`
- `test_engagement_budget_stops_before_peaks_are_pinned`：新候选受两项介入预算约束且 IQR > .05；
- `test_depth_budgets_alone_deliver_a_pinned_master`：把 `ACTIVE_BUDGET_FRACTION` 放宽到 1.01 →
  复现 active > .95 与 IQR < .01，证明差异来自闸门而非素材；
- `test_engagement_budget_is_inert_when_the_target_is_reachable`：目标可达时 `target_met` 且不被标记受限；
- 既有 `test_dynamic_budget_allows_below_target` 增加两项预算断言。

`tests/test_lowfreq_spatial_policy.py` 的字面版本断言同步为 `dsp-v4-20260930`。
全量回归：`739 passed, 1 skipped, 30 subtests passed in 158.30s`（改前 736/29）。
