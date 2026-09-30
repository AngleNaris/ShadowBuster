# 声场保护语义统一：reshape 与 mastering 同规则（2026-09-30）

## TODO

- [x] `apollo_scripts/soundstage_reshape.py`：`WIDTH_BANDS` 分带占比上限
      （0.40/0.60/0.75/0.70，单声道兼容语义）改为统一 **0.50 相位翻转边界**
      （分析带内 Side 能量不得被本次处理新推到超过 Mid 能量）。
- [x] `mastering/guard.py violations()`：
      移除 `mono_fold_down_loss_db` 预算与 `band_width:*` Δ 上限；
      `stereo_correlation` 由"变化量 < −0.05"改为"仅新引入翻转"
      （before ≥ 0 且 after < 0）；`side_mid` 保留下限 −0.5dB（防非预期
      收窄丢失已授权宽度）、移除 +1.0dB 上限（变宽是用户授权意图）；
      `crest_factor_db` 与 `tone_budget:*`（动态/音色，非声场）不变。
- [x] 测试更新：`test_adaptive_soundstage.py` 12dB 授权语义
      （不越界素材全额兑现 / 越界素材停在边界）。
- [x] 与 P0-1（`soundstage_phase_guard_20260930.md`）语义一致：
      两条路径都只保护"本次新引入的相位翻转"，输入本就负相关/超宽的
      素材原样保留。

## 起因

审计 P0-2 要求统一 reshape 与 mastering 的声场保护语义。用户裁决
（2026-09-30）修正方向：保护不得抑制用户调整意志；只在会发生相位翻转类
问题时保护；单声道兼容性不是缺陷（很多歌曲本就存在内容只在单声道）。

旧实现中三处属于"兼容性保护"，全部废弃或收窄：

1. reshape `WIDTH_BANDS` 分带 Side/Mid 占比上限——压制授权增长；
2. `violations()` 的 `mono_fold_down_loss_db` Δ 预算——以 mono 损失为由
   把 Reference Mastering 打回 fallback；
3. `violations()` 的 `band_width:*` Δ 上限与 `side_mid` +1.0dB 上限——
   以"变宽过多"为由否决用户授权的结果。

## 行为与接口

- `constrain_width_delta(mix, delta, sr, growth_db)` 签名与报告结构不变；
  `report['bands'][*]['max_side_fraction']` 现统一为 0.50。可兑现增长上限
  由 min(请求 growth_db, 翻转边界) 决定：边界内素材行为与旧版一致或更宽
  （低频段 0.40→0.50 放宽），只有会把带内 Side 推过 Mid 的请求被截住。
- `violations(before, after)` 返回的 reason 取值集合变化：不再产生
  `mono_fold_down_loss_db`、`band_width:*`；`side_mid_change` 仅在收窄
  > 0.5dB 时产生；`stereo_correlation` 仅在新引入翻转时产生。
  消费方（`protected_match` 的逐级 fallback、`pipeline.master_file` 的
  final validation）行为随之放宽——Reference Mastering 更少因声场理由
  回退，回退理由集中在动态/音色预算与真实相位翻转。
- `snapshot()` 采集字段不变（`mono_fold_down_loss_db`、`band_widths` 仍
  进报告 JSON，仅作数据，不再参与判定）。
- `audio_metrics.assess_quality()` 的 warn 阈值未动（报告层，仅提示，
  不影响 DSP；前端零改动）。
- 缓存身份：reshape / mastering DSP 代码变更使既有渲染缓存整体失效
  （既有语义）。

## 验证

- `pytest tests/test_adaptive_soundstage.py tests/test_soundstage_stage.py
  tests/test_lowfreq_spatial_policy.py tests/test_dsp_stage_composition.py
  tests/test_reference_mastering.py -q` → **94 passed**。
- 全量回归见提交说明（后台运行结果）。
- 边界语义抽查：ratio=0.3 素材（es=0.09·em）12dB 授权下兑现 ~10.5dB，
  带内 es' ≤ em（测试断言 side_e ≤ mid_e·(1+1e-3)）；ratio=0.2 素材
  12dB 全额兑现（>11.5dB）；ratio=2 已翻转素材维持零增长
  （`test_overwide_does_not_grow` 不变通过）。
