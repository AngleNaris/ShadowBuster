# 独立无风格母带：首批实现与验证

日期：2026-09-19。范围：计划批次 A 的初步可行性验证及 B 的工程实现。用户参考生产路径、声场拆分、预置风格入口调整与完整 Soren 依赖移除尚未实施。

## 实现

- `mastering/` 独立实现响度搜索、4× 过采样、声道联动限幅、动态预算和 PCM24+dither 输出；使用共享 `audio_metrics` 的响度测量。
- `style_mode=off` 通过 `stage_mastering` 调用音频解释器，不加载 Soren/profile/reference。保留 `stage_soren` 作为过渡分发接口，未改 UI/CLI 模式名称与默认值。
- 新算法用离线 ±5 ms 峰值预读/保持、1 ms 攻击平滑与指数释放，双声道共用一个增益。每次响度试算从原始数组重算，不累计多轮限幅。
- 不需要限幅时直接对原始数组施加统一增益，避免无意义的升降采样往返；测试验证该路径逐样本等于原声乘统一系数。
- 固定目标 soft/dynamic/normal/loud = −12.30/−11.14/−9.20/−6.70 LUFS。旧默认 Pop 基准 −9.1995547642，加原档位偏移后四舍五入到 0.01 LU；不拷贝或分发旧 profile。
- 延续原产品 P95/最大 GR 预算：soft、dynamic 4/14 dB；normal 6/18；loud 8/20。释放 soft/dynamic/normal 150 ms，loud 80 ms。它们是兼容值，尚不代表最佳听感阈值。
- TP 天花板 −0.4 dBTP，测量窗与当前质量报告对齐（4× Kaiser 8.6、line padding）。只量化一次，回读最终 PCM24 文件复测并更新旁车，不能达标时不返回成功。
- 保留取消、阶段缓存、报告复制与母带旁路；新模块与测量源码参与缓存身份。打包运行时解析支持 Apollo + mastering 而无需 Soren 文件夹。
- Windows/macOS 装配增加新源码；Windows 增加同源比较和关键 manifest。未实际装配，旧 Soren 仍供其他路径使用。

## 来源审计

`git log -S` 将原 linked limiter/process_transparent 追溯到 bca1f60，但该提交同时首次加入整份 soren_core，无法仅凭提交确定其中所有代码的来源。因此没有复制整份旧核心或从中机械抽取函数；新 finalizer 根据通用限幅/响度处理原理独立编写，以既有行为合同与测量数据校验。

`audio_metrics.py` 为仓库已有测量模块，本轮没有更改它。Matchering 源码未复制进仓库，安装验证版为 2.0.6；GPL 发行决策仍在正式接入/发行阶段处理。

## 测试

- 改动前：604 passed / 1 skipped / 29 subtests passed。
- 最终完整回归：627 passed / 1 skipped / 29 subtests passed（57.51 s），包含 22 项新增独立母带测试与新旧引擎缓存失效参数化覆盖。
- 已覆盖 44.1/48/88.2/96 kHz 输入边界与不含 Soren 的独立 runtime 目录真实子进程执行。
- Python 编译、PowerShell 装配脚本语法解析、macOS 装配脚本 `bash -n` 与 `git diff --check` 通过。当前会话无 IDE/cclsp diagnostics，未声称执行过这类诊断。
- 核验覆盖真实子进程执行与进度、取消、无 Soren 资源、源文件不可覆盖、动态预算未达标、非有限值/静音拒绝、声道比例、低限幅状态频谱、PCM24 回读以及缓存复用/源码失效。

## 两段真实音乐的新旧对照

输入来自现有 `_e2e_tmp/inference_samples/set_1/input.wav`（15.001 s）和 `set_2/input.wav`（9.160 s），只读使用。当前实验不包含 Lew/Demucs 重新推理，是对同一真实输入比较最终母带。

| 素材 | 档位 | 旧 LUFS | 新 LUFS | 新 TP dBTP | 结果 |
|---|---|---:|---:|---:|---|
| set_1 | soft | −12.94 | −13.12 | −0.50 | 动态预算限制 |
| set_1 | dynamic | −12.94 | −13.12 | −0.50 | 动态预算限制 |
| set_1 | normal | −11.32 | −11.59 | −0.50 | 动态预算限制 |
| set_1 | loud | −8.73 | −9.18 | −0.50 | 动态预算限制 |
| set_2 | soft | −12.30 | −12.30 | −4.55 | 达标 |
| set_2 | dynamic | −11.14 | −11.14 | −3.39 | 达标 |
| set_2 | normal | −9.20 | −9.20 | −1.45 | 达标 |
| set_2 | loud | −7.08 | −7.30 | −0.42 | 动态预算限制 |

全部新输出长度、采样率保持，TP 不越界。受限幅较重的素材，新旧差 0.18–0.45 LU，反映独立限幅包络行为不同，不主张位级等效。最终 Side/Mid 相对旧输出差约 0.00–0.05 dB（两段素材范围）；不能由此保证所有音乐主观宽度不变。

已生成两段×四档×新旧版本的等响 FLOAT 试听文件，命名 `*_equal_loudness.wav`。输出目录：`experiments/results/mastering_migration_20260919/baseline/`。这些文件仅供本地试听，不进安装包。还未进行用户听感验收。

重点试听：set_1 normal/loud 的鼓与混响尾部；set_2 normal 的透明度、loud 的密度。低动态预算下 soft/dynamic 在 set_1 收敛到相同响度是已知兼容行为，不是假装达标。

## Matchering 2.0.6 初步实测

环境：本地 `.venv-mastering`，Python 3.12.11、numpy 2.5.2、scipy 1.18.0、soundfile 0.14.0、pyloudnorm 0.2.0、numba 0.67.0、statsmodels 0.14.6、resampy 0.4.3。环境借用基础解释器可见的部分包，不是完全自包含的发行环境；正式打包还需干净环境验证。

- 使用 `Result(..., "FLOAT", use_limiter=False, normalize=False)`；探针将 Matchering limiter 替换为抛错函数，所有成功请求均未调用它。
- 两段真实输入均保持原始样本数；未限幅候选 TP 分别约 +11.07/+9.14 dBTP。FLOAT 保存正常，但这些是中间候选，不能直接作为最终成品。
- 44.1/48/88.2/96 kHz 的同内容合成信号：源/参考同率及单独变更源或参考采样率，共十组；输出均 44100 Hz、132300 帧、数值有限。
- 十组合成信号都测到 +1 sample 延迟；对齐后相关性接近 1。源码的偶数长度 FIR 与 `fftconvolve(..., "same")` 裁剪是后续适配检查点。现阶段仅记录，不直接对任意真实音乐用互相关估计并强制平移。
- mono/near-mono 能处理；1000 帧短输入被长度检查拒绝；静音报 `math domain error`。后续 adapter 应提供明确的用户错误/回退原因。
- 小素材匹配约亚秒到秒级，详见原始 JSON；本轮并行运行其他测试，且新旧 baseline 一个为子进程、一个进程内调用，耗时不可作为公平性能排名。
- 未测 15 分钟以上素材、整曲峰值内存和 GPU 打包解释器；不将短片段验证描述为生产就绪。

## 证据与复现

工具：`tools/probe_mastering_migration.py`，`baseline` 模式使用旧开发运行时，并先检查 core 与权威源码一致；`matchering` 模式在隔离环境执行。输入、参考均由参数提供，不从网络获取，不写原文件。

原始 JSON、日志与音频（本地实验目录，git 忽略）：

- `experiments/results/mastering_migration_20260919/baseline/baseline.json`
- `experiments/results/mastering_migration_20260919/matchering/matchering.json`

精简可提交指标见同目录文档 `mastering_migration_probe_20260919.json`，含输入 SHA-256 与版本信息，不包含音频。

## 下一批 C 的具体入口

1. adapter 先实现真实采样率/带宽、FLOAT 候选、已知长度/时延验证；只依赖用户参考，不引入风格资产。
2. 拆分现有 reshape 的去拥挤/降噪和最终宽度；不将旧分轨 delta 直接叠到匹配后的混音。
3. 先做候选接受/拒绝及无风格回退，再验证参数域参考强度；拒绝回退不得调用 Soren。
4. 匹配前后和最终文件均检查分频 M/S、mono、动态与音色预算。具体阈值尚需基于更多样本标定，不能直接把上述两段素材当作通用上限。
5. 接通后再迁移 GUI/CLI 设置、隐藏预置风格并清理 Soren 装配；不在首批冒进删除仍被用户参考/EQ 路径调用的资源。
