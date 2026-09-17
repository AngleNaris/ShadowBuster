# UI 旋钮 → 处理语义映射（20260913，批次 1–5 能力收编）

## 设计约束与原则

- **不新增 UI 控件**（面向非专业用户），只收编既有旋钮的语义；
- 新能力（侧链、自适应降噪、六轨）默认的安全性与有界性已由批次 2–4 保证，
  UI 映射只决定"哪个既有旋钮以多大比例驱动它"；
- 隐藏参数（`noise_mode` / `sidechain_amount` / `demucs_model` 等）显式传入时
  一律优先于映射值——高级用户/自动化可精确覆盖，普通用户无感；
- 映射是纯函数 `main.map_ui_params(params)`，可独立测试（9 项合同测试）。

## 映射表

| UI 控件（既有） | 原语义 | 新增/修订语义 | 边界 |
|---|---|---|---|
| 高频降噪 fader（0–1，默认 0.2） | 仅 other 轨 ≥10kHz 地板降噪（legacy） | **>0 时切换 Stage3 `adaptive_all`**：跨分轨置信度降噪（六轨运行覆盖 guitar/synth 组）；fader=0 保持 legacy | 只衰减不增益；上限 6dB×amount；无稳定嘶声的歌几乎零变化（如实告知） |
| 鼓身旋钮（0–10dB，默认 2） | drums punch/瞬态处理 | **同时驱动 Kick/Bass 有界让位**：amount = 0.5×punch/10（默认 0.1，最大 0.5） | 20–180Hz、attack 5ms/release 150ms、max duck 6dB 硬上限；punch=0 完全关闭 |
| 质量档（快速/标准/精细） | Lew 分块策略 | **精细档启用六轨 `htdemucs_6s`**（吉他与合成器/键盘分层，自适应降噪覆盖更全） | 权重未预置时明确回退四轨并写进度日志；六轨增强组保持中性，不擅自加音色 |
| Sub/瞬态/饱和/自动清晰/宽度/声场/人声/流派/响度/风格强度 | 不变 | 不变 | — |

## 为什么这样映射

1. **降噪**：非专业用户的目标是"去沙沙声但不破坏空气感"。legacy 地板降噪 audible
   但会伤镲片/空气；adaptive_all 按置信度只在确认稳定嘶声时动作，对干净歌零伤害
   ——把"fader 拉高才有变化、干净歌无变化"作为特性如实写进帮助文案。
2. **鼓身**：用户 complaint 是"鼓偏小、低频没弹性、Kick/Bass 互相遮蔽"。鼓身旋钮
   的语义本来就是"鼓的冲击力"，Kick 出现时 Bass 让位正是冲击力的一部分；侧链全部
   有界并在 `quality.json`/阶段报告显示实际 duck 量，量级由用户旋钮控制。
3. **质量档**：精细=更细的分层是直觉语义；六轨 GPU 分离实测比四轨更快、显存相当
   （docs/analysis/six_stem_eval_20260913.md），成本不构成负担；权重缺失时的回退
   是显式降级（进度日志可见），绝不静默混合。

## 实现

- `main.map_ui_params(params, six_stem_available=None)` → `(mapping, notices)`;
  `_run_batch` 解包 `**mapping` 进 `run_batch`，notices 写进度日志；
- `studio_backend.six_stem_weights_available()`：只查 HF 缓存目录（打包环境查
  预置 hf_home），不加载模型；
- UI 仅修改既有控件的帮助/aria 文案（质量档、鼓身、高频降噪），控件数量为零增加；
- 测试：`tests/test_ui_param_mapping.py`（9 项）+ 批次 5 `tests/test_float_internal.py`（7 项）；
  全量回归 522 passed, 1 skipped, 29 subtests。

## 批次 5（同日完成）：内部链路 float32 化

- `ffmpeg_convert` 默认 `pcm_f32le` + 转换后校验（sr/声道/可读），失败报错不回退；
- `mix_wet_dry` 重写：soundfile float64 计算、float32 写出，删除 `<i2` 截断与
  wave 模块回退；`_read_wav_pcm16`/`_ensure_pcm16`（死代码）删除；
- 缓存身份加入 `audio_format: f32-internal-1`；质量报告记录 `output_subtype`
  （Soren 旁路时如实记录 FLOAT，不伪装 PCM24）；全链路旁路仍逐字节透传。
- 精度测试：−94 dBFS 斜坡细节经混合后保留（容差 1e-6，远小于 16-bit 台阶 3e-5）。

## 修订（20260913 第二轮）：人声压缩/空气联动 + 吉他 UI

用户需求：人声也做压缩；人声 EQ 联动最终母带 EQ；**无论如何不能丢失人声空气感**；
吉他音色进 UI（高频/声场面板）。

| UI 控件 | 语义 | 边界 |
|---|---|---|
| （无新控件，UI 默认启用）人声压缩 | amount 0.4 的有界宽带压缩：阈值=活动窗口电平 P55，≈2.2:1，GR 上限 4.5dB，attack 15ms/release 150ms，立体声联动 | 宽带增益不改频谱（空气感无频谱损失）；CLI 默认 0=位级不变，`--vocal-comp-amount` 显式控制 |
| （无新控件）人声空气 EQ | 镜像补偿母带 8kHz 高架衰减（`SOREN_HIGH_SHELF_MID_DB=-1.5` → 人声 +1.5dB @11kHz高架），**只加不削**，封顶 2dB | 显式 `--vocal-air-db`（0–3）覆盖；comp/air 关闭时人声阶段与旧实现逐位一致 |
| 声场面板新增"吉他"推子（0–100%，默认 0） | 0=中性；非零时 presence=4v/mud=2.5v/harsh=1.5v/电平小幅跟随（+1.5v−0.75），并**自动要求六轨** | 权重缺失→明确提示回退四轨（吉他调整不生效）；显式 `--guitar-*` 分项优先 |

### 空气感硬保障（产品级取舍）

自适应降噪（`adaptive_all`）**排除 vocals 轨**（报告 `vocal_air_protected`）：人声的
气声/齿音可能被置信度门控误伤，与"无论如何不丢空气感"冲突。人声轨自带的嘶声因此
不在此处理——这是明确取舍，不是遗漏。

### 测试

`tests/test_vocal_compress_air.py`（7 项：宽带性/上限/响轻分层/静音/只加不削/位级
契约/参数拒绝）+ 映射测试扩至 16 项；全量回归 **535 passed, 1 skipped, 29 subtests**。

## 修订（20260913 第三轮）：人声轨回到降噪范围（上限减半）

用户指出：AI 人声的嘶声烙在人声内容里，分离后主要落在 vocals 轨——"整体排除人声轨"
等于放弃降噪的主要目标。修订：

- **取消 `vocal_air_protected` 排除**，vocals 轨正常参与 adaptive_all 降噪；
- 空气感余量改为**人声轨最大衰减减半**（`max_attenuation_db × 0.5`，默认 6→3dB）：
  置信度门控 + 瞬态保护 + 谐波保护不变（真气声/齿音达不到高置信度），确认是稳定
  嘶声的部分会被处理，最坏情况对人声空气的损伤有 3dB 硬顶；
- 人声空气高架（镜像补偿，只加不削）与有界压缩不受影响；
- 测试：新增 `test_adaptive_vocal_stem_gets_halved_cap`（人声 cap=3.0、其余 6.0），
  恢复六轨全处理断言；全量回归 **536 passed, 1 skipped, 29 subtests**。
