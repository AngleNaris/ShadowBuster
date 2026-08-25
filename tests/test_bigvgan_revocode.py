from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from experiments.bigvgan_revocode import (
    EXPECTED_CONFIG,
    GENERATOR_SHA256,
    HOP_LENGTH,
    LOG_CLAMP,
    MODEL_ID,
    MODEL_REVISION,
    N_FFT,
    NUM_MELS,
    REFLECT_PAD,
    BigVGANRevoCodeError,
    load_verified_runtime,
    numpy_mel_spectrogram,
    plan_mel_chunks,
    reconcile_output_length,
    reconstruct_from_mel,
    save_float_artifacts,
    seam_comparison,
    validate_config,
    validate_model_identity,
    verify_checkpoint_sha256,
)


class BigVGANRevoCodeTests(unittest.TestCase):
    def test_pinned_identity_and_strict_config(self):
        self.assertEqual(MODEL_ID, "nvidia/bigvgan_v2_44khz_128band_512x")
        self.assertEqual(MODEL_REVISION, "95a9d1dcb12906c03edd938d77b9333d6ded7dfb")
        self.assertEqual(
            GENERATOR_SHA256,
            "d9fe7ec6bd0b44ed9d66973d5012d8181c1570b01e5c72df51973e241dccd357",
        )
        self.assertEqual(validate_config(EXPECTED_CONFIG), EXPECTED_CONFIG)
        aliases = {
            "sample_rate": 44100, "n_mels": 128, "n_fft": 2048,
            "win_length": 2048, "hop_length": 512, "fmin": 0, "fmax": None,
        }
        self.assertEqual(validate_config(aliases), EXPECTED_CONFIG)
        numeric_json = {
            "sampling_rate": 44100.0, "num_mels": 128.0, "n_fft": 2048.0,
            "win_size": 2048.0, "hop_size": 512.0, "fmin": 0.0, "fmax": None,
        }
        self.assertEqual(validate_config(numeric_json), EXPECTED_CONFIG)
        equal_aliases = dict(EXPECTED_CONFIG, sample_rate=44100.0, n_mels=128.0)
        self.assertEqual(validate_config(equal_aliases), EXPECTED_CONFIG)
        for key in ("sampling_rate", "num_mels", "n_fft", "win_size", "hop_size", "fmin"):
            config = dict(EXPECTED_CONFIG)
            config[key] = bool(config[key])
            with self.subTest(bool_key=key), self.assertRaises(BigVGANRevoCodeError):
                validate_config(config)
        bool_alias = dict(EXPECTED_CONFIG, sample_rate=True)
        with self.assertRaises(BigVGANRevoCodeError):
            validate_config(bool_alias)
        for key, bad in (("sampling_rate", 48000), ("num_mels", 127),
                         ("n_fft", 1024), ("win_size", 1024),
                         ("hop_size", 256), ("fmin", 1), ("fmax", 22050)):
            config = dict(EXPECTED_CONFIG)
            config[key] = bad
            with self.subTest(key=key), self.assertRaises(BigVGANRevoCodeError):
                validate_config(config)
        with self.assertRaises(BigVGANRevoCodeError):
            validate_model_identity(MODEL_ID, "main")

    def test_numpy_frontend_exact_semantics(self):
        rng = np.random.default_rng(391)
        audio = rng.normal(0, 0.1, (4096, 2)).astype(np.float32)
        basis = rng.uniform(0, 0.01, (NUM_MELS, N_FFT // 2 + 1)).astype(np.float32)
        calls = []

        def mel_filter_fn(**kwargs):
            calls.append(kwargs)
            return basis

        actual = numpy_mel_spectrogram(audio, mel_filter_fn=mel_filter_fn)
        padded = np.pad(audio.T, ((0, 0), (REFLECT_PAD, REFLECT_PAD)), mode="reflect")
        window = np.hanning(N_FFT + 1)[:-1]
        frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT, axis=1)
        frames = frames[:, ::HOP_LENGTH, :]
        spectrum = np.fft.rfft(frames * window, axis=-1)
        magnitude = np.sqrt(spectrum.real ** 2 + spectrum.imag ** 2 + 1e-9)
        expected = np.log(np.maximum(np.einsum("mf,ctf->cmt", basis, magnitude), LOG_CLAMP))
        np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=2e-6)
        self.assertEqual(actual.shape, (2, NUM_MELS, 8))
        self.assertEqual(calls, [{
            "sr": 44100, "n_fft": 2048, "n_mels": 128,
            "fmin": 0, "fmax": None, "htk": False, "norm": "slaney",
        }])

    def test_context_planning_and_synchronized_channel_batch(self):
        plans = plan_mel_chunks(10, 4, 2)
        self.assertEqual(
            [(p.core_start, p.core_stop, p.expanded_start, p.expanded_stop) for p in plans],
            [(0, 4, 0, 6), (4, 8, 2, 10), (8, 10, 6, 10)],
        )
        mel = np.zeros((2, NUM_MELS, 10), dtype=np.float32)
        calls = []

        def generator(batch):
            calls.append(batch.shape)
            channels, _, frames = batch.shape
            output = np.empty((channels, frames * HOP_LENGTH), dtype=np.float32)
            output[0] = 1.0
            output[1] = 2.0
            return output[:, None, :]

        output, report = reconstruct_from_mel(
            mel, generator, target_samples=5000, core_frames=4, context_frames=2,
        )
        self.assertEqual(calls, [(2, NUM_MELS, 6), (2, NUM_MELS, 8), (2, NUM_MELS, 4)])
        self.assertEqual(output.shape, (5000, 2))
        np.testing.assert_array_equal(output[:, 0], 1.0)
        np.testing.assert_array_equal(output[:, 1], 2.0)
        self.assertEqual(report["channel_policy"],
                         "independent_mono_channels_in_synchronized_batch")
        self.assertEqual(report["length_reconciliation"]["action"], "trim_right")
        self.assertEqual([item["retained_start"] for item in report["chunks"]],
                         [0, 2 * HOP_LENGTH, 2 * HOP_LENGTH])

    def test_length_reconciliation_is_deterministic_and_reported(self):
        audio = np.arange(10, dtype=np.float32)[:, None]
        trimmed, trim_report = reconcile_output_length(audio, 7)
        np.testing.assert_array_equal(trimmed[:, 0], np.arange(7))
        self.assertEqual(trim_report["action"], "trim_right")
        padded, pad_report = reconcile_output_length(audio, 12)
        np.testing.assert_array_equal(padded[:10], audio)
        np.testing.assert_array_equal(padded[10:], 0.0)
        self.assertEqual(pad_report["action"], "pad_right_zero")
        exact, exact_report = reconcile_output_length(audio, 10)
        np.testing.assert_array_equal(exact, audio)
        self.assertEqual(exact_report["action"], "exact")

    def test_seam_comparison_separates_boundary_error(self):
        reference = np.zeros((100, 2), dtype=np.float32)
        chunked = reference.copy()
        chunked[48:52] = 0.25
        report = seam_comparison(chunked, reference, [50], radius=4)
        self.assertEqual(report["boundaries"], [50])
        self.assertEqual(report["seam"]["max_abs"], 0.25)
        self.assertEqual(report["non_seam"]["max_abs"], 0.0)

    def test_sha_verified_before_separate_loader_and_cuda_kernel_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"local fake checkpoint"
            checkpoint = root / "bigvgan_generator.pt"
            checkpoint.write_bytes(payload)
            (root / "config.json").write_text(json.dumps(EXPECTED_CONFIG), encoding="utf-8")
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(verify_checkpoint_sha256(checkpoint, expected)["sha256"], expected)
            called = []

            def loader(*args, **kwargs):
                called.append((args, kwargs))
                return FakeModel()

            with self.assertRaises(BigVGANRevoCodeError):
                load_verified_runtime(root, loader=loader)
            self.assertEqual(called, [])

            # Keep production pin immutable while proving verification precedes loading.
            from unittest import mock
            with mock.patch("experiments.bigvgan_revocode.GENERATOR_SHA256", expected):
                model, provenance = load_verified_runtime(root, loader=loader, device="cpu")
            self.assertTrue(model.eval_called)
            self.assertEqual(model.device, "cpu")
            self.assertFalse(called[0][1]["use_cuda_kernel"])
            self.assertTrue(called[0][1]["local_files_only"])
            self.assertEqual(called[0][1]["revision"], MODEL_REVISION)
            self.assertFalse(provenance["use_cuda_kernel"])

    def test_float_artifacts_provenance_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            audio = np.linspace(-0.25, 0.25, 1024, dtype=np.float32)[:, None]
            report = save_float_artifacts(
                output, {"reconstruction.wav": audio}, {"test": True}
            )
            self.assertEqual(sf.info(output / "reconstruction.wav").subtype, "FLOAT")
            self.assertEqual(report["model"]["revision"], MODEL_REVISION)
            saved = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["frontend"]["reflect_pad"], 768)
            self.assertEqual(saved["provenance"], {"test": True})
            with self.assertRaises(FileExistsError):
                save_float_artifacts(output, {"again.wav": audio}, {})


class FakeModel:
    def __init__(self):
        self.device = None
        self.eval_called = False

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        self.eval_called = True
        return self


if __name__ == "__main__":
    unittest.main()
