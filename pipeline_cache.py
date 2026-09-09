"""Bounded, content-addressed processing artifacts shared by GUI and CLI."""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time


def root():
    return Path(os.environ.get('SB_PROCESSING_CACHE_DIR', str(Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'ShadowBuster' / 'processing-cache')))


def md5(path):
    h = hashlib.md5()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path):
    p = Path(path)
    if p.is_file():
        return md5(p)
    if p.is_dir():
        return [[str(f.relative_to(p)), md5(f)] for f in sorted(p.rglob('*')) if f.is_file()]
    return None


@contextlib.contextmanager
def locked():
    r = root(); r.mkdir(parents=True, exist_ok=True)
    with (r / '.lock').open('a+b') as handle:
        handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('缓存正被其他任务使用，请稍后重试')
        try:
            yield r
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def get_settings():
    try:
        value = float(json.loads((root() / 'settings.json').read_text())['capacity_gb'])
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        value = 5.0
    return {'capacity_gb': value, 'enabled': value > 0}


def entries():
    r = root()
    return [p for p in r.glob('entry-*') if p.is_dir() and not p.is_symlink() and (p / 'manifest.json').is_file()]


def size(p):
    return sum(f.stat().st_size for f in p.rglob('*') if f.is_file())


def evict():
    items = sorted(entries(), key=lambda p: p.stat().st_mtime)
    total = sum(size(p) for p in items)
    limit = get_settings()['capacity_gb'] * 1024**3
    for p in items:
        if total <= limit:
            break
        total -= size(p); shutil.rmtree(p)


def get_info():
    with locked():
        items = entries()
        return {**get_settings(), 'used_bytes': sum(size(p) for p in items), 'entries': len(items)}


def set_capacity_gb(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('缓存容量必须为 0–100 GiB')
    with locked() as r:
        temp = r / 'settings.tmp'
        temp.write_text(json.dumps({'capacity_gb': value}), encoding='utf-8')
        os.replace(temp, r / 'settings.json'); evict()
    return get_info()


def clear_cache():
    with locked():
        for p in entries():
            shutil.rmtree(p)
    return get_info()


class StageCache:
    def __init__(self, enabled=True, identity=None):
        self.enabled = enabled and get_settings()['enabled'] and os.environ.get('SB_CACHE_DISABLE') != '1'
        self.identity = identity

    def run(self, stage, inputs, params, outputs, compute):
        if not self.enabled:
            return compute()
        try:
            signature = {'stage': stage, 'inputs': [fingerprint(p) for p in inputs], 'params': params, 'identity': self.identity}
            key = hashlib.sha256(json.dumps(signature, sort_keys=True, default=str).encode()).hexdigest()
            with locked() as r:
                entry = r / ('entry-' + key)
                if entry.is_dir():
                    try:
                        manifest = json.loads((entry / 'manifest.json').read_text())
                        artifacts = [entry / str(i) for i in range(len(outputs))]
                        result = manifest['result']
                        if result is not None and (not isinstance(result, (int, float)) or not math.isfinite(result) or not 0 < result <= 1):
                            raise ValueError('Invalid cached scale')
                        if manifest['hashes'] == [fingerprint(p) for p in artifacts] and all(p.exists() for p in artifacts):
                            for src, dst in zip(artifacts, outputs):
                                self.copy(src, Path(dst))
                            os.utime(entry, None)
                            print(f'缓存命中：{stage}')
                            return manifest['result']
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
        except (OSError, RuntimeError, ValueError):
            return compute()
        result = compute()
        try:
            if not all(Path(p).exists() for p in outputs):
                return result
            with locked() as r:
                if entry.exists():
                    shutil.rmtree(entry)
                temp = Path(tempfile.mkdtemp(prefix='pending-', dir=r))
                try:
                    for i, p in enumerate(outputs):
                        self.copy(Path(p), temp / str(i))
                    manifest = {'hashes': [fingerprint(temp / str(i)) for i in range(len(outputs))], 'result': result}
                    (temp / 'manifest.json').write_text(json.dumps(manifest, allow_nan=False), encoding='utf-8')
                    os.replace(temp, entry); evict()
                finally:
                    if temp.exists():
                        shutil.rmtree(temp)
        except (OSError, RuntimeError, ValueError, TypeError):
            pass
        return result

    @staticmethod
    def copy(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copyfile(src, dst)
