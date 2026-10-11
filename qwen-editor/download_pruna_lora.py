#!/usr/bin/env python3
"""Download Pruna LoRA for Qwen Image 2.1 into ComfyUI/models/loras/."""

from pathlib import Path

from huggingface_hub import hf_hub_download

REPO = "PrunaAI/Pruna-Qwen-Image-2.1"
FILE = "p_qwen_image_2.1_8step_v0.1.safetensors"
DEST = Path("/home/user/ComfyUI/models/loras")


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    path = hf_hub_download(repo_id=REPO, filename=FILE, local_dir=str(DEST))
    print("OK", path)


if __name__ == "__main__":
    main()
