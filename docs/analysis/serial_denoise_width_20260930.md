# 声场重塑：同轨多 delta 改 working_stem 串联（2026-09-30）

## TODO

- [x] `apollo_scripts/soundstage_reshape.py::main()`：adaptive_all 降噪块前移到
      宽度循环之前，新增 `working_stems` 字典，unmask → denoise → width 同轨真串联。
- [x] 宽度 delta 在降噪后的 working stem 上计算（旧实现两条 delta 都以 RAW stem
      为基准，宽度会把降噪刚削掉的高频噪声重新放大带回混音）。
- [x] 残差设计不变：最终仍是 `mix += Σ(各 delta)`；Demucs 解释不了的残差原样保留。
- [x] 测试：`tests/test_adaptive_denoise_stage.py::test_adaptive_width_delta_computed_on_denoised_stem`
      —— 12kHz 纯 side 嘶声 + wet=1 宽带拓宽，11-13kHz 频带能量必须 ≤ 1e-4 × 混音原值。
- [x] `studio_backend.DSP_ENGINE_VERSION` → `dsp-v3-20260930`（缓存身份自动失效）；
      `tests/test_lowfreq_spatial_policy.py` 版本断言同步。

## 起因

审计 P0-4：`soundstage_reshape.main()` 里 adaptive_all 的降噪 delta 与宽度 delta
都基于 RAW stem 独立计算后相加。降噪刚把 ≥8kHz 嘶声削掉，紧接着 `_reshape_stem`
在 RAW stem 上做高频 shelf + side gain，把削掉的那部分噪声原封不动放回 mix。
denoise 与 width 名义上串联，实际并联——P0 批次里最直接的"处理相互抵消"路径。

`other` 轨空间去拥挤已经做对（`other_stem_processed` 作为宽度输入），但 adaptive_all
六轨（vocals/drums/bass/other/guitar/piano）仍走 RAW。

## 行为与接口

- 顺序：`mix` → unmask_delta（other）→ adaptive_all 逐轨降噪 → 宽度循环。
- `working_stems[name]` 保存该轨当前工作副本：
  - unmask 应用时先种入 `other`；
  - 降噪每轨读 `working_stems.get(name)`（None 则按原逻辑从盘加载 × stem_scale），
    计算 `cleaned - base_stem` 累加到 `denoise_delta`，并写回 `working_stems[name] = cleaned`；
  - 宽度循环用 `working = working_stems.get(name, stem)` 替代 raw stem 做
    `_reshape_stem` / `_dynamic_side_shelf` / `drum_width_envelope`。
- `constrain_noise_delta(out, ...)` 紧跟降噪循环之后（`out` 已含 unmask_delta）。
- legacy `_spectral_denoise`（`--noise-mode other`）保持原位置：在宽度循环内、
  `processed = working + delta` 之后，行为完全不变（legacy 语义：先拓宽再削 ≥fc 噪声地板）。
- 报告结构不变：`noise_report["stems"][name]`、`noise_report["mix_budget"]`、
  `noise_report["applied"]` 语义一致；`non-adaptive_all` 分支的
  `noise_report.update(applied=..., reason=...)` 移到宽度循环后（legacy 分支下
  `denoise_delta` 在循环里才累加完成）。
- 峰值保护 / `width_report` / `constrain_width_delta(base, width_delta, ...)`
  语义不变；`base = out + denoise_delta` 仍是宽度预算的输入基底。

## 验证

- 目标 6 文件（`test_adaptive_denoise_stage.py`, `test_adaptive_soundstage.py`,
  `test_dsp_stage_composition.py`, `test_lowfreq_spatial_policy.py`,
  `test_reference_mastering.py`, `test_six_stem_pipeline.py`）
  → **85 passed in 13.22s**（含新增 `test_adaptive_width_delta_computed_on_denoised_stem`）。
- 新增测试口径：12kHz 纯正弦注入 `other` 轨 side 分量（L=+0.02, R=-0.02，mid=0）；
  fake `adaptive_denoise` 用 8kHz 低通 sosfiltfilt 精确抹除嘶声；`constrain_noise_delta`
  mock 为透传；`--wet 1 --space-amount 0 --noise-mode adaptive_all`。
  期望：输出 11-13kHz 频带能量 ≤ 1e-4 × mix 原值（≈ -40dB 以上）。
  旧实现下该断言失败（嘶声被宽度 delta 完整带回）。
- 版本：`backend.DSP_ENGINE_VERSION == "dsp-v3-20260930"`；缓存身份包含
  DSP_DIR 代码哈希 + 引擎语义版本（`studio_backend.py:1433,1458-1461`），
  自动失效旧 v2 reshape 缓存。
- 全量回归待 pytest 结果确认后补记。
