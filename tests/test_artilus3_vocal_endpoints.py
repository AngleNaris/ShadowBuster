from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf
from scipy import signal

from experiments import artilus3_vocal_endpoints as experiment
from experiments.bigvgan_revocode import EXPECTED_CONFIG


class Artilus3VocalEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mix = self.root / "ARTILUS3.wav"
        self.vocal = self.root / "ARTILUS3_vocal.wav"
        self.apollo_checkpoint = self.root / "apollo.bin"
        self.model_dir = self.root / "bigvgan"
        self.output = self.root / "output"
        self.model_dir.mkdir()
        self.apollo_checkpoint.write_bytes(b"official Apollo fixture")
        self.generator = self.model_dir / "bigvgan_generator.pt"
        self.generator.write_bytes(b"BigVGAN fixture")
        (self.model_dir / "config.json").write_text(
            json.dumps(EXPECTED_CONFIG), encoding="utf-8"
        )
        t = np.arange(48_000, dtype=np.float64) / 48_000
        vocal = np.column_stack((
            0.08 * np.sin(2 * np.pi * 311 * t),
            0.07 * np.sin(2 * np.pi * 311 * t + 0.01),
        )).astype(np.float32)
        backing = np.column_stack((
            0.02 * np.sin(2 * np.pi * 97 * t),
            0.02 * np.sin(2 * np.pi * 101 * t),
        )).astype(np.float32)
        sf.write(self.vocal, vocal, 48_000, subtype="FLOAT")
        sf.write(self.mix, vocal + backing, 48_000, subtype="FLOAT")
        self.hashes = {
            "source": experiment.sha256_file(self.mix),
            "vocal": experiment.sha256_file(self.vocal),
            "apollo": experiment.sha256_file(self.apollo_checkpoint),
            "bigvgan": experiment.sha256_file(self.generator),
        }
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, source, destination, **kwargs):
        self.calls.append(("prepare", Path(source), Path(destination)))
        audio, rate = sf.read(source, dtype="float32", always_2d=True)
        self.assertEqual(rate, 48_000)
        prepared = signal.resample_poly(audio, 147, 160, axis=0).astype(np.float32)
        sf.write(destination, prepared, 44_100, subtype="FLOAT")
        return Path(destination)

    def apollo(self, input_wav, output_wav, **kwargs):
        self.calls.append(("apollo", Path(input_wav), Path(output_wav), kwargs))
        audio, rate = sf.read(input_wav, dtype="float32", always_2d=True)
        sf.write(output_wav, audio * np.float32(0.999), rate, subtype="FLOAT")
        return {"renderer": "official_apollo_fixture"}

    def bigvgan(self, input_wav, output_wav, **kwargs):
        self.calls.append(("bigvgan", Path(input_wav), Path(output_wav), kwargs))
        audio, rate = sf.read(input_wav, dtype="float32", always_2d=True)
        sf.write(output_wav, audio * np.float32(0.998), rate, subtype="FLOAT")
        return {"renderer": "bigvgan_fixture"}

    def run_experiment(self, **changes):
        options = dict(
            source_mix=self.mix,
            source_vocal=self.vocal,
            apollo_checkpoint=self.apollo_checkpoint,
            bigvgan_model_dir=self.model_dir,
            output_dir=self.output,
            expected_source_sha256=self.hashes["source"],
            expected_vocal_sha256=self.hashes["vocal"],
            expected_apollo_sha256=self.hashes["apollo"],
            apollo_runner=self.apollo,
            bigvgan_renderer=self.bigvgan,
            prepare=self.prepare,
        )
        options.update(changes)
        with mock.patch.object(experiment, "GENERATOR_SHA256", self.hashes["bigvgan"]):
            return experiment.run_artilus3_vocal_endpoints(**options)

    def test_hash_mismatch_precedes_preparation_and_endpoint_calls(self):
        with self.assertRaises(experiment.Artilus3ExperimentError):
            self.run_experiment(expected_vocal_sha256="0" * 64)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.output.exists())

    def test_vocal_not_mix_goes_to_apollo_and_branches_are_parallel(self):
        report = self.run_experiment()
        prepare_call = next(call for call in self.calls if call[0] == "prepare")
        apollo_call = next(call for call in self.calls if call[0] == "apollo")
        bigvgan_call = next(call for call in self.calls if call[0] == "bigvgan")
        self.assertEqual(prepare_call[1], self.vocal.resolve())
        self.assertNotEqual(prepare_call[1], self.mix.resolve())
        self.assertEqual(apollo_call[1], prepare_call[2])
        self.assertEqual(bigvgan_call[1], prepare_call[2])
        self.assertNotEqual(bigvgan_call[1], apollo_call[2])
        self.assertEqual(report["topology"], "parallel_from_same_original_vocal")
        self.assertFalse(report["stacking"])
        self.assertEqual(
            report["endpoints"]["apollo"]["branch_input_sha256"],
            report["endpoints"]["bigvgan"]["branch_input_sha256"],
        )
        self.assertIsNone(report["endpoints"]["apollo"]["upstream_endpoint"])
        self.assertIsNone(report["endpoints"]["bigvgan"]["upstream_endpoint"])

    def test_affine_identity_source_immutability_float_and_strict_json(self):
        before = {path: path.read_bytes() for path in (self.mix, self.vocal)}
        report = self.run_experiment()
        self.assertTrue(report["source_hashes_unchanged"])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        for endpoint in report["endpoints"].values():
            for alpha in ("0.25", "0.50"):
                self.assertTrue(endpoint["renders"][alpha]["affine_identity"]["exact"])
            self.assertEqual(sf.info(endpoint["canonical_44100_raw_output"]).subtype, "FLOAT")
            self.assertEqual(
                sf.info(endpoint["source_rate_48000_raw_resampled_output"]).subtype,
                "FLOAT",
            )
            self.assertEqual(
                sf.info(endpoint["source_rate_48000_audited_matched_output"]).subtype,
                "FLOAT",
            )
            self.assertTrue(endpoint["audit_correction_requested"])
            self.assertEqual(
                endpoint["replacement_input"],
                endpoint["source_rate_48000_audited_matched_output"],
            )
        saved = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["normalization"])
        self.assertFalse(saved["limiting"])
        self.assertNotIn("NaN", (self.output / "report.json").read_text(encoding="utf-8"))

    def test_static_level_match_retains_raw_and_render_uses_audited(self):
        before = {path: experiment.sha256_file(path) for path in (self.mix, self.vocal)}

        def gained_endpoint(input_wav, output_wav, **kwargs):
            audio, rate = sf.read(input_wav, dtype="float32", always_2d=True)
            sf.write(output_wav, audio * np.float32(0.8), rate, subtype="FLOAT")
            return {"fixture_gain": 0.8}

        report = self.run_experiment(
            apollo_runner=gained_endpoint,
            bigvgan_renderer=gained_endpoint,
        )
        for endpoint in report["endpoints"].values():
            raw_path = Path(endpoint["source_rate_48000_raw_resampled_output"])
            audited_path = Path(endpoint["source_rate_48000_audited_matched_output"])
            self.assertTrue(raw_path.is_file())
            self.assertTrue(audited_path.is_file())
            raw, raw_rate = sf.read(raw_path, dtype="float32", always_2d=True)
            audited, audited_rate = sf.read(audited_path, dtype="float32", always_2d=True)
            self.assertEqual((raw_rate, audited_rate), (48_000, 48_000))
            self.assertFalse(np.array_equal(raw, audited))
            correction = endpoint["audit"]["correction"]
            self.assertTrue(correction["applied"])
            self.assertGreater(correction["gain_linear"], 1.1)
            self.assertLess(abs(endpoint["audit"]["after"]["level"]["gain_db"]), 0.01)
            self.assertIn(endpoint["resampling"]["length_action"],
                          ("exact", "trim_tail", "pad_tail_zero"))

            repaired_path = Path(endpoint["renders"]["0.25"]["paths"]["vocal_repaired"])
            repaired, repaired_rate = sf.read(
                repaired_path, dtype="float32", always_2d=True
            )
            self.assertEqual(repaired_rate, 48_000)
            np.testing.assert_array_equal(repaired, audited)
        self.assertEqual(
            {path: experiment.sha256_file(path) for path in before}, before
        )

    def test_nonempty_output_is_refused_before_any_call(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("do not overwrite", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.run_experiment()
        self.assertEqual(self.calls, [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not overwrite")

    def test_missing_bigvgan_renderer_fails_closed_before_output_creation(self):
        with self.assertRaisesRegex(experiment.Artilus3ExperimentError, "renderer is required"):
            self.run_experiment(bigvgan_renderer=None)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
