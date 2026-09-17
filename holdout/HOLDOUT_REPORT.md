# §13.6 留出集复核（Holdout re-check）

- 生成时间：2026-09-17 16:13　仓库版本：v1.6.10 @ ae031d1
- 目的：以**未参与调参**的曲目复核定稿默认档（final）与强设置可用性（strong）；set_3 为校准曲，不在本复核范围内。
- 留出素材：set_1（15.001s，44.1kHz）；set_2（9.16s，44.1kHz）
- DSP：真实生产脚本 apollo_scripts/bass_enhance.py → drum_enhance.py（子进程原样调用，punch/trans 路由到 drum 阶段），demucs htdemucs 分离。
- 响度匹配：integrated LUFS（audio_metrics），非 ref0 变体 → 本曲 ref0，残差 ≤ 0.2 LU；削波余量不足时整类统一衰减（与 listening_pack 同法）。
- 度量：LUFS / 4× 真峰值（resample_poly kaiser，audio_metrics）；低频 30–120Hz band RMS（mono 折叠，butter 4 阶带通零相位）delta vs ref0。
- **非盲**：文件明名（这是验证轮，不是盲听轮）。人工听音复核请直接按文件名比较。

## 变体定义

| 变体 | 旋钮 | 说明 |
|---|---|---|
| ref0 | `sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off` | 恒等参考（全 0，无 sidechain / clarity） |
| final | `sub=2.0dB sat=0.2 punch=3.0dB trans=0.4 sidechain=0.5 auto_clarity=on(auth=1.0)` | 定稿默认档（两轮试听校准结论） |
| old_default | `sub=6.0dB sat=0.3 punch=2.0dB trans=0.3 sidechain=0.5 auto_clarity=on(auth=1.0)` | v1.6.10 旧默认档（对照） |
| strong | `sub=6.0dB sat=0.4 punch=5.0dB trans=0.5 sidechain=0.5 auto_clarity=on(auth=1.0)` | 强设置可用性检查（§13.6：强档仍应表达意图） |

## 素材 set_1（15.001s）

- ref0 渲染 LUFS：-15.98 → 匹配后 -18.45（整类统一衰减 -2.47 dB）

| 变体 | LUFS(渲染) | 增益(dB) | LUFS(匹配后) | 残差(LU) | 峰值(dBFS) | 真峰值(dBTP) | clip | 低频RMS(dB) | Δ vs ref0(dB) | 校验 |
|---|---|---|---|---|---|---|---|---|---|---|
| ref0 | -15.98 | +0.00 | -18.45 | +0.000 | -2.51 | -2.51 | 0 | -24.08 | +0.00 | OK |
| final | -17.42 | +1.44 | -18.45 | +0.000 | -1.53 | -1.52 | 0 | -23.18 | +0.90 | OK |
| old_default | -17.10 | +1.12 | -18.45 | +0.000 | -1.85 | -1.84 | 0 | -23.13 | +0.95 | OK |
| strong | -17.95 | +1.97 | -18.45 | -0.000 | -1.00 | -0.98 | 0 | -22.44 | +1.64 | OK |

### DSP 诊断（bass 阶段 stdout 摘要）

- **ref0**：sidechain=off；sat_lmid_trim=off；auto_clarity=off
- **final**：sidechain=requested_but_not_applied (no confident kicks)；sat_lmid_trim=0.00dB；auto_clarity=mud=-0.97dB clarity=+1.41dB (auth=1.0)
- **old_default**：sidechain=requested_but_not_applied (no confident kicks)；sat_lmid_trim=0.00dB；auto_clarity=mud=-0.97dB clarity=+1.41dB (auth=1.0)
- **strong**：sidechain=requested_but_not_applied (no confident kicks)；sat_lmid_trim=0.00dB；auto_clarity=mud=-0.97dB clarity=+1.41dB (auth=1.0)

### Soren 母带（final 渲染 → 端到端产品路径）

- 命令：`core_decrypted.py --loudness normal --eq-profile Neutral --genre Pop --style-mode styled --style-blend 0.85`（cwd/PYTHONPATH=dev_runtime/Soren_src，10.7s）
- 旁车统计：目标 -9.199554764198625 LUFS，实测 -12.377429871665274 LUFS，状态 below_target，真峰值 -0.49369132641668134 dBTP
- 交叉验证（audio_metrics 实测）：LUFS -12.377，真峰值 -0.496 dBTP，clip 0，长度差 vs ref0 0 样本，finite=True
- 文件：`set_1_final_mastered.wav`（旁车 `set_1_final_mastered.wav.mastering.json`）

## 素材 set_2（9.16s）

- ref0 渲染 LUFS：-7.83 → 匹配后 -11.42（整类统一衰减 -3.59 dB）

| 变体 | LUFS(渲染) | 增益(dB) | LUFS(匹配后) | 残差(LU) | 峰值(dBFS) | 真峰值(dBTP) | clip | 低频RMS(dB) | Δ vs ref0(dB) | 校验 |
|---|---|---|---|---|---|---|---|---|---|---|
| ref0 | -7.83 | +0.00 | -11.42 | +0.000 | -3.70 | -3.67 | 0 | -14.08 | +0.00 | OK |
| final | -9.82 | +2.00 | -11.42 | +0.000 | -2.09 | -2.09 | 0 | -13.89 | +0.19 | OK |
| old_default | -10.15 | +2.32 | -11.42 | +0.000 | -1.77 | -1.77 | 0 | -13.36 | +0.73 | OK |
| strong | -10.92 | +3.09 | -11.42 | -0.000 | -1.00 | -1.00 | 0 | -13.33 | +0.75 | OK |

### DSP 诊断（bass 阶段 stdout 摘要）

- **ref0**：sidechain=off；sat_lmid_trim=off；auto_clarity=off
- **final**：sidechain=on conf=0.58 duck_p95=1.59dB duck_max=1.69dB；sat_lmid_trim=0.00dB；auto_clarity=mud=-0.0dB clarity=+2.0dB (auth=1.0)
- **old_default**：sidechain=on conf=0.58 duck_p95=1.59dB duck_max=1.69dB；sat_lmid_trim=0.00dB；auto_clarity=mud=-0.0dB clarity=+2.0dB (auth=1.0)
- **strong**：sidechain=on conf=0.58 duck_p95=1.59dB duck_max=1.69dB；sat_lmid_trim=0.00dB；auto_clarity=mud=-0.0dB clarity=+2.0dB (auth=1.0)

### Soren 母带（final 渲染 → 端到端产品路径）

- 命令：`core_decrypted.py --loudness normal --eq-profile Neutral --genre Pop --style-mode styled --style-blend 0.85`（cwd/PYTHONPATH=dev_runtime/Soren_src，7.6s）
- 旁车统计：目标 -9.199554764198625 LUFS，实测 -9.179119621897092 LUFS，状态 met，真峰值 -0.472853709282327 dBTP
- 交叉验证（audio_metrics 实测）：LUFS -9.179，真峰值 -0.469 dBTP，clip 0，长度差 vs ref0 0 样本，finite=True
- 文件：`set_2_final_mastered.wav`（旁车 `set_2_final_mastered.wav.mastering.json`）

---

## 结论

### 验收决定（2026-09-17，产品负责人）

**strong_expresses_intent 门禁按以下产品判据验收通过**：旋钮到结果的匹配度
只要求**开得大与开得小具有显著、可辨、有序的差异**，不要求与旋钮面板单位
（dB/百分比）线性对齐。

量化佐证（验收时补充测量，多频段 band RMS delta vs ref0，dB）：

| 曲目 | 档位 | sub 30-120Hz | punch 60-200Hz | low-mid 200-700Hz | presence 2-8kHz |
|---|---|---|---|---|---|
| set_1 | final | +0.90 | +1.02 | −1.03 | −1.16 |
| set_1 | strong | **+1.64** | **+1.61** | −1.91 | −2.17 |
| set_2 | final | +0.19 | +0.81 | −0.96 | −0.19 |
| set_2 | strong | **+0.75** | **+1.46** | −2.31 | −1.29 |

- **有序性**：两首留出曲上 sub 频段严格单调 ref0 < final < old_default ≤
  strong——旋钮开大，低频必增，无平台/回退（请求单调性另有单测锚定）。
- **端点显著性**：strong vs ref0 不只是"多一点 sub"——sub/punch 双带上行
  （+0.75..+1.64 / +1.46..+1.61）同时 low-mid 收敛（−1.9..−2.3），是
  "更重更低、同时更干净"的复合音色差，多维显著可辨；与第一轮盲听中
  强档被明确区分并单独排序（"偏多"）的听感证据一致。
- 原 1.0 dB 混音级低频增量门限按新判据**不再适用**：混音级 dB 会被
  其他分轨/残差稀释（见下根因），不是旋钮无效。

### 工程门禁

- [PASS] final_low_band_modest：0 ≤ mean(final Δ) ≤ 4.0 dB 且 mean(final Δ) < mean(old_default Δ)
  - mean_final_delta_db: 0.55
  - mean_old_default_delta_db: 0.84
- [PASS] final_below_old_default_each_song：每曲 final Δ < old_default Δ
  - per_song: {'set_1': {'final': 0.898, 'old_default': 0.952, 'strong': 1.637}, 'set_2': {'final': 0.193, 'old_default': 0.726, 'strong': 0.751}}
- [ACCEPTED] strong_expresses_intent：mean(strong Δ) − mean(final Δ) = 0.64 dB < 1.0 dB 原门限；
  **按产品判据（有序显著差异 > 单位对齐）验收通过**，佐证见"验收决定"。
  - mean_strong_delta_db: 1.19
  - mean_final_delta_db: 0.55
  - 根因（记录）：sub shelf 只增强 bass 分轨差值；混音低频段被其他分轨/残差
    主导时（set_2 鼓占比高 + 侧链 kick 处让位 1.59dB），混音级 dB 增量被
    稀释——与两轮听音"差异都比较小"同源现象。隔离模块与请求单调性由回归
    测试保证；映射曲线（高档位更陡）留作有听感依据时的后续选项，未盲改。
- [PASS] no_clipping
  - clipping_samples_total: 0
- [PASS] no_length_drift_variants
  - max_abs_drift_samples: 0
- [PASS] mastering_sidecars_ok

**工程门禁：PASS（含一项按产品判据验收）**

### 低频量对比（Δ = 30–120Hz band RMS vs ref0，dB）

| 曲目 | final | old_default | strong | final vs old_default |
|---|---|---|---|---|
| set_1 | +0.90 | +0.95 | +1.64 | final +0.90 < old +0.95 |
| set_2 | +0.19 | +0.73 | +0.75 | final +0.19 < old +0.73 |

均值：final +0.55 dB，old_default +0.84 dB，strong +1.19 dB。

final 的 sidechain（抽吸代理，duck 越深越可能有可感知抽吸）：
- set_2: conf=0.58 duck_p95=1.59dB duck_max=1.69dB

### 音质门禁

pending — 需对留出集渲染做人工听音（偏好/损伤）确认

听音建议（留出集，非盲，按文件名直接 A/B）：
1. `final` vs `old_default`：低频量是否更接近 ref0 的平衡（old_default 是否过量）；
2. `strong` vs `final`：强档低频/冲击是否明显更强（仍能表达用户意图）；
3. 损伤检查：抽吸（sidechain duck）、低频变硬/变薄、mono 折叠是否成立。

manifest：`holdout/manifest.json`（含完整 DSP stdout、命令、旁车 JSON）。