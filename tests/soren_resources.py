"""Soren 测试共享：资源目录解析（dev runtime 优先，SB_SOREN/外部源兜底）。

加载 canonical Soren 代码需满足其顶层 `import test_model`；三者均不可用时在
系统临时目录生成轻量 test_model stub（仅测试进程 sys.path 可见，不遮蔽生产），
保证新 checkout 无外部资源也能运行 Soren 单元测试。
"""
import os
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CANDIDATES = [ROOT / "dev_runtime" / "Soren_src"]
if os.environ.get("SB_SOREN"):
    _CANDIDATES.append(Path(os.environ["SB_SOREN"]))
_CANDIDATES.append(Path(r"D:/_3.AI/audio_upscale/Soren_src"))

SOREN_RESOURCE_DIR = next(
    (c for c in _CANDIDATES if (c / "test_model.py").is_file()), None)

if SOREN_RESOURCE_DIR is None:
    # 轻量 stub：仅满足 import；当前 Soren 测试走 reference/off/eq_only 路径，
    # 不触发真实模型建议，故零值建议不影响断言。stub 在临时目录，不遮蔽生产。
    SOREN_RESOURCE_DIR = Path(tempfile.mkdtemp(prefix="soren-test-resources-"))
    (SOREN_RESOURCE_DIR / "test_model.py").write_text(textwrap.dedent('''
        """测试专用轻量 test_model stub（无真实模型依赖）。"""

        def get_suggestions_for_genre(genre):
            return {"rms_mid": 0.0, "rms_side": 0.0, "stereo_width": 1.0}
    ''').lstrip(), encoding="utf-8")
