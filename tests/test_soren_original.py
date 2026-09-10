import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
# 权威 core：packaging/soren_core.py；test_model 资源走统一路由（dev runtime 优先）。
CORE = ROOT / 'packaging/soren_core.py'
from soren_resources import SOREN_RESOURCE_DIR as RESOURCE_DIR


def load(name, path):
    sys.path.insert(0, str(RESOURCE_DIR))  # test_model 顶层 import 资源
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_styled_matches_original_after_linked_input_attenuation():
    core = load('restored_adapter', CORE)
    original = load('original_reference', ROOT / 'packaging/soren_original.py')
    rng = np.random.default_rng(12)
    audio = rng.normal(0, .12, (2, 44100))
    audio[:, 1200] = [1.8, .9]
    reference = rng.normal(0, .1, audio.shape)
    config = core.Config()
    config.loudness_option = 'loud'
    original_config = original.Config()
    original_config.loudness_option = 'loud'
    tp = core.calculate_true_peak(audio, 44100, 4)
    gain = min(0., -.1 - max(tp, 20 * np.log10(np.max(np.abs(audio)))))
    protected = audio * 10 ** (gain / 20)
    np.random.seed(123)
    expected = original.process_audio(protected.copy(), reference.copy(), 5, original_config)
    np.random.seed(123)
    actual = core.process_audio(audio, reference.copy(), 5, config)
    np.testing.assert_allclose(actual, expected, atol=1e-12, rtol=0)
    assert config.last_mastering_stats['engine'] == 'soren_original'
    assert config.last_mastering_stats['input_protection']['applied_gain_db'] == gain
    assert audio[0, 1200] == 1.8


@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_styled_rejects_nonfinite_input(value):
    core = load('restored_validation', CORE)
    audio = np.zeros((2, 44100))
    audio[0, 100] = value
    with pytest.raises(ValueError, match='non-finite'):
        core.process_audio(audio, None, 5, core.Config())


def test_staged_sources_match_canonical_when_present():
    """stage 为派生产物：存在时必须与 canonical 逐字节一致；未构建则跳过比对。"""
    canonical_core = (ROOT / 'packaging/soren_core.py').read_bytes()
    canonical_original = (ROOT / 'packaging/soren_original.py').read_bytes()
    for runtime in ('runtime', 'runtime_gpu'):
        stage = ROOT / 'packaging/stage' / runtime / 'Soren_src'
        if not (stage / 'core_decrypted.py').is_file():
            continue
        assert (stage / 'core_decrypted.py').read_bytes() == canonical_core
        assert (stage / 'soren_original.py').read_bytes() == canonical_original
