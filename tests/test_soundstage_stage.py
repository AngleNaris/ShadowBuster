from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

import studio_backend

APOLLO_DIR = Path(__file__).resolve().parents[1] / "apollo_scripts"
if str(APOLLO_DIR) not in sys.path:
    sys.path.insert(0, str(APOLLO_DIR))

import soundstage_reshape  # noqa: E402  (Apollo 管线脚本)
import bass_enhance  # noqa: E402
import drum_enhance  # noqa: E402


def _write_wav(path: Path, seconds=0.25, sr=44_100):
    t = np.arange(int(sr * seconds)) / sr
    data = np.column_stack((0.2 * np.sin(2 * np.pi * 220 * t),
                            0.2 * np.sin(2 * np.pi * 221 * t)))
    sf.write(path, data.astype(np.float32), sr, subtype="FLOAT")
    return path


class StageReshapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _fake_stream(self, cmd, cwd, **kwargs):
        self.calls.append([str(c) for c in cmd])
        return ""

    def test_zero_wet_passes_through_without_subprocess(self):
        # wet<=0：位级透传，不调外部脚本
        src = _write_wav(self.root / "in.wav")
        dst = self.root / "out.wav"
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_reshape(src, self.root, dst, wet=0.0)
        self.assertEqual(self.calls, [])
        self.assertEqual(src.read_bytes(), dst.read_bytes())

    def test_zero_wet_with_denoise_still_invokes_subprocess(self):
        src = _write_wav(self.root / "in.wav")
        _write_wav(self.root / "drums.wav")
        _write_wav(self.root / "other.wav")
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_reshape(
                src, self.root, self.root / "out.wav", wet=0.0, denoise=0.2)
        cmd = self.calls[-1]
        self.assertEqual(cmd[cmd.index("--wet") + 1], "0.0")
        self.assertEqual(cmd[cmd.index("--other-denoise-amount") + 1], "0.2")

    def test_invokes_soundstage_with_broadband_and_wet(self):
        src = _write_wav(self.root / "in.wav")
        dst = self.root / "out.wav"
        stems = self.root
        _write_wav(stems / "drums.wav")
        _write_wav(stems / "other.wav")
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_reshape(src, stems, dst, wet=0.6, denoise=0.2)
        cmd = self.calls[-1]
        self.assertTrue(any(c.endswith("soundstage_reshape.py") for c in cmd))
        self.assertEqual(cmd[cmd.index("--stems-dir") + 1], str(stems))
        self.assertEqual(cmd[cmd.index("--mode") + 1], "broadband")
        self.assertEqual(cmd[cmd.index("--wet") + 1], "0.6")
        self.assertEqual(cmd[cmd.index("--side-gain-db") + 1], "3.0")
        self.assertIn("--other-denoise-amount", cmd)

    def test_width_override_forwarded(self):
        src = _write_wav(self.root / "in.wav")
        _write_wav(self.root / "drums.wav")
        _write_wav(self.root / "other.wav")
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_reshape(src, self.root, self.root / "o.wav",
                                         wet=0.5, width_db=4.5)
        cmd = self.calls[-1]
        self.assertEqual(cmd[cmd.index("--side-gain-db") + 1], "4.5")

    def test_denoise_zero_omits_flag(self):
        src = _write_wav(self.root / "in.wav")
        _write_wav(self.root / "drums.wav")
        _write_wav(self.root / "other.wav")
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_reshape(src, self.root, self.root / "o.wav", wet=1.0, denoise=0.0)
        self.assertNotIn("--other-denoise-amount", self.calls[-1])


class PipelineBypassTests(unittest.TestCase):
    """bypass 路由：命中的阶段不产生外部调用，未知阶段名报错。"""

    def test_unknown_bypass_stage_raises(self):
        with self.assertRaises(studio_backend.PipelineError):
            studio_backend.run_pipeline(
                "whatever.wav", tempfile.mkdtemp(), bypass=("nonsense",))

    def test_bypass_set_validation(self):
        # 合法集合不应在参数校验层抛错（后续缺文件错误是另一回事）
        try:
            studio_backend.run_pipeline("missing.wav", tempfile.mkdtemp(),
                                        bypass=("lew", "vocals", "bass", "drums", "reshape", "soren"))
        except studio_backend.PipelineError as e:
            self.assertIn("不存在", str(e))  # 缺文件先于 bypass 报错
    def test_low_frequency_panel_bypass_neutralizes_bass_and_drums(self):
        src = _write_wav(Path(tempfile.mkdtemp()) / "input.wav")
        out_dir = Path(tempfile.mkdtemp())
        seen = {}

        def fake_lew(in_wav, out_wav, **kwargs):
            Path(out_wav).write_bytes(Path(in_wav).read_bytes())

        def fake_demucs(in_wav, out_root, **kwargs):
            stem_dir = Path(out_root) / "htdemucs" / Path(in_wav).stem
            stem_dir.mkdir(parents=True)
            for name in ("bass", "drums", "other", "vocals"):
                _write_wav(stem_dir / f"{name}.wav")

        def fake_bass(stem_dir, in_mix, out_wav, **kwargs):
            seen["bass"] = kwargs
            _write_wav(Path(out_wav))

        def fake_drums(stem_dir, rest_wav, out_wav, **kwargs):
            seen["drums"] = kwargs
            _write_wav(Path(out_wav))

        def fake_reshape(in_wav, stems_dir, out_wav, **kwargs):
            Path(out_wav).write_bytes(Path(in_wav).read_bytes())

        def fake_soren(in_wav, out_wav, **kwargs):
            Path(out_wav).write_bytes(Path(in_wav).read_bytes())

        patches = (
            mock.patch.object(studio_backend, "stage_lew", fake_lew),
            mock.patch.object(studio_backend, "stage_demucs", fake_demucs),
            mock.patch.object(studio_backend, "stage_bass", fake_bass),
            mock.patch.object(studio_backend, "stage_drums", fake_drums),
            mock.patch.object(studio_backend, "stage_reshape", fake_reshape),
            mock.patch.object(studio_backend, "stage_soren", fake_soren),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            studio_backend.run_pipeline(
                src, out_dir, bypass=("bass", "drums"), work_dir=out_dir / "work")

        self.assertEqual(seen, {})
        output = out_dir / f"{src.stem}_shadowbuster.wav"
        self.assertEqual(output.read_bytes(), src.read_bytes())


class SoundstageDSPTests(unittest.TestCase):
    """生产脚本 DSP 守恒：wet=0 恒等、side-only 处理不改变 mono。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sr = 44_100
        t = np.arange(self.sr) / self.sr
        left = 0.2 * np.sin(2 * np.pi * 220 * t)
        right = 0.18 * np.sin(2 * np.pi * 221 * t) + 0.02 * np.sin(2 * np.pi * 60 * t)
        self.x = np.column_stack((left, right))

    def tearDown(self):
        self.temp.cleanup()

    def test_reshape_stem_preserves_mid_and_mono(self):
        out = soundstage_reshape._reshape_stem(self.x, self.sr, 0.0, 3500.0, 3.0)
        self.assertTrue(np.allclose(self.x.mean(axis=1), out.mean(axis=1), atol=1e-12))
        self.assertGreater(np.abs(out).std(), 0)

    def test_spectral_denoise_zero_amount_identity(self):
        out = soundstage_reshape._spectral_denoise(self.x, self.sr, 10000.0, 0.0)
        np.testing.assert_array_equal(self.x, out)

    def test_spectral_denoise_low_band_untouched(self):
        out = soundstage_reshape._spectral_denoise(self.x, self.sr, 10000.0, 0.4)
        self.assertTrue(np.isfinite(out).all())
        error = np.sqrt(np.mean((out[:, 0] - self.x[:, 0]) ** 2))
        source_rms = np.sqrt(np.mean(self.x[:, 0] ** 2))
        self.assertLess(error / source_rms, 1e-5)

    def test_spectral_denoise_does_not_over_attenuate_start(self):
        rng = np.random.default_rng(7)
        noise = rng.normal(0.0, 0.02, self.sr * 4)
        stereo = np.column_stack((noise, noise))
        out = soundstage_reshape._spectral_denoise(stereo, self.sr, 10000.0, 0.2)
        first = np.sqrt(np.mean(out[:self.sr] ** 2))
        last = np.sqrt(np.mean(out[-self.sr:] ** 2))
        self.assertGreater(first / last, 0.85)

    def test_spectral_denoise_preserves_mono_compatibility(self):
        rng = np.random.default_rng(11)
        mono = rng.normal(0.0, 0.02, self.sr)
        stereo = np.column_stack((mono, mono))
        out = soundstage_reshape._spectral_denoise(
            stereo, self.sr, 10000.0, 0.4)
        np.testing.assert_allclose(out[:, 0], out[:, 1], rtol=0, atol=1e-12)

    def test_spectral_denoise_handles_short_audio(self):
        short = self.x[:2048]
        out = soundstage_reshape._spectral_denoise(short, self.sr, 10000.0, 0.2)
        self.assertEqual(out.shape, short.shape)
        self.assertTrue(np.isfinite(out).all())

    def test_spectral_denoise_rejects_invalid_amount(self):
        with self.assertRaises(ValueError):
            soundstage_reshape._spectral_denoise(self.x, self.sr, 10000.0, 1.1)

    def test_neutral_bass_and_drums_preserve_samples(self):
        mono = self.x[:, 0]
        bass = bass_enhance.enhance_bass_stem(
            mono, self.sr, sub_db=0.0, punch_db=0.0, sat=0.0, trans=0.0)
        drums = drum_enhance.enhance_drum_stem(
            mono, self.sr, punch_db=0.0, trans=0.0)
        np.testing.assert_allclose(bass, mono.astype(np.float32), rtol=0, atol=0)
        np.testing.assert_allclose(drums, mono, rtol=0, atol=0)


class SideGainResolveTests(unittest.TestCase):
    """宽度上限语义：other 取设定值，drums 固定取一半；越界/非有限值拒绝。"""

    def test_default_uses_mode_preset(self):
        gains = soundstage_reshape.resolve_side_gains("broadband")
        self.assertEqual(gains["other"][2], 3.0)
        self.assertEqual(gains["drums"][2], 1.5)

    def test_override_applies_half_to_drums(self):
        gains = soundstage_reshape.resolve_side_gains("broadband", 4.0)
        self.assertEqual(gains["other"][2], 4.0)
        self.assertEqual(gains["drums"][2], 2.0)

    def test_override_rejects_invalid_values(self):
        for bad in (-0.5, 12.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                soundstage_reshape.resolve_side_gains("broadband", bad)


if __name__ == "__main__":
    unittest.main()
