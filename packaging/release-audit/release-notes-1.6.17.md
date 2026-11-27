# ShadowBuster v1.6.17

本版将全部四个独立母带响度档位统一至调优后的 `ZL Limiter 0.0.1` 限幅引擎，并修正分频密度压缩仅选局部频段时的音色倾斜问题。

- **统一母带限幅引擎（`shadowbuster-mastering-v11`）**：`柔和 / 动态 / 均衡 / 响亮` 四档统一使用 `ZL Limiter 0.0.1`（4× 过采样真峰、100% 立体声联动）与原曲时间轴段落动态保护（`section_profile`），解决此前非最大档因 `linked_limiter` 150 ms 释放触发 `local_active_fraction <= 0.70` 而在大动态曲目上提前截停、响度推不上去的问题。
- **消除最大响度档硬削波**：
  - 将 `ZL Limiter` 内部时间常数从默认 `lookahead 2 ms / attack 100 ms / recovery 50 ms` 调优为 `lookahead 6–8 ms / attack 20–25 ms / recovery 120–150 ms / release 250 ms`，显著降低低频谐波失真与天花板尖峰/平顶削波。
  - 全部档位在限幅前接入 4× 过采样 C1 光滑预软削峰（默认 `loud` 1.5 dB、其余三档 1.2 dB；纯增益可达时保持零重采样直通）。
  - 新增 10 ms RMS 全曲与 10 s 局部窗口限幅压减监测及分档过载保护预算，避免超大动态管弦配乐无节制硬顶 `+24 dB` 驱动。
- **分频密度压缩自动增益补偿**：每个启用压缩的频段按 `sqrt(p_in / p_out)` 做 RMS 等响度自动补偿（`makeup_gain_db`，上限 12 dB），修复仅勾选低频等局部频段时该频段下陷导致未选中的中高频整体抬升的问题。
- **报告遥测补全**：全部响度档位（含「响亮」档）均在报告页「处理量化明细」中如实展示「限幅压减（峰值 / P95 / 预算保护）」与「软削峰」统计。

## 下载与升级

Windows x64 使用 `ShadowBuster-Setup-1.6.17.exe` 安装或覆盖升级。已有 GPU 1.6.11 的用户可以继续使用，无需重新下载 GPU 环境。macOS 本次提供源代码和更新后的装配脚本。

Windows 安装包的 `licenses/mastering/` 包含上游许可证文本和对应版本的源码链接。 [母带依赖清单](https://github.com/AngleNaris/ShadowBuster/tree/v1.6.17/packaging/third-party/mastering)

## 验收

- 全量回归及新增单测通过（含分频密度压缩音色平衡测试与统一 `ZL Limiter` 防过载预算测试）。
- Windows 独立安装及本机原位覆盖升级后，实际冻结 GUI（Qt 6.11.2、桥接 1.6.17）、ZL Compressor、ZL Limiter、`mastering.loud.finalize_zl`、`mastering.density.densify`（含每频段 `makeup_gain_db`）与运行时全量导入检查均通过。

## 安装包校验

`ShadowBuster-Setup-1.6.17.exe`，904,122,295 字节。

SHA-256 `da61c2af15a2c19069dca33d0c51871c6c508c34870cd00dafc2c78e66745212`，也可下载 `SHA256SUMS.txt` 核对。
