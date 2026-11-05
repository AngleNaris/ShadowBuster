# 母带限制器：局部介入预算 / 起始斜坡 / −1.0 dBTP / 固定抖动种子（2026-11-04）

## TODO

- [x] 定量定位《在身体散作灰烬之前》"满的地方像削波"的来源
- [x] P1 介入预算（active ≤0.70、中位减益 ≤2.0 dB）同时约束最差 10 s 窗口
- [x] P2 真峰天花板 −0.4 → −1.0 dBTP（限制器目标随之 −1.1 dBFS）
- [x] P3 限制器 10 ms 预读/保持 + 10 ms 线性起始斜坡（原 5 ms 保持 + ~1 ms 斜坡）
- [x] P4 TPDF 抖动固定种子，同输入同设置逐位一致
- [x] `ENGINE_VERSION` → `shadowbuster-mastering-v5`，`DSP_ENGINE_VERSION` → `dsp-v6-20261104`
- [x] 回归测试 + 全量回归
- [x] 打包并替换本地安装版

## 起因

`4.在身体散作灰烬之前_shadowbuster.wav`（normal，−9.2 LUFS，声场 wet .5 / +6 dB）在编曲饱满处有类削波听感。
样本域排除：成品 0 个 ≥0 dBFS 样本，真峰 −0.49 dBTP；320k MP3 解码真峰 −0.09 dBTP（无溢出，但余量仅 0.09 dB）。

指纹与 2026-09-30《太虚引》相同——50 ms 块峰值钉死在天花板——但只发生在局部：

| 段落 | 估计平均减益 | 块峰值落在天花板 |
| --- | --- | --- |
| 0:40–1:50 主歌 | ≈0.2 dB | 5–20% |
| 2:00–2:35 副歌 | ≈0.8 dB | 35–48% |
| 3:35–4:55 末副歌 | ≈2.2 dB（max 4.4） | **77–96%** |

源文件末副歌 crest 10.4 dB、无块峰值接近其最大值，失真由母带引入。

根因：`limiter_engagement_budget_20260930` 加的介入预算是**整曲**统计。安静前奏/主歌把整曲
`active_fraction` 稀释到 0.48–0.59，末副歌 10 s 窗口内实为 **1.00**（中位减益 1.6–2.0 dB），闸门未触发。

## 实验（同曲复现链：48k→44.1k、widen(.5, 6)、finalize normal）

起始斜坡（快变增益调制残差 = 输出 − 输入×20 Hz 低通增益，最差 10 s 窗口相对能量）：

| 预读 / 斜坡半宽 | 残差 | LUFS |
| --- | --- | --- |
| 5 ms / ~1 ms（旧） | −36.1 dB | −9.23 |
| 5 ms / 2.5 ms | −36.4 dB | −9.21 |
| 5 ms / 5 ms | −36.8 dB | −9.19 |
| **10 ms / 5 ms** | **−39.0 dB** | −9.25 |

斜坡是次因（约 3 dB 改善、不损失响度）；主因是局部持续介入。

局部预算（10 s 窗口，沿用 0.70 / 2.0 dB，旧天花板）：−10.55 LUFS，最差窗口 active 0.69，max GR 1.40 dB。

## 行为与接口

- `linked_limiter` 统计新增 `local_active_fraction`、`local_gain_reduction_p50_db`：减益曲线 1 ms 抽点，
  10 s 窗口、1 s 步进取最差值；短于 10 s 的素材窗口即全曲（与整曲统计一致）。
- `finalize` 的 `feasible` 同时要求整曲与最差窗口满足 0.70 / 2.0 dB；不新增阈值参数。
- 限制器：`minimum_filter` 半宽 10 ms，`uniform_filter` 半宽 5 ms，增益取 `min(required, smoothed)`。
  斜坡半宽 ≤ 预读，任一峰值的平均窗口内全部 ≤ 该峰 required，峰值安全不依赖 `min`。
- `TRUE_PEAK_CEILING_DB = −1.0`（有损编码过冲余量）；`LIMITER_CEILING_DB = TRUE_PEAK_CEILING_DB − 0.1`。
  `audio_metrics` 的质量约束仍以 −0.4 dBTP 判定（覆盖非独立母带路径），新母带自然通过。
- `write_output` 抖动 `default_rng(0)`。
- 旁车新增 `limiter_local_window_seconds`、`limiter_lookahead_ms`、`limiter_attack_ramp_ms`。
- 不改 preset 数值与前端。目标不可达时如实标 `dynamic_budget`。

## 验证

同曲复现链新引擎：

| preset | LUFS | 真峰 | stop | 整曲 active | 最差窗口 active | max GR | 末副歌块峰值钉死 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| normal（旧引擎） | −9.23 | −0.5 | target_met | 0.48 | 1.00 | 3.77 | 77–96% |
| normal（新） | −11.16 | −1.10 | dynamic_budget | 0.08 | 0.69 | 1.39 | 23% |
| dynamic（新） | −11.24 | −1.10 | target_met | 0.06 | 0.56 | 1.30 | 19% |

即该曲在"不持续钳制 + −1 dBTP"下的上限约 −11.2 LUFS；normal 请求会如实回落并标记。

测试（`tests/test_independent_mastering.py`）：
- `test_local_engagement_budget_protects_a_short_dense_section`：30 s 安静前奏 + 12 s 密集段，局部预算生效且密集段块峰值 IQR > .05；
- `test_whole_song_budget_alone_pins_the_dense_section`：关掉局部窗口 → 整曲统计合规但密集段 IQR < .01，证明差异来自窗口；
- `test_limiter_attack_is_a_ramp_not_a_step`：孤立峰前的中间增益样本 ≥ 0.9 × 10 ms；
- `test_engagement_budget_is_inert_when_the_target_is_reachable` 改用 normal：−1.0 dBTP 后该合成素材的 loud 恰处预算边缘（active 0.698）。

全量回归：`764 passed, 11 skipped, 29 subtests passed`。

## 其余审查结论（未改）

- `oversampling_filter`（257 tap Kaiser 8.6）过渡带约 20.2–23.9 kHz，Nyquist 处仅 −6 dB：4× 限制后降采样会把
  22–24 kHz 调制产物少量混叠到 20–22 kHz，真峰计略低估。不可闻，暂不改。
- 声场加宽在限制器前，会抬高峰值、增加减益；属设计选择。

## 打包与本地安装（2026-11-04）

- `build_shell.ps1` → `runtime_sync.ps1` → ISCC：`packaging/out/ShadowBuster-Setup-1.6.13.exe`（898,634,416 B，SHA-256 `c6b7e894ab0127ceeeebf764f83a92a19f832d49a44127cdf4ac587e4754beff`）。
  版本号沿用未发布的 1.6.13（工作区同时包含报告页 KPI 移除改动）。
- `install_test.ps1 -Version 1.6.13` 覆盖安装到 `packaging/test_install`：GUI 冒烟与 runtime 导入探针通过；安装目录 `runtime/mastering/finalizer.py` 与源码一致。
- 真实输入复核（app 工作区 `_premaster.wav`，widen .5/+6）：normal −11.47 LUFS、loud −11.22 LUFS，均 `dynamic_budget`，
  最差窗口 active ≤0.70，末副歌块峰值钉死 19–28%（旧 77–96%）。用户 GPU 解释器 + 安装版代码端到端运行引擎为 v5、TP −1.10 dBTP。
