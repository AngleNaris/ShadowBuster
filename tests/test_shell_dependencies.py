import ast
from pathlib import Path


def test_shell_bundles_audio_metadata_dependency():
    root=Path(__file__).resolve().parents[1]
    tree=ast.parse((root/'ShadowBuster.spec').read_text(encoding='utf-8'))
    analysis=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='Analysis')
    args={k.arg:ast.literal_eval(k.value) for k in analysis.keywords if k.arg in ('hiddenimports','excludes')}
    assert 'soundfile' in args['hiddenimports']
    assert 'soundfile' not in args['excludes']
