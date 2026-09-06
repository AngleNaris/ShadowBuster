import unittest

import numpy as np

from apollo_scripts.audio_validation import validate_audio_pair, validate_audio, finite_range


class AudioValidationTests(unittest.TestCase):
    def test_pair_rejects_length_and_channels(self):
        a = np.zeros((4, 2)); b = np.zeros((3, 2))
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            validate_audio_pair(a, b, 44100)
        with self.assertRaisesRegex(ValueError, "channel count mismatch"):
            validate_audio_pair(a, np.zeros((4, 1)), 44100)

    def test_rejects_empty_and_nonfinite(self):
        with self.assertRaisesRegex(ValueError, "empty audio"):
            validate_audio(np.zeros((0, 2)))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validate_audio(np.array([[np.nan, 0.0]]))

    def test_finite_range(self):
        self.assertEqual(finite_range(2, "x", 0, 3), 2.0)
        for value in (-1, 4, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                finite_range(value, "x", 0, 3)


if __name__ == "__main__":
    unittest.main()
