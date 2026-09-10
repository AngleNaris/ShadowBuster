#!/usr/bin/env bash
# ShadowBuster — macOS 版 runtime_sync（对应 Windows 的 runtime_sync.ps1，Apple Silicon 专用）。
#
# 两种模式：
#   ./packaging/runtime_sync_macos.sh
#       开发装配（默认）：校验 SB_* 组件目录与权重，并把
#       packaging/soren_core.py  → $SB_SOREN/core_decrypted.py
#       packaging/soren_original.py → $SB_SOREN/soren_original.py
#       复制到位（Windows 上由 runtime_sync.ps1 完成，tests/test_soren_original.py
#       等依赖该布局）。
#   ./packaging/runtime_sync_macos.sh --runtime [输出目录]
#       装配打包用 runtime/ 树（对应 ps1 的 cpu flavor）：
#           runtime/env/bin/python        推理解释器（uv venv + 离线 wheels）
#           runtime/Apollo                lew + look2hear + ckpts + DSP 脚本
#           runtime/Soren_src             core_decrypted + soren_original + model/profiles
#           runtime/ffmpeg/bin/ffmpeg
#           runtime/torch_home|hf_home    预置 htdemucs 权重（首次需联网）
#           runtime/critical-manifest.sha256
#       产物供 build_macos.sh 打进 .app（Contents/MacOS/runtime）。
set -euo pipefail

# uv 常装在 ~/.local/bin（curl 安装脚本默认位置），非交互 shell 未必在 PATH 里
export PATH="$HOME/.local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
BUNDLE_DIR="$(dirname "$REPO_ROOT")"

log()  { printf '[runtime_sync] %s\n' "$*"; }
fail() { printf '[runtime_sync] ✗ %s\n' "$*" >&2; exit 1; }

assert_nonempty_file() {
    [[ -s "$1" ]] || fail "$2 不存在或为空: $1"
}
assert_tree_has_nonempty_file() {
    [[ -d "$1" ]] || fail "$2 目录不存在: $1"
    find "$1" -type f -size +0c -print -quit | grep -q . || fail "$2 中没有非空文件: $1"
}

SB_PYTHON="${SB_PYTHON:-$REPO_ROOT/.venv-infer/bin/python}"
SB_APOLLO="${SB_APOLLO:-$BUNDLE_DIR/component_Apollo}"
SB_SOREN="${SB_SOREN:-$BUNDLE_DIR/component_Soren_src}"
APP_APOLLO="$REPO_ROOT/apollo_scripts"
FFMPEG="${SB_FFMPEG:-$(command -v ffmpeg || true)}"

# ─── 公共校验（与 ps1 的 Assert-* 一致） ─────────────────────────────
assert_nonempty_file "$SB_PYTHON" "推理解释器 SB_PYTHON"
assert_nonempty_file "$SB_APOLLO/lew_upscale.py" "Lew 入口"
for dsp in bass_enhance.py drum_enhance.py soundstage_reshape.py vocal_adjust.py; do
    assert_nonempty_file "$APP_APOLLO/$dsp" "DSP 入口 $dsp"
done
assert_tree_has_nonempty_file "$SB_APOLLO/look2hear" "look2hear 源码"
assert_tree_has_nonempty_file "$SB_APOLLO/ckpts" "Apollo checkpoint"
assert_tree_has_nonempty_file "$SB_SOREN/model" "Soren 模型"
assert_tree_has_nonempty_file "$SB_SOREN/profiles" "Soren profiles"
assert_tree_has_nonempty_file "$SB_SOREN/secured_genres" "Soren secured genres"
assert_nonempty_file "$REPO_ROOT/packaging/soren_core.py" "Soren 入口源 packaging/soren_core.py"
assert_nonempty_file "$REPO_ROOT/packaging/soren_original.py" "Soren original 源 packaging/soren_original.py"
[[ -n "$FFMPEG" && -x "$FFMPEG" ]] || fail "未找到 ffmpeg（brew install ffmpeg 或设置 SB_FFMPEG）"

stage_soren() {
    log "落位 Soren 装配件 → $SB_SOREN"
    cp "$REPO_ROOT/packaging/soren_core.py" "$SB_SOREN/core_decrypted.py"
    cp "$REPO_ROOT/packaging/soren_original.py" "$SB_SOREN/soren_original.py"
    assert_nonempty_file "$SB_SOREN/core_decrypted.py" "Soren core_decrypted.py"
    assert_nonempty_file "$SB_SOREN/soren_original.py" "Soren soren_original.py"
}

if [[ "${1:-}" != "--runtime" ]]; then
    # ─── 开发装配：校验 + 落位 Soren 装配件 + 最小 stage（供 pytest） ──
    stage_soren
    # tests/test_soren_original.py 等 4 个测试直接读 packaging/stage/runtime/
    # 下的 Soren 副本（core_decrypted 模块级 import test_model 会加载
    # joblib 模型，必须带 model/profiles/secured_genres）。
    # runtime 与 runtime_gpu 两个 flavor 必须同源（发布一致性测试校验）；
    # macOS 无 CUDA flavor，两者内容天然一致。
    STAGE_SOREN="$REPO_ROOT/packaging/stage/runtime/Soren_src"
    STAGE_GPU_SOREN="$REPO_ROOT/packaging/stage/runtime_gpu/Soren_src"
    log "同步最小测试 stage → ${STAGE_SOREN}（+ runtime_gpu 镜像）"
    mkdir -p "$STAGE_SOREN" "$STAGE_GPU_SOREN"
    cp "$SB_SOREN/core_decrypted.py" "$SB_SOREN/soren_original.py" "$STAGE_SOREN/"
    cp "$SB_SOREN/test_model.py" "$STAGE_SOREN/"
    for d in model profiles secured_genres; do
        rm -rf "$STAGE_SOREN/$d"
        cp -R "$SB_SOREN/$d" "$STAGE_SOREN/$d"
        rm -rf "$STAGE_GPU_SOREN/$d"
        cp -R "$SB_SOREN/$d" "$STAGE_GPU_SOREN/$d"
    done
    cp "$STAGE_SOREN/core_decrypted.py" "$STAGE_SOREN/soren_original.py" \
       "$STAGE_SOREN/test_model.py" "$STAGE_GPU_SOREN/"

    # v1.6.7 起：开发态 canonical Soren runtime（tools/make_dev_runtime.py），
    # 代码取自 packaging/、资源经 SB_SOREN；执行 Soren 前后端会自动校验/重建，
    # 这里提前生成一次让错误尽早暴露
    log "生成 canonical dev Soren runtime（dev_runtime/Soren_src）"
    (cd "$REPO_ROOT" && SB_SOREN="$SB_SOREN" "$SB_PYTHON" tools/make_dev_runtime.py) \
        || fail "canonical dev runtime 生成失败"
    # Lew 入口同步为 mac 权威副本（MPS/fp16），保证开发态与打包态一致
    if [[ -f "$REPO_ROOT/packaging/macos/lew_upscale.py" ]]; then
        cp "$REPO_ROOT/packaging/macos/lew_upscale.py" "$SB_APOLLO/lew_upscale.py"
    fi
    log "开发装配完成（device 探测请运行: $SB_PYTHON -c 'import torch;print(torch.backends.mps.is_available())'）"
    exit 0
fi

# ─── 打包装配：--runtime [输出目录] ─────────────────────────────────
STAGE="${2:-$REPO_ROOT/packaging/stage/runtime}"
log "装配打包 runtime → $STAGE"
# 保留已预置的 torch/HF 权重缓存，重装配时不重复下载（约 300MB）
WEIGHTS_KEEP="$(mktemp -d)"
if [[ -d "$STAGE/torch_home" ]]; then mv "$STAGE/torch_home" "$WEIGHTS_KEEP/"; fi
if [[ -d "$STAGE/hf_home" ]]; then mv "$STAGE/hf_home" "$WEIGHTS_KEEP/"; fi
if [[ -e "$STAGE" ]]; then rm -rf "$STAGE"; fi
mkdir -p "$STAGE/Apollo" "$STAGE/Soren_src" "$STAGE/ffmpeg/bin"

log "[1/5] 拷贝 Apollo 工具链（lew + look2hear + ckpts + DSP 脚本）"
cp "$SB_APOLLO/lew_upscale.py" "$STAGE/Apollo/"
# mac 权威副本覆盖组件版本（MPS 设备支持 + fp16 autocast，见文件头注）
if [[ -f "$REPO_ROOT/packaging/macos/lew_upscale.py" ]]; then
    cp "$REPO_ROOT/packaging/macos/lew_upscale.py" "$STAGE/Apollo/lew_upscale.py"
fi
cp "$APP_APOLLO/bass_enhance.py" "$APP_APOLLO/drum_enhance.py" \
   "$APP_APOLLO/soundstage_reshape.py" "$APP_APOLLO/vocal_adjust.py" \
   "$APP_APOLLO/audio_validation.py" "$APP_APOLLO/stage_metadata.py" \
   "$APP_APOLLO/vocal_config.py" "$STAGE/Apollo/"
cp -R "$SB_APOLLO/look2hear" "$STAGE/Apollo/look2hear"
cp -R "$SB_APOLLO/ckpts" "$STAGE/Apollo/ckpts"

log "[2/5] 拷贝 Soren 母带链"
cp "$REPO_ROOT/packaging/soren_core.py" "$STAGE/Soren_src/core_decrypted.py"
cp "$REPO_ROOT/packaging/soren_original.py" "$STAGE/Soren_src/soren_original.py"
cp "$SB_SOREN/test_model.py" "$STAGE/Soren_src/"
cp -R "$SB_SOREN/model" "$STAGE/Soren_src/model"
cp -R "$SB_SOREN/profiles" "$STAGE/Soren_src/profiles"
cp -R "$SB_SOREN/secured_genres" "$STAGE/Soren_src/secured_genres"

log "[3/5] 拷贝 ffmpeg（$(basename "$FFMPEG")）"
# Homebrew 的 ffmpeg 可能是 symlink，解引用拷贝实体，保证 .app 自包含
FFMPEG_REAL="$(cd "$(dirname "$FFMPEG")" && pwd -P)/$(basename "$FFMPEG")"
[[ -e "$FFMPEG_REAL" ]] || FFMPEG_REAL="$FFMPEG"
cp "$FFMPEG_REAL" "$STAGE/ffmpeg/bin/ffmpeg"
chmod +x "$STAGE/ffmpeg/bin/ffmpeg"

log "[4/5] 装配推理解释器 env/（离线 wheels；torch 为 PyPI 原生 CPU 版，MPS 随系统）"
command -v uv >/dev/null 2>&1 || fail "需要 uv（https://docs.astral.sh/uv/）：uv python install 3.12"
WHEELS_DIR="${SB_WHEELS:-$BUNDLE_DIR/wheels-macos-arm64}"
[[ -d "$WHEELS_DIR" ]] || fail "wheels 目录不存在: $WHEELS_DIR"
REQS="${SB_REQUIREMENTS:-$BUNDLE_DIR/notes/requirements-macos-arm64.txt}"
[[ -f "$REQS" ]] || REQS="$REPO_ROOT/notes/requirements-macos-arm64.txt"
[[ -f "$REQS" ]] || fail "requirements 清单不存在（期望 $BUNDLE_DIR/notes/ 或 $REPO_ROOT/notes/）"
uv venv --python 3.12 "$STAGE/env"
uv pip install --python "$STAGE/env/bin/python" \
    --no-index --find-links "$WHEELS_DIR" -r "$REQS" \
    || fail "推理依赖安装失败"

log "[4b] 体积裁剪（__pycache__ / torch/include / wheel 残留）"
find "$STAGE/env" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$STAGE/env/lib/python3.12/site-packages/torch/include" 2>/dev/null || true
find "$STAGE/env" -name "*.whl" -delete 2>/dev/null || true

log "[5/5] 预置 htdemucs 权重（已有缓存则直接复用，首次运行需联网下载）"
SEED="$STAGE/_seed"
mkdir -p "$STAGE/torch_home" "$STAGE/hf_home"
if [[ -d "$WEIGHTS_KEEP/torch_home" ]]; then rm -rf "$STAGE/torch_home"; mv "$WEIGHTS_KEEP/torch_home" "$STAGE/torch_home"; fi
if [[ -d "$WEIGHTS_KEEP/hf_home" ]]; then rm -rf "$STAGE/hf_home"; mv "$WEIGHTS_KEEP/hf_home" "$STAGE/hf_home"; fi
rm -rf "$WEIGHTS_KEEP"
TORCH_HOME="$STAGE/torch_home" HF_HOME="$STAGE/hf_home" \
    "$STAGE/env/bin/python" -c "import soundfile as sf, numpy as np; sf.write('$SEED.wav', np.zeros((44100,2), dtype='float32'), 44100)"
TORCH_HOME="$STAGE/torch_home" HF_HOME="$STAGE/hf_home" \
    "$STAGE/env/bin/python" -m demucs --two-stems bass -n htdemucs -o "$SEED\_out" "$SEED.wav" \
    || fail "Demucs 权重预置失败"
TORCH_HOME="$STAGE/torch_home" HF_HOME="$STAGE/hf_home" HF_HUB_OFFLINE=1 \
    "$STAGE/env/bin/python" -c "from demucs.pretrained import get_model; m=get_model('htdemucs'); print('offline htdemucs models', len(m.models))" \
    || fail "Demucs 离线模型加载失败"

log "[5a] 验证关键依赖可导入"
PYTHONPATH="$STAGE/Apollo:$STAGE/Soren_src" "$STAGE/env/bin/python" - <<'EOF' || fail "关键依赖 import 验证失败"
import importlib
for name in ("torch", "torchaudio", "demucs", "numpy", "soundfile", "scipy",
             "librosa", "numba", "statsmodels", "pyloudnorm", "joblib",
             "cryptography", "look2hear.models"):
    importlib.import_module(name)
print("runtime imports OK", end=" ")
import torch
print(torch.__version__)
EOF

log "[5b] 生成 critical-manifest.sha256"
MANIFEST="$STAGE/critical-manifest.sha256"
: > "$MANIFEST"
stage_full="$(cd "$STAGE" && pwd -P)"
find "$STAGE/env/lib/python3.12/site-packages/numpy" "$STAGE/Apollo" "$STAGE/Soren_src" \
     "$STAGE/ffmpeg" "$STAGE/torch_home" "$STAGE/hf_home" \
     -type f -size +0c 2>/dev/null | sort | while read -r f; do
    rel="${f#"$stage_full"/}"
    printf '%s  %s\n' "$(shasum -a 256 "$f" | cut -d' ' -f1)" "$rel" >> "$MANIFEST"
done
assert_nonempty_file "$MANIFEST" "关键 manifest"

rm -rf "$SEED" "$SEED.wav" "$SEED\_out" 2>/dev/null || true
log "runtime 装配完成: $STAGE"
log "下一步: bash packaging/build_macos.sh 生成 ShadowBuster.app"
