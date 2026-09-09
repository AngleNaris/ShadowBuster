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
    for flag, low, high, default in [('sub-db',0,12,6),('punch-db',0,10,2),('sat',0,1,.3),('trans',0,1,.3),('space-wet',0,1,.6),('space-denoise',0,1,.2),('space-width-db',0,12,6),('vocal-gain-db',-6,6,0),('guidance',0,2,1.5)]:
        parser.add_argument('--'+flag, type=bounded(low, high), default=default)
    parser.add_argument('--bass-auto-clarity', action='store_true')
    parser.add_argument('--quality', type=int, choices=[0,1,2], default=1)
    parser.add_argument('--genre', default='Pop')
    parser.add_argument('--loudness', choices=['soft','dynamic','normal','loud'], default='normal')
    parser.add_argument('--eq-profile', default='Neutral')
    parser.add_argument('--style-mode', choices=['styled','off','eq_only'], default='off')
    parser.add_argument('--reference')
    parser.add_argument('--lowpass-cutoff', type=bounded(20,22000))
    parser.add_argument('--bypass', default='')
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--no-cache', action='store_true', help='Do not read or write processing cache for this run')
    with contextlib.ExitStack() as stack:
        if sys.stdout is None:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        if sys.stderr is None:
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        opts = parser.parse_args(args)
        report = {'version': backend.APP_VERSION, 'status': 'error', 'outputs': [], 'error': None}
        code = 1
        try:
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
            kwargs['device'] = 'cpu' if opts.cpu else 'cuda'
            results = backend.run_batch(inputs, opts.output, **kwargs)
            report['outputs'] = [{'input': i, 'output': o, 'error': e} for i,o,e in results]
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
