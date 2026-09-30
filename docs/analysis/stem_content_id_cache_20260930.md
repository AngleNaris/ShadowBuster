# Stem immutable content-id 去重复 hash + 分级缓存淘汰（2026-09-30）

## TODO

- [x] `pipeline_cache.py`：新增 `_md5_bytes`（原逐块读哈希），`md5()` 保持直算；
      `fingerprint()` 的文件哈希改走 `_memoized_md5`——以
      `(realpath, size, mtime_ns)` 为 content-id 记忆化，`_hash_memo`（带上限
      `_HASH_MEMO_CAP=4096` 清空兜底）。同一套 stems 被多阶段反复 fingerprint 时，
      每个 stem 全程只 MD5 一次。
- [x] `pipeline_cache.StageCache`：新增 `tier` 参数（默认 `'C'`，非法值回落 `'C'`），
      存入 manifest `{'hashes','result','tier'}`；`evict()` 改为
      **先按层级、再按 mtime** 删除（`_TIER_EVICT_GROUP={'C':0,'B':1,'A':2}`，
      高组最后淘汰）。无 `tier` 的旧条目按 `'C'` → 对它们与旧「纯 LRU」完全等价。
- [x] `prepared_audio.py`：lew / demucs 两个 AI StageCache 标 `tier='A'`
      （最贵 AI 产物，最后淘汰）。
- [x] `mastering/__main__.py`：参考音色目标缓存标 `tier='B'`。
- [x] 外层 `studio_backend` DSP StageCache 保持默认 `'C'`（保守：一个实例混装
      cheap DSP + mastering，不逐一分层）。
- [x] 测试：`tests/test_pipeline_cache.py` 新增
      `test_fingerprint_content_id_memoizes_repeated_hash`、
      `test_fingerprint_directory_hashes_each_stem_once`、
      `test_evict_keeps_tier_a_ai_product_over_newer_cheap_dsp`；
      同步 `tests/test_float_internal.py::SpyCache` 构造签名加 `tier`。

## 起因

审计 §9.7：多个下游阶段会对同一套 stems 重复 fingerprint。实测调用图里
`stem_dir`（demucs 的 6 个 stem WAV）同时是 bass / drums / reshape / guitar / synth /
vocals 六个 `cached` 阶段的输入，而 `StageCache.run` 每次都
`signature = {'inputs': [fingerprint(p) for p in inputs], ...}`，命中校验与落盘又要
`fingerprint` 产物——于是几百 MB 的 stem 在一次运行里被整段 MD5 六次以上。

`pipeline_cache` 已有跨参数复用的产物缓存，但没有「内容 id」：fingerprint 每次都从
字节重算。§9.7 建议给 AI 产物写 immutable manifest / content-id，下游直接依赖 id，
并按昂贵程度分 Tier A/B/C 优先保留 AI。

## 行为与接口

- **哈希值不变，只是不再重算**：`_memoized_md5` 返回的正是 `_md5_bytes` 的同一
  32 位十六进制摘要——任何缓存 key / signature / 校验都看到完全相同的值，
  **不改缓存身份、不失效既有产物、不动输出音频** → `DSP_ENGINE_VERSION` 不递增。
- **content-id 契约**：`(realpath, size, mtime_ns)` 三元组作为内容 id。音频产物遵循
  「写一次即不可变」——`os.replace`/新写入都会推进 `mtime_ns`，改写必然换 id 重算；
  realpath 归一别名路径，同文件多写法共享一次哈希。仅在「同 size 且同 ns 时间戳的
  原位改写」这一病态情形下会返回旧哈希，而应用的写once-then-move 纪律排除该情形。
  记忆是进程内字典，不跨进程持久化，重启即清空。
- **目录指纹**：`fingerprint(dir)` 仍逐文件列目录（廉价），重活（每文件 MD5）经 memo
  去重 → 六个 stem 只各算一次。
- **分级淘汰**：`evict` 删除序 = (层级组升序, mtime 升序)，即先删最旧 Tier C，Tier A
  最后删。旧条目/未标记条目视作 Tier C → 对既有缓存条目行为与旧 LRU 一致，向后兼容。
- 容量仍由 `settings.json:capacity_gb` 决定；`tier` 不改容量语义，只改「超限先删谁」。

## 验证

- 定向：`test_pipeline_cache.py`(含新增 3 项) + `test_float_internal`
  + `test_reference_mastering` + `test_independent_mastering` + `test_mastering_reports`
  → 全绿（其中 SpyCache 构造同步 `tier` 后 `test_pipeline_identity_carries_audio_format`
  通过）。
- `..._content_id_memoizes_repeated_hash`：monkeypatch `_md5_bytes` 计数，两次
  fingerprint 只算 1 次；改写文件 → 计数 +1 且值 = 新内容 md5。
- `..._hashes_each_stem_once`：6-stem 目录反复 fingerprint，`_md5_bytes` 只调 6 次，
  快照相等。
- `test_evict_keeps_tier_a_ai_product_over_newer_cheap_dsp`：Tier A 条目 mtime 人为
  置旧，再存更新的 Tier C 撑破容量 → 存活的恰是 Tier A（证明层级压过纯 LRU）。
- 全量回归：**728 passed, 1 skipped, 29 subtests**（P1-5 后为 725，本项 +3）。
