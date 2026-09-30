# Guard 指标按需计算（2026-09-30）

## TODO

- [x] `audio_metrics.py`：新增 `guard_metrics(audio, sr, *, bands=True)`——只算
      guard 决策真正读的键（`side_mid` / `stereo_correlation` / `crest_factor_db`
      / `band_energies`），公式与 `measure_audio` 完全一致（共享键逐位相等）。
- [x] `mastering/guard.py::snapshot()` 改为委托 `guard_metrics`（bands=True），
      不再跑完整 `measure_audio` 再挑 8 个键。
- [x] `mastering/soundstage.py::widen()` 改用 `guard_metrics(..., bands=False)`：
      它只读 `side_mid.db`，连整曲均值谱（`band_energies`）都不必算。
- [x] 测试：`tests/test_audio_metrics.py` 新增 4 项——共享键与 `measure_audio`
      逐位相等、昂贵键不出现、把昂贵整曲通道全炸掉 `guard_metrics` 仍正常
      （对照 `measure_audio` 必炸）、输入校验与 `measure_audio` 同口径。
- [x] 前端 / 报告契约零改动（见下"行为与接口"）。

## 起因

审计 P1-2：`snapshot()` 调完整 `measure_audio`，但 `violations()` 只读其中 4 组
键；`widen()` 调 `snapshot()` 两次却只读一个 `side_mid.db`。被白算的昂贵整曲
通道包括：

- `resample_poly(data, 4, 1)` 4× 过采样真峰（kaiser FIR 全曲卷积，单项最贵）；
- `integrated_lufs` + `_block_loudness`×2（short/momentary，各含 2 遍 K 加权
  lfilter + 分块循环）；
- `_band_widths`（4 个带通 × mid/side 两路 sosfiltfilt）；
- `_low_band_ratio`（带通 sosfiltfilt）；
- `widen` 路径下还有 `_mean_spectrum`（整曲 FFT）。

一次带参考的 `master_file` 里，`protected_match`（≤5 次）+ `final_validation`
（2 次）+ `widen`（≤4 次）≈ 11 次完整 `measure_audio`，其中绝大多数键算完即弃。

## 行为与接口

- `guard_metrics` 返回键：`sample_peak`、`rms`、`crest_factor_db`、
  `stereo_correlation`、`side_mid`，以及 `bands=True` 时的 `band_energies`。
  **不含** `integrated_lufs` / `true_peak_4x(_dbtp)` / `mono_fold_down_loss_db`
  / `band_widths` / `low_band_side_mid` / `short_term_lufs` / `momentary_lufs`
  / `lra_lu` / `spectral_centroid_hz` / `clipping_samples`。
- `violations()` 读取的 4 组键（`side_mid.db`、`stereo_correlation`、
  `crest_factor_db`、`band_energies`）全部保留，**裁决行为逐位不变**。
- `widen()` 的 `width_decreased` 判据只依赖 `side_mid.db`，`bands=False` 足够；
  其 report 的 `before/after` 仍含 `side_mid`（`test_independent_mastering`
  与 widen 测试只读该键 / `accepted_delta_gain` / `applied` / `reason`）。
- **持久化质量报告不受影响**：`write_quality_report` 仍对输入/输出各跑一次完整
  `measure_audio`，报告页的 `band_widths.side_mid_db` 扇形、`crest_factor_db`、
  `stereo_correlation` 等读数来源不变（`ui/app.js:3132-3151` 读的是
  `output.metrics`，不是 guard 快照）。
- guard 快照（`reference.final_validation.before/after`、`protected_match`
  report、`soundstage.before/after`）随之瘦身：经全仓 grep 确认无任何
  前端/测试消费被删键，报告只保留 guard 真正用到的指标，语义更诚实。
- 校验（非空、有限、sr≥8000、单声道提升为 [n,1]）与 `measure_audio` 同口径。

## 验证

- 目标 4 文件（`test_audio_metrics.py`、`test_reference_mastering.py`、
  `test_independent_mastering.py`、`test_objective_compare.py`）
  → **77 passed in 31.46s**。
- 新增 `test_guard_metrics_never_invokes_expensive_whole_file_passes`：把
  `integrated_lufs` / `_block_loudness` / `_band_widths` / `_low_band_ratio`
  / `scipy.signal.resample_poly` 全部 monkeypatch 成抛错，`guard_metrics`
  仍正常返回；对照 `measure_audio` 必然触发其中之一（证明补丁有效、
  guard 路径确实绕开了这些整曲通道）。
- `test_guard_metrics_matches_measure_audio_on_shared_keys`：4 组共享键
  与 `measure_audio` 逐位相等，保证 guard 裁决口径零漂移。
- 因 guard 决策值不变、最终音频逐样本不变，`DSP_ENGINE_VERSION` 不递增；
  缓存身份含代码哈希，改动文件自然失效旧缓存。
- 全量回归待 pytest 结果确认后补记。
