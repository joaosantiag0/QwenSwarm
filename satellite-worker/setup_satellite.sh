#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFY_DIR="${COMFY_DIR:-$HOME/ComfyUI}"
BASE_DIR="${BASE_DIR:-$HOME/satellite_worker}"

echo "=== [1/5] Clonando ComfyUI em $COMFY_DIR ==="
if [ ! -d "$COMFY_DIR" ]; then
    git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
else
    echo "ComfyUI já existe. Atualizando..."
    cd "$COMFY_DIR" && git pull || true
fi

echo "=== [2/5] Criando ambiente virtual Python ==="
if [ ! -d "$COMFY_DIR/venv" ]; then
    python3 -m venv "$COMFY_DIR/venv"
fi

"$COMFY_DIR/venv/bin/pip" install --upgrade pip
"$COMFY_DIR/venv/bin/pip" install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu124
"$COMFY_DIR/venv/bin/pip" install -r "$COMFY_DIR/requirements.txt"
"$COMFY_DIR/venv/bin/pip" install pillow requests huggingface_hub gguf

echo "=== [3/5] Instalando nós customizados (ComfyUI-GGUF) ==="
mkdir -p "$COMFY_DIR/custom_nodes"
if [ ! -d "$COMFY_DIR/custom_nodes/ComfyUI-GGUF" ]; then
    git clone https://github.com/city96/ComfyUI-GGUF.git "$COMFY_DIR/custom_nodes/ComfyUI-GGUF"
fi
if [ -f "$COMFY_DIR/custom_nodes/ComfyUI-GGUF/requirements.txt" ]; then
    "$COMFY_DIR/venv/bin/pip" install -r "$COMFY_DIR/custom_nodes/ComfyUI-GGUF/requirements.txt"
fi

echo "=== [4/5] Criando diretórios de modelos e workflow ==="
mkdir -p "$COMFY_DIR/models/diffusion_models"
mkdir -p "$COMFY_DIR/models/clip"
mkdir -p "$COMFY_DIR/models/text_encoders"
mkdir -p "$COMFY_DIR/models/vae"
mkdir -p "$COMFY_DIR/models/loras"
mkdir -p "$COMFY_DIR/user/default/workflows"
mkdir -p "$BASE_DIR/outbox"
mkdir -p "$BASE_DIR/pool"
mkdir -p "$BASE_DIR/resized"
mkdir -p "$BASE_DIR/preset_refs"

if [ -f "$SCRIPT_DIR/image_qwen_image_2_1_image_edit.json" ]; then
    echo "Copiando workflow padrão para $COMFY_DIR/user/default/workflows/..."
    cp "$SCRIPT_DIR/image_qwen_image_2_1_image_edit.json" "$COMFY_DIR/user/default/workflows/"
fi

echo "=== [5/5] Setup inicial concluído com sucesso! ==="
