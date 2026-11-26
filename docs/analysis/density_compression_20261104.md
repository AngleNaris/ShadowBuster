# 母带密度压缩：段落相对多频段压缩（2026-11-04）

## TODO

- [x] 量化用户 Audition 多频段压缩（"流行音乐大师"改）带来的"更饱满 / 包裹感"
- [x] 检索专业母带多频段压缩默认值
- [x] `mastering/density.py`：段落相对阈值、互补分频、静态补偿、减益封顶
- [x] `pipeline` 接入（声场之后、限制器之前），CLI `--density off|light|medium`，默认 `light`
- [x] `ENGINE_VERSION` → `shadowbuster-mastering-v6`（缓存身份已含 `mastering/*.py` 哈希）
- [x] 测试 `tests/test_mastering_density.py`；全量回归
- [x] 试听版本
- [ ] 用户盲听确认后：前端"密度"档位、打包

## 起因

用户反馈：母带后再在 Audition 加多频段压缩，响度对齐后仍"乐器包裹性更好"，但动态范围变小
（柔和段与高潮的响度差有助听感）。

《熵增的世界》v4 母带 vs Audition 版（已对齐到 −8.68 LUFS），50 ms 块电平：

| 低频 20–120 Hz | 原版 | Audition |
| --- | --- | --- |
| 段内起伏（3 s 窗内 p90−p50 的中位） | 4.42 dB | 2.56 dB |
| 段间差（3 s 窗均值 p90−p30） | 5.08 dB | 3.12 dB |

Audition 同时压了段内起伏（包裹感来源）与段间差（用户不想要的动态损失）——固定 dB 阈值的必然结果。
上游未做压缩（原始→premaster 低频 crest 14.8→14.4、块起伏 6.9→6.8），不会叠加。

## 设计

阈值 = 该频段约 3 s 居中能量 + `offset_db`（离线，无延迟）。主歌按主歌电平压、副歌按副歌电平压。

- 分频 120 / 2k / 10 kHz：二阶 Butterworth `sosfiltfilt` 零相位低通，逐级互补相减，频段之和逐位等于输入。
- 检测：立体声联动功率，居中 RMS 窗（低频 30 ms，> 40 Hz 一个周期；其余 10 ms）→ 软拐点 6 dB → dB 域起始/释放平滑。
- 时间常数（起始/释放 ms）：低 10/200、低中 5/100、高中 3/100、高 3/80。低频释放为中频 2 倍。
- 静态补偿：每段恢复整曲能量，音色不变，只改动态。减益封顶 `max_gr_db`。
- 档位：`light` 1.5:1、offset −3 dB、封顶 4 dB；`medium` 2:1、−3 dB、6 dB。

## 参数依据

专业资料共识（母带轻压）：比例 1.2–2:1，减益约 1–2 dB；低频起始偏慢、释放 ≥ 一个低频周期且约为中频 2 倍；
低分频 100–150 Hz、高分频不低于 2.5 kHz。

- Sonarworks：Pro Mastering Tips – Multiband Compression（低频 ~1.3:1、70–100 ms 起始、75–250 ms 释放）
- Sound On Sound：How And When To Use Multiband Compression / Multi-band Compression tips（低频释放约中频 2 倍；高分频 ≥2.5 kHz）
- audiospectra：Mastering Compression Settings（透明母带 1.2–2:1、30 ms、150 ms、1–2 dB）
- MusicProductionWiki / Unison / Gearspace：低频释放 ≥40 ms 防止跟随波形失真

实测（《在身体散作灰烬之前》premaster + widen .5/+6）起始时间几乎不影响结果（RMS 检测窗主导），
因此采用中等起始；offset/比例按"段内起伏下降、段间差不变、中位减益 1–2 dB"选定：

| 配置 | 低频段内 | 低频段间 | 低频减益 p50(激活)/p95 |
| --- | --- | --- | --- |
| off | 3.41 | 7.28 | – |
| 1.5:1, −3 dB（light） | 2.65 | 7.29 | 1.1 / 2.8 |
| 1.5:1, −6 dB | 2.59 | 7.14 | 2.1 / 3.8 |
| 2:1, −3 dB（medium） | 2.34 | 7.30 | 1.7 / 4.2 |
| 2:1, −6 dB | 2.26 | 7.13 | 3.1 / 5.7 |
| 2.5:1, −6 dB | 2.06 | 7.03 | 3.7 / 6.8 |

## 验证

`tests/test_mastering_density.py`：频段重建、off 恒等、light/medium 段内起伏下降且段间差 ±0.5 dB、
medium 强于 light、减益封顶、CLI 默认 light。全量回归 `770 passed, 11 skipped`。

试听版本（`D:/_4.Projects/_MY/熵增的世界/试听_密度压缩/`，《在身体散作灰烬之前》premaster，loud、widen .5/+6）：

| density | LUFS | 限制器 max GR | 低频段内 / 段间 | 低中段内 / 段间 | 高中段内 / 段间 |
| --- | --- | --- | --- | --- | --- |
| off | −11.22 | 2.17 | 3.39 / 7.12 | 2.81 / 6.52 | 6.61 / 14.69 |
| light | −11.25 | 1.82 | 2.63 / 7.08 | 2.45 / 6.54 | 5.51 / 14.31 |
| medium | −11.30 | 1.70 | 2.34 / 7.05 | 2.29 / 6.59 | 5.10 / 14.31 |

三版响度差 ≤0.08 LU，可直接盲听。密度压缩没有换来更高响度（该曲上限由局部介入预算决定），但限制器最大减益下降 16–22%。

---

# 改版：自研压缩器 → ZL Compressor（VST3）宿主（2026-11-05）

## TODO

- [x] 拆解 Audition 版"带劲"：响度对齐后底鼓尾巴 +2 dB、余音 +3 dB（击打不变），音色微笑曲线；自研 light 只压慢变化
- [x] 复刻 Audition 失败（18 组参数无一同时复现击打/尾巴/高频）→ 改用成熟开源插件
- [x] 可行性验证：pedalboard 0.9.26 + ZL Compressor 0.6.1 —— 0 延迟、逐位可复现、4 段整曲约 22 s、阈值下 −153 dB 透明
- [x] 用户盲听 Clean / Classic / Optical：选 Optical（Classic 略闷）
- [x] `mastering/density.py` 改为 ZL 宿主；保留互补分频；段落跟随阈值；`--density off|light|standard`（默认 light）
- [x] `tools/fetch_plugins.py`（固定版本 + SHA-256）、`/plugins/` gitignore、`runtime_sync.ps1` / `install_test.ps1` 装配与探针
- [x] 测试与全量回归（开发 venv）
- [x] 用户试听 `试听_ZL集成/`：认可 light / standard
- [x] UI「密度」分段 + 后端/CLI 连线
- [ ] 打包并替换本地安装版（待用户指示）

## 设计

- 每段一个 ZL Compressor 实例（Optical、硬拐点、立体声联动 Left Right Max 100%），参数取用户 Audition 预设：
  阈值 −22/−22/−22/−30 dB、比例 2.3/2.6/2.6/3、起始 2/3/2.5/3 ms、释放 100 ms。无逐段补偿（不等量压缩带来的音色倾斜是该预设听感的一部分）。
- 阈值平移 = 输入 LUFS − (−8.68)（Audition 当时的输入响度）+ 档位偏移（light +3 dB、standard 0）。
- 段落跟随：按 2048 样本分块处理（`reset=False`，与整段处理逐位一致），每块阈值再加该频段 3 s 段落电平 − 全曲电平（限幅 −12…+6 dB，
  避免追着淡出/静音压到底）。主歌、副歌各自按"处于全曲平均电平"的力度被压。
- 插件路径 `<mastering 上级>/plugins/ZL Compressor.vst3/Contents/x86_64-win/ZL Compressor.vst3`：开发态为仓库 `plugins/`，打包态为 `runtime/plugins/`。
  缺失时明确报错（提示运行 fetch 工具），不静默回退。

## 验证

《熵增的世界》（loud、widen .5/+6），响度对齐 −11.35：

| 版本 | 底鼓 0-30 / 30-80 / 80-150 | 8–16k | 段落差 LU | 自然响度 |
| --- | --- | --- | --- | --- |
| 旧 A（自研 light） | −14.4 / −19.6 / −21.4 | −19.0 | 7.1 | −11.35 |
| H-Optical（无段落跟随） | −15.4 / −18.5 / −19.7 | −15.9 | 3.3 | −8.92 |
| 新 light | −16.2 / −19.7 / −20.6 | −16.2 | 7.1 | −10.50 |
| 新 standard | −16.1 / −19.6 / −20.6 | −15.9 | 7.0 | −10.51 |

段落跟随保住了段落差（3.3 → 7.0 LU），代价是副歌不再被额外重压，底鼓尾巴回到旧 A 水平——"弹性/密度"与"主副歌起伏"在这里此消彼长，需要试听裁决。
第四首：light −10.82、standard −10.73 LUFS。

与 EQ 的交互：密度压缩位于 EQ 之后，会吸收部分高架（真实音乐 Bright 的 11–16k 相对提升 off +1.20 → light +0.78 → standard +0.64 dB）。
`test_global_eq_changes_final_audio_in_every_reference_case` 以 `density='off'` 隔离 EQ/参考链路；
`test_actual_reference_subprocess_has_no_soren_dependency[packaged]` 的模拟运行时同步复制 `plugins/`。

测试（`tests/test_mastering_density.py`，无 pedalboard/插件时跳过）：分频重建、off 恒等、两档降段内起伏且段落差 ±0.5 dB、
standard 强于 light、关闭段落跟随后段落差缩小 >1 dB、逐位可复现、CLI 默认 light。
全量回归（开发音频 venv `UniverSR/.venv`，已装 pedalboard）：`781 passed, 1 skipped` + 修复后的 packaged 用例单独复跑通过。
系统 Python 3.14 未装 pedalboard，跑母带流水线相关用例需用开发 venv。

## 许可

pedalboard GPL-3.0、ZL Compressor AGPL-3.0（源码 https://github.com/ZL-Audio/ZLCompressor）。本项目开源，打包分发需随附许可与源码链接。

## UI 与参数连线（2026-11-05）

- 母带面板保持两行：第一行「参考音频 | 风格强度」各半；第二行「EQ 风格 | 密度 | 响度」按按键数 4:3:4 分栏
  （`.rack[data-rack="soren"] .rack-fields` 22 栅格：11+11 / 8+6+8）。三行方案会让内容高 732px 溢出 860 默认窗口，且破坏与声场面板逐行对齐，已放弃。
- 「密度」：关 / 轻 / 标准（`data-density` = off / light / standard），默认轻；`localStorage sb_param_density` 持久化，进入预设槽快照。
- 链路：`collectParams().density` → `main.collect_pipeline_kwargs` → `run_pipeline(density=)` → `stage_soren` → `stage_mastering` → `python -m mastering --density`；
  `DEFAULTS["density"] = "light"`，CLI `--density`，质量报告 `processing.density`。只在独立母带路径（无风格 / EQ / 参考）生效；
  阶段缓存身份自动包含该参数。实时草稿播放器不模拟密度压缩（草稿本就是近似）。
- 资源版本 `style.css?v=97`、`app.js?v=92`。测试：`tests/test_density_wiring.py`（UI 默认、CLI 透传、母带命令、GUI 映射）。
  全量回归（开发 venv）：`789 passed, 1 skipped`；同步更新的既有契约：字段顺序（+密度）、`collectParams` 字符串、两处 `stage_mastering` 测试替身接受 `density`。

## 2026-11-20 压缩范围 TODO

- [x] 全局使用一个立体声联动的宽带压缩器。低、中、高频支持自由组合，高频继续使用已有的两个高频分段。
- [x] 接入面板、预设、命令行、缓存和报告。已有预设恢复原来的全频段独立压缩。
- [x] 验证实际交互、分频旁路与全局差异，并使用歌曲片段检查导出。

全局使用 Optical 模式、2:1 压缩比、15 ms 启动、100 ms 释放和 −22 dB 基准阈值。全局与分频均沿用原来的段落阈值跟随、强度档和立体声联动。独立母带报告的 `density.scope` 记录范围，`density.bands` 列出实际进入压缩器的分段。

用《脱离界基面》v4 的母带前音频截取 1:36–1:44，分别以全局、低频加高频、低中高全选导出。三份 PCM24 均通过回读校验，组合分别启用了 1、3、4 个压缩器。该测试验证信号路由，听感由试听判断。
