"""stem_enhance.py（第 4 批次六轨可选分轨增强）DSP 合同。

锚定契约：
  1. 全零控制位级透传（不进浮点运算），报告 status=not_applied；
  2. 缺 stem 透明降级：透传混音 + 报告 unavailable/missing_stem；
  3. delta-add 代数：out = in_mix + (processed − stem)，宽度只动 side（mid 增量恒 0）；
  4. 峰值保护与其他阶段同约定（-0.5dB 静态缩放，scale 入报告）；
  5. 参数范围即硬上限，越界/长度不匹配在处理开始前拒绝。
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import stem_enhance  # noqa: E402

SR = 44100


def _tone(freq, seconds, amp=.1, phase=0.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t + phase))[:, None] * np.ones((1, 2))


def _fixtures(tmp_path, stem=None, mix=None):
    stem = _tone(440, 1.0) if stem is None else stem
    mix = np.zeros_like(stem) if mix is None else mix
    sf.write(tmp_path / 'stem.wav', stem, SR, subtype='FLOAT')
    sf.write(tmp_path / 'mix.wav', mix, SR, subtype='FLOAT')
    return stem, mix


def invoke(tmp_path, *extra, stem_name='stem.wav', stem2_name=None, kind='guitar'):
    report = tmp_path / 'report.json'
    argv = ['stem', '--stem', str(tmp_path / stem_name),
            '--in-mix', str(tmp_path / 'mix.wav'),
            '--out', str(tmp_path / 'out.wav'), '--kind', kind,
            '--report-json', str(report), *extra]
    if stem2_name is not None:
        argv[argv.index('--report-json'):argv.index('--report-json')] = [
            '--stem2', str(tmp_path / stem2_name)]
    old = sys.argv
    sys.argv = argv
    try:
        stem_enhance.main()
    finally:
        sys.argv = old
    out = sf.read(tmp_path / 'out.wav', always_2d=True)[0]
    return out, json.loads(report.read_text(encoding='utf-8'))


def band_db(before, after, lo, hi):
    sos = signal.butter(4, [lo, hi], btype='bandpass', fs=SR, output='sos')
    e0 = np.sum(signal.sosfilt(sos, before, axis=0) ** 2)
    e1 = np.sum(signal.sosfilt(sos, after, axis=0) ** 2)
    return 10 * np.log10(e1 / e0)


def test_neutral_is_bit_exact_passthrough(tmp_path):
    stem, mix = _fixtures(tmp_path)
    out, report = invoke(tmp_path)
    assert (tmp_path / 'out.wav').read_bytes() == (tmp_path / 'mix.wav').read_bytes()
    assert report['extra']['status'] == 'not_applied'
    assert report['extra']['applied'] is False


def test_missing_stem_degrades_transparently(tmp_path):
    _, mix = _fixtures(tmp_path)
    out, report = invoke(tmp_path, '--gain-db', '3', stem_name='missing.wav')
    assert (tmp_path / 'out.wav').read_bytes() == (tmp_path / 'mix.wav').read_bytes()
    assert report['extra']['status'] == 'unavailable'
    assert report['extra']['applied'] is False
    assert report['extra']['missing'][-1].endswith('missing.wav')


def test_gain_delta_adds_scaled_stem(tmp_path):
    stem, mix = _fixtures(tmp_path)
    out, report = invoke(tmp_path, '--gain-db', '6')
    np.testing.assert_allclose(out, mix + stem * (10 ** (6 / 20) - 1), atol=2e-7)
    assert report['extra']['gains_db']['gain'] == 6.0


def test_mud_cut_attenuates_low_mid_only(tmp_path):
    # mix=stem（分轨本来就在混音里）→ out=processed：带内被削、带外原样。
    stem = _tone(300, 1.0) + _tone(2500, 1.0, amp=.1)
    low, _ = _fixtures(tmp_path, stem=stem, mix=stem)
    out, report = invoke(tmp_path, '--mud-cut-db', '6')
    assert -7.0 < band_db(low, out, 250, 350) < -5.0
    assert abs(band_db(low, out, 2000, 3000)) < 0.5
    assert report['extra']['gains_db']['mud_cut'] == 6.0


def test_presence_and_harsh_shelves(tmp_path):
    # 含 300Hz 真实成分，使"带外保持"断言有信号可测（纯噪声底的比值无意义）。
    bright = _tone(5000, 1.0, amp=.05) + _tone(9000, 1.0, amp=.05) + _tone(300, 1.0)
    bright, _ = _fixtures(tmp_path, stem=bright, mix=bright)
    out, _ = invoke(tmp_path, '--presence-db', '6')
    assert 4.0 < band_db(bright, out, 4000, 6000) < 7.0
    assert abs(band_db(bright, out, 200, 400)) < 0.5
    out, _ = invoke(tmp_path, '--harsh-cut-db', '6')
    assert -6.0 < band_db(bright, out, 8000, 11000) < -3.5
    assert abs(band_db(bright, out, 300, 600)) < 0.5


def test_width_moves_side_only(tmp_path):
    t = np.arange(SR) / SR
    stem = np.column_stack((.1 * np.sin(2 * np.pi * 800 * t),
                            .05 * np.sin(2 * np.pi * 800 * t)))
    mix = np.column_stack((.2 * np.sin(2 * np.pi * 300 * t),) * 2)
    _fixtures(tmp_path, stem=stem, mix=mix)
    out, _ = invoke(tmp_path, '--width-db', '6')
    np.testing.assert_allclose(out.mean(axis=1), mix.mean(axis=1), atol=2e-7)
    added_side = (out[:, 0] - out[:, 1]) / 2 - (mix[:, 0] - mix[:, 1]) / 2
    stem_side = (stem[:, 0] - stem[:, 1]) / 2
    np.testing.assert_allclose(added_side, stem_side * (10 ** (6 / 20) - 1), atol=2e-7)


def test_peak_protection_scales_and_reports(tmp_path):
    loud = _tone(440, 1.0, amp=1.0)
    _fixtures(tmp_path, stem=loud)
    out, report = invoke(tmp_path, '--gain-db', '6')
    assert np.max(np.abs(out)) <= 10 ** (-0.5 / 20) + 1e-6
    assert report['scale'] < 1.0


def test_out_of_range_and_mismatch_rejected_before_processing(tmp_path):
    _fixtures(tmp_path)
    for extra in (['--gain-db', '7'], ['--width-db', '-0.5']):
        with pytest.raises(SystemExit) as exc:
            invoke(tmp_path, *extra)
        assert exc.value.code == 2
    stem = _tone(440, 0.5)
    sf.write(tmp_path / 'stem.wav', stem, SR, subtype='FLOAT')
    with pytest.raises(SystemExit):
        invoke(tmp_path, '--gain-db', '3')


def test_synth_group_merges_other_and_piano(tmp_path):
    """synth 组 = other + piano 精确求和后整体处理：delta-add 对合并组成立。"""
    other = _tone(500, 1.0, amp=.06)
    piano = _tone(900, 1.0, amp=.04)
    mix = other + piano
    sf.write(tmp_path / 'other.wav', other, SR, subtype='FLOAT')
    sf.write(tmp_path / 'piano.wav', piano, SR, subtype='FLOAT')
    sf.write(tmp_path / 'mix.wav', mix, SR, subtype='FLOAT')
    out, report = invoke(tmp_path, '--gain-db', '6', stem_name='other.wav',
                         stem2_name='piano.wav', kind='synth')
    group = other + piano
    np.testing.assert_allclose(out, mix + group * (10 ** (6 / 20) - 1), atol=2e-7)
    assert report['stage'] == 'synth'
    assert report['extra']['sources'][-1].endswith('piano.wav')
    assert report['extra']['applied'] is True


def test_synth_missing_source_degrades_transparently(tmp_path):
    other = _tone(500, 1.0, amp=.06)
    sf.write(tmp_path / 'other.wav', other, SR, subtype='FLOAT')
    sf.write(tmp_path / 'mix.wav', other, SR, subtype='FLOAT')
    out, report = invoke(tmp_path, '--gain-db', '3', stem_name='other.wav',
                         stem2_name='missing.wav', kind='synth')
    assert (tmp_path / 'out.wav').read_bytes() == (tmp_path / 'mix.wav').read_bytes()
    assert report['extra']['status'] == 'unavailable'
    assert report['extra']['missing'] and 'missing.wav' in report['extra']['missing'][0]


def test_synth_source_length_mismatch_rejected(tmp_path):
    sf.write(tmp_path / 'other.wav', _tone(500, 1.0), SR, subtype='FLOAT')
    sf.write(tmp_path / 'piano.wav', _tone(900, 0.5), SR, subtype='FLOAT')
    sf.write(tmp_path / 'mix.wav', _tone(500, 1.0), SR, subtype='FLOAT')
    with pytest.raises(SystemExit):
        invoke(tmp_path, '--gain-db', '3', stem_name='other.wav',
               stem2_name='piano.wav', kind='synth')
