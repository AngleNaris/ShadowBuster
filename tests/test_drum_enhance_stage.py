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

import bass_enhance  # noqa: E402  (Apollo 工具脚本)
import drum_enhance  # noqa: E402  (Apollo 工具脚本)


def _write_wav(path: Path, seconds=0.2, sr=44_100):
    t = np.arange(int(sr * seconds)) / sr
    data = np.column_stack((0.2 * np.sin(2 * np.pi * 220 * t),
                            0.2 * np.sin(2 * np.pi * 221 * t)))
    sf.write(path, data.astype(np.float32), sr, subtype="FLOAT")
    return path


class RestFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_four_stem_rest_is_vocals_and_other(self):
        stem = self.root / "htdemucs" / "song"
        stem.mkdir(parents=True)
        for name in ("vocals", "other", "bass", "drums"):
            _write_wav(stem / f"{name}.wav")
        self.assertEqual(studio_backend._rest_files(stem),
                         [stem / "vocals.wav", stem / "other.wav"])

    def test_two_stems_fallback(self):
        stem = self.root / "htdemucs" / "song"
        stem.mkdir(parents=True)
        _write_wav(stem / "bass.wav")
        _write_wav(stem / "no_bass.wav")
        self.assertEqual(studio_backend._rest_files(stem), [stem / "no_bass.wav"])

    def test_missing_stems_raise(self):
        stem = self.root / "htdemucs" / "song"
        stem.mkdir(parents=True)
        with self.assertRaises(studio_backend.PipelineError):
            studio_backend._rest_files(stem)


class StageCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stem = self.root / "htdemucs" / "song"
        self.stem.mkdir(parents=True)
        for name in ("vocals", "other", "bass", "drums"):
            _write_wav(self.stem / f"{name}.wav")
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _fake_stream(self, cmd, cwd, **kwargs):
        self.calls.append([str(c) for c in cmd])
        return ""

    def test_stage_bass_uses_complete_input_mix(self):
        in_mix = _write_wav(self.root / "lew.wav")
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_bass(self.stem, in_mix, self.root / "bassmix.wav")
        cmd = self.calls[-1]
        self.assertTrue(any(c.endswith("bass_enhance.py") for c in cmd))
        self.assertEqual(cmd[cmd.index("--in-mix") + 1], str(in_mix))

    def test_stage_drums_invokes_drum_enhance(self):
        rest = _write_wav(self.root / "bassmix.wav")
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_drums(self.stem, rest, self.root / "drummix.wav",
                                       punch_db=2.0, trans=0.3)
        cmd = self.calls[-1]
        self.assertTrue(any(c.endswith("drum_enhance.py") for c in cmd))
        self.assertEqual(cmd[cmd.index("--drums") + 1], str(self.stem / "drums.wav"))
        self.assertEqual(cmd[cmd.index("--in-mix") + 1], str(rest))
        self.assertEqual(cmd[cmd.index("--punch-db") + 1], "2.0")
        self.assertEqual(cmd[cmd.index("--trans") + 1], "0.3")


class DrumEnhanceDSPTests(unittest.TestCase):
    """鼓增强的守恒与门控特性：输出 = rest + f(drums)，静止段不动，峰值不超 -0.5dB。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sr = 44_100
        t = np.arange(self.sr) / self.sr
        # 前 0.4s 有鼓点（指数衰减敲击），后 0.6s 静音
        hits = sum(np.exp(-t / 0.03) * np.sin(2 * np.pi * f * t)
                   for f in (60, 200, 3000)) * (t < 0.4)
        self.drums = np.column_stack((hits, hits)).astype(np.float32) * 0.5
        self.rest = np.zeros((len(t), 2), np.float32)
        sf.write(self.root / "drums.wav", self.drums, self.sr, subtype="FLOAT")
        sf.write(self.root / "rest.wav", self.rest, self.sr, subtype="FLOAT")

    def tearDown(self):
        self.temp.cleanup()

    def test_quiet_section_untouched_and_attack_boosted(self):
        out = drum_enhance.enhance_drum_stem(self.drums[:, 0], self.sr)
        # 静音段（后半段）门控关闭，仅剩 IIR 滤波尾音（远低于可闻电平）
        self.assertLess(float(np.abs(out[int(0.6 * self.sr):]).max()), 1e-9)
        # 鼓点段应被增强（punch bell + 瞬态）
        self.assertGreater(float(np.abs(out[: int(0.4 * self.sr)]).max()),
                           float(np.abs(self.drums[:, 0][: int(0.4 * self.sr)]).max()))


class DeltaAddGainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sr = 44_100
        t = np.arange(self.sr // 10) / self.sr
        self.stem = np.column_stack((
            0.04 * np.sin(2 * np.pi * 80 * t),
            0.03 * np.sin(2 * np.pi * 90 * t),
        )).astype(np.float32)
        self.in_mix = np.column_stack((
            0.08 * np.sin(2 * np.pi * 330 * t),
            0.07 * np.sin(2 * np.pi * 440 * t),
        )).astype(np.float32)
        sf.write(self.root / "stem.wav", self.stem, self.sr, subtype="FLOAT")
        sf.write(self.root / "in_mix.wav", self.in_mix, self.sr, subtype="FLOAT")

    def tearDown(self):
        self.temp.cleanup()

    def test_bass_gain_is_added_against_original_stem(self):
        output = self.root / "bass_out.wav"
        argv = [
            "bass_enhance.py",
            "--bass", str(self.root / "stem.wav"),
            "--in-mix", str(self.root / "in_mix.wav"),
            "--out", str(output),
            "--sub-db", "0",
            "--punch-db", "0",
            "--sat", "0",
            "--trans", "0",
            "--bass-gain-db", "6",
        ]
        with mock.patch.object(sys, "argv", argv):
            bass_enhance.main()
        actual, _ = sf.read(output, always_2d=True)
        gain = 10 ** (6 / 20.0)
        expected = self.in_mix + self.stem * (gain - 1.0)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-7)

    def test_drum_gain_is_added_against_original_stem(self):
        output = self.root / "drum_out.wav"
        argv = [
            "drum_enhance.py",
            "--drums", str(self.root / "stem.wav"),
            "--in-mix", str(self.root / "in_mix.wav"),
            "--out", str(output),
            "--punch-db", "0",
            "--trans", "0",
            "--drums-gain-db", "6",
        ]
        with mock.patch.object(sys, "argv", argv):
            drum_enhance.main()
        actual, _ = sf.read(output, always_2d=True)
        gain = 10 ** (6 / 20.0)
        expected = self.in_mix + self.stem * (gain - 1.0)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-7)


if __name__ == "__main__":
    unittest.main()
