"""Exercise the complete routing with real vocal/reshape DSP and synthetic stems."""
from contextlib import ExitStack
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

import studio_backend

import importlib.util
SOURCE_DIR = Path(__file__).resolve().parents[1] / "apollo_scripts"
sys.path.insert(0, str(SOURCE_DIR))

def _source_module(name):
    spec = importlib.util.spec_from_file_location("routing_" + name, SOURCE_DIR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

soundstage_reshape = _source_module("soundstage_reshape")
vocal_adjust = _source_module("vocal_adjust")


class VocalPipelineRoutingTests(unittest.TestCase):
    def test_vocal_gain_reaches_final_with_reshape_and_mastering_bypasses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sr = 44100
            t = np.arange(sr) / sr
            vocals = np.repeat((0.035 * np.sin(2 * np.pi * 700 * t))[:, None], 2, axis=1)
            other = np.column_stack((0.01 * np.sin(2 * np.pi * 2300 * t),
                                     -0.01 * np.sin(2 * np.pi * 2300 * t)))
            drums = np.repeat((0.01 * np.sin(2 * np.pi * 180 * t))[:, None], 2, axis=1)
            source = root / "song.wav"
            sf.write(source, vocals + other + drums, sr, subtype="FLOAT")
            seen = []

            def separate(src, destination, **kwargs):
                folder = Path(destination) / "htdemucs" / Path(src).stem
                folder.mkdir(parents=True, exist_ok=True)
                for name, samples in (("vocals", vocals), ("other", other), ("drums", drums)):
                    sf.write(folder / f"{name}.wav", samples, sr, subtype="FLOAT")

            def run_script(cmd, cwd, **kwargs):
                script = Path(cmd[1]).name
                seen.append((script, list(cmd)))
                with mock.patch.object(sys, "argv", [str(item) for item in cmd[1:]]):
                    {"vocal_adjust.py": vocal_adjust.main,
                     "soundstage_reshape.py": soundstage_reshape.main}[script]()
                return ""

            master_inputs = []

            def master(src, dst, **kwargs):
                master_inputs.append(Path(src).read_bytes())
                shutil.copyfile(src, dst)

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(studio_backend, "stage_demucs", separate))
                stack.enter_context(mock.patch.object(studio_backend, "_run_stream", run_script))
                stack.enter_context(mock.patch.object(studio_backend, "stage_soren", master))
                for reshape_off in (False, True):
                    for master_off in (False, True):
                        outputs = {}
                        for gain in (0.0, 4.0, 5.0, -3.0):
                            bypass = ["lew", "bass", "drums"]
                            if reshape_off:
                                bypass.append("reshape")
                            if master_off:
                                bypass.append("soren")
                            folder = root / f"run_{reshape_off}_{master_off}_{gain}"
                            with self.subTest(reshape_off=reshape_off, master_off=master_off, gain=gain):
                                studio_backend.run_pipeline(
                                    source, folder, bypass=bypass, vocal_gain_db=gain,
                                    space_wet=0.6, space_width_db=6.0, space_denoise=0.0,
                                    balance_target_db=None, balance_mode=None)
                                final = folder / "song_shadowbuster.wav"
                                outputs[gain], _ = sf.read(final, always_2d=True)
                                if not master_off:
                                    self.assertEqual(final.read_bytes(), master_inputs[-1])
                        for gain in (4.0, 5.0, -3.0):
                            self.assertTrue(np.isfinite(outputs[gain]).all())
                            self.assertGreater(np.max(np.abs(outputs[gain] - outputs[0.0])), 1e-5)
                        if reshape_off:
                            original, _ = sf.read(source, always_2d=True)
                            np.testing.assert_array_equal(outputs[0.0], original)
            for script, cmd in seen:
                if script == "soundstage_reshape.py":
                    self.assertEqual(cmd[cmd.index("--wet") + 1], "0.6")
                    self.assertEqual(cmd[cmd.index("--side-gain-db") + 1], "6.0")
                    self.assertTrue(cmd[cmd.index("--in-mix") + 1].endswith("_drummix.wav"))

    def test_default_routes_actual_original_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'song.wav'
            sf.write(source, np.ones((4410, 2))*.02, 44100)
            calls = []
            def copy(src, dst, **kwargs):
                shutil.copyfile(src, dst)
            def vocals(stems, src, dst, **kwargs):
                calls.append(kwargs)
                copy(src, dst)
            with mock.patch.object(studio_backend, 'stage_lew', copy), \
                 mock.patch.object(studio_backend, 'ffmpeg_convert', side_effect=copy) as decode, \
                 mock.patch.object(studio_backend, 'stage_demucs') as separate, \
                 mock.patch.object(studio_backend, 'stage_vocals', vocals):
                studio_backend.run_pipeline(source, root/'out',
                    bypass=('bass','drums','reshape','soren'))
            decode.assert_called_once()
            self.assertEqual(decode.call_args.args[0], source)
            self.assertEqual(separate.call_count, 2)
            routed = calls[0]
            self.assertEqual(routed['balance_mode'], studio_backend.REFERENCE_MODE)
            self.assertEqual(routed['reference_mix'].name, 'original_reference.wav')
            self.assertIn('original_reference_stems', str(routed['reference_vocals']))
            self.assertEqual(routed['vocal_scale'], 1.0)
            self.assertIsNone(routed['balance_target_db'])

    def test_vocal_bypass_reaches_final_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "song.wav"
            sf.write(source, np.ones((200, 2)) * 0.02, 44100, subtype="PCM_24")
            with mock.patch.object(studio_backend, "stage_demucs"), \
                    mock.patch.object(studio_backend, "stage_vocals") as vocals:
                studio_backend.run_pipeline(source, root / "output", vocal_gain_db=5,
                                            bypass=("lew", "bass", "drums", "vocals", "reshape", "soren"))
            vocals.assert_not_called()
            self.assertEqual((root / "output/song_shadowbuster.wav").read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
