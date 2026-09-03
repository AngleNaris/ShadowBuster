from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

import studio_backend

APOLLO_DIR = Path(studio_backend.APOLLO_DIR)
if str(APOLLO_DIR) not in sys.path:
    sys.path.insert(0, str(APOLLO_DIR))


def _write_wav(path: Path, seconds=0.2, sr=44_100):
    t = np.arange(int(sr * seconds)) / sr
    data = np.column_stack((0.2 * np.sin(2 * np.pi * 220 * t),
                            0.2 * np.sin(2 * np.pi * 221 * t)))
    sf.write(path, data.astype(np.float32), sr, subtype="FLOAT")
    return path


class VocalStageCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stem = self.root / "htdemucs" / "song"
        self.stem.mkdir(parents=True)
        _write_wav(self.stem / "vocals.wav")
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _fake_stream(self, cmd, cwd, **kwargs):
        self.calls.append([str(c) for c in cmd])
        return ""

    def test_stage_vocals_invokes_script_with_gain(self):
        in_mix = _write_wav(self.root / "drummix.wav")
        out = self.root / "vocalmix.wav"
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_vocals(self.stem, in_mix, out, gain_db=2.5)
        cmd = self.calls[-1]
        self.assertTrue(any(c.endswith("vocal_adjust.py") for c in cmd))
        self.assertEqual(cmd[cmd.index("--vocals") + 1], str(self.stem / "vocals.wav"))
        self.assertEqual(cmd[cmd.index("--in-mix") + 1], str(in_mix))
        self.assertEqual(cmd[cmd.index("--vocal-gain-db") + 1], "2.5")

    def test_zero_gain_is_bit_exact_passthrough(self):
        in_mix = _write_wav(self.root / "drummix.wav")
        out = self.root / "vocalmix.wav"
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_vocals(self.stem, in_mix, out, gain_db=0.0)
        self.assertEqual(self.calls, [])
        self.assertEqual(out.read_bytes(), in_mix.read_bytes())

    def test_missing_vocals_stem_passthrough(self):
        in_mix = _write_wav(self.root / "drummix.wav")
        out = self.root / "vocalmix.wav"
        (self.stem / "vocals.wav").unlink()
        with mock.patch.object(studio_backend, "_run_stream", self._fake_stream):
            studio_backend.stage_vocals(self.stem, in_mix, out, gain_db=2.0)
        self.assertEqual(self.calls, [])
        self.assertEqual(out.read_bytes(), in_mix.read_bytes())


class VocalGainDeltaTests(unittest.TestCase):
    """delta-add 语义：out = in_mix + vocals*(gain - 1)，整体增益不被抵消。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sr = 44_100
        t = np.arange(self.sr // 10) / self.sr
        self.vocals = np.column_stack((
            0.05 * np.sin(2 * np.pi * 300 * t),
            0.04 * np.sin(2 * np.pi * 310 * t),
        )).astype(np.float32)
        self.in_mix = np.column_stack((
            0.08 * np.sin(2 * np.pi * 120 * t),
            0.07 * np.sin(2 * np.pi * 130 * t),
        )).astype(np.float32)
        sf.write(self.root / "vocals.wav", self.vocals, self.sr, subtype="FLOAT")
        sf.write(self.root / "in_mix.wav", self.in_mix, self.sr, subtype="FLOAT")

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_gain_adds_against_original_vocals(self):
        import vocal_adjust  # noqa: E402  (Apollo 工具脚本)

        output = self.root / "out.wav"
        argv = [
            "vocal_adjust.py",
            "--vocals", str(self.root / "vocals.wav"),
            "--in-mix", str(self.root / "in_mix.wav"),
            "--out", str(output),
            "--vocal-gain-db", "6",
        ]
        with mock.patch.object(sys, "argv", argv):
            vocal_adjust.main()
        actual, _ = sf.read(output, always_2d=True)
        expected = self.in_mix + self.vocals * (10 ** (6 / 20.0) - 1.0)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-7)


if __name__ == "__main__":
    unittest.main()
