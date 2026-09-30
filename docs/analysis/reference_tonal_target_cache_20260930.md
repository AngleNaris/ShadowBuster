# Reference tonal target 独立缓存（2026-09-30）

## TODO

- [x] 新增 `mastering/reference_tone.py`：`tonal_target(candidate_fn, source, ref,
      *, cache, artifact)` 返回 `(frequencies, db, metadata)`；`cache=None` 走内存
      直算（逐位等于旧 inline `candidate→matching_curve`）；给定 cache 时以
      `pipeline_cache.StageCache` 按 **(source, reference) 内容**缓存一个 JSON 旁件。
      `identity()` 只把 `reference_tone.py / guard.py / matchering_adapter.py` 纳入
      失效集（决定曲线的三处），下游 pipeline/finalizer 变更不踢缓存。
- [x] `mastering/guard.py`：拆出 `protected_match_curve(audio, frequencies, db, strength)`
      （强度二分 + guard），`protected_match(audio, source, candidate, bandwidth, strength)`
      改为 `matching_curve` + 委托——旧签名与旧行为完全保留（直接单测不破）。
- [x] `mastering/pipeline.py`：`master_file` 新增 `reference_cache` /
      `reference_cache_artifact` 关键字（默认 None）；参考分支改调
      `tonal_target(candidate, match_source or input_path, reference, ...)` 再
      `protected_match_curve`。`candidate` 仍以 pipeline 模块符号传入，monkeypatch 生效。
- [x] `mastering/__main__.py`：`--reference` 且未 `--no-reference-cache` 时构造 StageCache
      并把曲线旁件放临时目录传入；**best-effort import**——打包 runtime 若无
      `pipeline_cache` 则回落冷路径（保「独立母带子进程无额外依赖」契约）。
- [x] 打包：`packaging/runtime_sync.ps1` / `runtime_sync_macos.sh` 把
      `pipeline_cache.py` 随 `audio_metrics.py` 拷入 mastering runtime（+ PS 侧
      Assert-SameFile 与 critical-manifest 根），使 §9.8 缓存在发布态真正生效。
- [x] 测试：`tests/test_reference_mastering.py` 新增 3 项（冷=旧、命中跳 candidate
      且 warm==cold、master_file 跨响度只生成一次 candidate）。

## 起因

审计 §9.8：Draft Preview 早已有 reference curve cache（`draft_preview` 用主进程
StageCache 包住 `python -m mastering.draft_curve`）。正式 Reference Mastering 没有：
`pipeline.master_file` 每次 `reference and strength>0` 都跑 `candidate()`（Matchering，
昂贵）+ `matching_curve()`。外层母带阶段缓存按 **全部参数**（loudness / final width /
EQ / strength / input_md5 …）建签名，于是**任何下游旋钮变动 → 外层 miss → 整个
`python -m mastering` 子进程重跑 → 重新生成 candidate**。

但 `candidate→matching_curve` 得到的「broad tonal target」只依赖 (source, reference)
两路音频内容，与 loudness/width/EQ 无关。§9.8 要求把这一段独立缓存，并保留「最终 guard
仍针对当前 candidate 重算」。

## 行为与接口

- **冷路径逐位不变**：`cache=None` 时 `tonal_target` 直接 `candidate()` 后
  `matching_curve()`，返回的就是旧 `protected_match` 内部所用同一 `(frequencies, db)`；
  `protected_match_curve` 与原 `protected_match` 后半段字节级相同 → 输出音频、
  `reference` 报告结构（含 `adapter` / `attempts` / `curve_*` / `accepted_strength`）全等。
- **热路径 = 冷路径**：旁件以 `float(v)`（Python float 即 fp64）写 JSON，
  `repr` 往返精确 → 恢复的 `(frequencies, db)` 数组 `array_equal` 冷算值，`metadata`
  字典相等 → warm 与 cold 输出逐位一致。
- **失效边界**：StageCache key = sha256(stage + [fingerprint(source),
  fingerprint(reference)] + params{{} + identity})。改任一音频内容 → 新 key → 重算；
  `identity.code` 覆盖 candidate/matching_curve/序列化三文件，算法变更自然失效。
  下游 loudness/width/EQ **不进 key** → 正是要复用的那批变动。
- **子进程依赖**：`import pipeline_cache` 为 best-effort（try/except）；发布 runtime
  现随包附带 `pipeline_cache.py`，缺失时静默冷路径。缓存落 `LOCALAPPDATA/ShadowBuster/
  processing-cache`（与主进程同一全局缓存根、同一文件锁；外层 stage 缓存在跑子进程时不
  持锁，无死锁）。
- `--no-reference-cache` 逃生阀；`DSP_ENGINE_VERSION` 不递增（冷/热输出均不变）。

## 验证

- 定向：`test_reference_mastering.py`(全量含新增 3 项) + `test_mastering_reports`
  + `test_independent_mastering` + `test_pipeline_cache` + `test_float_internal`
  + `test_soren_style_off` + `test_reference_loudness_contract` → **96 passed**。
- `test_tonal_target_uncached_equals_candidate_and_matching_curve`：cold 与
  `guard.matching_curve` `array_equal`，candidate 计 1 次。
- `test_tonal_target_cache_hit_skips_candidate_and_matches_cold`：二次调用 candidate
  不再跑（计 1）、warm==cold、改参考内容后 candidate 复跑（计 2）。
- `test_master_file_reuses_reference_candidate_across_downstream_params`：normal→soft
  两次 `master_file` 只生成 1 次 candidate，`accepted_strength` 一致、
  `target_lufs` 各异（下游各自生效）。
- 全量回归：**725 passed, 1 skipped, 29 subtests**（P1-4 后为 722，本项 +3）。
