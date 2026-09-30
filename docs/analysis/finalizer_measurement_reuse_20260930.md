# Finalizer 收尾复用搜索循环测量（2026-09-30）

## TODO

- [x] `mastering/finalizer.py`：拆出 `_apply_measurements(stats, actual, true_peak_dbtp)`
      （纯记录，含 target_status / target_met / stop_reason 覆盖逻辑），
      `_update_measurements(audio, sr, stats)` 改为「整曲测量 + 委托」的薄封装。
- [x] `finalize()` 搜索循环：把胜出候选的 `actual`（trim 后 LUFS）与
      `peak_before_trim`（trim 前真峰）一并存入 `best`。
- [x] `finalize()` 收尾：用 `_apply_measurements(stats, actual_lufs, peak_before_trim + trim)`
      替代 `_update_measurements(result, ...)`——不再对 result 重跑整曲 LUFS + 4× 真峰。
- [x] 测试：`tests/test_independent_mastering.py` 新增
      `test_finalize_reuses_loop_measurements_and_matches_full_rescan`
      （复用值 vs 独立全量重测一致）与
      `test_finalize_does_not_rescan_result_after_search`
      （整曲扫描计数：LUFS = 1+iters、真峰 = iters，收尾不多出一次）。
- [x] `write_output` 回读复测、`pipeline.master_file` 的 baseline 二次 finalize
      均**不动**（见下）。

## 起因

审计 P1-3：`finalize()` 收尾的 `_update_measurements(result, ...)` 对最终候选又跑了
一遍整曲 `integrated_lufs`（K 加权 2 遍 lfilter + 门控）和 `true_peak_db`
（`resample_poly` 4× kaiser FIR 全曲卷积）。但搜索循环里**每个候选**（含胜出者）
都已经算过这两项：

- `actual, _ = integrated_lufs(candidate, sr)` 在 trim 之后调用，`candidate` 就是
  最终 `result`（同一数组对象）→ 收尾 LUFS 与循环值逐位相同；
- `true_peak_db(candidate)` 在 trim 之前调用，`result = candidate × 10^(trim/20)`
  是标量增益 → 真峰在实数域严格平移 `trim` dB。

带参考的 `master_file` 里 `finalize` 跑两次（shaped + baseline），每次收尾都白扫
一遍整曲，纯属重复。

## 行为与接口

- **输出音频逐样本不变**：候选生成（limiter / resample / trim / drive 搜索）一行未改，
  `result` 数组与旧实现完全一致。只有 `stats` 里 `actual_lufs` / `true_peak_dbtp`
  的**来源**从「收尾重测」变为「循环复用」。
- `actual_lufs`：复用值 = `integrated_lufs(result)`（同一数组、同一确定性函数）→
  **逐位相同**，`target_error_lu` / `target_met` / `target_status` / `stop_reason`
  覆盖逻辑随之完全一致。
- `true_peak_dbtp`：`peak_before_trim + trim`。标量增益下 `resample_poly(x·g)` 与
  `resample_poly(x)·g` 在实数域相等，浮点舍入差 ~1e-15 dB——远低于 0.01 dB 报告
  口径与 `.02 dB` 安全余量，且在 `write_output` 路径会被回读测量覆盖（见下）。
- **未改动（刻意保留）**：
  - `write_output` 对 PCM24 回读文件仍跑完整 `_update_measurements(written)`——
    这是「验证实际落盘文件」的契约，量化 + dither 后的真峰必须实测，不能复用
    float 阶段的值；
  - 循环内每候选的 `true_peak_db` / `integrated_lufs`——是 trim 决策与误差二分
    的输入，算法必需；
  - `pipeline.master_file` 的 baseline 二次 `finalize`——limit 后安全校验需要
    baseline 音频本身，无法跨不同音频复用。
- 每次 `finalize` 净省：1× 整曲 LUFS + 1× 4× 重采样真峰。

## 验证

- 目标 2 文件（`test_independent_mastering.py`、`test_reference_mastering.py`）
  → **61 passed in 30.76s**。
- `test_finalize_does_not_rescan_result_after_search`：monkeypatch 计数
  `integrated_lufs` / `true_peak_db`，断言 `len(lufs) == 1 + iterations`、
  `len(peak) == iterations`（旧实现分别为 `2 + iterations` / `iterations + 1`）。
- `test_finalize_reuses_loop_measurements_and_matches_full_rescan`：独立调用
  `integrated_lufs(result)` / `true_peak_db(result)` 与 stats 对比，LUFS 逐位相等、
  真峰差 < 1e-9，且 `true_peak_dbtp <= TRUE_PEAK_CEILING_DB`。
- 既有 `test_loudness_presets_write_verified_pcm24`（actual_lufs vs measure_audio
  abs=1e-8）、`test_unlimited_output_preserves_spectrum_and_stereo`、
  `test_dynamic_budget_allows_below_target`（target_status）全部通过，佐证 stats
  契约与输出音频不变。
- 输出音频逐样本不变 → `DSP_ENGINE_VERSION` 不递增；缓存身份含代码哈希，
  改动文件自然失效旧缓存。
- 全量回归待 pytest 结果确认后补记。
