"""Apply the additive style-off patch to hash-pinned runtime baselines."""
from pathlib import Path
import hashlib
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT/'experiments/results/soren_style_20260906_192550_399848'
p = ROOT/'packaging/stage/runtime/Soren_src/core_decrypted.py'
s = p.read_text(encoding='utf-8')
assert hashlib.sha256(p.read_bytes()).hexdigest() == '3104340f58306080495750de36605af69fe258ae3a2037a9bd224a39b202af74'
module = (ROOT/'apollo_scripts/soren_transparent.py').read_text(encoding='utf-8')
fn = module[module.index('def process_transparent'):]
s = s.replace('def process_audio(target, reference, step, config, genre_profile=None):', fn + '\n\ndef process_audio(target, reference, step, config, genre_profile=None):\n    if config.style_mode == "off":\n        return process_transparent(target, config, loudness_target_lufs(genre_profile, config.loudness_option), sys.modules[__name__])\n    if config.style_mode != "styled":\n        raise ValueError("Unknown style mode")')
s = s.replace('self.eq_style = "Neutral"', 'self.style_mode = "styled"\n        self.eq_style = "Neutral"')
s = s.replace('    if config.genre:\n        print(f"Using genre profile:', '    if config.style_mode == "off":\n        genre_profile = load_genre_profile(config.genre or "Pop")\n        reference = None\n    elif config.genre:\n        print(f"Using genre profile:')
s = s.replace('    print("After Final Processing:")', '    config.last_mastering_stats = {"style_mode": "styled", "target_lufs": requested_lufs, "actual_lufs": float(final_lufs), "true_peak_dbtp": float(dithered_true_peak_db), "limiter": limiter_stats if step >= 5 else None, "pre_limiter": pre_limiter_stats if step >= 5 else None, "spectral_processing": step >= 2}\n\n    print("After Final Processing:")')
s = s.replace('    start_time = time.time()\n    print(f"Master audio function', '    if os.path.realpath(input_file) == os.path.realpath(output_file):\n        raise ValueError("Input and output must be different files")\n    start_time = time.time()\n    print(f"Master audio function')
s = s.replace('    save_end = time.time()', '    config.last_mastering_stats.update(actual_lufs=float(calculate_lufs(written_audio.T, sr)), true_peak_dbtp=float(written_true_peak_db))\n    with open(output_file + ".mastering.json", "w", encoding="utf-8") as handle:\n        json.dump(config.last_mastering_stats, handle, indent=2, allow_nan=False)\n    save_end = time.time()')
s = s.replace('    args = parser.parse_args()', '    parser.add_argument("--style-mode", choices=["styled", "off"], default="styled")\n    args = parser.parse_args()')
s = s.replace('    config = Config()\n', '    config = Config()\n    config.style_mode = args.style_mode\n')
for dst in [p, ROOT/'packaging/stage/runtime_gpu/Soren_src/core_decrypted.py']:
    assert hashlib.sha256(dst.read_bytes()).hexdigest() == '3104340f58306080495750de36605af69fe258ae3a2037a9bd224a39b202af74'
    dst.write_text(s, encoding='utf-8')
(BASE/'core_style_off.patch').write_text(__import__('difflib').unified_diff if False else ''.join(__import__('difflib').unified_diff((BASE/'core_before_1.py').read_text(encoding='utf-8').splitlines(True),s.splitlines(True),fromfile='before/core_decrypted.py',tofile='after/core_decrypted.py')),encoding='utf-8')
