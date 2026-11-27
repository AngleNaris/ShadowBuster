"""Section-relative density compression: even in-section swings, keep section contrast."""
import numpy as np
import pytest

from mastering.density import DENSITY_MODES, PLUGIN, densify, split_bands

pytest.importorskip("pedalboard")
needs_plugin = pytest.mark.skipif(not PLUGIN.is_file(), reason="run tools/fetch_plugins.py")

SR = 44100


def pulsing_sections(seed=3):
    """Bass-heavy noise pulsing 1x/s (beat-level in-section swing); 6 s quiet verse, 6 s loud chorus."""
    rng = np.random.default_rng(seed)
    t = np.arange(SR * 12) / SR
    carrier = np.sin(2 * np.pi * 60 * t) + .3 * rng.standard_normal(len(t))
    pulse = .35 + .65 * (np.sin(2 * np.pi * 1 * t) > 0)
    section = np.where(t < 6, .1, .4)
    mono = carrier * pulse * section
    return np.column_stack((mono, mono * .9))


def block_db(x, ms=50):
    b = round(SR * ms / 1000)
    p = (x.mean(axis=1)[:len(x) // b * b] ** 2).reshape(-1, b).mean(axis=1)
    return 10 * np.log10(p + 1e-20)


def in_section_spread(x):
    db = block_db(x)
    return np.median([np.percentile(db[i:i + 40], 90) - np.percentile(db[i:i + 40], 10)
                      for i in range(20, len(db) - 40, 40)])


def section_contrast(x):
    return 10 * np.log10(np.mean(x[SR * 7:SR * 11] ** 2) / np.mean(x[SR:SR * 5] ** 2))


def test_bands_sum_back_to_input():
    x = pulsing_sections()
    np.testing.assert_allclose(sum(split_bands(x, SR)), x, atol=1e-12)


def test_off_is_identity():
    x = pulsing_sections()
    y, report = densify(x, "off")
    np.testing.assert_array_equal(y, x)
    assert not report["applied"]


@needs_plugin
def test_density_scope_routes_full_mix_and_selected_bands():
    x = pulsing_sections()[:2 * SR]
    low, _ = densify(x, scope="low")
    high, _ = densify(x, scope="high")
    low_high, selected = densify(x, scope="high,low")
    assert selected["scope"] == "low,high"
    assert set(selected["bands"]) == {"low", "high_mid", "high"}
    np.testing.assert_allclose(low_high - x, (low - x) + (high - x), atol=1e-12)
    global_out, broadband = densify(x, scope="global")
    all_bands, multiband = densify(x, scope="low,mid,high")
    assert list(broadband["bands"]) == ["global"] and broadband["crossovers_hz"] == []
    assert len(multiband["bands"]) == 4
    assert not np.allclose(global_out, all_bands)
    np.testing.assert_array_equal(all_bands, densify(x)[0])
    np.testing.assert_array_equal(densify(x, scope="none")[0], x)
    with pytest.raises(ValueError):
        densify(x, scope="global,high")


@needs_plugin
@pytest.mark.parametrize("mode", [m for m in DENSITY_MODES if m != "off"])
def test_reduces_in_section_swing_but_keeps_section_contrast(mode):
    x = pulsing_sections()
    y, report = densify(x, mode)
    assert report["applied"]
    assert in_section_spread(y) < in_section_spread(x) - .5
    assert section_contrast(y) == pytest.approx(section_contrast(x), abs=.5)
    assert report["style"] == "Optical"


@needs_plugin
def test_standard_is_stronger_than_light():
    x = pulsing_sections()
    light, _ = densify(x, "light")
    standard, _ = densify(x, "standard")
    assert in_section_spread(standard) < in_section_spread(light)


@needs_plugin
def test_section_following_keeps_contrast_a_fixed_threshold_loses(monkeypatch):
    """Same input with section following disabled: the loud chorus is compressed
    harder than the verse, so the verse/chorus contrast shrinks."""
    import mastering.density as density
    x = pulsing_sections()
    followed, _ = densify(x, "standard")
    monkeypatch.setattr(density, "_follow_db", lambda band, sr: np.zeros(len(band) // density.BLOCK + 1))
    fixed, _ = densify(x, "standard")
    assert section_contrast(fixed) < section_contrast(followed) - 1.0


@needs_plugin
def test_partial_band_compression_preserves_tonal_balance_without_midhigh_lift():
    """Compressing only low (<120 Hz) must tighten low-end dynamics without sinking
    low energy relative to mid/high (which previously caused a +4..6 dB mid-high lift)."""
    x = pulsing_sections()
    y, report = densify(x, "standard", sr=SR, scope="low")
    assert report["applied"] and list(report["bands"]) == ["low"]
    assert report["bands"]["low"]["makeup_gain_db"] > 0.5
    in_bands = split_bands(x, SR)
    out_bands = split_bands(y, SR)
    in_low_vs_rest = 10 * np.log10(np.mean(in_bands[0] ** 2) / np.mean((x - in_bands[0]) ** 2))
    out_low_vs_rest = 10 * np.log10(np.mean(out_bands[0] ** 2) / np.mean((y - out_bands[0]) ** 2))
    assert abs(out_low_vs_rest - in_low_vs_rest) < 0.25
    assert in_section_spread(out_bands[0]) < in_section_spread(in_bands[0]) - 0.5


@needs_plugin
def test_is_deterministic():
    x = pulsing_sections()
    np.testing.assert_array_equal(densify(x, "light")[0], densify(x, "light")[0])


def test_cli_defaults_to_light(monkeypatch, tmp_path):
    import sys
    import mastering.__main__ as cli
    seen = {}
    monkeypatch.setattr(cli, "master_file", lambda *a, **k: seen.update(k) or
                        {"actual_lufs": -9.0, "true_peak_dbtp": -1.1})
    monkeypatch.setattr(sys, "argv", ["mastering", str(tmp_path / "a.wav"), str(tmp_path / "b.wav")])
    cli.main()
    assert seen["density"] == "light"
