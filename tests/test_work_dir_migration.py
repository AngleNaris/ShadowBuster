"""工作目录命名迁移与清理回归（v1.6.5 → 新命名）。"""
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

import studio_backend as backend


def _write_wav(path: Path, seconds=0.25, sr=44_100):
    t = np.arange(int(sr * seconds)) / sr
    data = np.column_stack((0.2 * np.sin(2 * np.pi * 220 * t),
                            0.2 * np.sin(2 * np.pi * 221 * t)))
    sf.write(path, data.astype(np.float32), sr, subtype="FLOAT")
    return path


def test_explicit_work_dir_wins(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    explicit = tmp_path / "custom_work"
    assert backend._resolve_work_dir(out, explicit) == explicit
    assert backend._resolve_work_dir(out, str(explicit)) == Path(str(explicit))


def test_new_dir_name_and_legacy_adoption(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    legacy = out / backend.LEGACY_WORK_DIR_NAME
    legacy.mkdir()
    (legacy / "marker.txt").write_text("x", encoding="utf-8")

    work = backend._resolve_work_dir(out, None)
    assert work == out / backend.WORK_DIR_NAME
    assert not legacy.exists()
    assert (work / "marker.txt").read_text(encoding="utf-8") == "x"


def test_legacy_locked_falls_back(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    legacy = out / backend.LEGACY_WORK_DIR_NAME
    legacy.mkdir()

    def blocked(self, target):
        raise OSError("locked")

    monkeypatch.setattr(Path, "rename", blocked)
    work = backend._resolve_work_dir(out, None)
    assert work == legacy


def test_both_dirs_present_prefers_new(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / backend.LEGACY_WORK_DIR_NAME).mkdir()
    (out / backend.WORK_DIR_NAME).mkdir()
    work = backend._resolve_work_dir(out, None)
    assert work == out / backend.WORK_DIR_NAME


def test_pipeline_uses_new_work_dir(tmp_path):
    """全跳过链路端到端：工作目录用新名，运行结束被清理，旧名不出现。"""
    out = tmp_path / "out"
    out.mkdir()
    src = _write_wav(tmp_path / "in.wav")

    def passthrough(in_wav, out_wav, **kwargs):
        Path(out_wav).write_bytes(Path(in_wav).read_bytes())

    def fake_demucs(in_wav, out_root, **kwargs):
        stem_dir = Path(out_root) / "htdemucs" / Path(in_wav).stem
        stem_dir.mkdir(parents=True)
        for name in ("bass", "drums", "other", "vocals"):
            _write_wav(stem_dir / f"{name}.wav")

    def fake_reshape(in_wav, stems_dir, out_wav, **kwargs):
        Path(out_wav).write_bytes(Path(in_wav).read_bytes())
        return 1.0

    patches = (
        mock.patch.object(backend, "stage_demucs", fake_demucs),
        mock.patch.object(backend, "stage_reshape", fake_reshape),
    )
    with patches[0], patches[1]:
        out_final = backend.run_pipeline(
            src, out, bypass=("lew", "vocals", "bass", "drums", "reshape", "soren"))

    assert Path(out_final).exists()
    assert Path(out_final).name == "in_shadowbuster.wav"
    assert not (out / backend.WORK_DIR_NAME).exists()
    assert not (out / backend.LEGACY_WORK_DIR_NAME).exists()
