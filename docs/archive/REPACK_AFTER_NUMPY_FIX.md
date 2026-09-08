# ShadowBuster 修复后重新打包指南

本文说明如何修复 `ModuleNotFoundError: No module named 'numpy'` 后，从干净目录重新生成并验证 Windows 安装包。

## 先确认问题版本

截图中出现的调用路径是：

```text
runtime\Apollo\New_upscale.py
```

当前仓库 v1.5.0 的装配流程使用：

```text
runtime\Apollo\lew_upscale.py
```

因此，不能只把 `numpy` 复制到现有安装目录，也不能复用旧的 `stage`、`dist` 或安装器。必须重新装配 runtime、重新构建外壳并重新编译安装器。

## 构建前置条件

以下操作必须在 Windows PowerShell 中、项目根目录 `C:\_MY_WORK\shadowbuster` 执行。

需要准备：

- Python 3.12.x 便携运行时（python-build-standalone，目录内必须有 `python.exe`）。
- 一个用于构建外壳和运行 `pip` 的 Python 环境。该环境需要能导入 `PySide6`、`PyInstaller` 和 `numpy`。
- Apollo 源码目录，至少包含 `lew_upscale.py`、`look2hear\` 和 `ckpts\`。
- `Soren_src` 目录，至少包含 `core_decrypted.py`、`test_model.py`、`model\`、`profiles\` 和 `secured_genres\`。
- `ffmpeg.exe`，并且已经加入当前用户的 `PATH`，或可以通过 `Get-Command ffmpeg` 找到。
- Inno Setup 6，并且 `iscc.exe` 已加入 `PATH`。
- 能够访问 PyPI 和 PyTorch wheel 索引的网络。`runtime_sync.ps1` 会下载 torch、音频依赖和其他推理依赖。

建议先确认工具：

```powershell
python --version
ffmpeg -version
iscc /?
```

## 设置构建变量

以下变量显式指定构建输入，避免脚本回退到旧开发机路径。路径根据实际机器修改：

```powershell
$env:SB_SHELL_PYTHON = "C:\Tools\ShadowBuster\.venv\Scripts\python.exe"
$env:SB_PYTHON = "C:\Tools\ShadowBuster\.venv\Scripts\python.exe"
$env:SB_PORTABLE_PYTHON = "C:\Tools\python\cpython-3.12.13-windows-x86_64-none"
$env:SB_APOLLO = "C:\_MY_WORK\Apollo"
$env:SB_SOREN = "C:\_MY_WORK\Soren_src"
```

`SB_SHELL_PYTHON` 和 `SB_PYTHON` 可以指向同一个环境，但该环境必须同时满足外壳构建和依赖安装要求。`SB_PORTABLE_PYTHON` 指向目录，不是 `python.exe` 文件本身。

检查变量和目录：

```powershell
& $env:SB_SHELL_PYTHON -c "import numpy, PyInstaller; print('shell dependencies OK', numpy.__version__)"
Test-Path "$env:SB_PORTABLE_PYTHON\python.exe"
Test-Path "$env:SB_APOLLO\lew_upscale.py"
Test-Path "$env:SB_SOREN\core_decrypted.py"
Get-Command ffmpeg
Get-Command iscc
```

所有检查都必须成功后再开始构建。

## 清理旧构建产物

以下目录是生成目录，不包含源代码。清理它们可以避免旧版本入口或旧依赖混入新安装包：

```powershell
Remove-Item build, dist, packaging\stage, packaging\out -Recurse -Force -ErrorAction SilentlyContinue
```

如果机器上曾安装过现场截图对应的旧版本，卸载旧应用后也建议删除旧的用户级 GPU runtime：

```powershell
Remove-Item "$env:LOCALAPPDATA\ShadowBuster\runtime-gpu" -Recurse -Force -ErrorAction SilentlyContinue
```

这一步不会删除项目源目录、Apollo 源码或 `Soren_src`。

## 重建 CPU 安装包

按以下顺序执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_shell.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\runtime_sync.ps1 -Flavor cpu
iscc packaging\installer.iss
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\install_test.ps1 -Version 1.5.0
```

每一步都必须成功。脚本现在会执行以下阻断式检查：

- 外壳构建前导入 `PyInstaller` 和 `numpy`。
- PyInstaller onedir 产物中存在 numpy 目录和原生 `.pyd` 扩展。
- `build\ShadowBuster\warn-ShadowBuster.txt` 没有报告 numpy 缺失。
- runtime 的全部第三方依赖导入成功，且 numpy 版本为 `2.5.2`。
- Apollo 的 `look2hear` 通过安装目录 `PYTHONPATH` 导入。
- 安装后的 `runtime\env\python.exe` 再次执行同一套导入检查。

安装测试的成功标准不是只有 `EXIT=0`，还必须看到类似输出：

```text
installed runtime imports OK 2.5.2
```

## 安装后手工核验

如果需要留存发布证据，在 `packaging` 目录下执行：

```powershell
$install = (Resolve-Path packaging\test_install).Path
$runtime = Join-Path $install "runtime\env\python.exe"

& $runtime -c @"
import importlib
import numpy
for name in ('torch', 'torchaudio', 'demucs', 'numpy', 'soundfile', 'scipy', 'librosa', 'numba', 'statsmodels', 'pyloudnorm', 'joblib', 'cryptography'):
    importlib.import_module(name)
assert numpy.__version__ == '2.5.2', numpy.__version__
print('clean install runtime OK', numpy.__version__)
"@

Get-ChildItem "$install\runtime\Apollo" -Force | Select-Object Name
Get-ChildItem "$install\runtime\env\Lib\site-packages\numpy" -Filter "*.pyd" -Recurse
Get-Content "$install\runtime\critical-manifest.sha256" | Select-String "env/python.exe|numpy"
```

重点确认：

- 安装目录中存在 `runtime\Apollo\lew_upscale.py`，而不是只存在旧的 `New_upscale.py`。
- `runtime\env\Lib\site-packages\numpy\` 存在，并且包含 `.pyd` 原生扩展。
- `critical-manifest.sha256` 包含便携 Python 和 numpy 文件。
- 应用处理时调用的解释器是安装目录的 `runtime\env\python.exe`。

## GPU runtime 额外步骤

CPU 安装包和 GPU 环境包是两条独立的发布链路。需要更新 GPU 包时，先完成 CPU runtime 的装配，再单独执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\runtime_sync.ps1 -Flavor gpu
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_gpu_env.ps1
```

`build_gpu_env.ps1` 会从 `stage\runtime_gpu\env` 生成 GPU 环境 zip、分卷和清单。重新生成后必须检查：

```powershell
Get-ChildItem packaging\out\gpu-env -Filter "gpu-env-1.5.0.*" |
    Select-Object Name, Length
```

每个分卷必须小于 2 GiB。GPU 环境安装到用户目录后，应用会在原子切换前执行 runtime 导入探针；只有写入 `gpu-env.json` 的 `runtimeValidated=true` 后，应用才会采用该环境。旧版没有该标记，或缺少 numpy 的 GPU 环境会自动被忽略，并回退到安装包内 CPU runtime。

## Release 发布检查

本次代码修复需要先推送代码，再发布重新生成的安装器。不要把旧的、包含 `New_upscale.py` 的安装器重新上传为修复版。

上传前至少确认：

```powershell
python -m pytest tests\ -q
```

预期结果为当前完整测试套件全部通过。安装器发布时使用与 `studio_backend.py` 和 `packaging\installer.iss` 一致的版本号；`tests\test_app_version.py` 会阻止两处版本号不一致。

如果发布新版本，先同步修改：

- `studio_backend.py` 中的 `APP_VERSION`。
- `packaging\installer.iss` 中的 `MyAppVersion`。

然后重新执行外壳、runtime、安装器和安装测试。GPU 清单中的版本也必须与新版本一致。

## 故障排查

### 仍然显示 `New_upscale.py`

当前安装器来自旧构建。删除旧的 `build`、`dist`、`packaging\stage` 和 `packaging\out` 后，从 `build_shell.ps1` 开始完整重建。

### 构建前提示缺少 PyInstaller 或 numpy

检查 `SB_SHELL_PYTHON` 是否指向正确的 Python，并执行：

```powershell
& $env:SB_SHELL_PYTHON -m pip install PySide6 PyInstaller numpy
```

### runtime 探针仍然提示缺少 numpy

检查 `SB_PORTABLE_PYTHON` 是否指向包含 `python.exe` 的目录，并确认依赖安装命令使用了 `--target runtime\env\Lib\site-packages`。不要依赖宿主机 Python 的 site-packages。

### 安装器返回 0，但安装测试失败

这是预期的阻断行为，说明 Inno Setup 解压成功，但安装后的 runtime 依赖不完整。查看 `install_test.ps1` 输出中的第一个缺失模块，修复装配输入后重新从空 `stage` 构建。

### GPU 环境显示已下载但应用没有使用

检查 `%LOCALAPPDATA%\ShadowBuster\runtime-gpu\gpu-env.json` 中是否有 `runtimeValidated: true`，以及 `runtime-gpu\env\Lib\site-packages\numpy\__init__.py` 是否存在。没有这两项时，应用会继续使用安装目录内的 CPU runtime。
