# 声场宽度分布问题分析报告

**日期**：2026-09-07
**样本**：`造梦双子星2/version2/ARTILUS3_shadowbuster.wav`（v1.6.4 管线产物，styled 模式）
**参照**：原曲 `ARTILUS3.wav`、旧版产物 `pass@1/`、人类商业母带 NIN《As Alive As You Need Me To Be》

## 1. 问题现象

对处理后文件做 M/S（中间/侧边）分频段测量，核心指标为 S/M 能量比（越高越宽）与左右声道相关系数（1.0 为纯单声道，越小越宽）。

### 1.1 整体宽度：已达人类母带水平

| 指标 | 原曲 | pass@1 (08-25) | version2 (09-07) | NIN 人类母带 |
|---|---|---|---|---|
| 左右相关系数 | 0.885 | 0.882 | **0.676** | 0.689 |
| S/M 宽度 | −12.1 dB | −12.0 dB | **−7.1 dB** | −7.4 dB |
| 单声道兼容损失 | −0.26 dB | −0.26 dB | −0.77 dB | −0.73 dB |
| 瞬时反相帧占比 | 2.3% | 0.0% | 6.0% | 1.1% |

整体宽度和相关系数与 NIN 几乎重合：**总量达标，无单声道抵消风险**。

### 1.2 宽度分布：与人类母带相反

| 频段 | version2 S/M | NIN S/M | 差异 |
|---|---|---|---|
| 20–60 Hz | −17.5 dB（相关 0.97） | −20.0 dB（0.98） | 均为准单声道低频 |
| 60–150 Hz | **−5.4 dB（0.55）** | −9.9 dB（0.81） | version2 宽 4.5 dB |
| 150–400 Hz | −4.0 dB（0.43） | −4.7 dB（0.49） | 接近 |
| 400 Hz–2.5 kHz | −4.1 ~ −6.3 dB | −3.5 ~ −4.3 dB | NIN 略宽 |
| 2.5–6 kHz | −5.1 dB（0.53） | −3.6 dB（0.40） | NIN 宽 1.5 dB |
| 6–20 kHz | **−11.8 ~ −12.1 dB（0.87–0.89）** | **−5.8 ~ −5.9 dB（0.58–0.59）** | NIN 宽约 6 dB |

- **version2**：宽度集中在低中频（60–150 Hz 全曲最宽），高频相对"贴中间"。
- **NIN 人类母带**：低音区收紧（相关 0.81），宽度堆在 2.5 kHz 以上的"空气感"频段（相关 0.4–0.59）。

即：**总量对了，分布反了**。这正是本报告要解释的差异。

## 2. 根因一：前置声场算法是"保形放大"，不是"重塑分布"

### 2.1 算法实现

母带（`stage_soren`）之前有独立的声场重塑阶段 `stage_reshape`（studio_backend.py:513），调用 `apollo_scripts/soundstage_reshape.py`。管线硬编码参数（studio_backend.py:531）：

```
--mode broadband --wet 1.0 --side-gain-db 6.0
```

broadband 预设（soundstage_reshape.py:183）的语义：

- `other` 轨（铺底/合成器/混响）的 side 通道**全频段 ×2.0（+6 dB）**；
- `drums` 轨 side ×1.41（+3 dB，固定为设定值的一半）；
- bass / vocals 轨不动；
- 高频 shelf 参数为 0，代码注释自述"**无频谱倾斜，纯 side 增益**"；
- 无低频保护，所有频段的 side 一视同仁。

### 2.2 后果

宽带增益**不改变 side 的频谱分布**，只是把混音里"原本就有 side 能量的地方"等比放大。立体声铺底与混响的 side 内容天然集中在低中频，于是 60–150 Hz 成为全曲最宽的频段。

实测各频段 side 能量变化（对总能量归一，原曲 → version2）：

| 频段 | side 变化 | 归因 |
|---|---|---|
| 20–60 Hz | +5.7 dB | reshape 全额通过 |
| **60–150 Hz** | **+6.3 dB** | 与算法 +6 dB 严格一致 |
| 150–400 Hz | +4.3 dB | 被母带链收掉一部分（见根因二） |
| 400 Hz–12 kHz | +2.1 ~ +2.9 dB | 被母带链收掉约 3–4 dB |
| 12–20 kHz | +4.9 dB | 原曲起点极低（S/M −18.7），净增仍不足 |

### 2.3 讽刺点：人类式预设存在但从不启用

`MODES` 里已有面向人类分布的预设：

- `shelf-air`：side 在 7 kHz 以上再 +3 dB（把宽度推向"空气感"，即人类做法）；
- `shelf3k`：3.5 kHz 以上 +3 dB；
- `dynamic`：shelf3k 参数 + de-esser 式动态门（镲片瞬态时自动收）。

但 `stage_reshape` 写死 `--mode broadband`，这些预设永远不会被管线走到。

## 3. 根因二：母带链的采样率错配，反向收窄中高频 side

version2 中高频 side 只涨了 2~3 dB（而非 +6 dB），是 styled 母带链造成的。

### 3.1 涉案代码

`process_audio`（styled 链，packaging/soren_original.py）step 2 调用：

```python
# soren_original.py:825
target_side = low_shelf_tighten(target_side, config.internal_sample_rate,
                                cutoff_freq=100, gain=0.5, order=4)
```

而此时信号已被 `oversample(target, 4)` 升到 **176.4 kHz**；`low_shelf_tighten`（soren_original.py:1044）内部用 `butter(order, cutoff/nyquist)` 按 **44.1 kHz（Nyquist 22050）** 设计，再作用于 176.4 kHz 信号——归一化截止频率被放大 4 倍，**实际截止频率 = 400 Hz**（实测响应：400 Hz 处 −2.9 dB，800 Hz 处 −24 dB）。

叠加其实现 `audio*gain + lowpass(audio)*(1-gain)`（gain=0.5，即"保低切高"），净效果为：**side 通道 ~500 Hz 以上被压掉最多 6 dB（−6 dB 渐近）**。

### 3.2 复合效应

- `add_subtle_mid_channel_saturation`（soren_original.py:152）只对 mid 做 tanh 饱和，产生的相关谐波抬高 mid 高频能量，进一步压低高频 S/M 比。
- `match_rms_ms`（soren_original.py:248）对 side 的增益**无上限**（直接 `reference_rms / target_rms`）；`finalize_stereo_image` 是宽带 side ≤+15%。两者都是"总量"控制，不塑造分布。
- 18 kHz 低通、限幅器等对宽度分布影响中性。

结论：高频 side 被"reshape 加 6 dB、母带链收 6 dB"，12–20 kHz 最终只到 S/M −12.1 dB，比 NIN（−5.8）窄约 6 dB；低中频则全额保留 +6 dB，形成"低宽高窄"的最终形态。

> 附注：`low_shelf_tighten` 的函数名与参数（cutoff=100）暗示本意可能是"收紧 side 低频"（业界常规是 side 低频收窄），但其实现是保低切高，方向本身就与常规相反——修复前需先确认设计意图。

## 4. 结论

| 维度 | 状态 | 说明 |
|---|---|---|
| 宽度总量 | ✅ 达标 | +6 dB 宽带 side 增益使整体 S/M、相关系数与人类母带持平 |
| 宽度分布 | ❌ 与人类相反 | broadband 保形放大把宽度留在低中频；人类做法是低收顶开 |
| 单声道兼容 | ✅ 安全 | 损失 0.77 dB（<3 dB 无抵消风险），反相帧 6% 偏高但可接受 |
| 母带链 side 高频架 | 🐛 双重缺陷 | 采样率错配（100 Hz→实际 400 Hz）+ 实现方向疑似与意图相反 |

pass@1（08-25 产物）宽度与原曲一致而 version2（09-07）明显变宽，说明 reshape 链路是近期版本才实际生效的行为。

## 5. 修复建议

按收益排序：

1. **重塑宽度分布（主要矛盾）**：`stage_reshape` 改用 `shelf-air` 模式（自带 +3 dB@7 kHz side shelf），或给 broadband 的 side 增益加 ~250 Hz 高通（低频不 boost）+ 顶部 side shelf。目标形态：60–150 Hz 收回 3–4 dB，6 kHz 以上再加 4–6 dB。
2. **修复采样率错配**：`low_shelf_tighten` / `boost_band` / `high_shelf_boost` 一律给 `scipy.signal.butter` 传 `fs=当前实际采样率`（或把 cutoff 除以过采样倍数 `oversampling_factor`）。同文件中 `apply_eq_style` 也有同样隐患（仅 eq_style≠Neutral 时触发，本次 Neutral 未受影响）。
3. **确认 `low_shelf_tighten` 设计意图**：若本意是"side 低频收窄"（标准做法），应改为高通语义；`gain=0.5` 的"保低切高"实现需要重写。
4. **给 side 增益加上限**：`match_rms_ms` 的 side 增益建议加 clip（soren_core 版已有 `_bounded_gain_db`，styled 路径的 soren_original 版没有）。
5. **回归验证**：修复后用同一首歌重跑管线，对比分频段 S/M 分布与 NIN 参照的贴合度；单声道兼容损失应保持 <1 dB。

## 6. 测量方法备注

- M/S 分解：`M=(L+R)/2, S=(L−R)/2`；S/M 宽度 = side RMS / mid RMS（dB）。
- 分频段：8192 点 Hann 窗、50% 重叠 STFT，按频段累计 M/S 功率与互谱；相关系数 = ΣRe(L·R*)/√(Σ|L|²·Σ|R|²)。
- 绝对能量分解：各频段 M/S 功率对全曲总功率归一后求差，以剥离整体响度变化。
- 三个版本的时长均为 298.2 s（同一首歌，可逐段对齐）；NIN 样本为 48 kHz MP3 解码。
- 本次分析未做主观听音对比，分频段结论为客观测量；不同曲目间的对比含编曲差异，属风格参考而非严格结论。
