# CLI 与 Bass 自动清晰度

## 入口与发行版

源码：`python main.py --cli --help`，或 `python processing_cli.py --help`。
重新通过 `ShadowBuster.spec` 打包后：`ShadowBuster.exe --cli ...`。CLI 分支在导入 Qt 前执行，不启动窗口；静态 import 会被 PyInstaller 收集，无需单独复制 processing_cli.py。

v1.6.6 起发行版包含该入口。旧发行版 EXE 不具备该入口；源码修改不会自动更新已安装程序，需要安装新版。
运行需要完整配套 runtime 和模型，与 GUI 相同。Agent 可启动进程、等待退出并读取 JSON，不需要点击界面。无控制台 EXE 不保证终端可见输出，应使用 `--result-json`。

```powershell
$arguments = '--cli -i "D:\Music\song.wav" -o "D:\Music\processed" --bass-auto-clarity --sub-db 4 --punch-db 2 --result-json "D:\Music\run-001.json"'
$p = Start-Process -FilePath 'C:\Program Files\ShadowBuster\ShadowBuster.exe' -ArgumentList $arguments -Wait -PassThru
$p.ExitCode
Get-Content -Raw -Encoding UTF8 'D:\Music\run-001.json'
```

将 EXE 路径替换为实际安装路径。`--input` 可以重复提供；同名输出冲突会拒绝。默认不覆盖已有音频，确需覆盖时显式使用 `--overwrite`。结果 JSON 必须是新路径，避免覆盖旧报告。运行成功退出 0；处理/文件错误退出 1；参数解析错误退出 2（解析错误不生成 JSON）。JSON 包含 status、exit_code、outputs、error。

`--cpu` 使用 CPU，默认 cuda。主要旋钮与 UI 范围一致：Sub 0–12dB，Punch 0–10dB，sat/trans/space-wet/space-denoise 0–1，space-width-db 0–12dB。默认母带 style-mode=off（UI 无风格），默认 space-wet=.6、space-denoise=.2。`--loudness loud` 不只是更高的目标响度（流派 profile +2.5dB），还配合更快的限制器恢复（release 由 150ms 收紧到 80ms，其余响度档保持 150ms）。`--style-mode styled` 时 `--style-blend` 是风格处理强度（0–1，默认 0.85）：0 不施加风格处理，1 完整风格处理；它是处理强度而非波形比例，100%（1.0）与旧版"末端干湿混合"的实现不再位级一致。off / eq_only 忽略该强度；eq_only 的 EQ 与强度相互独立。`--help` 列出完整参数。bypass 不跳过分轨阶段，仅当低频/鼓/声场/人声四个分轨消费阶段全部旁路时才自动跳过分轨（此时分轨产物无人消费）。旁路 Lew 时输入若探测到非 44.1kHz 采样率，会先统一重采样为 44.1kHz 再进入后续阶段（Soren 母带只接受 44.1k；已是 44.1k 不重编码；六个阶段全部旁路时逐字节透传）。

## 母带验证与试听

将 `lew,bass,drums,reshape,vocals` 全部旁路而保留 `soren`，表示原曲直接进入母带（必要时重采样），不代表重跑了前三个上游处理环节。比较风格强度应使用相同母带输入、相同响度档和 EQ；不能把与历史完整流水线成品的差异全部归因于母带。

`loud` 是目标响度档，不保证在任何素材上固定增加音量。Pop 的目标约 −6.70 LUFS，属于较高密度输出；达到目标若超过动态预算，优先保留安全输出并在统计中标记未达标。更重视自然动态时使用 normal 或 dynamic。

## 自动清晰度

自动清晰在应用内恒开（发闷必然有害；有界 ±2dB 且有置信度门控）。CLI `--bass-auto-clarity` 为 opt-in（后端默认关，与 GUI 行为的差异是有意的：命令行保留完全显式的处理语义）。该功能与后端参数，默认关闭。开启后先对整首 Bass 分轨做一次联动频谱分析，再在 Sub/饱和前施加约 285Hz 的低中频削减和 800Hz 的清晰度提升，各最多 2dB。左右声道使用相同 EQ，分析平均声道功率而非相加波形。

这是固定规则的保守启发式，不是机器学习音色分类器，也不能保证识别分离串音。静音、纯低频、噪声样频谱和已明亮的合成测试素材不提亮；不生成缺失谐波，不改变 Sub 旋钮含义，不保证所有歌曲更好听。采用全曲固定 EQ，不是逐音符动态自适应。

应用一直将 Punch/瞬态送到 drums 分轨；Bass 的这两个内部参数保持 0 是有意路由，不是 GUI 漏处理。新清晰度开关则已贯通 GUI、CLI、Bass DSP。

## 高频自适应降噪（Stage3，可选）

默认 `--noise-mode other`（兼容路径）：仅沿用既有的 other 轨 ≥10kHz 噪声地板降噪，`--noise-low-hz` 等新参数不进入处理命令，默认命令的输出行为与旧版一致。要启用 Stage3 自适应降噪需显式传 `--noise-mode adaptive_all`（opt-in）。

adaptive_all 的参数范围：`--noise-low-hz` 8000–20000（默认 8000）、`--noise-high-hz` 8000–22000（默认 20000）、`--noise-max-attenuation-db` 0–6（默认 6）。adaptive_all 要求 low < high；非法模式按参数解析错误退出 2，越界数值或无效频带在处理开始前拒绝（退出 1，错误写入结果 JSON）。`--other-denoise-amount` 在两种模式下都是共同的降噪强度；`--space-wet` 仍只缩放声场宽度——wet=0 且降噪量 >0 时降噪照常执行（与既有行为一致，wet=0 且降噪量=0 才逐字节透传）。

adaptive_all 对可用分轨（drums/other，以及存在的 vocals/bass）分别做置信度门控的高频衰减：仅当频段被判定为稳态噪声地板时才衰减，通道链接、只衰减不增益，衰减上限 = max-attenuation-db × amount，混音级还有能量预算约束（分轨削減叠加不允许把混音抬响）。已知限制：这不是完美的噪声/音乐分类器——不保证噪声被完全去除，也不保证音乐完全不受影响；STFT 窗有泄漏，cut off 不是时间域砖墙；极短素材（<0.75s）与静音段直接跳过。诊断数据（每轨统计与混音预算）写入 reshape 报告的 `extra.noise`，并进入最终 `<成品>.quality.json` 的 `stages` 段；该报告与阶段成品一起缓存，缓存命中同样恢复相同诊断；被旁路或透传的阶段不产生阶段数据，也绝不引用上次运行遗留的报告。本次发布 DSP 代码变更（新增 noise_profile 等）按既有缓存身份语义使旧阶段缓存整体失效一次，属预期行为。

## 人声压缩与空气补偿（默认关闭）

`--vocal-comp-amount`（0–1，默认 0）启用有界宽带人声压缩：阈值取活动窗口电平 P55（只压偏响部分）、表观比约 2.2:1、增益衰减上限 4.5×amount、attack 15ms/release 150ms、立体声联动；宽带增益不改频谱。`--vocal-air-db`（0–3）在 11kHz 高架上给人声只加不削的空气提升；典型用法是镜像补偿母带链 8kHz 高架的 −1.5dB（即传 1.5），使人声空气感穿过母带不丢失。两项都关闭（默认）时人声阶段输出与旧版逐位一致。GUI 下人声压缩默认 0.4、空气补偿自动镜像，无需操作。

人声轨参与自适应降噪：AI 人声的嘶声烙在人声内容里，分离后主要落在 vocals 轨，排除它等于放弃降噪的主要目标。空气感的余量改用**人声轨衰减上限减半**（默认 6dB cap → 人声 3dB）：气声/齿音由瞬态与谐波保护负责（达不到高置信度就不会被削），而确认是稳定嘶声的部分仍会被处理。

## 六轨分离与可选分轨增强（Stage4，opt-in）

默认 `--demucs-model htdemucs`（四轨，命令与输出行为与旧版一致）。显式传 `--demucs-model htdemucs_6s` 时分离额外输出 guitar/piano 分轨，并在声场重塑之后、人声之前路由两个默认中性的有界增强阶段（顺序 guitar → synth）：

- **synth（合成器/键盘层）= other + piano 两轨求和**：覆盖合成器、铺底、效果器、键盘、钢琴等和声性内容。这是同模型分轨的精确数学合并（同长度、同采样率），用于解决"很多歌没有钢琴、独立 piano 轨常为空"的问题；它不是合成器专用分离器，组内也无法再拆分。
- 每组五个控制（范围即硬上限，全部默认 0=中性位级透传）：`--guitar-*` / `--synth-*` 的 `gain-db`（−6..6，全轨增益）、`mud-cut-db`（0..6，300Hz 泥浊削减）、`presence-db`（0..6，3kHz 存在感高架）、`harsh-cut-db`（0..6，7kHz 毛刺收敛）、`width-db`（0..6，side 宽度，M/S 声道联动）。
- 全部控制只作用于该组的处理差值（delta-add），混音残差照常保留；峰值保护与其他阶段同一约定（−0.5dB 静态缩放，scale 入报告）。注意 synth 组是全频段的，可能含低中频能量；低频可先用 mud-cut 收敛。
- 源分轨缺失时透明降级：透传混音并在阶段报告标记 unavailable/missing_stem（synth 组要求 other 与 piano 都在）；四个分轨消费阶段全部旁路时这两个阶段与分离一起跳过。
- 模型名进入 stem 产物目录与处理缓存身份：`htdemucs` 与 `htdemucs_6s` 的产物不混用，切换模型会使分离及后续阶段缓存重算。诊断进入 `<成品>.quality.json` 的 `stages.guitar` / `stages.synth`，`processing` 段在六轨运行时记录 `demucs_model` 与各组控制。
- 已知限制（能力评估见 docs/analysis/six_stem_eval_20260913.md；未做听感验证，不构成音质宣称）：htdemucs_6s 的混合残差比四轨大约 8–10dB（分解更不完整）；分出的 guitar 可能包含被误标的合成器/弦乐内容——六轨分组不等于乐器隔离。权重（HF 量化约 +53MB）在构建期预置进 runtime hf_home，安装后离线可用。

## 处理缓存

应用内提供片段级「预览」（选择 5–45 秒片段按当前面板参数快速渲染试听，正式批处理不做实时试听）。调整参数后需要重新处理或重新渲染预览，再用外部播放器打开成品比较。

GUI 与 CLI 共用处理缓存和容量设置。默认开启，容量上限为 5 GiB；缓存位于 `%LOCALAPPDATA%\ShadowBuster\processing-cache`。设置界面提供关闭及 2 / 5 / 10 / 20 / 50 / 100 GiB 选项，显示已用空间，并支持二次确认后清空。超过容量时优先淘汰较久未使用的阶段结果；降低容量会立即淘汰超额结果，设为 0 会清空阶段结果并关闭缓存。清空不删除原始歌曲或输出目录中的成品。

CLI 的 `--no-cache` 仅对本次运行停用缓存读写，不删除已有缓存。开发与测试可用 `SB_PROCESSING_CACHE_DIR` 指定独立缓存目录。

同一输入通过文件内容 MD5 识别。每个阶段还检查上游结果、参数和算法／运行环境标识；修改后段参数可以复用前段结果，修改上游参数会使受影响的后续缓存失效。引擎源码（Soren_src 的 `*.py`，含 core_decrypted / soren_original）内容参与算法标识：算法更新或 dev runtime 重建后旧缓存自动失效，不会命中旧算法结果。母带统计旁车与母带成品一起缓存，缓存命中同样恢复统计（见下节）。首次处理仍需完整计算；缓存被淘汰或损坏时重新计算。缓存仅用于加速，不提供试听。

## 母带统计旁车

Soren 母带成功后，引擎把统计写到母带输出旁的 `<out>.mastering.json`：目标/实测 LUFS、真峰值、`target_status`（met / below_target / above_target）等。该旁车与成品一起进入处理缓存——缓存命中也能恢复统计，不会因命中而丢失指标；统计内容只来自引擎，缺失（母带被旁路、引擎未产出）时一律不生成，也绝不伪造旧缓存的统计。处理成功后统计旁车复制为输出目录里的 `<最终wav>.mastering.json`（成品同名加后缀）；本次运行无统计时会移除同名的过期旁车，避免旧渲染的指标被误读。GUI 既有进度日志会展示目标 LUFS / 实测 LUFS / 达标状态。旁车文件按字节复制，原样保留引擎输出（含 NaN 字面量），不改写、不丢字段。

## 内部音频格式（float32 链路）

内部链路（FFmpeg 中转、Lew 干湿混合、参考人声分离）统一使用 44.1kHz/双声道/32-bit float WAV，16-bit 截断台阶只存在于历史版本；最终量化只发生在 Soren 母带的 PCM24 输出（含 TPDF dither）。转换后校验采样率/声道/可读性，失败即报错，绝不静默回退 PCM16。全链路旁路仍逐字节透传用户文件；Soren 被旁路时最终产物是内部 float 格式，`quality.json` 的 `processing.output_subtype` 如实记录实际 subtype（不伪装 PCM24）。缓存身份携带 `audio_format` 版本，旧 PCM16 时代的缓存产物不会与本版本混用。

## 中间文件

中间产物（Lew / 分轨 / 各阶段混音、bass/drums/reshape 报告、工作目录内的母带统计旁车）只在本次工作目录生成并随工作目录清理；失败时不复制半成品。成功后输出目录保留成品 WAV 与其统计旁车 `<最终wav>.mastering.json`（见上节）。历史遗留文件不自动删除，仅同名过期统计旁车在本次无统计时移除。
