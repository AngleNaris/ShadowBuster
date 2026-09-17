# ShadowBuster 低频试听校准包（§6.7 低频专项验收 / §13 试听校准）

- 生成时间：2026-09-17 13:10　版本：v1.6.10 @ 0347559　盲听种子：20260917
- DSP：真实生产脚本 apollo_scripts/bass_enhance.py + drum_enhance.py（子进程原样调用）
- 响度匹配：integrated LUFS（audio_metrics），非 ref 变体 → 同素材 ref0，残差 ≤ 0.2 LU；削波余量不足时整类统一衰减后再匹配。文件均为 32-bit float WAV。
- **合成素材不含分离残差**：separation artifacts 不在本包覆盖范围内；真实歌曲素材（如有）包含分离残差与全频段内容。

## 听音时回答的问题（§6.7）

- kick 起音是否更清楚（每个变体 vs 同类其它变体）？
- bass 音符是否更可辨（音高/音符边界）？
- 长尾音（808/sub/持续 bass）是否连续、有无断裂或抖动？
- 有无抽吸（pumping）/低频变薄（kick 落下瞬间 bass 被吃掉）？
- mono 折叠（小音箱）下以上结论是否仍然成立？

## 比对协议（§13.4）

- 所有变体已响度匹配（≤ 0.2 LU），用**外部播放器**以舒适音量逐对比较，不要用浏览器/混音软件做判断。
- **偏好与损伤分开评**：哪个更好听（preference）与 是否出现抽吸/变薄/断裂/染色（damage）分别记录。
- **强设置应仍能表达用户意图**：strong 档若听起来与 default 无差别或反而更弱，属于校准问题，请记录。
- 同素材类内比较（A 组内比 A、B 组内比 B …）；mono 文件用于小音箱/单声道检验。
- ref0 是无处理恒等参考（响度匹配后），可作为“原样”基线。

## 素材 A — A_shortkick_sustained_bass：短 kick（每 0.5s，~80ms 衰减，60Hz 体 + 2kHz click）叠持续 bass（55Hz 基频 + 二次谐波，每 2s 换音），~120 BPM。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| A_v1.wav | 30.00 | -19.38 | +0.00 | 恒等参考 |
| A_v3.wav | 30.00 | -19.38 | -3.19 |  |
| A_v2.wav | 30.00 | -19.38 | -1.38 |  |
| A_v4.wav | 30.00 | -19.38 | -5.58 |  |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| A_v1_mono.wav | 30.00 | -19.38 |
| A_v3_mono.wav | 30.00 | -19.38 |
| A_v2_mono.wav | 30.00 | -19.38 |

## 素材 B — B_long808_dense_kick：长 808 音符（40–55Hz 滑音，0.8s 指数衰减，每 1s 触发）+ 密集 kick（每 0.25s），~120 BPM。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| B_v2.wav | 30.00 | -24.27 | +0.00 | 恒等参考 |
| B_v4.wav | 30.00 | -24.27 | -3.86 |  |
| B_v3.wav | 30.00 | -24.27 | -1.50 |  |
| B_v1.wav | 30.00 | -24.27 | -6.74 |  |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| B_v2_mono.wav | 30.00 | -24.27 |
| B_v4_mono.wav | 30.00 | -24.27 |
| B_v3_mono.wav | 30.00 | -24.27 |

## 素材 C — C_acoustic_bass_drums：原声质感 bass（80–110Hz + 谐波至 ~500Hz，拨弦包络，每 1s 换音）+ 鼓组（kick 1&3 拍、snare 2&4 拍 180Hz+噪声、hat 8 分音 8kHz 噪声、hat 有宽度），120 BPM。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| C_v2.wav | 30.00 | -20.56 | +0.00 | 恒等参考 |
| C_v3.wav | 30.00 | -20.56 | -1.66 |  |
| C_v1.wav | 30.00 | -20.56 | -0.93 |  |
| C_v4.wav | 30.00 | -20.56 | -2.83 |  |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| C_v2_mono.wav | 30.00 | -20.57 |
| C_v3_mono.wav | 30.00 | -20.57 |
| C_v1_mono.wav | 30.00 | -20.55 |

## 素材 D — D_sub_only_electronic：纯 sub 正弦 30–60Hz 滑音（portamento，每 3s 换音）+ 轻微 hat（8kHz 噪声 tick，8 分音）作节拍参照。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| D_v2.wav | 30.00 | -16.66 | +0.00 | 恒等参考 |
| D_v4.wav | 30.00 | -16.66 | -5.88 |  |
| D_v3.wav | 30.00 | -16.66 | -2.55 |  |
| D_v1.wav | 30.00 | -16.66 | -8.12 |  |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| D_v2_mono.wav | 30.00 | -16.68 |
| D_v4_mono.wav | 30.00 | -16.67 |
| D_v3_mono.wav | 30.00 | -16.67 |

## 素材 R — 真实歌曲（demucs htdemucs 四轨分离 bass/drums；in_mix = 原始输入，34.1s）。注意：真实素材包含分离残差与全频段内容，与合成素材互补。

| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |
|---|---|---|---|---|
| R_v2.wav | 34.12 | -10.79 | +0.00 | 恒等参考；整类统一衰减 -4.74dB |
| R_v4.wav | 34.12 | -10.79 | +3.00 | 整类统一衰减 -4.74dB |
| R_v3.wav | 34.12 | -10.79 | +1.60 | 整类统一衰减 -4.74dB |
| R_v1.wav | 34.12 | -10.79 | +4.24 | 整类统一衰减 -4.74dB |

mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：

| 盲名 | 时长(s) | LUFS |
|---|---|---|
| R_v2_mono.wav | 34.12 | -12.16 |
| R_v4_mono.wav | 34.12 | -11.95 |
| R_v3_mono.wav | 34.12 | -12.05 |

---

# ⚠️ 以下为 DSP 诊断信息（含变体身份剧透，完成听音前勿往下读）

## 素材 A 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\bassout_A_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\final_A_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- default_v1610（旋钮：sub=6.0dB sat=0.3 punch=2.0dB trans=0.3 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\bassout_A_default_v1610.wav | sub=6.0dB punch=0.0dB sat=0.3 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=1.00 duck_p95=2.51dB duck_max=2.62dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\final_A_default_v1610.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\bassout_A_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.50) sidechain=on conf=1.00 duck_p95=1.26dB duck_max=1.31dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\final_A_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- strong（旋钮：sub=9.0dB sat=0.5 punch=4.0dB trans=0.5 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\bassout_A_strong.wav | sub=9.0dB punch=0.0dB sat=0.5 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=1.00 duck_p95=2.51dB duck_max=2.62dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_A\final_A_strong.wav | punch=4.0dB trans=0.5 | 44100Hz 2ch

## 素材 B 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\bassout_B_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\final_B_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- default_v1610（旋钮：sub=6.0dB sat=0.3 punch=2.0dB trans=0.3 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\bassout_B_default_v1610.wav | sub=6.0dB punch=0.0dB sat=0.3 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=0.85 duck_p95=2.16dB duck_max=2.26dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\final_B_default_v1610.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\bassout_B_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.50) sidechain=on conf=0.85 duck_p95=1.08dB duck_max=1.13dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\final_B_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- strong（旋钮：sub=9.0dB sat=0.5 punch=4.0dB trans=0.5 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\bassout_B_strong.wav | sub=9.0dB punch=0.0dB sat=0.5 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=0.85 duck_p95=2.16dB duck_max=2.26dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_B\final_B_strong.wav | punch=4.0dB trans=0.5 | 44100Hz 2ch

## 素材 C 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\bassout_C_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\final_C_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- default_v1610（旋钮：sub=6.0dB sat=0.3 punch=2.0dB trans=0.3 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\bassout_C_default_v1610.wav | sub=6.0dB punch=0.0dB sat=0.3 trans=0.0 sat_lmid_trim=3.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=0.38 duck_p95=0.59dB duck_max=0.62dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\final_C_default_v1610.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\bassout_C_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=1.42dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.50) sidechain=on conf=0.38 duck_p95=0.29dB duck_max=0.31dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\final_C_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- strong（旋钮：sub=9.0dB sat=0.5 punch=4.0dB trans=0.5 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\bassout_C_strong.wav | sub=9.0dB punch=0.0dB sat=0.5 trans=0.0 sat_lmid_trim=3.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=on conf=0.38 duck_p95=0.59dB duck_max=0.62dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_C\final_C_strong.wav | punch=4.0dB trans=0.5 | 44100Hz 2ch

## 素材 D 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\bassout_D_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\final_D_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- default_v1610（旋钮：sub=6.0dB sat=0.3 punch=2.0dB trans=0.3 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\bassout_D_default_v1610.wav | sub=6.0dB punch=0.0dB sat=0.3 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=requested_but_not_applied (no confident kicks) | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\final_D_default_v1610.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\bassout_D_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=0.50) sidechain=requested_but_not_applied (no confident kicks) | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\final_D_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- strong（旋钮：sub=9.0dB sat=0.5 punch=4.0dB trans=0.5 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\bassout_D_strong.wav | sub=9.0dB punch=0.0dB sat=0.5 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(0.0, 0.0)(auth=1.00) sidechain=requested_but_not_applied (no confident kicks) | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\class_D\final_D_strong.wav | punch=4.0dB trans=0.5 | 44100Hz 2ch

## 素材 R 诊断

- ref0（旋钮：sub=0.0dB sat=0.0 punch=0.0dB trans=0.0 sidechain=0.0 auto_clarity=off）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\bassout_R_ref0.wav | sub=0.0dB punch=0.0dB sat=0.0 trans=0.0 sat_lmid_trim=off (heuristic, not listening-verified) auto_clarity=off sidechain=off | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\final_R_ref0.wav | punch=0.0dB trans=0.0 | 44100Hz 2ch
- default_v1610（旋钮：sub=6.0dB sat=0.3 punch=2.0dB trans=0.3 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\bassout_R_default_v1610.wav | sub=6.0dB punch=0.0dB sat=0.3 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(-1.18, 1.73)(auth=1.00) sidechain=on conf=0.27 duck_p95=0.51dB duck_max=0.70dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\final_R_default_v1610.wav | punch=2.0dB trans=0.3 | 44100Hz 2ch
- rc0（旋钮：sub=2.0dB sat=0.2 punch=1.5dB trans=0.25 sidechain=0.25 auto_clarity=on(auth=0.5)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\bassout_R_rc0.wav | sub=2.0dB punch=0.0dB sat=0.2 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(-0.59, 0.865)(auth=0.50) sidechain=on conf=0.27 duck_p95=0.25dB duck_max=0.35dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\final_R_rc0.wav | punch=1.5dB trans=0.25 | 44100Hz 2ch
- strong（旋钮：sub=9.0dB sat=0.5 punch=4.0dB trans=0.5 sidechain=0.5 auto_clarity=on(auth=1.0)）
  - bass: Bass-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\bassout_R_strong.wav | sub=9.0dB punch=0.0dB sat=0.5 trans=0.0 sat_lmid_trim=0.00dB (heuristic, not listening-verified) auto_clarity=(-1.18, 1.73)(auth=1.00) sidechain=on conf=0.27 duck_p95=0.51dB duck_max=0.70dB | 44100Hz 2ch
  - drums: Drum-enhanced mix done: D:\_3.AI\audio_upscale\SorenStudio\listening_pack\_work\final_R_strong.wav | punch=4.0dB trans=0.5 | 44100Hz 2ch

盲名 ↔ 变体映射见 **KEY.md**（先完成听音再读）。
