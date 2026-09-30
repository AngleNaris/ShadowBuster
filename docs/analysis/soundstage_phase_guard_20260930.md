# 最终 Width 保护改为仅相位翻转触发（2026-09-30）

## TODO

- [x] `mastering/soundstage.py widen()`：移除 Side 能量占比封顶
      （`GLOBAL_SIDE_FRACTION=0.70`、`WIDTH_BANDS` 分带预算及其 welch/csd 计算）。
- [x] 新保护：仅当"本次处理新引入相位翻转"时二分收回增益
      （输入 broadband correlation ≥ 0 且请求会推到 < 0 → 收回至 corr ≥ 0）。
- [x] 输入本就负相关（corr < 0）的素材：保护完全不介入，请求照常兑现。
- [x] mono fold-down loss 不再作为任何保护/收回依据。
- [x] 测试更新：授权语义、相位 guard、已负相关素材照常处理。
- [ ] P0-2（另行）：统一 `soundstage_reshape.py` 与 mastering 的保护语义。

## 起因

v1.6.10 DSP 审计报告 P0-1 指出 Quality Report 的 correlation / mono 警告只报告、
不回灌 DSP。用户裁决（2026-09-30）修正了报告的建议方向：

1. **保护不得抑制用户的调整意志**——请求的 width/wet 原则上全额兑现；
2. **只在会发生相位翻转类问题时才保护**——即本次处理把非负 correlation
   新推成负值；
3. **单声道兼容性不是缺陷**——很多歌曲本来就存在内容只在单声道的情况，
   mono fold-down loss 不作为损伤依据。

据此没有采用报告原文"correlation + mono degradation 双预算自动收回"方案，
而是实现为最小干预的单条件 guard。旧占比封顶（2026-09-28 语义，见
`width_saturation_knob_20260928.md`）正是"抑制用户意志"的保护，废弃。

## 行为与接口

`widen(audio, wet, width_db, sr)` 签名与返回结构不变。语义变化：

- `accepted_delta_gain`：旧 = min(请求, 占比封顶解出的 cap)；
  新 = 请求值 `wet·(10^(width_db/20)−1)`，仅相位 guard 触发时被二分收回。
- 报告字段：`band_caps` 移除（无前端消费方，已 grep 确认）；新增
  `phase_guard: {input_correlation, unlimited_correlation, admitted_correlation}`
  （仅触发时存在）；`reason` 新增取值 `phase_inversion_guard`（收回至 0，
  即请求完全无法安全兑现时）；`existing_width_at_budget` 不再产生。
  `no_existing_side` / `nonconstructive_side_delta` / `width_decreased` 保留。
- correlation 用 DC 居中后的 M/S 二阶矩解析计算
  （`_side_correlation`），与 `audio_metrics.measure_audio` 的
  `stereo_correlation` 定义一致（实测差 ≤ 1e-8），guard 本身零额外整曲扫描。
- `before`/`after` snapshot 仍走完整 `measure_audio`（P1-2 再瘦身）。
- 前端零改动。

## 验证

- `pytest tests/test_reference_mastering.py -q` → **35 passed**（含更新的
  `test_width_request_authorizes_growth_without_mono_ceiling`、
  `test_width_keeps_mono_and_honors_request_on_already_wide_audio`，新增
  `test_width_phase_guard_limits_only_new_inversion`）。
- 报告第 13 节反例复现（4s 合成，input corr +0.171 / side fraction 0.416）：
  - 默认档 wet=0.6/6dB：requested 0.597 → accepted 0.192，输出 corr = 0.000
    （旧实现输出 -0.157）；
  - 满档 wet=1/12dB：requested 2.981 → accepted 0.192，输出 corr = 0.000
    （旧实现输出 -0.400）。
  - 两档均 `applied=True`：guard 是最小干预，不整体否决。
- 解析 correlation 与 `measure_audio` 全量测量一致性：测试内断言
  `atol=1e-8` 通过。
- 缓存身份：mastering DSP 代码变更使既有渲染缓存整体失效（既有语义）。
