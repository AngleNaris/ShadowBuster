# 声场重塑：wet=0 宽度整块旁路（2026-09-30）

## TODO

- [x] `apollo_scripts/soundstage_reshape.py::main()` 宽度循环：`wet == 0` 且无
      legacy other 降噪时整轨 `continue`——不加载 stem、不做 reshape / 鼓包络 /
      delta 低频保护，`width_delta` 恒为零。
- [x] 保留 legacy `_spectral_denoise`（`--noise-mode other` 且 amount>0）所需的
      最小 other 轨路径：此时仍加载 other.wav，`processed = working`（delta=0
      等价），降噪照常累加 `denoise_delta`。
- [x] adaptive_all 降噪 / 空间去拥挤（unmask）路径完全不受影响（二者在宽度循环
      之前独立完成）。
- [x] 测试：`tests/test_adaptive_denoise_stage.py` 新增
      `test_wet_zero_bypasses_width_dsp_and_stem_io`（宽度 DSP 全部 monkeypatch
      为 fail，drums.wav/other.wav 均不得被读取，输出与输入位级一致，
      `width.bands == []`）与 `test_wet_zero_keeps_legacy_other_denoise`
      （drums 不加载、other 仍加载、降噪 applied）。

## 起因

审计 P1-1：`wet=0` 时旧实现仍完整执行 `_reshape_stem` / `_dynamic_side_shelf` /
`drum_width_envelope` / `_protect_widen_delta`，再把结果乘以 0。整轨 stem 的
FFT / sosfiltfilt / 包络估计全部白做，drums.wav 也被无谓加载。wet=0 是
"只听降噪/去拥挤、不动声场"的常用预览路径，浪费最直接。

## 行为与接口

- CLI / 后端契约零改动：`--wet`、`--noise-mode`、`--other-denoise-amount`、
  `--space-amount` 语义不变；报告 JSON 结构不变。
- **输出位级一致**：wet=0 时旧实现 `delta = (reshaped - working) * 0` 恒为零，
  `_protect_widen_delta(全零)` 走 `not np.any(delta)` 早返回，`processed =
  working + 0 == working`。新实现直接 `processed = working`，跳过零乘。
  WAV / report 逐字节相同，仅 stdout 不再打印未生效的宽度参数。
- 因输出不变，`DSP_ENGINE_VERSION` 不递增；缓存身份含 DSP_DIR 代码哈希
  （`studio_backend.py:1458-1461`），改动文件自然失效旧缓存，语义版本无需变动。
- 旁路条件：`wet == 0 and not (name == "other" and noise_mode == "other"
  and other_denoise_amount > 0)`。即 wet=0 时 drums 轨永远跳过；other 轨仅在
  legacy 降噪开启时进入最小路径。

## 验证

- 目标 4 文件（`test_adaptive_denoise_stage.py`, `test_dsp_stage_composition.py`,
  `test_lowfreq_spatial_policy.py`, `test_adaptive_soundstage.py`）
  → **49 passed in 4.51s**（含 2 项新增旁路测试）。
- `test_legacy_default_matches_prechange_code`（与 `HEAD` 旧版逐样本对比）仍通过，
  佐证 wet=0 路径位级不变。
- 全量回归待 pytest 结果确认后补记。
