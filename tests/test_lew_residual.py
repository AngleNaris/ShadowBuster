from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from experiments.lew_residual import (
    SAMPLE_RATE,
    activity_gate,
    consistency_mask,
    constructive_residual_gate,
    extract_added,
    frequency_mask,
    istft_channels,
    mix_without_normalization,
    prepare_audio,
    require_matching_audio,
    stft_channels,
    verify_exact_alignment,
)
from experiments.lew_residual_cli import build_parser


class LewResidualTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(1234)

    def _shift(self, audio, shift, channel=None):
        wet = audio.copy()
        channels = range(audio.shape[1]) if channel is None else (channel,)
        for index in channels:
            wet[:, index] = 0
            if shift > 0:
                wet[shift:, index] = audio[:-shift, index]
            else:
                wet[:shift, index] = audio[-shift:, index]
        return wet

    def test_alignment_rejects_one_sample_and_larger_shifts(self):
        dry = self.rng.normal(0, 0.1, (24000, 2))
        for shift in (1, -1, 17, -33):
            with self.assertRaises(RuntimeError):
                verify_exact_alignment(dry, self._shift(dry, shift))

    def test_alignment_rejects_one_shifted_channel(self):
        dry = self.rng.normal(0, 0.1, (24000, 2))
        with self.assertRaises(RuntimeError):
            verify_exact_alignment(dry, self._shift(dry, 1, channel=1))

    def test_alignment_rejects_unrelated_and_polarity(self):
        dry = self.rng.normal(0, 0.1, (24000, 2))
        for wet in (self.rng.normal(0, 0.1, dry.shape), -dry):
            with self.assertRaises(RuntimeError):
                verify_exact_alignment(dry, wet)

    def test_alignment_identity_passes(self):
        dry = self.rng.normal(0, 0.1, (24000, 2))
        result = verify_exact_alignment(dry, dry.copy())
        self.assertTrue(result["passed"])
        self.assertTrue(result["identity"])

    def test_shape_and_rate_mismatch_rejected(self):
        dry = np.zeros((100, 2))
        with self.assertRaises(ValueError):
            require_matching_audio(dry, np.zeros((99, 2)), SAMPLE_RATE, SAMPLE_RATE)
        with self.assertRaises(ValueError):
            require_matching_audio(dry, dry, 48000, SAMPLE_RATE)

    def test_stft_roundtrip_preserves_channels_and_exact_length(self):
        audio = self.rng.normal(0, 0.1, (12345, 3))
        spectrum = stft_channels(audio, n_fft=1024, hop=256)
        restored = istft_channels(spectrum, len(audio), n_fft=1024, hop=256)
        self.assertEqual(restored.shape, audio.shape)
        np.testing.assert_allclose(restored, audio, atol=2e-12)

    def test_frequency_mask_boundaries(self):
        n_fft = 44100
        mask = frequency_mask(n_fft)
        frequency = np.fft.rfftfreq(n_fft, 1 / SAMPLE_RATE)

        def value(hz):
            return mask[np.argmin(np.abs(frequency - hz))]

        self.assertEqual(value(5000), 0.0)
        self.assertAlmostEqual(value(6000), 0.0, places=12)
        self.assertGreater(value(7000), 0.0)
        self.assertLess(value(7000), 1.0)
        self.assertAlmostEqual(value(8000), 1.0, places=12)
        self.assertAlmostEqual(value(16000), 1.0, places=12)
        self.assertAlmostEqual(value(20000), 0.2, places=12)
        self.assertAlmostEqual(value(SAMPLE_RATE / 2), 0.0, places=12)

    def test_low_band_residual_is_blocked(self):
        length = SAMPLE_RATE
        time = np.arange(length) / SAMPLE_RATE
        dry = (0.1 * np.sin(2 * np.pi * 3000 * time))[:, None]
        wet = dry + (0.01 * np.sin(2 * np.pi * 1000 * time))[:, None]
        added, _ = extract_added(dry, wet, strength=1.0)
        # Finite-window edge transients are excluded; all supported interior bins are blocked.
        self.assertLess(np.max(np.abs(added[4096:-4096])), 1e-10)

    def test_activity_gate_blocks_silence(self):
        spectrum = np.zeros((1025, 5, 2), dtype=np.complex128)
        frequencies = np.fft.rfftfreq(2048, 1 / SAMPLE_RATE)
        gate = activity_gate(spectrum, frequencies)
        self.assertTrue(np.array_equal(gate, np.zeros((5, 2))))

    def test_complex_consistency_rejects_opposite_phase(self):
        base = np.ones((8, 4, 2), dtype=np.complex128) * (1 + 1j)
        same = consistency_mask([base, base * 0.9])
        opposite = consistency_mask([base, -base])
        self.assertGreater(float(np.min(same)), 0.85)
        self.assertLess(float(np.max(opposite)), 1e-12)

    def test_constructive_gate_admits_same_phase_and_rejects_opposite_phase(self):
        dry = np.array([1 + 1j, 2 - 1j], dtype=np.complex128)
        same_phase = dry * np.array([0.25, 0.5])
        admitted, admitted_stats = constructive_residual_gate(dry, same_phase)
        np.testing.assert_array_equal(admitted, same_phase)
        self.assertEqual(admitted_stats["accepted_fraction"], 1.0)
        self.assertEqual(admitted_stats["rejected_destructive_fraction"], 0.0)

        opposite_phase = -0.25 * dry
        rejected, rejected_stats = constructive_residual_gate(dry, opposite_phase)
        np.testing.assert_array_equal(rejected, np.zeros_like(opposite_phase))
        self.assertEqual(rejected_stats["accepted_fraction"], 0.0)
        self.assertEqual(rejected_stats["rejected_destructive_fraction"], 1.0)
        self.assertEqual(rejected_stats["minimum_analysis_bin_energy_ratio"], 1.0)

    def test_constructive_gate_quadrature_preserves_phase_and_magnitude(self):
        dry = np.array([1 + 2j, -3 + 0.5j], dtype=np.complex128)
        quadrature = 0.4j * dry
        accepted, stats = constructive_residual_gate(dry, quadrature)
        np.testing.assert_array_equal(accepted, quadrature)
        self.assertGreaterEqual(stats["minimum_analysis_bin_energy_ratio"], 1.0)
        self.assertTrue(np.all(np.abs(dry + accepted) >= np.abs(dry)))

    def test_constructive_gate_stft_domain_invariant_and_near_zero_dry(self):
        dry = (self.rng.normal(size=(65, 9, 2)) +
               1j * self.rng.normal(size=(65, 9, 2)))
        candidate = 0.3 * (self.rng.normal(size=dry.shape) +
                           1j * self.rng.normal(size=dry.shape))
        dry[0, 0, 0] = 0.0
        dry[1, 0, 0] = 1e-30 + 0j
        candidate_before = candidate.copy()
        accepted, stats = constructive_residual_gate(dry, candidate)
        meaningful = np.abs(dry) > stats["dry_floor"]
        ratio = np.abs(dry[meaningful] + accepted[meaningful]) / np.abs(dry[meaningful])
        self.assertGreaterEqual(float(np.min(ratio)), 1.0 - 2e-15)
        self.assertGreaterEqual(stats["minimum_analysis_bin_energy_ratio"], 1.0 - 4e-15)
        # The gate only retains or zeros coefficients, including ratio-masked candidates.
        retained = accepted != 0
        np.testing.assert_array_equal(accepted[retained], candidate_before[retained])
        self.assertEqual(accepted[0, 0, 0], candidate_before[0, 0, 0])
        self.assertEqual(accepted[1, 0, 0], candidate_before[1, 0, 0])

    def test_extract_added_reports_gate_and_preserves_low_band_conservation(self):
        length = SAMPLE_RATE
        time = np.arange(length) / SAMPLE_RATE
        dry = (0.1 * np.sin(2 * np.pi * 3000 * time) +
               0.05 * np.sin(2 * np.pi * 10000 * time))[:, None]
        wet = dry + (0.01 * np.sin(2 * np.pi * 1000 * time) +
                     0.005 * np.sin(2 * np.pi * 10000 * time))[:, None]
        added, report = extract_added(dry, wet, strength=1.0)
        gate = report["constructive_gate"]
        self.assertGreaterEqual(gate["minimum_analysis_bin_energy_ratio"], 1.0 - 4e-15)
        self.assertGreaterEqual(gate["accepted_fraction"], 0.0)
        self.assertLessEqual(gate["accepted_fraction"], 1.0)
        low_added = np.fft.rfft(added[:, 0])
        frequencies = np.fft.rfftfreq(length, 1 / SAMPLE_RATE)
        low = frequencies < 4000
        self.assertLess(np.linalg.norm(low_added[low]), 1e-5 * np.linalg.norm(np.fft.rfft(dry[:, 0])[low]))

    def test_strength_zero_produces_no_added_signal(self):
        dry = self.rng.normal(0, 0.05, (12000, 1))
        wet = dry + self.rng.normal(0, 0.001, dry.shape)
        added, _ = extract_added(dry, wet, strength=0.0)
        self.assertTrue(np.array_equal(added, np.zeros_like(added)))

    def test_mix_is_conservative_sum_without_normalization(self):
        dry = np.full((32, 2), 0.8)
        added = np.full((32, 2), 0.4)
        with self.assertRaises(RuntimeError):
            mix_without_normalization(dry, added)
        mixed, clipping = mix_without_normalization(dry, added, allow_over=True)
        np.testing.assert_allclose(mixed, 1.2)
        self.assertAlmostEqual(clipping["peak"], 1.2)
        self.assertEqual(clipping["over_samples"], 64)

    def test_float_preparation_preserves_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            destination = Path(directory) / "prepared.wav"
            audio = self.rng.normal(0, 0.1, (1000, 3)).astype(np.float32)
            sf.write(source, audio, SAMPLE_RATE, subtype="FLOAT")
            prepare_audio(source, destination)
            info = sf.info(destination)
            restored, rate = sf.read(destination, always_2d=True, dtype="float32")
            self.assertEqual(info.subtype, "FLOAT")
            self.assertEqual(rate, SAMPLE_RATE)
            self.assertEqual(restored.shape, audio.shape)
            np.testing.assert_array_equal(restored, audio)

    def test_cli_requires_explicit_output_directory(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["input.wav"])


if __name__ == "__main__":
    unittest.main()
