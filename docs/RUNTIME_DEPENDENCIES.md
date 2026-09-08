# ShadowBuster 运行时依赖清单

本文记录随安装包分发的 runtime（CPU torch）内全部 Python 依赖：哪些是推理链直接 import 的硬依赖（必须显式 pin），哪些仅为传递依赖，哪些训练侧依赖已在 v1.6.5 工作区整理中移除。依赖唯一声明点是 `packaging/runtime_sync.ps1`。

## 推理链与直接依赖

| 链路 | 入口 | 直接 import |
|---|---|---|
| Lew 高频重建 | `Apollo/lew_upscale.py` | numpy、soundfile、torch、`look2hear.models`（→ torch、huggingface_hub） |
| Demucs 四轨分离 | `python -m demucs` | demucs 4.1.0（传递引入 einops、julius、lameenc、tqdm、pyyaml、safetensors、sphn） |
| DSP 四脚本 | `apollo_scripts/bass_enhance.py` 等 | numpy、soundfile、scipy |
| Soren 母带 | `Soren_src/core_decrypted.py` | numpy、pyloudnorm、librosa、soundfile、scipy、statsmodels、numba、cryptography（Fernet）、joblib |
| 外壳（exe，PyInstaller） | `main.py` | PySide6、numpy、soundfile |

## 显式 pin 清单（runtime_sync.ps1）

torch==2.7.1、torchaudio==2.7.1、numpy==2.5.2、soundfile==0.14.0、scipy==1.18.0、librosa==1.0.0、numba==0.67.0、llvmlite==0.49.0、statsmodels==0.14.6、pyloudnorm==0.2.0、joblib==1.5.3、cryptography==50.0.0、setuptools==78.1.0、demucs==4.1.0、einops==0.8.2、julius==0.2.8、lameenc==1.8.4、tqdm==4.70.0、huggingface_hub==0.36.2、soxr==1.1.0、omegaconf==2.0.6

- `llvmlite`（numba 要求 `<0.50`）、`soxr`（librosa 要求 `>=1.0.0`）、`einops/julius/lameenc/tqdm`（demucs 直接要求）属于传递依赖，但为了可复现构建保持显式 pin。
- `huggingface_hub` 是 demucs 传递依赖 + `look2hear/models/base_model.py` 直接 import，必须显式声明。
- `joblib` 由 `soren_core.py` 直接 import。
- `setuptools` 为 Python 3.12 提供 distutils shim（`look2hear/utils/stft.py` 的 `LooseVersion`），成本极低，保留作兼容垫。
- `omegaconf==2.0.6` 元数据是旧式 `PyYAML>=5.1.*` 写法，需 pip 24.0 临时 shim 安装（见 `runtime_sync.ps1`）。

## 已移除的训练侧依赖（v1.6.5 整理）

以下包只在 look2hear 的训练/解析辅助模块中被引用，不在任何发布运行路径上，已经从依赖清单删除：

| 包 | 原引用位置（未运行） |
|---|---|
| pytorch-lightning==2.6.5 | `look2hear/models/base_model.py` 的 `serialize()`（lew 走 `from_pretrain`→`torch.load`，不触发） |
| lightning-utilities==0.15.3 | 仅 pytorch-lightning 的 METADATA 传递要求 |
| omegaconf==2.0.6* | `look2hear/utils/parser_utils.py`、`system/audio_litmodule.py`（训练侧） |
| torch-complex==0.4.4 | `look2hear/utils/complex_utils.py` |
| rich==15.0.0 | `look2hear/utils/lightning_utils.py` |

*omegaconf 仍在 pin 清单中：虽然主推理链不触发它，但其移除风险与收益评估未完成，保留至下一轮验证。

移除前已做两级验证：① 负向探针——屏蔽 5 个包后 `import look2hear.models` 成功；② 重建后的 runtime 通过 `look2hear.models` 导入探针与 htdemucs 离线加载。若未来启用 look2hear 训练/解析 CLI，需把上表包加回。

## 体积裁剪约定（runtime_sync.ps1 [4b]）

- 删：`__pycache__`、`torch/include`、site-packages 根下的 0 字节 `.whl` 残留。
- 不删：sklearn（Soren 的 `model/` 是含 sklearn 对象的 joblib pickle，反序列化需要）；numba/statsmodels/pandas（Soren 硬依赖）。
