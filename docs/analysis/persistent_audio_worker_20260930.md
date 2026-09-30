# 常驻音频 worker 地基（§9.9 / P1-8，opt-in 默认关）（2026-09-30）

## TODO

- [x] `audio_worker.py`：常驻 worker 服务端。行分隔 JSON-RPC（stdin/stdout）。
      `run` 作业经 `runpy.run_module`/`runpy.run_path` 在**同进程内**执行，重依赖
      （numpy/scipy/torch）导入一次后常驻；主线程串行跑作业（契合 GPU 串行）。
      独立线程读 stdin：`cancel` 置位当前作业 Event（协作式取消）、`shutdown` 退出、
      `ping` 应答。作业 stdout/stderr 经 `_Forwarder` 转发识别到的
      `*_PROGRESS`/tqdm 行为 progress 帧；作业异常被捕获为 `error` 帧（worker 不死）。
      注册 `sb_worker` 模块暴露 `cancel_requested()`/`check()`/`Cancelled` 供在跑代码协作轮询。
  - [x] `studio_backend.py`：worker 客户端 `_AudioWorker`（懒起/重启/`stop`、单作业锁）、
      `worker_enabled()`、`_cmd_to_job()`、`_env_matches_worker()`、`stream_or_worker()`。
      **仅当 `SB_WORKER=1` 且作业环境匹配 worker 固定环境**才走 worker，否则
      `_run_stream` 子进程；worker 不可用/不识别的 cmd 形状也自动回退。
- [x] 生产调用点**不改**：各 stage 仍调 `_run_stream`，默认 `SB_WORKER` 未设 → 行为与
      今日**逐字节一致**。worker 为「地基 + 逐阶段 opt-in 迁移」预留。
- [x] 测试：`tests/test_audio_worker.py`（CPU、无 torch/GUI/ffmpeg）——默认关走子进程、
      env 不匹配回退、脚本作业跑通并转发进度、作业崩溃隔离且 worker 存活、协作式取消。

## 起因

审计 §9.9：QtWebEngine 与同进程 CUDA 初始化曾卡死，故当前每个 stage 都 `python -m …`
子进程隔离。代价是每次重起解释器、重复 import、AI stage 重复加载 torch 模型。方向不是把
Torch 放回 GUI，而是 GUI 进程 ↕ IPC ↕ **常驻** worker（模型只加载一次、少 Python/runtime
启动、GUI 继续隔离）。审计明确要求「先确认方案再动手」，且核心收益（torch 模型复用）需要把
lew/demucs 改为**带模块级模型缓存与协作取消的 in-process 入口**——那是依赖 GPU/实机、无法在
此 headless 环境验证的大改。故本项先交付**安全、可测、默认关**的 worker 地基。

## 行为与接口

- **默认零影响**：`SB_WORKER` 未设 → `stream_or_worker` 直接 `_run_stream`；且现有 stage
  根本尚未调用 `stream_or_worker`。生产路径与输出完全不变。
- **单 worker 一环境**：worker 以 `PYTHONPATH=MASTERING_ROOT` 固定环境常驻。只有 env/cwd
  与之匹配的阶段（如 `mastering`：PYTHONPATH=MASTERING_ROOT）才可能被路由；demucs（TORCH_HOME/
  HF_HOME）、lew（PYTHONPATH=APOLLO_DIR）等**环境不匹配 → 回退子进程**，直到各自带 GPU 实机
  验证后单独迁移。`_env_matches_worker` 即这道闸。
- **协作式取消**（用户选定语义）：客户端 `cancel()` 置真后向 worker 发 `{"op":"cancel"}`，
  worker 置作业 Event；**只有轮询 `sb_worker.check()` 的代码**会中止。未轮询的第三方作业
  会跑完（worker 不强杀，保留模型）。迁移某 stage 时需在其分块/分段循环加 `check()` 轮询。
- **崩溃隔离**：作业异常 → `error` 帧 → 客户端抛 `PipelineError`；worker 进程**存活**，
  下个作业继续用已加载依赖。进程真死（EOF）→ `WorkerUnavailable` → 回退 `_run_stream`。
- **协议**：`ready`/`progress`/`done`/`cancelled`/`error`/`bye` 帧，`run`/`cancel`/`ping`/
  `shutdown` 请求，均 JSON 行。
- 缓存身份：`studio_backend.py` 在实现指纹内（本次改动令既有缓存自然失效一次，行为不变）；
  `audio_worker.py` 暂不在 AI/mastering 指纹内——**未来按阶段启用 worker 时**须把
  `audio_worker.py` 及被路由脚本纳入该阶段缓存身份，并在实机验证后才打开。`DSP_ENGINE_VERSION`
  不递增（默认路径输出不变）。

## 验证

- `tests/test_audio_worker.py`（5 项，CPU）→ **全通过**：
  - `..._disabled_uses_subprocess_stream`：默认关 → `_run_stream`，不 spawn。
  - `..._env_mismatch_falls_back_to_subprocess`：`TORCH_HOME` env → 回退。
  - `..._runs_script_and_streams_progress`：脚本作业写产物、进度帧回调、输出回传。
  - `..._crash_is_isolated_and_survives`：坏作业抛 `PipelineError`，随后好作业成功（worker 存活）。
  - `..._cooperative_cancel`：轮询 `sb_worker.check()` 的脚本被取消 → `PipelineError('用户取消')`。
- 全量回归：**736 passed, 1 skipped, 29 subtests**（P1-7 后为 731，本项 +5；worker 为
  opt-in 默认关，其余 731 项证明既有行为零回归）。
- **未在实机验证（有意为之）**：把任何**生产 stage** 接进 worker（尤其 torch lew/demucs 的
  in-process 模型缓存 + 协作取消、CUDA 主线程语义、QtWebEngine+CUDA 卡死回归）必须在带
  GPU 的真实 app 上逐项验收后再启用；本地基默认关，未验收阶段零风险。

## 后续（逐阶段迁移，非本地基范围）

1. 给要启用 worker 的 stage 在调用点用 `stream_or_worker(...)` 替换 `_run_stream(...)`，
   实机跑通后开 `SB_WORKER`。
2. torch 阶段：把 lew/demucs 改为 in-process 可导入入口 + 模块级模型缓存 + 分块
   `sb_worker.check()` 轮询（demucs 需按第三方 API 自写分离循环）。
3. 把 `audio_worker.py` 与被路由脚本纳入相应阶段缓存身份。
