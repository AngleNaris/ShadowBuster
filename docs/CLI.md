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

`--cpu` 使用 CPU，默认 cuda。主要旋钮与 UI 范围一致：Sub 0–12dB，Punch 0–10dB，sat/trans/space-wet/space-denoise 0–1，space-width-db 0–12dB。默认母带 style-mode=off（UI 无风格），默认 space-wet=.6、space-denoise=.2。`--help` 列出完整参数。bypass 不跳过分轨阶段。

## 自动清晰度

UI 低频面板的“自动清晰”与 CLI `--bass-auto-clarity` 使用同一后端参数，默认关闭。开启后先对整首 Bass 分轨做一次联动频谱分析，再在 Sub/饱和前施加约 285Hz 的低中频削减和 800Hz 的清晰度提升，各最多 2dB。左右声道使用相同 EQ，分析平均声道功率而非相加波形。

这是固定规则的保守启发式，不是机器学习音色分类器，也不能保证识别分离串音。静音、纯低频、噪声样频谱和已明亮的合成测试素材不提亮；不生成缺失谐波，不改变 Sub 旋钮含义，不保证所有歌曲更好听。采用全曲固定 EQ，不是逐音符动态自适应。

应用一直将 Punch/瞬态送到 drums 分轨；Bass 的这两个内部参数保持 0 是有意路由，不是 GUI 漏处理。新清晰度开关则已贯通 GUI、CLI、Bass DSP。

## 处理缓存

应用不提供实时处理试听或应用内播放。调整参数后需要重新处理，再用外部播放器打开成品比较。

GUI 与 CLI 共用处理缓存和容量设置。默认开启，容量上限为 5 GiB；缓存位于 `%LOCALAPPDATA%\ShadowBuster\processing-cache`。设置界面提供关闭及 2 / 5 / 10 / 20 / 50 / 100 GiB 选项，显示已用空间，并支持二次确认后清空。超过容量时优先淘汰较久未使用的阶段结果；降低容量会立即淘汰超额结果，设为 0 会清空阶段结果并关闭缓存。清空不删除原始歌曲或输出目录中的成品。

CLI 的 `--no-cache` 仅对本次运行停用缓存读写，不删除已有缓存。开发与测试可用 `SB_PROCESSING_CACHE_DIR` 指定独立缓存目录。

同一输入通过文件内容 MD5 识别。每个阶段还检查上游结果、参数和算法／运行环境标识；修改后段参数可以复用前段结果，修改上游参数会使受影响的后续缓存失效。首次处理仍需完整计算；缓存被淘汰或损坏时重新计算。缓存仅用于加速，不提供试听。

## 中间文件

母带输出及其 `.mastering.json` / `.mastering` 旁车只在本次工作目录生成，成功后仅成品 WAV 复制到输出目录。失败时不复制半成品。已有用户文件和历史旁车不自动删除。
