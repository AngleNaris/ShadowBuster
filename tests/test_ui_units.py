from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def attr(control, name):
    m = re.search(rf'id="{re.escape(control)}"[^>]*', HTML)
    assert m, control
    x = re.search(rf'data-{name}="([^"]+)"', m.group())
    assert x, (control, name)
    return x.group(1)


def test_real_ranges_and_fractional_steps():
    assert (attr("knob-vocal", "min"), attr("knob-vocal", "max"), attr("knob-vocal", "step")) == ("-6", "6", "0.5")
    assert (attr("knob-guidance", "min"), attr("knob-guidance", "max"), attr("knob-guidance", "step")) == ("0", "2", "0.1")
    assert (attr("width-meter", "min"), attr("width-meter", "max"), attr("width-meter", "step")) == ("0", "12", "0.5")
    assert 'v.toFixed(1)' in APP


def test_percentage_defaults_and_formatters():
    assert 'id="val-trans">30%</output>' in HTML
    assert 'id="val-sat">30%</output>' in HTML
    assert 'Math.round(v * 100) + "%"' in APP
    assert 'getSpace = bindFader' in APP and 'getDenoise = bindFader' in APP


def test_all_interaction_paths_snap_and_clamp():
    assert APP.count('snapToStep(value +') >= 6
    assert 'snapToStep(min + ratio * (max - min)' in APP
    assert 'snapToStep(value + d, min, max, step)' in APP
    assert 'return Math.max(min, Math.min(max, snapped))' in APP


def test_migration_is_idempotent_and_storage_failures_are_guarded():
    assert 'const UNIT_DIVISORS' in APP
    assert 'snapshot.version === 1' in APP
    assert 'alreadyActual ? 1 : divisor' in APP
    assert APP.count('catch (e) {}') >= 1
    assert '1.5.1' not in APP


def test_help_does_not_make_absolute_invariance_claims_or_link_controls():
    assert '逐样本不变' not in APP
    assert '绝对不变' not in APP
    assert '人声' in APP and '全混音峰值保护' in APP
    # The help describes independent domains; no automatic coupling promise is made.
    assert '声场重塑与高频降噪会一起旁路' in APP
