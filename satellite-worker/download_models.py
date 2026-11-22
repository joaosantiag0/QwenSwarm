#!/usr/bin/env python3
"""
download_models.py
Baixa e configura os modelos necessários para o Qwen 2.1 Image Edit no worker satélite
usando a API oficial huggingface_hub.
"""

import os
import shutil
from pathlib import Path
from huggingface_hub import hf_hub_download

COMFY_DIR = Path(os.environ.get("COMFY_DIR", Path.home() / "ComfyUI"))
MODELS_DIR = COMFY_DIR / "models"

TASKS = [
    {
        "desc": "VAE bf16",
        "repo": "KasugaiSakura/Qwen-Image-2.1-Uncensored-Abenzerps-GGUF",
        "subfolder": "vae",
        "filename": "qwen_image_2.1_vae_bf16.safetensors",
        "target": MODELS_DIR / "vae" / "qwen_image_2.1_vae_bf16.safetensors",
    },
    {
        "desc": "LoRA Pruna 8-step",
        "repo": "PrunaAI/Pruna-Qwen-Image-2.1",
        "subfolder": None,
        "filename": "p_q_image_2.1_8_steps_v0.1.safetensors",
        "target": MODELS_DIR / "loras" / "p_qwen_image_2.1_8step_v0.1.safetensors",
    },
    {
        "desc": "UNet GGUF Q4_K_M",
        "repo": "KasugaiSakura/Qwen-Image-2.1-Uncensored-Abenzerps-GGUF",
        "subfolder": None,
        "filename": "qwen-image-2.1-UC-Q4_K_M.gguf",
        "target": MODELS_DIR / "diffusion_models" / "qwen-image-2.1-UC-Q4_K_M.gguf",
    },
    {
        "desc": "Text Encoder CLIP INT8",
        "repo": "KasugaiSakura/Qwen-Image-2.1-Uncensored-Abenzerps-GGUF",
        "subfolder": "text_encoders",
        "filename": "qwen3vl_8b_int8_convrot.safetensors",
        "target": MODELS_DIR / "clip" / "qwen3vl_8b_int8_convrot.safetensors",
    },
]


def main():
    print("=== Iniciando sincronização dos modelos Qwen 2.1 ===", flush=True)

    for t in TASKS:
        target = t["target"]
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.is_file() and target.stat().st_size > 10 * 1024 * 1024:
            print(f"[✓] Já existe e válido: {target.name} ({target.stat().st_size / (1024*1024):.1f} MB)", flush=True)
            continue

        print(f"[>] Baixando {t['desc']} ({t['filename']}) de {t['repo']}...", flush=True)
        kwargs = {
            "repo_id": t["repo"],
            "filename": t["filename"],
        }
        if t["subfolder"]:
            kwargs["subfolder"] = t["subfolder"]

        downloaded_path = hf_hub_download(**kwargs)
        shutil.copy(downloaded_path, target)
        print(f"[✓] Instalado em {target} ({target.stat().st_size / (1024*1024):.1f} MB)", flush=True)

    # Symlinks para compatibilidade com loaders do ComfyUI
    te_dir = MODELS_DIR / "text_encoders"
    te_dir.mkdir(parents=True, exist_ok=True)
    te_link = te_dir / "qwen3vl_8b_int8_convrot.safetensors"
    clip_src = MODELS_DIR / "clip" / "qwen3vl_8b_int8_convrot.safetensors"
    if clip_src.is_file() and not te_link.exists():
        te_link.symlink_to(clip_src)
        print("[✓] Symlink criado: models/text_encoders/qwen3vl_8b_int8_convrot.safetensors", flush=True)

    unet_dir = MODELS_DIR / "unet"
    unet_dir.mkdir(parents=True, exist_ok=True)
    unet_link = unet_dir / "qwen-image-2.1-UC-Q4_K_M.gguf"
    diff_src = MODELS_DIR / "diffusion_models" / "qwen-image-2.1-UC-Q4_K_M.gguf"
    if diff_src.is_file() and not unet_link.exists():
        unet_link.symlink_to(diff_src)
        print("[✓] Symlink criado: models/unet/qwen-image-2.1-UC-Q4_K_M.gguf", flush=True)

    print("=== Todos os modelos estão prontos e verificados! ===", flush=True)


if __name__ == "__main__":
    main()
