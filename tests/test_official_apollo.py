from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from experiments.official_apollo import audit_endpoint, run_experiment
from experiments.official_apollo_cli import build_parser
from experiments.lew_residual import SAMPLE_RATE


class OfficialApolloExperimentTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7821)

    def test_endpoint_audit_keeps_full_wet_separate_from_conservative_mix(self):
        length = SAMPLE_RATE
        t = np.arange(length) / SAMPLE_RATE
        dry = (0.08 * np.sin(2 * np.pi * 3000 * t) +
               0.03 * np.sin(2 * np.pi * 10000 * t))[:, None]
        wet = dry + 0.005 * np.sin(2 * np.pi * 10000 * t)[:, None]
        added, mixed, report = audit_endpoint(dry, wet, strength=0.5)
        self.assertEqual(added.shape, dry.shape)
        np.testing.assert_allclose(mixed, dry + added, atol=0.0, rtol=0.0)
        self.assertFalse(np.array_equal(mixed, wet))
        self.assertTrue(report["conservation"]["passed"])

    def test_endpoint_zero_strength_is_exact_dry(self):
        dry = self.rng.normal(0, 0.02, (12000, 2))
        wet = dry + self.rng.normal(0, 1e-4, dry.shape)
        added, mixed, _ = audit_endpoint(dry, wet, strength=0.0)
        self.assertTrue(np.array_equal(added, np.zeros_like(added)))
        self.assertTrue(np.array_equal(mixed, dry))

    def test_run_experiment_writes_float_artifacts_without_mutating_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            output = root / "result"
            runtime = root / "python.exe"
            script = root / "inference.py"
            checkpoint = root / "checkpoint.bin"
            for path in (runtime, script, checkpoint):
                path.write_bytes(b"placeholder")
            audio = self.rng.normal(0, 0.02, (16000, 2)).astype(np.float32)
            sf.write(source, audio, SAMPLE_RATE, subtype="FLOAT")
            before = source.read_bytes()

            def fake_apollo(input_wav, output_wav, **kwargs):
                dry, rate = sf.read(input_wav, always_2d=True, dtype="float32")
                sf.write(output_wav, dry, rate, subtype="FLOAT")
                return {"command": [], "seconds": 0.01, "stdout": ""}

            with mock.patch("experiments.official_apollo.run_official_apollo",
                            side_effect=fake_apollo):
                report = run_experiment(
                    source, output, python=runtime, inference_script=script,
                    checkpoint=checkpoint, strength=0.0,
                )
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(report["adapter"], "official_apollo_endpoint_audit")
            for name in ("dry_prepared.wav", "apollo_official_wet.wav",
                         "apollo_official_residual_raw.wav",
                         "apollo_official_residual_added.wav",
                         "apollo_official_conservative.wav"):
                self.assertEqual(sf.info(output / name).subtype, "FLOAT")
            with self.assertRaises(FileExistsError):
                run_experiment(source, output, python=runtime,
                               inference_script=script, checkpoint=checkpoint)

    def test_cli_requires_output_directory(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["input.wav"])


if __name__ == "__main__":
    unittest.main()
