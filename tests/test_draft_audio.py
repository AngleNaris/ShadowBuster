import shutil
import subprocess
from pathlib import Path
import pytest


def test_draft_dsp_audio_contracts():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    subprocess.run([node,str(Path(__file__).with_name('draft_audio.cjs'))],check=True,capture_output=True,text=True)
    subprocess.run([node,str(Path(__file__).with_name('draft_player.cjs'))],check=True,capture_output=True,text=True)
