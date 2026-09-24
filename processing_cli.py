"""Headless processing entry shared by source and packaged applications."""
import argparse
import contextlib
import io
import json
import math
import sys
from pathlib import Path


def bounded(lo, hi):
    def parse(value):
        number = float(value)
        if not math.isfinite(number) or not lo <= number <= hi:
            raise argparse.ArgumentTypeError(f"expected {lo}..{hi}")
        return number
    return parse


def main(argv=None):
    import studio_backend as backend
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="ShadowBuster headless audio processing")
    parser.add_argument('--cli', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--version', action='version', version=backend.APP_VERSION)
    parser.add_argument('-i', '--input', action='append', required=True)
    parser.add_argument('-o', '--output', required=True, help='Output directory')
    parser.add_argument('--result-json', type=Path, help='New JSON result file (recommended for windowed EXE)')
    parser.add_argument('--overwrite', action='store_true')
    # 处理默认值来自 backend.DEFAULTS 单一来源（ENG-01）：CLI 与 GUI/后端
    # 解析结果一致，改动默认值只动一处。
    d = backend.DEFAULTS
    for flag, low, high, default in [('sub-db',0,12,d['sub_db']),
                                      ('punch-db',0,10,d['punch_db']),
                                      ('sat',0,1,d['sat']),
                                      ('trans',0,1,d['trans']),
                                      ('space-wet',0,1,d['space_wet']),
                                      ('space-denoise',0,1,d['space_denoise']),
                                      ('space-width-db',0,12,d['space_width_db']),
                                      ('vocal-gain-db',-6,6,d['vocal_gain_db']),
                                      ('guidance',0,2,d['guidance'])]:
        parser.add_argument('--'+flag, type=bounded(low, high), default=default)
    parser.add_argument('--vocal-comp-amount', type=bounded(0, 1), default=0.0,
                        help='bounded broadband vocal compression 0-1 (0=off, exact)')
    parser.add_argument('--vocal-air-db', type=bounded(0, 3), default=None,
                        help='boost-only vocal air high-shelf dB (0-3); omit to disable')
    parser.add_argument('--bass-auto-clarity', action='store_true')
    parser.add_argument('--sidechain-amount', type=bounded(0, 1), default=0.0,
                        help='Kick/Bass 低频侧链强度，默认关闭')
    parser.add_argument('--sidechain-attack-ms', type=bounded(1, 50), default=5.0)
    parser.add_argument('--sidechain-release-ms', type=bounded(20, 500), default=150.0)
    parser.add_argument('--sidechain-max-duck-db', type=bounded(0, 12), default=6.0)
    parser.add_argument('--clarity-auth', type=bounded(0, 1), default=None,
                        help='auto-clarity authorization 0-1 (low-end knobs derived, '
                             'spec 6.3/6.4); omit to let the DSP derive from '
                             'sub/punch/trans/sat; 0 forces gains (0,0)')
    parser.add_argument('--space-amount', type=bounded(0, 1), default=None,
                        help='space intensity authorization 0-1 (spec 7.4): scales '
                             'width delta and authorizes bounded 200-700Hz other-stem '
                             'de-crowding (<=1.5*a dB, evidence-gated); defaults to '
                             '--space-wet when omitted')
    parser.add_argument('--noise-mode', choices=['other', 'adaptive_all'], default='other',
                        help='Stage3 opt-in noise denoise mode: other=legacy other-stem '
                             'denoise (default), adaptive_all=confidence-gated adaptive '
                             'high-band denoise on available stems')
    parser.add_argument('--noise-low-hz', type=bounded(8000, 20000), default=8000.0,
                        help='adaptive_all noise band lower edge (Hz)')
    parser.add_argument('--noise-high-hz', type=bounded(8000, 22000), default=20000.0,
                        help='adaptive_all noise band upper edge (Hz)')
    parser.add_argument('--noise-max-attenuation-db', type=bounded(0, 6), default=6.0,
                        help='adaptive_all maximum attenuation cap (dB)')
    parser.add_argument('--demucs-model', choices=['htdemucs', 'htdemucs_6s'], default='htdemucs_6s',
                        help='Separation model: htdemucs=legacy 4-stem ('
                             'compatibility); htdemucs_6s=default six-stem that also outputs '
                             'guitar/piano, routes a guitar enhancer and a synth group '
                             '(other+piano merged) enhancer')
    for prefix in ('guitar', 'synth'):
        parser.add_argument(f'--{prefix}-gain-db', type=bounded(-6, 6), default=0.0,
                            help=f'opt-in six-stem {prefix} gain (dB), 0=neutral')
        parser.add_argument(f'--{prefix}-mud-cut-db', type=bounded(0, 6), default=0.0,
                            help=f'opt-in six-stem {prefix} low-mid mud cut (dB), 0=neutral')
        parser.add_argument(f'--{prefix}-presence-db', type=bounded(0, 6), default=0.0,
                            help=f'opt-in six-stem {prefix} presence shelf (dB), 0=neutral')
        parser.add_argument(f'--{prefix}-harsh-cut-db', type=bounded(0, 6), default=0.0,
                            help=f'opt-in six-stem {prefix} high harshness shelf cut (dB), 0=neutral')
        parser.add_argument(f'--{prefix}-width-db', type=bounded(0, 6), default=0.0,
                            help=f'opt-in six-stem {prefix} side width (dB), 0=neutral')
    parser.add_argument('--quality', type=int, choices=[0,1,2], default=backend.DEFAULTS['quality'])
    parser.add_argument('--genre', default=backend.DEFAULTS['genre'])
    parser.add_argument('--loudness', choices=['soft','dynamic','normal','loud'],
                        default=backend.DEFAULTS['loudness'])
    parser.add_argument('--eq-profile', default=backend.DEFAULTS['eq_profile'],
                        help='global user EQ, active with or without reference/style matching')
    parser.add_argument('--style-mode', choices=['styled','off','eq_only'], default='off')
    parser.add_argument('--style-blend', type=bounded(0,1), default=backend.DEFAULTS['style_blend'],
                        help='styled processing intensity 0-1: 0=no style processing, '
                             '1=full style; ignored by off/eq_only; 100%% is no longer '
                             'bit-identical to the legacy release')
    parser.add_argument('--reference')
    parser.add_argument('--lowpass-cutoff', type=bounded(20,22000))
    parser.add_argument('--hygiene-low-cut-hz', type=bounded(0, 500),
                        default=backend.DEFAULTS['hygiene_low_cut_hz'],
                        help='母带前低切（Hz，2 阶巴特沃斯高通；0=关闭）：收掉 <40Hz '
                             '次低频隆隆声，把限制器余量还给可听频段')
    parser.add_argument('--hygiene-lowpass-hz', type=bounded(0, 22050),
                        default=backend.DEFAULTS['hygiene_lowpass_hz'],
                        help='母带前低通（Hz，线性相位 FIR；0=关闭）：摘掉 20kHz 以上'
                             '上游生成的合成高频')
    parser.add_argument('--bypass', default='')
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--no-cache', action='store_true', help='Do not read or write processing cache for this run')
    with contextlib.ExitStack() as stack:
        if sys.stdout is None:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        if sys.stderr is None:
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        opts = parser.parse_args(args)
        if opts.reference and not any(a == '--style-mode' or a.startswith('--style-mode=') for a in args):
            opts.style_mode = 'styled'
        report = {'version': backend.APP_VERSION, 'status': 'error', 'outputs': [], 'error': None}
        code = 1
        try:
            # 自适应降噪为 opt-in：显式给的无效频带在任何处理开始前拒绝。
            if opts.noise_mode == 'adaptive_all' and \
                    not opts.noise_low_hz < opts.noise_high_hz:
                raise ValueError('adaptive_all requires --noise-low-hz < --noise-high-hz '
                                 f'(got {opts.noise_low_hz:g} >= {opts.noise_high_hz:g})')
            if opts.result_json and opts.result_json.exists():
                raise ValueError('Result JSON already exists; choose a new path')
            inputs = [Path(p).resolve() for p in opts.input]
            targets = [Path(opts.output).resolve() / (p.stem + '_shadowbuster.wav') for p in inputs]
            if len(set(str(p).casefold() for p in targets)) != len(targets):
                raise ValueError('Inputs would produce duplicate output names')
            if any(not p.is_file() for p in inputs):
                raise ValueError('An input file does not exist')
            if any(p in inputs for p in targets):
                raise ValueError('Output must not replace an input file')
            if not opts.overwrite and any(p.exists() for p in targets):
                raise ValueError('Output already exists; use --overwrite explicitly')
            if opts.result_json and opts.result_json.resolve() in inputs + targets:
                raise ValueError('Result JSON must be separate from audio paths')
            kwargs = vars(opts).copy()
            for key in ('cli','input','output','result_json','overwrite','cpu'):
                kwargs.pop(key)
            kwargs['bypass'] = [p.strip() for p in opts.bypass.split(',') if p.strip()]
            kwargs['cache_enabled'] = not kwargs.pop('no_cache')
            kwargs['device'] = 'cpu' if opts.cpu else backend.auto_device()
            # --space-amount 缺省 = --space-wet（声场强度授权与宽度混合比例
            # 同源，规格 §7.2/§7.4；显式传参可解耦）。
            if kwargs.get('space_amount') is None:
                kwargs['space_amount'] = opts.space_wet
            # 输出目录与输入一致地解析为绝对路径：阶段子进程（Lew 等）的
            # 工作目录不在应用根，相对路径会在子进程里指错位置。
            results = backend.run_batch(inputs, str(Path(opts.output).resolve()), **kwargs)
            report['outputs'] = []
            for i, o, e in results:
                item = {'input': i, 'output': o, 'error': e}
                if e is None:
                    quality = backend.read_quality_report(o)
                    item['quality_report'] = str(backend.quality_report_path(o)) if quality else None
                    item['quality'] = backend.quality_summary(quality)
                report['outputs'].append(item)
            code = 1 if any(e for _,_,e in results) else 0
            report['status'] = 'error' if code else 'success'
        except Exception as exc:
            report['error'] = str(exc)
        report['exit_code'] = code
        if opts.result_json and not opts.result_json.exists():
            try:
                opts.result_json.parent.mkdir(parents=True, exist_ok=True)
                with opts.result_json.open('x', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
            except OSError as exc:
                print(f'Cannot write result JSON: {exc}', file=sys.stderr)
                return 1
        print(json.dumps(report, ensure_ascii=False))
        return code


if __name__ == '__main__':
    raise SystemExit(main())
