# 逐样本 attack/release 包络 JIT 化（2026-09-30）

## TODO

- [x] `apollo_scripts/bass_enhance.py`：抽出共享递推 `_ar_envelope_py(x, a, r)`
      （保留原逐样本 Python 形式，作参照与无 numba 环境回退），
      `_ar_envelope = njit(cache=True, fastmath=False)(_ar_envelope_py)`，
      `ImportError` 时 `_ar_envelope = _ar_envelope_py`。
- [x] 替换 3 处 bass 内联循环为 `return _ar_envelope(target/level, a, r)`：
      `_relative_activity_gate`、`_follower`、`_gate_curve`（各自的 a/r 系数行不变）。
- [x] `apollo_scripts/drum_enhance.py`：`_gate` 的逐样本循环改为
      `return _ar_envelope(target, a, r)`；`from bass_enhance import _ar_envelope, ...`。
- [x] 测试：`tests/test_transient_saturation_v2.py` 新增
      `test_ar_envelope_jit_matches_original_per_sample_loop`
      （JIT 与 Python 回退都对原循环逐位一致，控制曲线 max diff = 0）与
      `test_gate_functions_bit_identical_to_pre_jit_head`
      （git HEAD 快照对比 4 个门函数输出数组）。

## 起因

审计 P1-4：管线里有 4 处「一阶 attack/release 平滑」是纯 Python `for` 循环，逐样本
串行递推、无法向量化（下一样本依赖上一 `acc`），长素材下 CPU 热点。4 处递推公式完全
相同：

```
acc += (a if x[i] >= acc else r) * (x[i] - acc);  out[i] = acc    # acc 初值 0.0
```

散落在 `bass_enhance._relative_activity_gate`（瞬态相对门）、`_follower`（包络跟随）、
`_gate_curve`（RMS 活动门）与 `drum_enhance._gate`（鼓活动门）。串行递推不能靠 NumPy
消除，但可用 Numba JIT 编译成机器码——仓库已有先例 `mastering/finalizer._release_envelope`
（`@njit(cache=True, fastmath=False)`）。

## 行为与接口

- **输出逐样本位级不变**：`fastmath=False` 禁止 FMA 收缩与浮点重结合，编译后的算术
  顺序与 Python 逐样本循环一致 → 4 个门函数返回数组与旧实现 `array_equal`（实测
  6 组 (a,r) × 二进制/连续/斜坡/冲激 4 类信号，maxdiff=0.0）。控制曲线不变 → 下游
  delta、干湿混合、母带输入全部不变。
- 语义、签名、调用方均不改：4 处仍接收同一 `(target/level, a, r)`，只是循环体搬进
  共享 `_ar_envelope`。
- `cache=True`：首次 import 编译后落盘缓存（`__pycache__`），后续进程冷启动直接命中，
  避免每次运行重复编译。
- 无 numba 环境（`ImportError`）时 `_ar_envelope` 回退 `_ar_envelope_py`，行为与改造前
  完全相同，仅少掉加速——非破坏性降级。
- 每次处理净省：4 处（按素材实际命中数）逐样本 Python 解释开销，长曲显著。

## 验证

- 目标文件 `tests/test_transient_saturation_v2.py` → **14 passed**。
- 定向 bass/drum 回归集
  （`test_drum_enhance_stage` / `test_bass_sidechain` / `test_sub_shelf_calibration` /
  `test_dsp_stage_composition`）→ **62 passed**。
- `test_ar_envelope_jit_matches_original_per_sample_loop`：内联 `_orig_ar_loop` 为原
  循环参照，断言 `_ar_envelope`（JIT）与 `_ar_envelope_py` 都 `assert_array_equal`。
- `test_gate_functions_bit_identical_to_pre_jit_head`：`git show HEAD:` 导出
  bass/drum 两文件快照、`importlib` 加载，对 `_gate`/`_gate_curve`/
  `_relative_activity_gate`/`_follower` 输出 `assert_array_equal`（bass/drum 仅被
  P1-4 触碰，HEAD 差异即隔离本改动）。
- 全量回归：**722 passed, 1 skipped, 29 subtests**（P1-3 后为 720，本项 +2）。
- 输出音频逐样本不变 → `DSP_ENGINE_VERSION` 不递增；缓存身份含代码哈希，
  改动文件自然失效旧缓存。
