#!/usr/bin/env python3
"""Download Qwen-Image-Edit-Rapid-AIO GGUF dependencies."""

import subprocess
from pathlib import Path

COMFY = Path("/home/user/ComfyUI")
BASE = "https://huggingface.co/Phil2Sat/Qwen-Image-Edit-Rapid-AIO-GGUF/resolve/main"

DOWNLOADS = [
    (
        f"{BASE}/v90/qwen-rapid-nsfw-v9.0-Q3_K_S.gguf",
        COMFY / "models/diffusion_models/qwen-rapid-nsfw-v9.0-Q3_K_S.gguf",
    ),
    (
        f"{BASE}/Qwen2.5-VL-7B-Instruct-abliterated/Qwen2.5-VL-7B-Instruct-abliterated.Q4_K_M.gguf",
        COMFY / "models/text_encoders/Qwen2.5-VL-7B-Instruct-abliterated.Q4_K_M.gguf",
    ),
    (
        f"{BASE}/Qwen2.5-VL-7B-Instruct-abliterated/Qwen2.5-VL-7B-Instruct-abliterated.mmproj-Q8_0.gguf",
        COMFY / "models/text_encoders/Qwen2.5-VL-7B-Instruct-abliterated.mmproj-Q8_0.gguf",
    ),
    (
        "https://huggingface.co/Comfy-Org/Qwen-Image-Edit/resolve/main/vae/qwen_image_vae.safetensors",
        COMFY / "models/vae/qwen_image_vae.safetensors",
    ),
]


def download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"skip {dest}")
        return
    print(f"download {url}\n  -> {dest}")
    subprocess.run(
        ["wget", "-c", "-O", str(partial), url],
        check=True,
    )
    partial.rename(dest)
    print(f"done {dest.name} ({dest.stat().st_size // (1024 * 1024)} MB)")


def main():
    for url, dest in DOWNLOADS:
        try:
            download(url, dest)
        except subprocess.CalledProcessError as exc:
            print(f"failed {dest.name}: {exc}")
    print("all downloads attempted")


if __name__ == "__main__":
    main()
