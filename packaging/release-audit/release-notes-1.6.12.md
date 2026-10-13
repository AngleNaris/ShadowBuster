# ShadowBuster v1.6.12

修复 v1.6.11 Windows 图形界面启动时的 `ImportError: DLL load failed while importing QtCore`。

## 修复内容

- 构建时隔离 Windows DLL 搜索路径，防止 Qt 的系统 ICU 依赖被构建环境 PATH 中的 Poppler ICU 78 替代。后者导出 `ucnv_open_78`，Qt 需要 `ucnv_open`，造成入口缺失。
- 覆盖安装自动清理 v1.6.11 残留的 `_internal/icuuc.dll` 和 `_internal/icudt78.dll`。
- 构建与安装验收增加实际冻结 GUI 检查：Qt 导入、主窗口、WebEngine 页面、界面 DOM、QWebChannel 桥接和版本。此前只运行 CLI 音频链，绕过了 Qt，未发现 GUI 启动错误。
- 音频处理算法不变，保留 v1.6.11 的全部 DSP 和质量报告修复。

## 升级

关闭客户端后覆盖安装。本版应用版本为 **1.6.12**，GPU 环境继续使用 **1.6.11**；已经安装 GPU 1.6.11 的用户无需重新下载。旧 GPU 1.5.0 的用户仍需在设置中安装 GPU 1.6.11。

本次发布 Windows 安装器、校验文件和源码。macOS 未构建新应用包。

## 验证

- 全量回归：**767 passed / 1 skipped / 29 subtests passed**，154.75 秒；跳过项为缺少历史本地实验音频。
- 重新构建外壳与 CPU 运行时；CPU 依赖导入、四轨/六轨分离及离线模型加载通过。
- 冻结 EXE 的 offscreen 与 Windows 窗口路径均通过：Qt 6.11.2，439 个界面元素，页面加载完成、QWebChannel 桥接就绪，应用/桥接版本均为 1.6.12。
- 本机从 1.6.11 覆盖升级通过；验证升级前保留的两个冲突 DLL 被清理，安装目录的 Windows GUI 检查通过，GPU 1.6.11 标记保持一致，桌面和开始菜单指向新版。
- 安装后 1,000 个关键文件 SHA-256 复核通过；实际安装 EXE 完成真实歌曲 12 秒片段及参考母带完整处理，耗时 38.23 秒（本机 RTX 5060 Ti）。成品为 44.1 kHz、双声道、PCM24、529,200 帧，无非有限值及采样削波，质量旁车存在。
- 质量报告真峰值 −0.500 dBTP、响度 −9.251 LUFS；峰值、削波、声道、响度及动态预算约束均通过。报告保留“降噪未实际应用，不能声称消噪”的策略提示。

## 安装包校验

文件：`ShadowBuster-Setup-1.6.12.exe`

大小：**897,901,981 bytes**。

SHA-256：`df20ad5e1d3330424453956ffbd1bacb70b39a4f1139f43a000a14540a89d90b`

GPU 清单和三个分卷复用 [v1.6.11 的 GPU 发布资产](https://github.com/AngleNaris/ShadowBuster/releases/tag/v1.6.11)。
