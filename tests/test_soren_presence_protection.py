from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "packaging/soren_core.py"  # 权威源码；stage 一致性另测


def test_mid_presence_protection_contract_is_present():
    source = CORE.read_text(encoding="utf-8")
    assert "freqs >= 1500.0" in source
    assert "freqs <= 5000.0" in source
    assert "10 ** (-1.0 / 20.0)" in source


def test_mid_presence_relative_correction_is_bounded_and_reported():
    source = CORE.read_text(encoding="utf-8")
    assert "def correct_mid_presence" in source
    assert "np.clip(20.0 * np.log10(reference_rms / target_rms), -max_db, max_db)" in source
    assert "max ±0.75 dB" in source
