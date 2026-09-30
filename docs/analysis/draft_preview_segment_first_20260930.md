# Draft Preview 片段优先准备（2026-09-30）

## TODO

- [x] `draft_preview.py`：`prepare()` 拆成一次性（`session=None`，整曲 endpoints，行为
      不变）与实时（`session`）**两条路径**。实时路径：
      - `Session` 改为渐进式：`_window`（files + offset + length）先覆盖首听区，
        `_full` 由后台整曲 endpoints 完成后原子替换；`chunk(index)` 命中已准备区即读，
        否则在 `threading.Condition` 上**阻塞等待**（前端把 chunk 报错当致命，故不报错）；
        取消/后台失败经 `_error`/`_cancel` 唤醒被阻塞读者。
      - 短曲（`<= FULL_SYNC_SECONDS=45s`）**同步整曲**、无后台（避免二次 AI，保持
        「读 chunk 不再触发 AI」原语义）。
      - 长曲：选段 ±`CONTEXT_SECONDS` 且至少 `MIN_WINDOW_SECONDS` 的窗口切一份
        `window_source.wav`，同步 `endpoints` 得首窗素材并秒级回 `draftReady`；
        另起后台线程 `endpoints` 整曲，`set_full` 后唤醒等待者。
  - 音频切片写 **PCM_24**（见「行为」），否则首窗 AI 每次重算。
- [x] `main.py::_draft_worker`：`_gpu_lock` 的释放交给**幂等 token**；长曲后台线程
      启动成功（`session.deferred_release=True`）后由后台 finally 释放，否则
      worker finally 释放——既不让后台整曲期间被并发 GPU 抢占，也不泄漏锁。
- [x] `prepared_audio.py`：**无 API 变更**（窗口=对切片调用既有 `endpoints`）。
- [x] 测试：`tests/test_prepared_audio.py` 新增
      `test_long_song_session_prepares_window_then_background_full`、
      `test_long_song_second_prepare_reuses_cached_ai`、
      `test_background_failure_releases_lock_and_wakes_far_reader`；
      原 `test_full_song_session_reads_bounded_chunks_without_more_ai` 补
      `deferred_release is False` 断言。

## 起因

审计 §9.5：Draft Preview 虽按 1s chunk 取数，但实时 `prepare()`（`session` 非空）会把
选段覆盖为整曲并**先准备完整 endpoints**——整曲解码 + 整曲 Lew + dry 6-stem + reconstructed
6-stem——用户才听得到局部。对「快速提升」工具，关键是 *time to first audible result*。
前端 `prefetch()` 只保留 `index`/`index+1` 两块、且 app.js:57 把带 `error` 的 chunk 当
**致命**（`playerError` + stop）。所以后台渐进不能让远端 chunk 报错，只能让它「等就绪」。

## 行为与接口

- **前端协议零改动**：payload 仍是 `{stream, frames=整曲, chunkFrames, curve, levels,
  endpoints:True}`；`draftReadChunk` 仍每请求起线程读 `session.chunk(index)`。播放器一次
  只挂 ≤2 个 chunk 请求，故「窗外阻塞等待」最多占 2 线程，播放头在窗内即时出声。
- **首窗素材 vs 整曲**：窗口对切片单独跑 Lew/demucs，与整曲结果在边界/素材上不同——
  草稿本就标注「近似试听」，后台 `set_full` 后远端读取切回整曲素材，一次生成 bump/seek
  自然对齐。
- **content-id 稳定性（关键）**：窗口切片必须**字节稳定**才能跨 prepare 命中缓存。
  `sf.write(..., 'FLOAT')` 的 WAV 带时间戳 PEAK chunk，两次 prepare 同一切片指纹不同
  （实测 `14084fb0` vs `239d9eeb`）→ 首窗 AI 每次重算。改用 **PCM_24**（无 PEAK、量化
  误差 ~−144dB、草稿可忽略）后切片指纹稳定 → 同选段/映射同窗的再次 prepare **全命中缓存**
  （实测二次 prepare `lew/demucs` 计数不再增长）。整曲输入是用户原始 wav，本就稳定。
- **AI 成本**：长曲首次 = 窗口(1 lew + 2 demucs，快) + 后台整曲(1 lew + 2 demucs)。
  整曲走 §9.7/P1-6 content 缓存；窗口走稳定切片缓存。二次同素材 prepare 全部命中。
- **锁与取消**：后台线程独享 `_gpu_lock` 至整曲结束/失败再放；`_cancel_flag` 置位时后台
  `endpoints` 中断 → `session.fail` → 唤醒窗外读者抛错（窗内首听仍可播）；`gpu_release`
  token 幂等，失败路径同样恰好释放一次。
- **正式导出不变**：整曲链路 `run_pipeline` 与 `endpoints(整曲)` 完全独立，本项只动草稿
  试听；输出音频不变 → `DSP_ENGINE_VERSION` 不递增。

## 验证

- `tests/test_prepared_audio.py` 含新增 3 项 + 既有 4 项 → **7 passed**。
- `..._window_then_background_full`：60s 曲 prepare 即回 `stream` 且 `deferred_release=True`；
  chunk(0) 窗内立即可读；后台 join 后 chunk(40) 可读；`calls == {lew:2, demucs:4}`
  （窗口+整曲各一遍，读 chunk 零额外 AI）；`gpu_release` 恰一次。
- `..._second_prepare_reuses_cached_ai`：同曲再次 prepare（选段 [0,1] 与 [2,3] 均映射
  到窗口 [0,16]）计数不增长 → 窗口 + 整曲全命中。
- `..._background_failure_releases_lock_and_wakes_far_reader`：后台 lew 抛错 → 锁仍释放、
  chunk(0) 窗内可播、chunk(40)（只有整曲能覆盖）读到 surfaced 错误而非挂死。
- `test_full_song_session...`（36s）走短曲同步整曲分支：`deferred_release=False`、
  chunk 0/30/36 全即时、`{lew:1,demucs:2}` 不变。
- 全量回归：**731 passed, 1 skipped, 29 subtests**（P1-6 后为 728，本项 +3）。
- **需实机复测**：live 播放的出声时序、远跳「等待」手感、连续旋钮复用缓存、取消即时性
  只能在真实 GUI 试听验证（headless 测不到流式体感）。
