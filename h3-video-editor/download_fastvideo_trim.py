#!/usr/bin/env python3
"""Download FastH3 Trim weights for ComfyUI (port 3002).

Source (BF16 diffusers): https://huggingface.co/FastVideo/FastVideo-FastH3-Trim-8-Step
ComfyUI single-file UNet may appear on FastVideo/FastVideo-FastH3-Comfy when repackaged.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

COMFY_MODELS = Path(os.environ.get("COMFY_MODELS", "/home/user/ComfyUI/models"))
WEB_ROOT = Path(__file__).resolve().parent

V2_REPO = "FastVideo/FastVideo-FastH3-8-Step-V2"
TRIM_REPO = "FastVideo/FastVideo-FastH3-Trim-8-Step"
COMFY_REPO = "FastVideo/FastVideo-FastH3-Comfy"

# (repo, remote path, local path under COMFY_MODELS)
FILES: list[tuple[str, str, str]] = [
    (TRIM_REPO, "vae/minimax_h3_video_vae_int8_convrot.safetensors", "vae/minimax_h3_video_vae_int8_convrot.safetensors"),
    (TRIM_REPO, "audio_vae/diffusion_pytorch_model.safetensors", "vae/minimax_h3_audio_vae_fp32.safetensors"),
    (V2_REPO, "fastvideo_inference.json", None),  # -> WEB_ROOT
    (
        COMFY_REPO,
        "diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
        "diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
    ),
    (
        COMFY_REPO,
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    ),
]


def fetch(repo: str, remote: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1000:
        print(f"OK (exists) {dest}")
        return
    print(f"Downloading {repo}/{remote} -> {dest}", flush=True)
    cached = hf_hub_download(repo_id=repo, filename=remote)
    shutil.copy2(cached, dest)


def main() -> int:
    print(f"Comfy models: {COMFY_MODELS}")
    print(f"V2 source: https://huggingface.co/{V2_REPO}")
    print(f"Comfy repack: https://huggingface.co/{COMFY_REPO}")
    errors = 0
    for repo, remote, rel in FILES:
        if rel is None:
            dest = WEB_ROOT / Path(remote).name
            try:
                fetch(repo, remote, dest)
            except Exception as e:
                print(f"WARN {remote}: {e}", file=sys.stderr)
            continue
        dest = COMFY_MODELS / rel
        try:
            fetch(repo, remote, dest)
        except Exception as e:
            print(f"FAILED {rel} ({repo}): {e}", file=sys.stderr)
            errors += 1
    unet = COMFY_MODELS / "diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors"
    if not unet.is_file() or unet.stat().st_size < 1_000_000_000:
        print(f"\nUNet Comfy incompleto: {unet}", file=sys.stderr)
        errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
