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
        self.limiter_release_ms = 1500
        self.reference_file = os.path.join(os.path.dirname(__file__), 'reference_track.mp3')
        self.fft_size = 4096
        self.lin_log_oversampling = 4
        self.clipping_threshold = 0.99
        self.clipping_samples_threshold = 8
        self.high_shelf_freq = 8000
        self.high_shelf_gain_db_mid = -1.5  # Reduced from -2.5
        self.high_shelf_gain_db_side = -0.5  # Reduced from -0.8
        self.lowpass_cutoff = 18000  # Changed to 18kHz
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
        self.oversampling_factor = 4  # Increased from 4 to 8
        self.epsilon = 1e-8  # Small value to prevent division by zero
        self.bass_preservation_freq = 10
        self.bass_preservation_blend = 0.98
        self.apply_stereo_widening = True  # or False
        self.stereo_width_adjustment_factor = 0.2  # Adjusts 20% of the difference by default
        self.sample_rate = 44100  # Add this line
        self.loudness_option = "normal"  # Default to "normal"
        self.eq_style = "Neutral"  # Default to "Neutral"
        self.use_loudest_parts = True
        self.loudness_threshold = 0.4
        # Styled mastering strength: reference matching is deliberately capped,
        # while dynamic density increases continuously with this value.
        self.style_strength = 1.0
        self.last_mastering_stats = {}

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

def apply_dither(audio, bits=24):
    """
    Fast triangular dithering using optimized NumPy operations
    """
    # Single random operation instead of two separate ones
    noise = (2 * np.random.random(audio.shape) - 1) * (1.0 / (2**(bits-1)))
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

def match_rms_ms(target_mid, target_side, reference_mid, reference_side, sample_rate, config):
    strength = float(np.clip(getattr(config, "style_strength", 1.0), 0.0, 1.0))
    max_correction_db = 0.25 * strength

    def match_rms(target, reference):
        target_rms = calculate_improved_rms(target, sample_rate, config)
        reference_rms = calculate_improved_rms(reference, sample_rate, config)
        if target_rms <= config.epsilon or strength <= 0.0:
            return target, 0.0
        requested_db = 20.0 * np.log10(max(reference_rms, config.epsilon) / target_rms)
        applied_db = float(np.clip(requested_db, -max_correction_db, max_correction_db))
        return target * 10.0 ** (applied_db / 20.0), applied_db

    target_side_rms = calculate_improved_rms(target_side, sample_rate, config)
    target_mid_rms = calculate_improved_rms(target_mid, sample_rate, config)
    mono_threshold = 0.01

    matched_mid, mid_db = match_rms(target_mid, reference_mid)
    if target_mid_rms <= config.epsilon or target_side_rms / target_mid_rms < mono_threshold:
        print("Detected nearly mono audio. Skipping side channel RMS matching.")
        matched_side = target_side * (10.0 ** (mid_db / 20.0))
        side_db = mid_db
    else:
        matched_side, side_db = match_rms(target_side, reference_side)

    config.last_mastering_stats.update(rms_match_mid_db=float(mid_db), rms_match_side_db=float(side_db))
    return matched_mid, matched_side

def frequency_dependent_mix(freq, low_freq=10, high_freq=100000):
    return 0.99 * (1 - np.exp(-freq/low_freq)) * np.exp(-freq/high_freq)

def match_frequencies_ms(target_mid, target_side, reference_mid, reference_side, config):
    strength = float(np.clip(config.style_strength, 0.0, 1.0))
    if strength == 0.0:
        return target_mid.copy(), target_side.copy()

    def average_spectrum(audio):
        size = min(config.fft_size, len(audio))
        _, power = signal.welch(audio, fs=config.internal_sample_rate * config.oversampling_factor,
                                nperseg=size, nfft=config.fft_size, noverlap=size // 2)
        return np.sqrt(np.maximum(power, 0.0))

    analysis_rate = config.internal_sample_rate * config.oversampling_factor
    freqs = np.fft.rfftfreq(config.fft_size, 1.0 / analysis_rate)
    body_band = (freqs >= 250.0) & (freqs <= 2000.0)
    high_band = (freqs >= 4000.0) & (freqs <= 16000.0)
    curves = []
    darkening_guard_dbs = []
    for is_side, target, reference in ((False, target_mid, reference_mid),
                                        (True, target_side, reference_side)):
        target_fft, reference_fft = average_spectrum(target), average_spectrum(reference)
        target_energy, reference_energy = np.linalg.norm(target_fft), np.linalg.norm(reference_fft)
        if min(target_energy, reference_energy) < config.epsilon:
            curves.append(np.zeros_like(freqs))
            darkening_guard_dbs.append(0.0)
            continue
        target_fft /= target_energy
        reference_fft /= reference_energy
        reliable = ((reference_fft > reference_fft.max() * 1e-3) &
                    (target_fft > target_fft.max() * 1e-3))
        ud = getattr(config, "upstream_delta", None)
        if ud:
            shape = np.asarray(ud["side_db" if is_side else "mid_db"], dtype=np.float64)
            reference_fft *= 10.0 ** (np.interp(freqs, ud["freqs"], shape) / 20.0)
        requested = 20.0 * np.log10(np.maximum(reference_fft, 1e-12) /
                                    np.maximum(target_fft, 1e-12))
        # Fit broad trends only; a missing reference band must never become a cut.
        centers = np.geomspace(30.0, min(20000.0, analysis_rate * 0.45), 32)
        values = []
        for center in centers:
            band = (freqs >= center / 1.414) & (freqs <= center * 1.414) & reliable
            values.append(float(np.median(requested[band])) if np.any(band) else 0.0)
        smoothed = np.interp(freqs, centers, values)
        limit = (0.75 if is_side else 1.25) * strength
        curve_db = np.clip(smoothed * (0.2 * strength), -limit, limit)
        bass_preservation = 1 - (1 - config.bass_preservation_blend) / (
            1 + (freqs / config.bass_preservation_freq) ** 2)
        curve_db *= bass_preservation
        bandwidth = min(getattr(config, "reference_bandwidth_hz", analysis_rate * 0.45),
                        analysis_rate * 0.45)
        curve_db *= np.clip((bandwidth - freqs) / max(1.0, bandwidth * 0.15), 0.0, 1.0)
        curve_db *= np.clip((freqs - 15.0) / 20.0, 0.0, 1.0)
        # Bound reference-driven darkening even when it comes from a body boost
        # rather than an explicit high-frequency cut.
        darkening_guard_db = 0.0
        if np.any(body_band) and np.any(high_band):
            body_level = float(np.median(curve_db[body_band]))
            high_level = float(np.median(curve_db[high_band]))
            minimum_relative_high = -0.35 * strength
            if high_level - body_level < minimum_relative_high:
                correction = minimum_relative_high - (high_level - body_level)
                body_weight = np.minimum(
                    np.clip((freqs - 100.0) / 150.0, 0.0, 1.0),
                    np.clip((4000.0 - freqs) / 2000.0, 0.0, 1.0),
                )
                curve_db -= correction * body_weight
                curve_db = np.clip(curve_db, -limit, limit)
                darkening_guard_db = float(correction)
        curves.append(curve_db)
        darkening_guard_dbs.append(darkening_guard_db)

    # Keep differential EQ small so reference matching cannot redraw the soundstage.
    curves[1] = curves[0] + np.clip(curves[1] - curves[0], -0.3 * strength, 0.3 * strength)
    curves[1] = np.clip(curves[1], -0.75 * strength, 0.75 * strength)
    curves[0] = curves[1] + np.clip(curves[0] - curves[1], -0.3 * strength, 0.3 * strength)
    outputs = []
    for index, (name, audio, curve) in enumerate(
            zip(("mid", "side"), (target_mid, target_side), curves)):
        # Odd symmetric FIR + centered convolution retains sample alignment.
        taps = signal.firwin2(config.fft_size + 1, freqs, 10.0 ** (curve / 20.0),
                              fs=analysis_rate, window="hann")
        outputs.append(signal.fftconvolve(audio, taps, mode="same"))
        config.last_mastering_stats[f"spectral_{name}_min_db"] = float(curve.min())
        config.last_mastering_stats[f"spectral_{name}_max_db"] = float(curve.max())
        if np.any(body_band) and np.any(high_band):
            config.last_mastering_stats[f"spectral_{name}_high_vs_body_db"] = float(
                np.median(curve[high_band]) - np.median(curve[body_band]))
        config.last_mastering_stats[f"spectral_{name}_darkening_guard_db"] = (
            darkening_guard_dbs[index])
    return tuple(outputs)


def gradual_level_correction(target_mid, target_side, reference_mid, reference_side, config):
    strength = float(np.clip(config.style_strength, 0.0, 1.0))
    target_rms = np.sqrt(np.mean(target_mid**2 + target_side**2))
    reference_rms = np.sqrt(np.mean(reference_mid**2 + reference_side**2))
    requested = 20.0 * np.log10(max(reference_rms, config.epsilon) /
                                max(target_rms, config.epsilon))
    gain_db = float(np.clip(requested * 0.1 * strength, -0.25 * strength, 0.25 * strength))
    config.last_mastering_stats["level_correction_db"] = gain_db
    gain = 10.0 ** (gain_db / 20.0)
    return target_mid * gain, target_side * gain

def rms(audio):
    return np.sqrt(np.mean(np.square(audio)))

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

def finalize_stereo_image(target_mid, target_side, reference_mid, reference_side, config):
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
        max_adjustment = 0.03 * config.style_strength
        config.last_mastering_stats["stereo_widening_db"] = 0.0
        
        if width_difference > 0:
            adjustment_factor = 1 + min(width_difference / initial_width, max_adjustment)
            adjusted_side = target_side * adjustment_factor
            config.last_mastering_stats["stereo_widening_db"] = float(20 * np.log10(adjustment_factor))
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

def multi_stage_limiter(audio, config):
     # First stage: existing implementation
    threshold1 = 10 ** (-0.6 / 20)  # -0.6 dB 
    knee_width1 = 0.1
    attack_time1 = 1.0  # ms
    release_time1 = 200.0  # ms 
    
    # Second stage: slightly more aggressive
    threshold2 = 10 ** (-0.5 / 20)  # -0.5 dB
    knee_width2 = 0.1
    attack_time2 = 3.0  # ms
    release_time2 = 900.0  # ms 
    
    sample_rate = config.internal_sample_rate * config.oversampling_factor
    
    # First stage (your existing implementation)
    result = process_limiter_stage(
        audio,
        threshold1,
        knee_width1,
        attack_time1,
        release_time1,
        sample_rate
    )
    
    # Second stage
    result = process_limiter_stage(
        result,
        threshold2,
        knee_width2,
        attack_time2,
        release_time2,
        sample_rate
    )
    
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
    # Ensure audio is in float32 format
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    
    # Ensure audio is in the range -1.0 to 1.0
    if audio.max() > 1.0 or audio.min() < -1.0:
        audio = audio / np.max(np.abs(audio))
    
    # Create BS.1770 meter
    meter = pyln.Meter(sr)
    
    # Ensure audio is in (samples, channels) shape
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    elif audio.shape[0] == 2 and audio.shape[1] > 2:
        audio = audio.T
    
    # Calculate integrated loudness
    loudness = meter.integrated_loudness(audio)
    return loudness

# ratio, attack ms, release ms, maximum gain reduction dB, saturation drive
DYNAMIC_PROFILES = {
    "Pop": (1.65, 18.0, 120.0, 2.5, 0.15),
    "Rock": (1.55, 30.0, 100.0, 2.2, 0.22),
    "EDM": (1.9, 12.0, 80.0, 3.0, 0.12),
    "Dance": (1.8, 15.0, 100.0, 2.8, 0.12),
    "Hiphop": (1.6, 28.0, 140.0, 2.5, 0.18),
    "Ambient": (1.2, 45.0, 300.0, 1.0, 0.04),
    "Chillout": (1.3, 35.0, 220.0, 1.5, 0.08),
    "Piano": (1.15, 45.0, 250.0, 0.8, 0.0),
    "Orchestral": (1.12, 55.0, 350.0, 0.7, 0.0),
    "Speech": (1.75, 10.0, 180.0, 3.0, 0.04),
    "Reference": (1.4, 25.0, 160.0, 2.0, 0.08),
}


@numba.jit(nopython=True)
def _smooth_style_reduction(required, attack_coeff, release_coeff):
    gain_db = np.empty_like(required)
    envelope = 0.0
    for index in range(required.size):
        coeff = attack_coeff if required[index] > envelope else release_coeff
        envelope = coeff * envelope + (1.0 - coeff) * required[index]
        gain_db[index] = envelope
    return gain_db


def apply_style_dynamics(audio, config, genre_profile):
    strength = config.style_strength
    genre = (genre_profile or {}).get("genre") or config.genre or "Reference"
    ratio, attack, release, budget, drive = DYNAMIC_PROFILES.get(genre, DYNAMIC_PROFILES["Reference"])
    ratio = 1.0 + (ratio - 1.0) * strength
    budget *= strength
    sr = config.internal_sample_rate * config.oversampling_factor
    detector = np.max(np.abs(audio), axis=0)
    rms_level = np.sqrt(np.mean(audio**2))
    threshold = 20.0 * np.log10(max(rms_level, config.epsilon)) + 3.0
    over = 20.0 * np.log10(np.maximum(detector, config.epsilon)) - threshold
    knee = 6.0
    shaped = np.where(over <= -knee / 2, 0.0,
                      np.where(over >= knee / 2, over, (over + knee / 2)**2 / (2 * knee)))
    required = np.clip(shaped * (1 - 1 / ratio), 0.0, budget)
    reduction = _smooth_style_reduction(required, np.exp(-1 / (sr * attack / 1000)),
                                        np.exp(-1 / (sr * release / 1000)))
    result = audio * 10.0 ** (-reduction[np.newaxis, :] / 20.0)
    # A shared instantaneous gain preserves the stereo image through saturation.
    saturation = drive * strength
    if saturation > 0:
        scale = 0.25 / max(rms_level, config.epsilon)
        peak = np.max(np.abs(result), axis=0)
        driven = peak * scale * saturation
        sat_gain = np.tanh(driven) / np.maximum(driven, config.epsilon)
        sat_gain = np.maximum(sat_gain, 10 ** (-0.35 * strength / 20))
        result *= sat_gain[np.newaxis, :]
    config.last_mastering_stats["dynamics"] = {
        "profile": genre, "ratio": float(ratio), "attack_ms": attack, "release_ms": release,
        "budget_db": float(budget), "max_gain_reduction_db": float(reduction.max()),
        "gain_reduction_p95_db": float(np.percentile(reduction, 95)),
        "saturation_drive": float(saturation),
    }
    return result


def process_audio(target, reference, step, config, genre_profile=None):
    """Apply bounded style processing; the core adapter owns final LUFS/true peak."""
    strength = float(config.style_strength)
    if not np.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("Style strength must be finite and between 0 and 1")
    audio = np.array(target, dtype=np.float64, copy=True)
    config.last_mastering_stats = {"style_strength": strength}
    if strength == 0:
        if step >= 2 and config.eq_style != "Neutral":
            mid, side = lr_to_ms(audio)
            mid, side = apply_eq_style(mid, side, config.internal_sample_rate, config.eq_style)
            audio = ms_to_lr(mid, side)
        return audio

    up = oversample(audio, config.oversampling_factor)
    reference_up = oversample(np.array(reference, dtype=np.float64, copy=True), config.oversampling_factor)
    rate = config.internal_sample_rate * config.oversampling_factor
    target_mid, target_side = lr_to_ms(up)
    reference_mid, reference_side = lr_to_ms(reference_up)
    source_ratio = np.sqrt(np.mean(target_side**2) / max(np.mean(target_mid**2), config.epsilon))
    ud = getattr(config, "upstream_delta", None)
    if ud:
        reference_mid *= float(ud["rms_mid"])
        reference_side *= float(ud["rms_side"])
    if step >= 1:
        target_mid, target_side = match_rms_ms(target_mid, target_side, reference_mid, reference_side, rate, config)
    if step >= 2:
        target_mid, target_side = match_frequencies_ms(target_mid, target_side, reference_mid, reference_side, config)
    if step >= 3:
        target_mid, target_side = gradual_level_correction(target_mid, target_side, reference_mid, reference_side, config)
    if step >= 4:
        result = finalize_stereo_image(target_mid, target_side, reference_mid, reference_side, config)
        target_mid, target_side = lr_to_ms(result)
    ratio = np.sqrt(np.mean(target_side**2) / max(np.mean(target_mid**2), config.epsilon))
    change_db = 20 * np.log10(max(ratio, config.epsilon) / max(source_ratio, config.epsilon))
    bounded = float(np.clip(change_db, -0.35 * strength, 0.35 * strength))
    target_side *= 10 ** ((bounded - change_db) / 20)
    config.last_mastering_stats["style_width_change_db"] = bounded
    # Apply EQ style after frequency matching: explicit EQ is independent of genre strength.
    if step >= 2 and config.eq_style != "Neutral":
        target_mid, target_side = apply_eq_style(target_mid, target_side, rate, config.eq_style)
    result = ms_to_lr(target_mid, target_side)
    if step >= 2 and getattr(config, "explicit_lowpass", False):
        result = apply_lowpass_filter(result, config)
    if step >= 5:
        result = apply_style_dynamics(result, config, genre_profile)
    return signal.resample_poly(result, 1, config.oversampling_factor, axis=-1)

def calculate_true_peak(audio, sample_rate):
    # Upsample by a factor of 4 for true peak calculation
    upsampled = signal.resample_poly(audio, 4, 1, axis=-1)
    peak = np.max(np.abs(upsampled))
    true_peak_db = 20 * np.log10(peak)
    return true_peak_db

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
    # RBJ low-shelf (same topology as soren_core): attenuates content below
    # cutoff_freq by `gain` while leaving highs at unity. The previous
    # audio*gain + lowpass*(1-gain) mix actually cut the highs instead.
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

def high_shelf_eq(audio, sample_rate, cutoff_freq, gain_db):
    # RBJ high shelf: unity below cutoff_freq, +gain_db above (minimum phase;
    # the butter-mix shelf phase-cancels near cutoff and must not be used).
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * cutoff_freq / sample_rate
    alpha = np.sin(w0) / np.sqrt(2.0)
    cos_w0 = np.cos(w0)
    beta = 2 * np.sqrt(A) * alpha

    b0 = A * ((A + 1) + (A - 1) * cos_w0 + beta)
    b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
    b2 = A * ((A + 1) + (A - 1) * cos_w0 - beta)
    a0 = (A + 1) - (A - 1) * cos_w0 + beta
    a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
    a2 = (A + 1) - (A - 1) * cos_w0 - beta

    sos = np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])
    return signal.sosfilt(sos, audio)

def presence_eq(audio, sample_rate, center_freq, gain_db, q=0.9):
    # RBJ peaking bell, +gain_db at center_freq (unity DC/Nyquist gain).
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * center_freq / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * A
    b1 = -2 * cos_w0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cos_w0
    a2 = 1 - alpha / A

    sos = np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])
    return signal.sosfilt(sos, audio)

def apply_eq_style(mid, side, sample_rate, eq_style):
    """Shared minimum-phase EQ curves for styled and EQ-only mastering."""
    print(f"Applying {eq_style} EQ style")
    if eq_style == "Neutral":
        return mid, side
    if eq_style == "Warm":
        # Mid channel: light low warmth, keep presence and air intact.
        mid = low_shelf_tighten(mid, sample_rate, cutoff_freq=150, gain=10 ** (1.5 / 20))  # +1.5 dB shelf below 150 Hz

    elif eq_style == "Bright":
        # Mid channel: moderate presence (not harsh) + air shelf.
        mid = presence_eq(mid, sample_rate, center_freq=3200, gain_db=1.0)  # +1.0 dB presence bump
        mid = high_shelf_eq(mid, sample_rate, cutoff_freq=9000, gain_db=1.5)  # +1.5 dB air shelf

        # Side channel: slight low-mid cleanup + matching air shelf.
        side = presence_eq(side, sample_rate, center_freq=250, gain_db=-0.7, q=1.0)  # -0.7 dB low-mid dip
        side = high_shelf_eq(side, sample_rate, cutoff_freq=8000, gain_db=1.5)  # +1.5 dB air shelf

    elif eq_style == "Fusion":
        # Mid channel: mild warm tilt + mild air tilt, no presence emphasis.
        mid = low_shelf_tighten(mid, sample_rate, cutoff_freq=150, gain=10 ** (0.8 / 20))  # +0.8 dB shelf below 150 Hz
        mid = high_shelf_eq(mid, sample_rate, cutoff_freq=9000, gain_db=0.8)  # +0.8 dB air shelf

        # Side channel: slight low-mid cleanup + mild air shelf.
        side = presence_eq(side, sample_rate, center_freq=250, gain_db=-0.26, q=1.0)  # -0.26 dB low-mid dip
        side = high_shelf_eq(side, sample_rate, cutoff_freq=8000, gain_db=0.8)  # +0.8 dB air shelf

    print(f"After EQ - Mid max: {np.max(np.abs(mid)):.4f}, Side max: {np.max(np.abs(side)):.4f}")
    return mid, side

def master_audio(input_file, output_file, config, eq_style, is_preview=False):
    # Standalone callers must use the same finalizer as the application adapter.
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent
    source = root / "soren_core.py"
    if not source.is_file():
        source = root / "core_decrypted.py"
    spec = importlib.util.spec_from_file_location("soren_original_finalizer", source)
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    final_config = core.Config()
    for name in ("genre", "reference_file", "loudness_option", "eq_style"):
        setattr(final_config, name, getattr(config, name))
    final_config.style_blend = config.style_strength
    core.master_audio(input_file, output_file, final_config, eq_style, is_preview)
    config.last_mastering_stats = final_config.last_mastering_stats

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audio Mastering Tool")
    parser.add_argument("input_file", help="Path to the input audio file")
    parser.add_argument("output_file", help="Path to the output audio file")
    parser.add_argument("--reference", help="Path to a custom reference audio file")
    parser.add_argument("--genre", help="Genre profile to use for mastering")
    parser.add_argument("--loudness", choices=['soft', 'dynamic', 'normal', 'loud'], default="normal", help="Loudness option")
    parser.add_argument("--eq-profile", choices=["Neutral", "Warm", "Bright", "Fusion"], default="Neutral", help="EQ profile to use for mastering")
    parser.add_argument("--preview", action="store_true", help="Process only the preview")
    args = parser.parse_args()
    
    config = Config()
    config.loudness_option = args.loudness
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