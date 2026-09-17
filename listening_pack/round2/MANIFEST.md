# ShadowBuster 低频试听校准包 Round 2（弹性隔离：sub 固定 2dB，弹性递增）

- 生成时间：2026-09-17 13:57　版本：v1.6.10 @ 489aa29　盲听种子：20260918
- DSP：真实生产脚本 apollo_scripts/bass_enhance.py + drum_enhance.py（子进程原样调用）
- 响度匹配：integrated LUFS（audio_metrics），非 ref 变体 → 同素材 ref0，残差 ≤ 0.2 LU；削波余量不足时整类统一衰减后再匹配。文件均为 32-bit float WAV。
- **合成素材不含分离残差**：separation artifacts 不在本包覆盖范围内；真实歌曲素材（如有）包含分离残差与全频段内容。

## 听音时回答的问题（§6.7）

- 低频量：rc1/rc1e 是否与 rc0 保持同一量级（sub 不变，低频量不应反弹变多）？
- 弹性：相比 rc0，kick 起音 / bass 律动是否更有弹性？rc1 与 rc1e 哪个合适？
- 过量检查：rc1e 是否开始出现低频偏多 / 抽吸（pumping）/ 低频变硬？
- mono 折叠（小音箱）下以上结论是否仍然成立？

## 比对协议（§13.4）

- 所有变体已响度匹配（≤ 0.2 LU），用**外部播放器**以舒适音量逐对比较，不要用浏览器/混音软件做判断。
- **偏好与损伤分开评**：哪个更好听（preference）与 是否出现抽吸/变薄/断裂/染色（damage）分别记录。
- **隔离实验**：非 ref 变体的 sub/sat 完全相同（低频量维度固定），仅弹性维度（punch/trans/sidechain/clarity 授权）递增；rc0 为第一轮锚点（低频量对、弹性不足）。
- 同素材类内比较（A 组内比 A、B 组内比 B …）；mono 文件用于小音箱/单声道检验。
- ref0 是无处理恒等参考（响度匹配后），可作为“原样”基线。

## 素材 A — A_shortkick_sustained_bass：短 kick（每 0.5s，~80ms 衰减，60Hz 体 + 2kHz click）叠持续 bass（55Hz 基频 + 二次谐波，每 2s 换音），~120 BPM。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| A_v3.wav | 30.00 | -19.38 | +0.00 | 恒等参考 |
| A_v4.wav | 30.00 | -19.38 | -1.38 |  |
| A_v1.wav | 30.00 | -19.38 | -1.20 |  |
| A_v2.wav | 30.00 | -19.38 | -0.87 |  |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| A_v3_mono.wav | 30.00 | -19.38 |
| A_v4_mono.wav | 30.00 | -19.38 |
| A_v1_mono.wav | 30.00 | -19.38 |
| A_v2_mono.wav | 30.00 | -19.38 |

## 素材 B — B_long808_dense_kick：长 808 音符（40–55Hz 滑音，0.8s 指数衰减，每 1s 触发）+ 密集 kick（每 0.25s），~120 BPM。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| B_v4.wav | 30.00 | -24.27 | +0.00 | 恒等参考 |
| B_v3.wav | 30.00 | -24.27 | -1.50 |  |
| B_v1.wav | 30.00 | -24.27 | -1.31 |  |
| B_v2.wav | 30.00 | -24.27 | -0.99 |  |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| B_v4_mono.wav | 30.00 | -24.27 |
| B_v3_mono.wav | 30.00 | -24.27 |
| B_v1_mono.wav | 30.00 | -24.27 |
| B_v2_mono.wav | 30.00 | -24.27 |

## 素材 R — 真实歌曲（demucs htdemucs 四轨分离 bass/drums；in_mix = 原始输入，34.1s）。注意：真实素材包含分离残差与全频段内容，与合成素材互补。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| R_v1.wav | 34.12 | -8.92 | +0.00 | 恒等参考；整类统一衰减 -2.87dB |
| R_v4.wav | 34.12 | -8.92 | +1.59 | 整类统一衰减 -2.87dB |
| R_v3.wav | 34.12 | -8.92 | +1.85 | 整类统一衰减 -2.87dB |
| R_v2.wav | 34.12 | -8.92 | +2.37 | 整类统一衰减 -2.87dB |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| R_v1_mono.wav | 34.12 | -10.29 |
| R_v4_mono.wav | 34.12 | -10.19 |
| R_v3_mono.wav | 34.12 | -10.17 |
| R_v2_mono.wav | 34.12 | -10.15 |

---

# ⚠️ 以下为 DSP 诊断信息（含变体身份剧透，完成听音前勿往下读）

## 素材 A 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\bassout_A_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\final_A_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\bassout_A_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.50) sidechain=on conf=1.00 duck_p95=1.26dB duck_max=1.31dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\final_A_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- rc1（旋钮：sub=2.0dB sat=0.2 punch=2.0dB trans=0.3 sidechain=0.3333 auto_clarity=on(auth=0.6667)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\bassout_A_rc1.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.67) sidechain=on conf=1.00 duck_p95=1.67dB duck_max=1.75dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\final_A_rc1.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc1e（旋钮：sub=2.0dB sat=0.2 punch=3.0dB trans=0.4 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\bassout_A_rc1e.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=1.00 duck_p95=2.51dB duck_max=2.62dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_A\final_A_rc1e.wav | punch=3.0dB trans=0.4 | 44100Hz 2ch

## 素材 B 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\bassout_B_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\final_B_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\bassout_B_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.50) sidechain=on conf=0.85 duck_p95=1.08dB duck_max=1.13dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\final_B_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- rc1（旋钮：sub=2.0dB sat=0.2 punch=2.0dB trans=0.3 sidechain=0.3333 auto_clarity=on(auth=0.6667)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\bassout_B_rc1.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.67) sidechain=on conf=0.85 duck_p95=1.44dB duck_max=1.50dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\final_B_rc1.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc1e（旋钮：sub=2.0dB sat=0.2 punch=3.0dB trans=0.4 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\bassout_B_rc1e.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=0.85 duck_p95=2.16dB duck_max=2.26dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\class_B\final_B_rc1e.wav | punch=3.0dB trans=0.4 | 44100Hz 2ch

## 素材 R 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\bassout_R_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\final_R_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\bassout_R_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(-0.62, 0.915)(auth=0.50) sidechain=on conf=0.24 duck_p95=0.23dB duck_max=0.35dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\final_R_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- rc1（旋钮：sub=2.0dB sat=0.2 punch=2.0dB trans=0.3 sidechain=0.3333 auto_clarity=on(auth=0.6667)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\bassout_R_rc1.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(-0.826708, 1.220061)(auth=0.67) sidechain=on conf=0.24 duck_p95=0.30dB duck_max=0.47dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\final_R_rc1.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc1e（旋钮：sub=2.0dB sat=0.2 punch=3.0dB trans=0.4 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\bassout_R_rc1e.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(-1.24, 1.83)(auth=1.00) sidechain=on conf=0.24 duck_p95=0.46dB duck_max=0.70dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\round2\_work\final_R_rc1e.wav | punch=3.0dB trans=0.4 | 44100Hz 2ch

盲名 ↔ 变体映射见 **KEY.md**（先完成听音再读）。
