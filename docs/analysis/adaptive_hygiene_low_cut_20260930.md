# Premaster Hygiene 低切改素材自适应（2026-09-30）

## TODO

- [x] `apollo_scripts/premaster_hygiene.py`：新增 `analyze_low_band()` /
      `choose_low_cut_hz()`，低切点由素材证据裁决，请求值（默认 40Hz）降为上限。
- [x] 裁决档位：不处理（强音乐性）/ 27.5Hz / 35Hz / 请求值。
- [x] `main()` 接入裁决；stage 报告 `extra.low_cut` 新增 `requested_hz` 与
      `analysis`（证据 + chosen_hz），`hz` 语义改为实际裁决值。
- [x] 测试：音乐性 sub 不处理、rumble 维持请求值、恒定音/短素材保守、
      chosen ≤ requested、CLI 报告落盘 + 位级透传。
- [x] 后端 / CLI / UI 零改动（`stage_hygiene` 与 `--hygiene-low-cut-hz`
      契约不变，仍传请求值）。

## 起因

审计 P0-3：固定 40Hz/-3dB 高通在 31.5Hz 已衰减 -6.33dB。对 EDM sub /
cinematic bass / synth bass，31.5Hz 可能是真正的音乐内容——"低于 40Hz"
不能直接等价成"无用能量"。改为素材自适应，UI 无需暴露 cutoff。

## 行为与接口

`choose_low_cut_hz(audio, sr, requested_hz) -> (chosen_hz, analysis)`：

- 证据（次声带 10Hz–请求值 vs 基频带 请求值–4×请求值）：
  - `sub_ratio_db`：全曲能量占比（占比高 → 音乐主体，且反映限制器余量占用）；
  - `envelope_correlation`：100ms 帧包络（dB 域）Pearson 相关，仅当两带
    活跃帧包络 std ≥ 0.5dB 才计算（恒定电平无"联动"可言，记 0）；
  - `silent_leak_db`：基频带静默帧（峰值 −40dB 以下）里次声带相对活跃帧的
    中位电平——rumble 整曲漂移（≈0dB），音乐性 sub 随音乐消失（深负）。
- 档位（自上而下取第一个满足）：
  | 联动 | 能量占比 | 泄漏 | 裁决 |
  |---|---|---|---|
  | ≥ 0.50 | ≥ −20dB | ≤ −6dB | 不处理（0.0） |
  | ≥ 0.35 | ≥ −26dB | ≤ −3dB | 27.5Hz |
  | ≥ 0.25 | ≥ −30dB | 不查 | 35Hz |
  | 其余 / 素材 < 2s / 帧数不足 | | | 请求值 |
- chosen 恒 ≤ requested；0.0 为假值，与 `apply_hygiene` / CLI 的"未启用"
  语义直接兼容（不处理时位级透传，无峰值缩放路径）。
- 27.5Hz 档对 31.5Hz 内容损失 ≤ ~1.2dB（零相位平方响应），40Hz 档为 −6.3dB。
- `run_pipeline` 记录的 `hygiene_low_cut_hz`（quality report processing 段）
  仍是请求值；实际裁决值在 hygiene stage 报告 `extra.low_cut` 里。

## 验证

- `pytest tests/test_premaster_hygiene.py -q` → **17 passed**（新增 6 项：
  musical sub → chosen 0 / rumble → 40 / 恒定音与短素材 → 40 /
  chosen ≤ requested 全档位 / CLI 子进程报告 + 位级透传）。
- 合成素材口径：音乐性 sub = 31.5Hz 与 100Hz 基频共享 1s 门控 × 0.5Hz 慢起伏
  包络（联动 ≈1、静默段泄漏深负）；rumble = 30Hz 低通白噪恒定漂移 + 门控音乐
  （联动 ≈0）。真实素材分布待 P0-5 听音套件（暂缓，无素材）。
- 缓存身份：DSP_DIR 代码哈希入 identity（`studio_backend.py:1433,1458-1461`），
  本次改动自动失效旧 hygiene 缓存；`DSP_ENGINE_VERSION` 待 P0 批次收尾统一递增。
