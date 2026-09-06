import numpy as np
import pyloudnorm as pyln
import librosa
import soundfile as sf
from scipy import signal, interpolate
from statsmodels.nonparametric.smoothers_lowess import lowess
import os
import sys
import scipy.signal
import json
from multiprocessing import Pool
import numba
import argparse
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet
import io
import base64
import random
import joblib
from test_model import get_suggestions_for_genre
from scipy.signal import butter, filtfilt
import scipy.signal as signal
import time

# Use a simple string as a key
KEY = "SimpleKey123"

def xor_encrypt_decrypt(data, key):
    return bytes(a ^ ord(key[i % len(key)]) for i, a in enumerate(data))

class Config:
    def __init__(self):
        self.threshold = 0.95
        self.knee_width = 0.1
        self.min_value = 1e-8
        self.max_piece_size = 44100 * 5  # 5 seconds
        self.internal_sample_rate = 44100
        self.lowess_frac = 0.15  # Increased for more smoothing
        self.lowess_it = 2  # Increased for more robustness
        self.lowess_delta = 0.1  # Increased for faster computation
        self.rms_correction_steps = 4
        self.limiter_threshold = 0.98
        self.limiter_knee_width = 0.3
        self.limiter_attack_ms = 1
        self.limiter_release_ms = 150
        self.reference_file = os.path.join(os.path.dirname(__file__), 'reference_track.mp3')
        self.fft_size = 4096
        self.lin_log_oversampling = 4
        self.clipping_threshold = 0.99
        self.clipping_samples_threshold = 8
        self.high_shelf_freq = 8000
        self.high_shelf_gain_db_mid = -1.5  # Reduced from -2.5
        self.high_shelf_gain_db_side = -0.5  # Reduced from -0.8
        self.lowpass_cutoff = 20000  # Default mastering lowpass: 20 kHz
        self.bypass_high_shelf = False
        self.compressor_threshold = -3
        self.compressor_ratio = 4
        self.compressor_knee_width = 6
        self.compressor_attack_ms = 5
        self.compressor_release_ms = 50
        self.limiter_thresholds = [0.95, 0.98]
        self.limiter_knee_widths = [0.1, 0.05]
        self.limiter_attack_times = [1, 0.5]
        self.limiter_release_times = [50, 25]
        self.limiter_mix = 0.95  # Reduced limited signal mix
        self.genre = None
        self.oversampling_factor = 4
        self.true_peak_oversampling = 4
        self.true_peak_ceiling_db = -0.4
        self.limiter_ceiling_db = -0.5
        self.limiter_lookahead_ms = 5.0
        self.limiter_attack_ms = 1.0
        self.limiter_release_ms = 150.0
        self.epsilon = 1e-8  # Small value to prevent division by zero
        self.bass_preservation_freq = 10
        self.bass_preservation_blend = 0.98
        self.apply_stereo_widening = True  # or False
        self.stereo_width_adjustment_factor = 0.2  # Adjusts 20% of the difference by default
        self.sample_rate = 44100  # Add this line
        self.loudness_option = "normal"  # Default to "normal"
        self.style_mode = "styled"
        self.eq_style = "Neutral"  # Default to "Neutral"
        self.use_loudest_parts = True
        self.loudness_threshold = 0.4
        self.rms_match_limit_db = 3.0
        self.gradual_level_limit_db = 1.5
        self.limiter_drive_budget_db = 1.5

def load_secured_audio(file_path, config):
    with open(file_path, 'rb') as f:
        encrypted_data = f.read()
    decrypted_data = xor_encrypt_decrypt(encrypted_data, KEY)
    mp3_buffer = io.BytesIO(decrypted_data)
    audio, sr = sf.read(mp3_buffer)
    return audio.T, sr

def load_audio(file_path, config):
    print(f"Loading audio file: {file_path}")
    if file_path.endswith('.secgnr'):
        audio, sr = load_secured_audio(file_path, config)
    else:
        audio, sr = librosa.load(file_path, sr=None, mono=False)
    print(f"Loaded audio shape: {audio.shape}, max={np.max(np.abs(audio))}, min={np.min(np.abs(audio))}")
    return audio, sr

def save_audio(audio, file_path, sr):
    print(f"Saving audio to: {file_path}")
    print(f"Audio shape: {audio.shape}, max={np.max(np.abs(audio))}, min={np.min(np.abs(audio))}")
    print(f"Saving with sample rate: {sr}")
    sf.write(file_path, audio.T, sr, subtype='PCM_24')

def apply_dither(audio, bits=24, rng=None):
    """Apply standard two-LSB peak-to-peak TPDF dither before PCM quantization."""
    if rng is None:
        rng = np.random.default_rng()
    lsb = 1.0 / (2 ** (bits - 1))
    noise = (rng.random(audio.shape) - rng.random(audio.shape)) * lsb
    return audio + noise

def lr_to_ms(array):
    mid = (array[0] + array[1]) * 0.5
    side = (array[0] - array[1]) * 0.5
    return mid, side

def ms_to_lr(mid, side):
    min_length = min(len(mid), len(side))
    mid = mid[:min_length]
    side = side[:min_length]

    left = mid + side
    right = mid - side
    return np.vstack((left, right))

def oversample(audio, factor):
    return signal.resample_poly(audio, factor, 1, axis=-1)

def downsample(audio, factor, sample_rate):
    filtered_audio = improved_anti_aliasing_filter(audio, sample_rate)
    return signal.resample_poly(filtered_audio, 1, factor, axis=-1)

def improved_anti_aliasing_filter(audio, sample_rate):
    nyquist = sample_rate / 2
    cutoff = 0.9 * nyquist
    sos = signal.butter(10, cutoff / nyquist, btype='low', output='sos')

    filtered_audio = np.zeros_like(audio)
    for channel in range(audio.shape[0]):
        filtered_audio[channel] = signal.sosfilt(sos, audio[channel])

    return filtered_audio

def apply_lowpass_filter(audio, config):
    nyquist = config.internal_sample_rate * config.oversampling_factor / 2
    sos = signal.butter(10, config.lowpass_cutoff / nyquist, btype='low', output='sos')

    filtered_audio = np.zeros_like(audio)
    for channel in range(audio.shape[0]):
        filtered_audio[channel] = signal.sosfilt(sos, audio[channel])

    return filtered_audio

def add_subtle_mid_channel_saturation(mid, config):
    # Define saturation parameters
    saturation_amount = 0.03  # Very subtle, adjust as needed
    blend_factor = 0.3  # Subtle blend, adjust as needed

    # Apply saturation to the entire mid channel
    saturated_mid = np.tanh(mid * (1 + saturation_amount)) / (1 + saturation_amount)

    # Blend the saturated mid with the original mid
    mid_enhanced = mid * (1 - blend_factor) + saturated_mid * blend_factor

    return mid_enhanced

def apply_peaking_filter(signal, freq, q, gain_db, sample_rate):
    w0 = 2 * np.pi * freq / sample_rate
    alpha = np.sin(w0) / (2 * q)
    A = 10 ** (gain_db / 40)

    b0 = 1 + alpha * A
    b1 = -2 * np.cos(w0)
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * np.cos(w0)
    a2 = 1 - alpha / A

    b = [b0, b1, b2]
    a = [a0, a1, a2]

    return signal.filtfilt(b, a, signal, padlen=len(signal)-1)

def calculate_average_spectrum(audio, sample_rate, fft_size):
    _, _, specs = signal.stft(
        audio,
        sample_rate,
        window="hann",
        nperseg=fft_size,
        noverlap=fft_size // 2,
        boundary=None,
        padded=False,
    )
    return np.abs(specs).mean(axis=1)

def smooth_spectrum(spectrum, config):
    fft_size = (len(spectrum) - 1) * 2
    grid_linear = np.linspace(0, config.internal_sample_rate / 2, len(spectrum))
    grid_logarithmic = np.logspace(
        np.log10(4 * config.internal_sample_rate / fft_size),
        np.log10(config.internal_sample_rate / 2),
        (len(spectrum) - 1) * config.lin_log_oversampling + 1,
    )

    interpolator = interpolate.interp1d(grid_linear, spectrum, "cubic", bounds_error=False, fill_value="extrapolate")
    spectrum_log = interpolator(grid_logarithmic)

    spectrum_smoothed = lowess(
        spectrum_log,
        np.arange(len(spectrum_log)),
        frac=config.lowess_frac,
        it=config.lowess_it,
        delta=config.lowess_delta * len(spectrum_log),
    )[:, 1]

    interpolator = interpolate.interp1d(
        grid_logarithmic, spectrum_smoothed, "cubic", bounds_error=False, fill_value="extrapolate"
    )
    spectrum_filtered = interpolator(grid_linear)

    spectrum_filtered[0] = 0
    spectrum_filtered[1] = spectrum[1]

    return spectrum_filtered

def calculate_improved_rms(audio, sample_rate, config):
    def rms(x):
        return np.sqrt(np.mean(np.square(x)) + config.epsilon)

    # Define piece size (3 seconds)
    piece_size = 3 * sample_rate

    # Divide audio into pieces
    pieces = np.array_split(audio, max(1, len(audio) // piece_size))

    # Calculate RMS for each piece
    rms_values = np.array([rms(piece) for piece in pieces])

    # Calculate average RMS
    avg_rms = np.mean(rms_values)

    # Identify loudest pieces (above average RMS)
    loud_mask = rms_values >= avg_rms

    # Calculate final RMS using only the loudest pieces
    final_rms = rms(np.concatenate([pieces[i] for i in range(len(pieces)) if loud_mask[i]]))

    return final_rms

def _bounded_gain_db(target_rms, reference_rms, limit_db, epsilon):
    raw_gain_db = 20 * np.log10(max(reference_rms, epsilon) / max(target_rms, epsilon))
    return float(np.clip(raw_gain_db, -limit_db, limit_db))


def match_rms_ms(target_mid, target_side, reference_mid, reference_side, sample_rate, config):
    def match_rms(target, reference):
        target_rms = calculate_improved_rms(target, sample_rate, config)
        reference_rms = calculate_improved_rms(reference, sample_rate, config)
        gain_db = _bounded_gain_db(
            target_rms, reference_rms, config.rms_match_limit_db, config.epsilon
        )
        return target * 10 ** (gain_db / 20), gain_db

    target_side_rms = calculate_improved_rms(target_side, sample_rate, config)
    target_mid_rms = calculate_improved_rms(target_mid, sample_rate, config)
    mono_threshold = 0.01

    matched_mid, mid_gain_db = match_rms(target_mid, reference_mid)

    if target_side_rms / max(target_mid_rms, config.epsilon) < mono_threshold:
        print("Detected nearly mono audio. Skipping side channel RMS matching.")
        matched_side = target_side * 10 ** (mid_gain_db / 20)
        side_gain_db = mid_gain_db
    else:
        matched_side, side_gain_db = match_rms(target_side, reference_side)

    print(
        f"Bounded RMS match gains: mid={mid_gain_db:.2f} dB, "
        f"side={side_gain_db:.2f} dB, limit=±{config.rms_match_limit_db:.2f} dB"
    )
    return matched_mid, matched_side

def match_frequencies_ms(target_mid, target_side, reference_mid, reference_side, config):
    def calculate_average_fft(*args, sample_rate, fft_size, config):
        if len(args) == 1:
            # Single audio input
            audio = args[0]
            mid = side = audio
        elif len(args) == 2:
            # Separate mid and side inputs
            mid, side = args
        else:
            raise ValueError("Invalid number of arguments for calculate_average_fft")

        if config.use_loudest_parts:
            segment_length = sample_rate // 10  # 100ms segments
            num_segments = len(mid) // segment_length
            segments_mid = np.array_split(mid[:num_segments * segment_length], num_segments)

            # Calculate RMS based on mid channel only
            segment_rms = np.sqrt(np.mean(np.square(segments_mid), axis=1))

            loud_mask = segment_rms > (config.loudness_threshold * np.max(segment_rms))
            loud_segments_mid = [seg for seg, is_loud in zip(segments_mid, loud_mask) if is_loud]

            if len(loud_segments_mid) == 0:
                print("No segments above threshold, using entire audio.")
                return mid, side
            else:
                percentage_used = (len(loud_segments_mid) / len(segments_mid)) * 100
                print(f"Using {percentage_used:.2f}% of the audio (threshold: {config.loudness_threshold})")

            # Use the same mask for side channel
            segments_side = np.array_split(side[:num_segments * segment_length], num_segments)
            loud_segments_side = [seg for seg, is_loud in zip(segments_side, loud_mask) if is_loud]

            mid = np.concatenate(loud_segments_mid)
            side = np.concatenate(loud_segments_side)

        _, _, specs_mid = signal.stft(
            mid,
            sample_rate,
            window="hann",
            nperseg=fft_size,
            noverlap=fft_size // 2,
            boundary=None,
            padded=False,
        )
        _, _, specs_side = signal.stft(
            side,
            sample_rate,
            window="hann",
            nperseg=fft_size,
            noverlap=fft_size // 2,
            boundary=None,
            padded=False,
        )
        return np.abs(specs_mid).mean(axis=1), np.abs(specs_side).mean(axis=1)

    def smooth_spectrum(spectrum, config):
        fft_size = (len(spectrum) - 1) * 2
        grid_linear = np.linspace(0, config.internal_sample_rate / 2, len(spectrum))
        grid_logarithmic = np.logspace(
            np.log10(4 * config.internal_sample_rate / fft_size),
            np.log10(config.internal_sample_rate / 2),
            (len(spectrum) - 1) * config.lin_log_oversampling + 1,
        )

        interpolator = interpolate.interp1d(grid_linear, spectrum, "cubic", bounds_error=False, fill_value="extrapolate")
        spectrum_log = interpolator(grid_logarithmic)

        spectrum_smoothed = lowess(
            spectrum_log,
            np.arange(len(spectrum_log)),
            frac=config.lowess_frac,
            it=config.lowess_it,
            delta=config.lowess_delta * len(spectrum_log),
        )[:, 1]

        interpolator = interpolate.interp1d(
            grid_logarithmic, spectrum_smoothed, "cubic", bounds_error=False, fill_value="extrapolate"
        )
        spectrum_filtered = interpolator(grid_linear)

        spectrum_filtered[0] = 0
        spectrum_filtered[1] = spectrum[1]

        return spectrum_filtered

    def get_fir(target_mid, target_side, reference_mid, reference_side, config, is_side=False):
        analysis_sample_rate = config.internal_sample_rate * config.oversampling_factor
        target_fft_mid, target_fft_side = calculate_average_fft(
            target_mid, target_side,
            sample_rate=analysis_sample_rate,
            fft_size=config.fft_size,
            config=config
        )
        reference_fft_mid, reference_fft_side = calculate_average_fft(
            reference_mid, reference_side,
            sample_rate=analysis_sample_rate,
            fft_size=config.fft_size,
            config=config
        )

        target_fft = target_fft_side if is_side else target_fft_mid
        reference_fft = reference_fft_side if is_side else reference_fft_mid

        target_fft = np.maximum(target_fft, config.min_value)
        matching_fft = reference_fft / target_fft

        max_boost_db = 2 if is_side else 4  # Further reduced max boost
        matching_fft = np.clip(matching_fft, 10**(-max_boost_db/20), 10**(max_boost_db/20))
        matching_fft_filtered = smooth_spectrum(matching_fft, config)

        # These are frequency-domain curves and must be applied before building
        # the FIR. Applying a len(audio) curve to samples makes the mastering
        # strength depend on where a sound occurs in the song.
        freqs = np.linspace(0, analysis_sample_rate / 2, len(matching_fft_filtered))
        mix = 0.99 * (1 - np.exp(-freqs / 10.0)) * np.exp(-freqs / 100000.0)
        matching_fft_filtered = 1 + (matching_fft_filtered - 1) * mix
        # Preserve the Mid presence band: never apply more than 1 dB of cut
        # between 1.5 and 5 kHz, and shrink the correction toward unity.
        if not is_side:
            presence = (freqs >= 1500.0) & (freqs <= 5000.0)
            matching_fft_filtered[presence] = 1.0 + 0.5 * (
                np.maximum(matching_fft_filtered[presence], 10 ** (-1.0 / 20.0)) - 1.0
            )
        bass_preservation = 1 - (1 - config.bass_preservation_blend) * (
            1 / (1 + (freqs / config.bass_preservation_freq)**2)
        )
        matching_fft_filtered = 1 + (matching_fft_filtered - 1) * bass_preservation

        fir = np.fft.irfft(matching_fft_filtered)
        fir = np.fft.ifftshift(fir) * signal.windows.hann(len(fir))

        return fir

    mid_fir = get_fir(target_mid, target_side, reference_mid, reference_side, config, is_side=False)
    side_fir = get_fir(target_mid, target_side, reference_mid, reference_side, config, is_side=True)

    result_mid = signal.fftconvolve(target_mid, mid_fir, mode="same")
    result_side = signal.fftconvolve(target_side, side_fir, mode="same")

    return result_mid, result_side

def gradual_level_correction(target_mid, target_side, reference_mid, reference_side, config):
    def total_gain_db(target, reference):
        target_rms = np.sqrt(np.mean(target**2) + config.epsilon)
        reference_rms = np.sqrt(np.mean(reference**2) + config.epsilon)
        return _bounded_gain_db(
            target_rms,
            reference_rms,
            config.gradual_level_limit_db,
            config.epsilon,
        )

    mid_gain_db = total_gain_db(target_mid, reference_mid)
    side_gain_db = total_gain_db(target_side, reference_side)
    step_mid_gain = 10 ** (mid_gain_db / (20 * config.rms_correction_steps))
    step_side_gain = 10 ** (side_gain_db / (20 * config.rms_correction_steps))

    for _ in range(config.rms_correction_steps):
        target_mid *= step_mid_gain
        target_side *= step_side_gain

    print(
        f"Bounded gradual correction totals: mid={mid_gain_db:.2f} dB, "
        f"side={side_gain_db:.2f} dB, limit=±{config.gradual_level_limit_db:.2f} dB"
    )
    return target_mid, target_side

def rms(audio):
    return np.sqrt(np.mean(np.square(audio)))


def correct_mid_presence(target_mid, reference_mid, sample_rate, max_db=0.75):
    """Apply a bounded relative RMS correction to Mid content from 1-4 kHz."""
    sos = signal.butter(4, [1000.0, 4000.0], btype="bandpass", fs=sample_rate, output="sos")
    target_band = signal.sosfilt(sos, target_mid)
    reference_band = signal.sosfilt(sos, reference_mid)
    target_rms = np.sqrt(np.mean(target_band ** 2) + 1e-12)
    reference_rms = np.sqrt(np.mean(reference_band ** 2) + 1e-12)
    correction_db = float(np.clip(20.0 * np.log10(reference_rms / target_rms), -max_db, max_db))
    gain = 10 ** (correction_db / 20.0)
    return target_mid + (target_band * (gain - 1.0)), correction_db

def segment_audio(audio, config):
    segment_length = config.internal_sample_rate  # 1 second segments
    num_full_segments = len(audio) // segment_length
    segments = np.array_split(audio[:num_full_segments * segment_length], num_full_segments)
    return np.array(segments)

def analyze_stereo_width(mid, side):
    mid_energy = np.mean(np.square(mid))
    side_energy = np.mean(np.square(side))
    return side_energy / (mid_energy + side_energy + 1e-8)

def adjust_stereo_balance(mid, side, target_width, config):
    current_width = analyze_stereo_width(mid, side, config)

    width_difference = target_width - current_width
    if abs(width_difference) < 0.05:  # Less than 5% difference
        return mid, side

    adjustment_factor = 1 + config.stereo_width_adjustment_factor * width_difference

    # Only adjust the side channel
    adjusted_side = side * adjustment_factor

    # Ensure RMS remains constant
    original_rms = np.sqrt(np.mean(mid**2 + side**2))
    adjusted_rms = np.sqrt(np.mean(mid**2 + adjusted_side**2))
    rms_correction = original_rms / adjusted_rms

    return mid, adjusted_side * rms_correction

def finalize_stereo_image(target_mid, target_side, reference_mid, reference_side, config, source_width=None):
    print("Finalizing stereo image...")
    try:
        # Calculate stereo widths
        initial_width = analyze_stereo_width(target_mid, target_side)
        reference_width = analyze_stereo_width(reference_mid, reference_side)

        print(f"Initial stereo width: {initial_width:.4f}")
        print(f"Reference stereo width: {reference_width:.4f}")

        # Check for near-mono signal
        if initial_width < 0.02:
            print("Input signal is nearly mono. Skipping stereo adjustment.")
            return ms_to_lr(target_mid, target_side)

        # Calculate initial RMS
        initial_rms = np.sqrt(np.mean(target_mid**2 + target_side**2))

        # Calculate width difference and apply adjustment with upper limit
        width_difference = reference_width - initial_width
        max_adjustment = 0.15  # 15% maximum adjustment

        if width_difference > 0:
            adjustment_factor = 1 + min(width_difference / initial_width, max_adjustment)
            adjusted_side = target_side * adjustment_factor
            print(f"Applied {(adjustment_factor - 1) * 100:.1f}% stereo width increase.")
        else:
            print("No stereo width increase needed.")
            adjusted_side = target_side

        # Convert to left-right
        result = ms_to_lr(target_mid, adjusted_side)

        # RMS matching
        current_rms = np.sqrt(np.mean(result**2))
        rms_adjustment = initial_rms / current_rms
        result *= rms_adjustment

        final_mid, final_side = lr_to_ms(result)
        final_width = analyze_stereo_width(final_mid, final_side)

        # 宽度保底：side 的 RMS/频谱匹配与 side 低频收紧会把比参考更宽的素材收窄；
        # 保证最终宽度不低于源素材（只补偿收窄，不削弱上面已有的扩展）。
        if source_width is not None and final_width < source_width:
            m_e = np.mean(final_mid**2)
            s_e = np.mean(final_side**2)
            w = s_e / (m_e + s_e + 1e-12)
            # 宽度 = sideE/(midE+sideE)，对 side 幅度的平方敏感 → 开平方得振幅增益
            ratio = np.sqrt((source_width * (1.0 - w)) / (w * (1.0 - source_width) + 1e-12))
            ratio = min(ratio, 4.0)  # 防近单声道/全 side 时的发散
            boosted_side = final_side * ratio
            result = ms_to_lr(final_mid, boosted_side)
            # 整体缩放保持 RMS（mid/side 同缩不改变宽度），响度由后级限制器回归
            rms0 = np.sqrt(np.mean(final_mid**2 + final_side**2))
            rms1 = np.sqrt(np.mean(result**2))
            if rms1 > 1e-12:
                result *= rms0 / rms1
            final_width = analyze_stereo_width(*lr_to_ms(result))
            print(f"Applied width floor restore (source {source_width:.4f}): "
                  f"+{(ratio - 1) * 100:.1f}% side gain -> final {final_width:.4f}")

        print(f"Initial stereo width: {initial_width:.4f}")
        print(f"Final stereo width: {final_width:.4f}")
        print(f"Stereo image finalization complete. Output max amplitude: {np.max(np.abs(result)):.4f}")

        return result
    except Exception as e:
        print(f"Error during stereo image finalization: {str(e)}")
        return ms_to_lr(target_mid, target_side)

def process_band(args):
    mid, side, target_balance, config, band = args
    if band[0] == 0:
        sos = signal.butter(10, band[1], btype='lowpass', fs=config.internal_sample_rate, output='sos')
    else:
        sos = signal.butter(10, band, btype='bandpass', fs=config.internal_sample_rate, output='sos')

    band_mid = signal.sosfilt(sos, mid)
    band_side = signal.sosfilt(sos, side)

    return adjust_stereo_balance(band_mid, band_side, target_balance, config)

def frequency_band_stereo_adjust(mid, side, target_balance, config):
    bands = [0, 250, 8000, config.internal_sample_rate // 2]  # Reduced to 3 bands

    with Pool() as pool:
        results = pool.map(process_band, [(mid, side, target_balance, config, (bands[i], bands[i+1])) for i in range(len(bands) - 1)])

    adjusted_mid = sum(result[0] for result in results)
    adjusted_side = sum(result[1] for result in results)

    return adjusted_mid, adjusted_side


@numba.jit(nopython=True)
def process_chunk(chunk, threshold, knee_width, attack_coeff, release_coeff):
    x = np.abs(chunk)
    gain_reduction = np.maximum(x / threshold, 1.0)

    for i in range(x.shape[0]):
        for j in range(x.shape[1]):
            if threshold - knee_width / 2 < x[i, j] < threshold + knee_width / 2:
                gain_reduction[i, j] = 1.0 + ((x[i, j] - (threshold - knee_width / 2)) / knee_width) ** 2 * (x[i, j] / threshold - 1.0) / 2

    smoothed_gain = np.zeros_like(gain_reduction)
    smoothed_gain[:, 0] = gain_reduction[:, 0]

    for i in range(gain_reduction.shape[0]):
        for j in range(1, gain_reduction.shape[1]):
            if gain_reduction[i, j] > smoothed_gain[i, j-1]:
                smoothed_gain[i, j] = attack_coeff * smoothed_gain[i, j-1] + (1 - attack_coeff) * gain_reduction[i, j]
            else:
                smoothed_gain[i, j] = release_coeff * smoothed_gain[i, j-1] + (1 - release_coeff) * gain_reduction[i, j]

    return chunk / smoothed_gain

def soft_knee_compressor(audio, config):
    threshold = -6.0  # dB, slightly lower for more gentle compression
    ratio = 2.5  # Gentler ratio
    knee_width = 6.0
    attack_ms = 10.0
    release_ms = 500.0  # Longer release for smoother action

    threshold_linear = 10 ** (threshold / 20)

    attack_coeff = np.exp(-1 / (attack_ms * config.internal_sample_rate * config.oversampling_factor / 1000))
    release_coeff = np.exp(-1 / (release_ms * config.internal_sample_rate * config.oversampling_factor / 1000))

    chunk_size = 4096  # Matching the FFT size from the reference
    result = np.zeros_like(audio)

    for i in range(0, audio.shape[1], chunk_size):
        chunk = audio[:, i:i+chunk_size]
        compressed_chunk = process_chunk(chunk, threshold_linear, knee_width, attack_coeff, release_coeff)

        # Apply compression ratio
        compressed_chunk = np.sign(compressed_chunk) * (np.abs(compressed_chunk) ** (1/ratio))
        result[:, i:i+chunk_size] = compressed_chunk

    return result



@numba.jit(nopython=True)
def process_multi_stage_chunk(chunk, thresholds, knee_widths, attack_coeffs, release_coeffs):
    result = chunk.copy()
    for threshold, knee_width, attack_coeff, release_coeff in zip(thresholds, knee_widths, attack_coeffs, release_coeffs):
        result = process_chunk(result, threshold, knee_width, attack_coeff, release_coeff)
    return result


def envelope_follower(x, attack_samples, release_samples):
    env = np.zeros_like(x)
    for i in range(1, x.shape[1]):
        env[:, i] = np.maximum(x[:, i], env[:, i-1] + (x[:, i] - env[:, i-1]) * (1 - np.exp(-1 / release_samples)))
    return env

@numba.jit(nopython=True, parallel=True)
def process_limiter_stage(audio, threshold, knee_width, attack_ms, release_ms, sample_rate):
    attack_samples = int(attack_ms * sample_rate / 1000)
    release_samples = int(release_ms * sample_rate / 1000)

    # Pre-calculate exponential terms
    attack_coeff = 1 - np.exp(-1 / attack_samples)
    release_coeff = 1 - np.exp(-1 / release_samples)

    # Calculate gain reduction
    gain_reduction = np.maximum(1, np.abs(audio) / threshold)

    # Apply knee
    knee_range = knee_width / 2
    soft_knee = np.clip((gain_reduction - (1 - knee_range)) / knee_width, 0, 1)
    gain_reduction = 1 + soft_knee**2 * (gain_reduction - 1)

    # Calculate smoothed gain reduction using optimized envelope follower
    smoothed_gain_reduction = np.zeros_like(gain_reduction)

    for i in numba.prange(audio.shape[0]):
        env = 0
        for j in range(audio.shape[1]):
            if gain_reduction[i, j] > env:
                env += (gain_reduction[i, j] - env) * attack_coeff
            else:
                env += (gain_reduction[i, j] - env) * release_coeff
            smoothed_gain_reduction[i, j] = env

    # Apply gain reduction only where necessary, avoiding division by zero
    epsilon = 1e-10  # Small value to prevent division by zero
    result = np.where(smoothed_gain_reduction > 1,
                      audio / np.maximum(smoothed_gain_reduction, epsilon),
                      audio)

    return result

def process_limiter_stage_with_logging(audio, threshold, knee_width, attack_ms, release_ms, sample_rate):
    result = process_limiter_stage(audio, threshold, knee_width, attack_ms, release_ms, sample_rate)

    print(f"Limiter stage - Threshold: {threshold}, Max input: {np.max(np.abs(audio))}")
    print(f"Max gain reduction: {np.max(result / (audio + np.finfo(audio.dtype).eps))}")
    print(f"Max output: {np.max(np.abs(result))}")

    return result

@numba.jit(nopython=True)
def _future_minimum(values, window_samples):
    output = np.empty_like(values)
    queue = np.empty(values.size, dtype=np.int64)
    head = 0
    tail = 0

    for index in range(values.size - 1, -1, -1):
        while head < tail and queue[head] > index + window_samples:
            head += 1
        while head < tail and values[index] <= values[queue[tail - 1]]:
            tail -= 1
        queue[tail] = index
        tail += 1
        output[index] = values[queue[head]]

    return output


@numba.jit(nopython=True)
def _smooth_limiter_attack(required_gain, attack_coeff):
    gain = np.empty_like(required_gain)
    gain[-1] = required_gain[-1]
    for index in range(required_gain.size - 2, -1, -1):
        anticipated_gain = 1.0 - (1.0 - gain[index + 1]) * attack_coeff
        gain[index] = min(required_gain[index], anticipated_gain)
    return gain


@numba.jit(nopython=True)
def _smooth_limiter_release(required_gain, release_coeff):
    gain = np.empty_like(required_gain)
    gain[0] = required_gain[0]
    for index in range(1, required_gain.size):
        if required_gain[index] < gain[index - 1]:
            gain[index] = required_gain[index]
        else:
            recovering_gain = 1.0 - (1.0 - gain[index - 1]) * release_coeff
            gain[index] = min(required_gain[index], recovering_gain)
    return gain


def linked_lookahead_limiter(audio, config):
    if audio.ndim != 2 or audio.shape[0] == 0:
        raise ValueError("Limiter expects audio shaped as (channels, samples)")
    if not np.all(np.isfinite(audio)):
        raise ValueError("Limiter input contains NaN or infinite values")
    if audio.shape[1] == 0:
        return audio.copy(), {
            "max_gain_reduction_db": 0.0,
            "active_fraction": 0.0,
            "gain_reduction_p50_db": 0.0,
            "gain_reduction_p95_db": 0.0,
            "output_peak": 0.0,
        }

    sample_rate = config.internal_sample_rate * config.oversampling_factor
    ceiling = 10 ** (config.limiter_ceiling_db / 20.0)
    lookahead_samples = max(1, int(round(config.limiter_lookahead_ms * sample_rate / 1000.0)))
    attack_samples = max(1.0, config.limiter_attack_ms * sample_rate / 1000.0)
    release_samples = max(1.0, config.limiter_release_ms * sample_rate / 1000.0)
    attack_coeff = np.exp(-1.0 / attack_samples)
    release_coeff = np.exp(-1.0 / release_samples)

    detector = np.max(np.abs(audio), axis=0)
    required_gain = np.minimum(1.0, ceiling / np.maximum(detector, config.epsilon))
    anticipated_gain = _future_minimum(required_gain, lookahead_samples)
    attacked_gain = _smooth_limiter_attack(anticipated_gain, attack_coeff)
    gain = _smooth_limiter_release(attacked_gain, release_coeff)

    result = audio * gain[np.newaxis, :]
    output_peak = float(np.max(np.abs(result)))
    tolerance = 1e-12
    if output_peak > ceiling + tolerance:
        raise RuntimeError(
            f"Lookahead limiter exceeded ceiling: {output_peak:.12f} > {ceiling:.12f}"
        )

    gain_reduction_db = -20 * np.log10(np.maximum(gain, config.epsilon))
    stats = {
        "input_peak": float(np.max(detector)),
        "input_peak_dbfs": float(20 * np.log10(max(float(np.max(detector)), config.epsilon))),
        "drive_above_ceiling_db": max(
            0.0,
            float(20 * np.log10(max(float(np.max(detector)), config.epsilon)) - config.limiter_ceiling_db),
        ),
        "max_gain_reduction_db": float(np.max(gain_reduction_db)),
        "active_fraction": float(np.mean(gain_reduction_db > 0.1)),
        "gain_reduction_p50_db": float(np.percentile(gain_reduction_db, 50)),
        "gain_reduction_p95_db": float(np.percentile(gain_reduction_db, 95)),
        "output_peak": output_peak,
    }
    return result, stats


def multi_stage_limiter(audio, config):
    result, _ = linked_lookahead_limiter(audio, config)
    return result

def load_genre_profile(genre):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(script_dir, "profiles", f"{genre}_profile.json")

    with open(profile_path, "r") as f:
        profile = json.load(f)

    # If the profile has a 'features' key, return its contents
    # Otherwise, return the whole profile (for new flat structure)
    return profile.get('features', profile)

def calculate_lufs(audio, sr):
    audio = np.asarray(audio, dtype=np.float64)
    if not np.all(np.isfinite(audio)):
        raise ValueError("LUFS input contains NaN or infinite values")

    meter = pyln.Meter(sr)
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    elif audio.shape[0] == 2 and audio.shape[1] > 2:
        audio = audio.T

    return meter.integrated_loudness(audio)


def loudness_target_lufs(genre_profile, loudness_option):
    offsets = {
        "soft": -3.10,
        "dynamic": -1.94,
        "normal": 0.0,
        "loud": 1.58,
    }
    if loudness_option not in offsets:
        raise ValueError(f"Unknown loudness option: {loudness_option}")
    if genre_profile is None:
        return None
    return float(genre_profile["lufs"]) + offsets[loudness_option]


def apply_bounded_pre_limiter_gain(result, config, requested_lufs):
    measured_lufs = calculate_lufs(result, config.internal_sample_rate * config.oversampling_factor)
    loudness_gain_db = 0.0 if requested_lufs is None else requested_lufs - measured_lufs
    peak = float(np.max(np.abs(result))) if result.size else 0.0
    peak_dbfs = 20 * np.log10(max(peak, config.epsilon))
    headroom_gain_db = (
        config.limiter_ceiling_db + config.limiter_drive_budget_db - peak_dbfs
    )
    applied_gain_db = min(loudness_gain_db, headroom_gain_db)
    result = result * 10 ** (applied_gain_db / 20)
    stats = {
        "measured_lufs": float(measured_lufs),
        "requested_lufs": None if requested_lufs is None else float(requested_lufs),
        "loudness_gain_db": float(loudness_gain_db),
        "headroom_gain_db": float(headroom_gain_db),
        "applied_gain_db": float(applied_gain_db),
        "output_peak_dbfs": float(peak_dbfs + applied_gain_db),
    }
    return result, stats

def process_transparent(target, config, requested_lufs, core):
    target = np.array(target, dtype=np.float64, copy=True)
    if target.ndim != 2 or target.shape[0] != 2 or not target.shape[1]:
        raise ValueError('Transparent mastering requires nonempty stereo audio')
    if not np.isfinite(target).all():
        raise ValueError('Transparent input must be finite')
    sr = config.internal_sample_rate
    initial = core.calculate_lufs(target, sr)
    if requested_lufs is None or not np.isfinite(requested_lufs):
        raise ValueError('Transparent mastering requires a finite target LUFS')
    if not np.isfinite(initial):
        raise ValueError('Cannot loudness-normalize silent audio')
    up = core.oversample(target, config.oversampling_factor)
    drive = requested_lufs - core.calculate_lufs(up, sr * config.oversampling_factor)
    best = None
    # Always render from the unchanged input, never cascade limiters between trials.
    for attempt in range(12):
        limited, stats = core.linked_lookahead_limiter(up * 10 ** (drive / 20), config)
        result = signal.resample_poly(limited, 1, config.oversampling_factor, axis=-1)
        tp = core.calculate_true_peak(result, sr, config.true_peak_oversampling)
        trim = min(0.0, config.true_peak_ceiling_db - 0.01 - tp)
        result *= 10 ** (trim / 20)
        measured = core.calculate_lufs(result, sr)
        error = requested_lufs - measured
        candidate = (abs(error), result, stats, measured, trim, drive, attempt + 1)
        if best is None or candidate[0] < best[0]:
            best = candidate
        if abs(error) <= 0.1:
            break
        if drive > 30 or (attempt > 0 and abs(error) >= previous_error - 0.005):
            break
        previous_error = abs(error)
        drive += float(np.clip(error, -3, 3))
    error, result, stats, measured, trim, drive, attempts = best
    result = core.apply_dither(result)
    tp = core.calculate_true_peak(result, sr, config.true_peak_oversampling)
    if tp > config.true_peak_ceiling_db:
        raise RuntimeError('Transparent output exceeds true peak ceiling')
    config.last_mastering_stats = {
        'style_mode': 'off', 'target_lufs': float(requested_lufs),
        'input_lufs': float(initial), 'actual_lufs': float(core.calculate_lufs(result, sr)),
        'target_error_lu': float(error), 'target_met': bool(error <= 0.2),
        'applied_gain_db': float(drive), 'safety_trim_db': float(trim),
        'true_peak_dbtp': float(tp), 'limiter': stats, 'iterations': attempts,
        'spectral_processing': False,
    }
    return result


def process_audio(target, reference, step, config, genre_profile=None):
    if config.style_mode in ("off", "eq_only"):
        if config.style_mode == "eq_only" and config.eq_style != "Neutral":
            mid, side = lr_to_ms(np.array(target, dtype=np.float64, copy=True))
            mid, side = apply_eq_style(mid, side, config.internal_sample_rate, config.eq_style)
            target = ms_to_lr(mid, side)
        result = process_transparent(target, config, loudness_target_lufs(genre_profile, config.loudness_option), sys.modules[__name__])
        config.last_mastering_stats.update(
            style_mode=config.style_mode, eq_profile=config.eq_style,
            spectral_processing=config.style_mode == "eq_only" and config.eq_style != "Neutral")
        return result
    if config.style_mode != "styled":
        raise ValueError("Unknown style mode")
    start_time = time.time()
    print(f"Processing step {step}")
    print(f"Input target shape: {target.shape}, max={np.max(np.abs(target))}, min={np.min(np.abs(target))}")

    log_audio_metrics(target, "Target (Before Processing)", config)
    log_audio_metrics(reference, "Reference" if genre_profile is None else "Synthetic Reference", config)

    # Calculate and log initial LUFS
    target_lufs = calculate_lufs(target, config.internal_sample_rate)
    print(f"Target LUFS before processing: {target_lufs:.2f}")

    if genre_profile is None:
        reference_lufs = calculate_lufs(reference, config.internal_sample_rate)
        print(f"Reference LUFS: {reference_lufs:.2f}")
    else:
        synthetic_reference_lufs = calculate_lufs(reference, config.internal_sample_rate)
        print(f"Synthetic Reference LUFS: {synthetic_reference_lufs:.2f}")
        print(f"Genre profile LUFS: {genre_profile['lufs']:.2f}")

    def calculate_rms(audio):
        return np.sqrt(np.mean(np.square(audio)))

    # Calculate and log initial RMS values
    target_rms = calculate_rms(target)
    if genre_profile is None:
        reference_rms = calculate_rms(reference)
        print(f"Initial RMS - Reference: {reference_rms:.6f}, Target: {target_rms:.6f}")
    else:
        synthetic_reference_rms = calculate_rms(reference)
        print(f"Initial RMS - Synthetic Reference: {synthetic_reference_rms:.6f}, Target: {target_rms:.6f}")
        print(f"Genre Profile Initial RMS: {genre_profile['initial_rms']:.6f}")

    requested_lufs = loudness_target_lufs(genre_profile, config.loudness_option)
    if requested_lufs is None:
        print("Custom reference mode: preserving measured loudness subject to limiter headroom")
    else:
        print(
            f"Requested loudness: profile={genre_profile['lufs']:.2f} LUFS, "
            f"mode={config.loudness_option}, target={requested_lufs:.2f} LUFS"
        )

    # Ensure input audio is in 64-bit float precision
    target = target.astype(np.float64)
    reference = reference.astype(np.float64)

    # Oversample
    target = oversample(target, config.oversampling_factor)
    reference = oversample(reference, config.oversampling_factor)

    oversampled_rate = config.internal_sample_rate * config.oversampling_factor
    print(f"After oversampling: target_max={np.max(np.abs(target))}")

    # Apply anti-aliasing filter
    target = improved_anti_aliasing_filter(target, oversampled_rate)
    reference = improved_anti_aliasing_filter(reference, oversampled_rate)
    print(f"After anti-aliasing: target_max={np.max(np.abs(target))}, reference_max={np.max(np.abs(reference))}")

    # Convert to mid-side
    target_mid, target_side = lr_to_ms(target)
    # 记录源素材宽度：side 的匹配/收紧可能收窄宽素材，收尾时以此作宽度保底
    source_width = analyze_stereo_width(target_mid, target_side)
    print(f"Source stereo width: {source_width:.4f}")
    reference_mid, reference_side = lr_to_ms(reference)
    print(f"After mid-side conversion: target_mid_max={np.max(np.abs(target_mid))}, target_side_max={np.max(np.abs(target_side))}")
    print(f"reference_mid_max={np.max(np.abs(reference_mid))}, reference_side_max={np.max(np.abs(reference_side))}")

    # Apply processing steps
    if step >= 1:
        if genre_profile is None:
            target_mid, target_side = match_rms_ms(target_mid, target_side, reference_mid, reference_side, oversampled_rate, config)
        else:
            target_mid, target_side = match_rms_ms(target_mid, target_side, reference_mid, reference_side, oversampled_rate, config)
        print(f"After RMS matching: target_mid_max={np.max(np.abs(target_mid))}, target_side_max={np.max(np.abs(target_side))}")

        # Calculate and log RMS after matching
        processed_mid_side = ms_to_lr(target_mid, target_side)
        processed_rms = calculate_improved_rms(processed_mid_side, oversampled_rate, config)
        reference_rms = calculate_improved_rms(reference, oversampled_rate, config)
        print(f"After RMS matching - Reference RMS: {reference_rms:.6f}, Processed RMS: {processed_rms:.6f}")

        print("After RMS Matching:")
        log_audio_metrics(ms_to_lr(target_mid, target_side), "Target", config)

    if step >= 2:

        # Apply subtle saturation to mid channel
        target_mid = add_subtle_mid_channel_saturation(target_mid, config)
        print(f"After mid channel saturation: target_mid_max={np.max(np.abs(target_mid))}, target_side_max={np.max(np.abs(target_side))}")

        if genre_profile is None:
            target_mid, target_side = match_frequencies_ms(target_mid, target_side, reference_mid, reference_side, config)
        else:
            target_mid, target_side = match_frequencies_ms(target_mid, target_side, reference_mid, reference_side, config)
        print(f"After frequency matching: target_mid_max={np.max(np.abs(target_mid))}, target_side_max={np.max(np.abs(target_side))}")

        # Tighten only the low-frequency side content. Processing is currently
        # in the oversampled domain, so filter design must use that rate.
        target_side = low_shelf_tighten(
            target_side, oversampled_rate, cutoff_freq=100, gain=0.5, order=4
        )
        print(f"After side low-frequency tightening: target_side_max={np.max(np.abs(target_side))}")

        # Apply EQ style after frequency matching
        if config.eq_style != "Neutral":
            print(f"Applying {config.eq_style} EQ style")
            target_mid, target_side = apply_eq_style(
                target_mid, target_side, oversampled_rate, config.eq_style
            )

        # Apply lowpass filter
        result = ms_to_lr(target_mid, target_side)
        result = apply_lowpass_filter(result, config)
        target_mid, target_side = lr_to_ms(result)
        print("After Lowpass Filter:")
        log_audio_metrics(result, "Target", config)

    if step >= 3:
        if genre_profile is None:
            target_mid, target_side = gradual_level_correction(target_mid, target_side, reference_mid, reference_side, config)
        else:
            target_mid, target_side = gradual_level_correction(target_mid, target_side, reference_mid, reference_side, config)
        print(f"After gradual level correction: target_mid_max={np.max(np.abs(target_mid))}, target_side_max={np.max(np.abs(target_side))}")
        target_mid, presence_correction_db = correct_mid_presence(
            target_mid, reference_mid, oversampled_rate
        )
        print(f"Mid presence relative correction (1-4 kHz): {presence_correction_db:+.2f} dB (max ±0.75 dB)")

        print("After Level Correction:")
        log_audio_metrics(ms_to_lr(target_mid, target_side), "Target", config)

    if step >= 4:
        if genre_profile is None:
            result = finalize_stereo_image(target_mid, target_side, reference_mid, reference_side, config, source_width=source_width)
        else:
            result = finalize_stereo_image(target_mid, target_side, reference_mid, reference_side, config, source_width=source_width)
        print(f"After stereo finalization: result_max={np.max(np.abs(result))}")

        print("After Stereo Adjustment:")
        log_audio_metrics(result, "Target", config)
    else:
        result = ms_to_lr(target_mid, target_side)

    if step >= 5:
        print("Applying final mastering processes...")
        print(f"Before lookahead limiting: result_max={np.max(np.abs(result)):.4f}")

        result, pre_limiter_stats = apply_bounded_pre_limiter_gain(
            result, config, requested_lufs
        )
        print(
            "Static pre-limiter gain: "
            f"measured={pre_limiter_stats['measured_lufs']:.2f} LUFS, "
            f"requested={pre_limiter_stats['requested_lufs']}, "
            f"loudness_gain={pre_limiter_stats['loudness_gain_db']:.2f} dB, "
            f"headroom_gain={pre_limiter_stats['headroom_gain_db']:.2f} dB, "
            f"applied={pre_limiter_stats['applied_gain_db']:.2f} dB, "
            f"input_peak={pre_limiter_stats['output_peak_dbfs']:.2f} dBFS"
        )
        result, limiter_stats = linked_lookahead_limiter(result, config)
        print(
            "After linked lookahead limiting: "
            f"input_peak={limiter_stats['input_peak_dbfs']:.2f} dBFS, "
            f"drive={limiter_stats['drive_above_ceiling_db']:.2f} dB, "
            f"result_max={limiter_stats['output_peak']:.4f}, "
            f"max_gain_reduction={limiter_stats['max_gain_reduction_db']:.2f} dB, "
            f"active_fraction={limiter_stats['active_fraction']:.6f}, "
            f"p50={limiter_stats['gain_reduction_p50_db']:.2f} dB, "
            f"p95={limiter_stats['gain_reduction_p95_db']:.2f} dB"
        )

    limiter_ceiling = 10 ** (config.limiter_ceiling_db / 20.0)
    oversampled_excess = int(np.count_nonzero(np.abs(result) > limiter_ceiling + 1e-12))
    if oversampled_excess:
        raise RuntimeError(
            f"Peak controller left {oversampled_excess} samples above its ceiling"
        )

    result = downsample(result, config.oversampling_factor, oversampled_rate)
    print(f"After downsampling: result_max={np.max(np.abs(result)):.4f}")

    current_peak = float(np.max(np.abs(result)))
    current_peak_db = 20 * np.log10(max(current_peak, config.epsilon))
    print(
        f"Post-limiter peak is {current_peak_db:.2f} dBFS. "
        "Upward peak normalization is disabled."
    )

    if genre_profile and genre_profile['genre'] in ['Piano', 'Orchestral', 'Speech']:
        if config.loudness_option == "dynamic":
            result *= 10 ** (-0.3 / 20)
        elif config.loudness_option == "soft":
            result *= 10 ** (-0.6 / 20)

    print(f"Final output before dither: result_max={np.max(np.abs(result)):.4f}")
    initial_lufs = calculate_lufs(result, config.internal_sample_rate)
    print(f"LUFS before dither and True Peak safety trim: {initial_lufs:.2f}")

    true_peak_db = calculate_true_peak(
        result,
        config.internal_sample_rate,
        config.true_peak_oversampling,
    )
    safety_target_db = config.true_peak_ceiling_db - 0.01
    print(f"True Peak before safety trim (dBTP): {true_peak_db:.2f}")

    if true_peak_db > safety_target_db:
        gain_reduction_db = safety_target_db - true_peak_db
        result *= 10 ** (gain_reduction_db / 20)
        print(f"Applied True Peak safety trim. Gain reduction: {gain_reduction_db:.2f} dB")
        true_peak_db = calculate_true_peak(
            result,
            config.internal_sample_rate,
            config.true_peak_oversampling,
        )
        print(f"True Peak after safety trim (dBTP): {true_peak_db:.2f}")
    else:
        print(
            f"True Peak is already below the {safety_target_db:.2f} dBTP safety target. "
            "No safety trim applied."
        )

    result = apply_dither(result)
    dithered_true_peak_db = calculate_true_peak(
        result,
        config.internal_sample_rate,
        config.true_peak_oversampling,
    )
    if dithered_true_peak_db > config.true_peak_ceiling_db:
        raise RuntimeError(
            f"Dithered output exceeds True Peak ceiling: "
            f"{dithered_true_peak_db:.6f} > {config.true_peak_ceiling_db:.6f} dBTP"
        )
    print(f"Final dithered True Peak (dBTP): {dithered_true_peak_db:.2f}")

    final_lufs = calculate_lufs(result, config.internal_sample_rate)
    print(f"Final LUFS after all processing: {final_lufs:.2f}")

    config.last_mastering_stats = {"style_mode": "styled", "target_lufs": requested_lufs, "actual_lufs": float(final_lufs), "true_peak_dbtp": float(dithered_true_peak_db), "limiter": limiter_stats if step >= 5 else None, "pre_limiter": pre_limiter_stats if step >= 5 else None, "spectral_processing": step >= 2}

    print("After Final Processing:")
    log_audio_metrics(result, "Target", config)

    end_time = time.time()
    total_time = end_time - start_time
    print(f"Total processing time for step {step}: {total_time:.2f} seconds")

    return result

def calculate_true_peak(audio, sample_rate, oversampling=4):
    del sample_rate
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak == 0.0:
        return float("-inf")
    upsampled = signal.resample_poly(audio, oversampling, 1, axis=-1)
    true_peak = float(np.max(np.abs(upsampled)))
    return 20 * np.log10(max(true_peak, np.finfo(np.float64).tiny))

def log_audio_metrics(audio, name, config):
    print(f"--- {name} Metrics ---")
    print(f"Shape: {audio.shape}")
    print(f"Max amplitude: {np.max(np.abs(audio)):.4f}")
    lufs = calculate_lufs(audio, config.internal_sample_rate)
    print(f"LUFS: {lufs:.2f}")

    mid, side = lr_to_ms(audio)
    print(f"Mid RMS: {np.sqrt(np.mean(np.square(mid))):.4f}")
    print(f"Side RMS: {np.sqrt(np.mean(np.square(side))):.4f}")

    stereo_width = analyze_stereo_width(mid, side)
    print(f"Stereo Width: {stereo_width:.4f}")
    print("-------------------")

from test_model import get_suggestions_for_genre

def get_model_suggestions(genre):
    return get_suggestions_for_genre(genre)

def apply_guardrails(initial_value, suggested_value):
    max_increase = initial_value * 1.25  # 25% increase limit
    min_decrease = initial_value * 0.95  # 5% decrease limit
    return max(min(suggested_value, max_increase), min_decrease)

def create_reference_from_profile(genre_profile, config):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    genre = genre_profile['genre']
    secured_genre_file = os.path.join(script_dir, 'secured_genres', f"{genre}.secgnr")

    if not os.path.exists(secured_genre_file):
        raise FileNotFoundError(f"Secured genre file not found: {secured_genre_file}")

    print(f"Loading secured genre file: {secured_genre_file}")
    audio, sr = load_secured_audio(secured_genre_file, config)

    # Ensure the audio is stereo
    if audio.ndim == 1:
        audio = np.tile(audio, (2, 1))
    elif audio.shape[0] > 2:
        audio = audio[:2]

    # Downsample by factor of 4
    target_sr = sr // 4
    audio = librosa.resample(y=audio, orig_sr=sr, target_sr=target_sr)

    # Get model suggestions
    model_suggestions = get_model_suggestions(genre)
    print(f"Model suggestions for {genre}:")
    print(f"RMS Mid: {model_suggestions['rms_mid']:.4f}")
    print(f"RMS Side: {model_suggestions['rms_side']:.4f}")
    print(f"Stereo Width: {model_suggestions['stereo_width']:.4f}")

    # Calculate Initial RMS
    mid, side = lr_to_ms(audio)
    initial_rms_mid = np.sqrt(np.mean(mid**2))
    initial_rms_side = np.sqrt(np.mean(side**2))
    print(f"Initial RMS - Mid: {initial_rms_mid:.4f}, Side: {initial_rms_side:.4f}")

    # Apply guardrails
    suggested_rms_mid = apply_guardrails(initial_rms_mid, model_suggestions['rms_mid'])
    suggested_rms_side = apply_guardrails(initial_rms_side, model_suggestions['rms_side'])
    print(f"After guardrails - RMS Mid: {suggested_rms_mid:.4f}, RMS Side: {suggested_rms_side:.4f}")

    # Adjust RMS
    mid_factor = suggested_rms_mid / initial_rms_mid
    side_factor = suggested_rms_side / initial_rms_side

    adjusted_mid = mid * mid_factor
    adjusted_side = side * side_factor

    adjusted_audio = ms_to_lr(adjusted_mid, adjusted_side)

    # Calculate final RMS values
    final_mid, final_side = lr_to_ms(adjusted_audio)
    final_rms_mid = np.sqrt(np.mean(final_mid**2))
    final_rms_side = np.sqrt(np.mean(final_side**2))
    print(f"Final RMS - Mid: {final_rms_mid:.4f}, Side: {final_rms_side:.4f}")

    return adjusted_audio, target_sr

def boost_band(audio, sample_rate, low_cutoff, high_cutoff, gain, order=4):
    nyquist = 0.5 * sample_rate
    low = low_cutoff / nyquist
    high = high_cutoff / nyquist
    sos = signal.butter(order, [low, high], btype='bandpass', output='sos')
    filtered_signal = signal.sosfilt(sos, audio)
    return audio + (filtered_signal * (gain - 1))

def high_shelf_boost(audio, sample_rate, cutoff_freq, gain, order=4):
    nyquist = 0.5 * sample_rate
    normal_cutoff = cutoff_freq / nyquist
    sos = signal.butter(order, normal_cutoff, btype='highpass', output='sos')
    filtered_signal = signal.sosfilt(sos, audio)
    return audio + (filtered_signal * (gain - 1))

def low_shelf_tighten(audio, sample_rate, cutoff_freq, gain, order=4):
    gain_db = 20 * np.log10(max(gain, 1e-8))
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * cutoff_freq / sample_rate
    alpha = np.sin(w0) / np.sqrt(2.0)
    cos_w0 = np.cos(w0)
    beta = 2 * np.sqrt(A) * alpha

    b0 = A * ((A + 1) - (A - 1) * cos_w0 + beta)
    b1 = 2 * A * ((A - 1) - (A + 1) * cos_w0)
    b2 = A * ((A + 1) - (A - 1) * cos_w0 - beta)
    a0 = (A + 1) + (A - 1) * cos_w0 + beta
    a1 = -2 * ((A - 1) + (A + 1) * cos_w0)
    a2 = (A + 1) + (A - 1) * cos_w0 - beta

    sos = np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])
    return signal.sosfilt(sos, audio)

def apply_eq_style(mid, side, sample_rate, eq_style):
    print(f"Applying {eq_style} EQ style")
    if eq_style == "Warm":
        # Mid channel processing
        mid = boost_band(mid, sample_rate, low_cutoff=200, high_cutoff=300, gain=1.19, order=4)  # +1.5dB
        mid = boost_band(mid, sample_rate, low_cutoff=2000, high_cutoff=3000, gain=0.89, order=4)  # -1dB

        # Side channel processing
        side = boost_band(side, sample_rate, low_cutoff=3500, high_cutoff=4500, gain=0.92, order=4)  # -0.7dB
        side = boost_band(side, sample_rate, low_cutoff=150, high_cutoff=210, gain=1.06, order=4)  # +0.5dB

    elif eq_style == "Bright":
        # Mid channel processing
        mid = boost_band(mid, sample_rate, low_cutoff=2700, high_cutoff=3300, gain=1.19, order=4)  # +1.5dB
        mid = boost_band(mid, sample_rate, low_cutoff=500, high_cutoff=600, gain=1.08, order=4)  # +0.7dB

        # Side channel processing
        side = boost_band(side, sample_rate, low_cutoff=200, high_cutoff=300, gain=0.92, order=4)  # -0.7dB
        side = high_shelf_boost(side, sample_rate, cutoff_freq=8000, gain=1.19, order=4)  # +1.5dB

    elif eq_style == "Fusion":
        # Combination of both Warm and Bright
        # Mid channel processing
        mid = boost_band(mid, sample_rate, low_cutoff=200, high_cutoff=300, gain=1.10, order=4)  # Moderate low boost
        mid = boost_band(mid, sample_rate, low_cutoff=2700, high_cutoff=3300, gain=1.15, order=4)  # Boost similar to bright

        # Side channel processing
        side = boost_band(side, sample_rate, low_cutoff=200, high_cutoff=300, gain=0.97, order=4)  # Slight cut
        side = high_shelf_boost(side, sample_rate, cutoff_freq=8000, gain=1.12, order=4)  # Slight high-end boost

    print(f"After EQ - Mid max: {np.max(np.abs(mid)):.4f}, Side max: {np.max(np.abs(side)):.4f}")
    return mid, side

def master_audio(input_file, output_file, config, eq_style, is_preview=False):
    if os.path.realpath(input_file) == os.path.realpath(output_file):
        raise ValueError("Input and output must be different files")
    start_time = time.time()
    print(f"Master audio function called with: input_file={input_file}, output_file={output_file}, reference_file={config.reference_file}, eq_style={eq_style}, is_preview={is_preview}")
    config.eq_style = eq_style

    load_start = time.time()
    target, sr = load_audio(input_file, config)
    if sr != config.internal_sample_rate:
        raise ValueError(
            f"Soren requires {config.internal_sample_rate} Hz input, received {sr} Hz. "
            "Resample the source before mastering."
        )
    load_end = time.time()
    print(f"Audio loading time: {load_end - load_start:.2f} seconds")

    print(f"Original audio length: {len(target[0])} samples")
    print(f"Original audio duration: {len(target[0]) / sr:.2f} seconds")

    if is_preview:
        preview_start = time.time()
        preview_duration = 30  # seconds
        preview_samples = min(sr * preview_duration, target.shape[1])
        target = target[:, :preview_samples]
        preview_end = time.time()
        print(f"Preview creation time: {preview_end - preview_start:.2f} seconds")
        print(f"Processing preview: {preview_samples} samples")
        print(f"Preview duration: {preview_samples / sr:.2f} seconds")
    else:
        print(f"Processing full track: {len(target[0])} samples")

    if config.style_mode in ("off", "eq_only"):
        genre_profile = load_genre_profile(config.genre or "Pop")
        reference = None
    elif config.genre:
        print(f"Using genre profile: {config.genre}")
        genre_profile = load_genre_profile(config.genre)
        reference, _ = create_reference_from_profile(genre_profile, config)
        log_audio_metrics(reference, "Reference from Genre", config)
    elif config.reference_file:
        print(f"Using reference file: {config.reference_file}")
        reference, _ = load_audio(config.reference_file, config)
        genre_profile = None
    else:
        raise ValueError("Either genre or reference file must be specified")

    process_start = time.time()
    processed_audio = process_audio(target, reference, 5, config, genre_profile)
    process_end = time.time()
    print(f"Audio processing time: {process_end - process_start:.2f} seconds")

    save_start = time.time()
    save_audio(processed_audio, output_file, sr)
    written_audio, written_sr = sf.read(output_file, always_2d=True, dtype="float64")
    if written_sr != sr:
        raise RuntimeError(f"Written sample rate mismatch: {written_sr} != {sr}")
    written_true_peak_db = calculate_true_peak(
        written_audio.T,
        written_sr,
        config.true_peak_oversampling,
    )
    print(f"PCM24 readback True Peak (dBTP): {written_true_peak_db:.2f}")
    if written_true_peak_db > config.true_peak_ceiling_db:
        raise RuntimeError(
            f"PCM24 output exceeds True Peak ceiling: "
            f"{written_true_peak_db:.6f} > {config.true_peak_ceiling_db:.6f} dBTP"
        )
    config.last_mastering_stats.update(actual_lufs=float(calculate_lufs(written_audio.T, sr)), true_peak_dbtp=float(written_true_peak_db))
    with open(output_file + ".mastering.json", "w", encoding="utf-8") as handle:
        json.dump(config.last_mastering_stats, handle, indent=2, allow_nan=False)
    save_end = time.time()
    print(f"Audio saving time: {save_end - save_start:.2f} seconds")

    end_time = time.time()
    total_time = end_time - start_time
    print(f"Total Python processing time: {total_time:.2f} seconds")
    print("Mastering completed")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audio Mastering Tool")
    parser.add_argument("input_file", help="Path to the input audio file")
    parser.add_argument("output_file", help="Path to the output audio file")
    parser.add_argument("--reference", help="Path to a custom reference audio file")
    parser.add_argument("--genre", help="Genre profile to use for mastering")
    parser.add_argument("--loudness", choices=['soft', 'dynamic', 'normal', 'loud'], default="normal", help="Loudness option")
    parser.add_argument("--eq-profile", choices=["Neutral", "Warm", "Bright", "Fusion"], default="Neutral", help="EQ profile to use for mastering")
    parser.add_argument("--preview", action="store_true", help="Process only the preview")
    parser.add_argument("--lowpass-cutoff", type=float, default=20000,
                        help="Mastering lowpass cutoff in Hz (default: 20000 / 20 kHz)")
    parser.add_argument("--style-mode", choices=["styled", "off", "eq_only"], default="styled")
    args = parser.parse_args()

    config = Config()
    config.style_mode = args.style_mode
    config.loudness_option = args.loudness
    config.lowpass_cutoff = args.lowpass_cutoff
    if args.reference:
        config.reference_file = args.reference
        print(f"Using custom reference file: {config.reference_file}")
    elif args.genre:
        config.genre = args.genre
        print(f"Using genre profile: {config.genre}")
    else:
        print("Using default reference file")

    print(f"Received parameters: input_file={args.input_file}, output_file={args.output_file}, reference_file={config.reference_file}, genre={config.genre}, loudness={config.loudness_option}, eq_profile={args.eq_profile}, preview={args.preview}")

    master_audio(args.input_file, args.output_file, config, args.eq_profile, is_preview=args.preview)
