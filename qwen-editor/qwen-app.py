#!/usr/bin/env python3
"""Simple web UI for Qwen image editing via ComfyUI API."""

from __future__ import annotations

import cgi
import hashlib
import html as html_module
import json
import math
import random
import re
import shutil
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    LOCAL_TZ = None

PRESET_SLOT_HOURS = 5


def ts_to_local(ts: float) -> datetime:
    if LOCAL_TZ is not None:
        return datetime.fromtimestamp(ts, LOCAL_TZ)
    return datetime.fromtimestamp(ts)


def preset_time_slot(ts: float) -> tuple[str, str]:
    """5-hour buckets in local time, e.g. 10h–14h on 05/10/2026."""
    if not ts:
        return ("", "")
    dt = ts_to_local(float(ts))
    start = (dt.hour // PRESET_SLOT_HOURS) * PRESET_SLOT_HOURS
    end = min(start + PRESET_SLOT_HOURS - 1, 23)
    slot_key = f"{dt.strftime('%Y-%m-%d')}_{start:02d}"
    slot_label = f"{dt.strftime('%d/%m/%Y')} {start:02d}h–{end:02d}h"
    return slot_key, slot_label


def iso_local(ts: float) -> str:
    if not ts:
        return ""
    return ts_to_local(float(ts)).strftime("%Y-%m-%d %H:%M")

COMFY_API = "http://127.0.0.1:8188"
COMFY_INPUT = Path("/home/user/ComfyUI/input")
COMFY_OUTPUT = Path("/home/user/ComfyUI/output")
COMFY_MODELS = Path("/home/user/ComfyUI/models")
WORKFLOW_QWEN21 = Path("/home/user/ComfyUI/user/default/workflows/image_qwen_image_2_1_image_edit.json")
WEB_ROOT = Path(__file__).resolve().parent
PROMPTS_FILE = WEB_ROOT / "prompts.json"
META_FILE = WEB_ROOT / "image_meta.json"
PRUNA_CONFIG_FILE = WEB_ROOT / "pruna_config.json"
PORT = 3000

PRUNA_LORA_8STEP = "p_qwen_image_2.1_8step_v0.1.safetensors"
PRUNA_STEPS = 8
# Sigmas from PrunaAI card (8-step); terminal 0 included for Comfy ManualSigmas.
PRUNA_SIGMAS_8 = (
    "1, 0.9333333333, 0.8571428571, 0.7692307692, 0.6666666667, "
    "0.5454545455, 0.4, 0.2222222222, 0"
)

FILENAME_META_RE = re.compile(
    r"^qwen_web_(\d{8})_(\d{6})_([a-zA-Z0-9_-]+)_\d+\.png$"
)

RAPID_UNET = "qwen-rapid-nsfw-v9.0-Q3_K_S.gguf"
RAPID_CLIP = "Qwen2.5-VL-7B-Instruct-abliterated.Q4_K_M.gguf"
RAPID_VAE_CANDIDATES = ("qwen_image_vae.safetensors", "qwen_image_2.1_vae_bf16.safetensors")

QUALITY_PRESETS_QWEN21 = {
    "ultra": {"resolution": 512, "steps": 15, "label": "Ultra rápido (~1-2 min)"},
    "fast": {"resolution": 768, "steps": 20, "label": "Rápido (~3-4 min)"},
    "normal": {"resolution": 1024, "steps": 25, "label": "Normal (~5 min)"},
    "high": {"resolution": 1024, "steps": 35, "label": "Alta qualidade (~7 min)"},
}

QUALITY_PRESETS_RAPID = {
    "ultra": {"resolution": 512, "steps": 4, "label": "Ultra — 512px, 4 passos (~30s)"},
    "fast": {"resolution": 768, "steps": 4, "label": "Rápido — 768px, 4 passos (~1 min)"},
    "normal": {"resolution": 1024, "steps": 4, "label": "Normal — 1024px, 4 passos (~1-2 min)"},
    "high": {"resolution": 1024, "steps": 4, "label": "Alta — 1024px, 4 passos (~1-2 min)"},
}

QUALITY_PRESETS_PRUNA = {
    "ultra": {"resolution": 512, "steps": PRUNA_STEPS, "label": "Ultra — 512px, 8 passos Pruna"},
    "fast": {"resolution": 768, "steps": PRUNA_STEPS, "label": "Rápido — 768px, 8 passos (~1-2 min)"},
    "normal": {"resolution": 1024, "steps": PRUNA_STEPS, "label": "Normal — 1024px, 8 passos (~2 min)"},
    "high": {"resolution": 1024, "steps": PRUNA_STEPS, "label": "1024px, 8 passos (qualidade Pruna v0.1)"},
}

MODELS = {
    "qwen21": {
        "label": "Qwen Image 2.1 — melhor qualidade",
        "presets": QUALITY_PRESETS_QWEN21,
        "hint": "Edição de alta qualidade. Normal ≈ 5 min na RTX 3060.",
    },
    "rapid": {
        "label": "Rapid AIO — ultra rápido (4 passos)",
        "presets": QUALITY_PRESETS_RAPID,
        "hint": "Modelo Rapid AIO com Lightning embutido. ~1 min por imagem.",
    },
    "qwen21_pruna": {
        "label": "Qwen 2.1 + Pruna (8 passos, teste)",
        "presets": QUALITY_PRESETS_PRUNA,
        "hint": (
            "LoRA PrunaAI em cima do mesmo Qwen 2.1 — menos passos, qualidade v0.1 abaixo do normal. "
            "Para voltar ao modo antigo: escolha «Qwen Image 2.1» ou desative em pruna_config.json."
        ),
    },
}


def load_pruna_config() -> dict:
    default = {"enabled": True, "lora": PRUNA_LORA_8STEP}
    if not PRUNA_CONFIG_FILE.is_file():
        return default
    try:
        with PRUNA_CONFIG_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        return {**default, **data}
    except (json.JSONDecodeError, OSError):
        return default


def pruna_feature_enabled() -> bool:
    return bool(load_pruna_config().get("enabled", True))


def pruna_lora_filename() -> str:
    return str(load_pruna_config().get("lora") or PRUNA_LORA_8STEP)


def pruna_assets_ready() -> bool:
    return (COMFY_MODELS / "loras" / pruna_lora_filename()).is_file()


def models_for_ui() -> dict:
    out = {k: v for k, v in MODELS.items() if k != "qwen21_pruna"}
    if pruna_feature_enabled():
        out["qwen21_pruna"] = MODELS["qwen21_pruna"]
    return out

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
prompts_lock = threading.Lock()
meta_lock = threading.Lock()


def field_has_upload(field) -> bool:
    if field is None:
        return False
    return bool(getattr(field, "file", None) and getattr(field, "filename", None))


def form_text(form, key: str) -> str:
    val = form.getvalue(key)
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[-1] or "").strip()
    return str(val).strip()


def rapid_assets_ready() -> bool:
    unet_ok = (COMFY_MODELS / "diffusion_models" / RAPID_UNET).is_file()
    clip_ok = (COMFY_MODELS / "text_encoders" / RAPID_CLIP).is_file()
    mmproj_ok = (COMFY_MODELS / "text_encoders" / "Qwen2.5-VL-7B-Instruct-abliterated.mmproj-Q8_0.gguf").is_file()
    vae_ok = any((COMFY_MODELS / "vae" / name).is_file() for name in RAPID_VAE_CANDIDATES)
    return unet_ok and clip_ok and mmproj_ok and vae_ok


def rapid_vae_name() -> str:
    for name in RAPID_VAE_CANDIDATES:
        if (COMFY_MODELS / "vae" / name).is_file():
            return name
    return RAPID_VAE_CANDIDATES[0]


def model_available(mid: str) -> bool:
    if mid == "rapid":
        return rapid_assets_ready()
    if mid == "qwen21_pruna":
        return pruna_assets_ready()
    return True


def model_status() -> dict:
    return {
        mid: {
            "id": mid,
            "label": cfg["label"],
            "hint": cfg["hint"],
            "available": model_available(mid),
            "presets": cfg["presets"],
        }
        for mid, cfg in models_for_ui().items()
    }


def qwen_target_size(width: int, height: int, resolution: int) -> tuple[int, int]:
    if resolution > 0:
        ratio = width / height
        new_w = round(math.sqrt(resolution * resolution * ratio) / 32) * 32
        new_h = round(math.sqrt(resolution * resolution / ratio) / 32) * 32
    else:
        new_w = round(width / 32) * 32
        new_h = round(height / 32) * 32
    return max(32, new_w), max(32, new_h)


def resize_image_for_qwen(path: Path, resolution: int) -> tuple[int, int, int, int]:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        orig_w, orig_h = im.size
        new_w, new_h = qwen_target_size(orig_w, orig_h, resolution)
        if (new_w, new_h) != (orig_w, orig_h):
            im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        png_path = path.with_suffix(".png")
        im.save(png_path, format="PNG", optimize=True)
    if png_path != path and path.exists():
        path.unlink()
    return orig_w, orig_h, new_w, new_h


def parse_link(link):
    if isinstance(link, dict):
        return (
            link["id"],
            link["origin_id"],
            link["origin_slot"],
            link["target_id"],
            link["target_slot"],
            link.get("type"),
        )
    return link[0], link[1], link[2], link[3], link[4], link[5] if len(link) > 5 else None


def sanitize_preset_token(value: str | None) -> str:
    if not value:
        return "custom"
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", str(value).strip())[:24]
    return cleaned or "custom"


def make_filename_prefix(prompt_preset_id: str | None) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = sanitize_preset_token(prompt_preset_id)
    return f"qwen_web_{ts}_{token}"


def load_image_meta() -> dict:
    if not META_FILE.is_file():
        return {}
    try:
        with META_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_image_meta(data: dict) -> None:
    tmp = META_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(META_FILE)


def prompt_text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def record_image_meta(
    filename: str,
    *,
    prompt_preset_id: str,
    prompt_label: str,
    job_id: str,
    created_at: float,
    preset_updated_at: float | None = None,
    prompt_text_hash_value: str = "",
    regenerated_from: str | None = None,
) -> None:
    with meta_lock:
        meta = load_image_meta()
        entry = {
            "prompt_id": prompt_preset_id or "custom",
            "prompt_label": prompt_label or "",
            "job_id": job_id,
            "created_at": created_at,
            "preset_updated_at": float(preset_updated_at or created_at),
            "prompt_text_hash": prompt_text_hash_value or "",
        }
        if regenerated_from:
            entry["regenerated_from"] = regenerated_from
        meta[filename] = entry
        save_image_meta(meta)


def remove_image_meta(filename: str) -> None:
    with meta_lock:
        meta = load_image_meta()
        if filename in meta:
            del meta[filename]
            save_image_meta(meta)


def meta_from_filename(filename: str) -> dict:
    m = FILENAME_META_RE.match(Path(filename).name)
    if not m:
        return {}
    date_part, time_part, prompt_id = m.groups()
    iso = f"{date_part}T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}:00"
    try:
        created_at = datetime.strptime(f"{date_part} {time_part}", "%Y%m%d %H%M%S").timestamp()
    except ValueError:
        created_at = None
    return {
        "prompt_id": prompt_id,
        "created_at": created_at,
        "created_at_iso": iso,
    }


def presets_by_id() -> dict[str, dict]:
    return {p["id"]: p for p in load_prompt_presets()}


def image_needs_regen(stored: dict, prompt_id: str, presets: dict[str, dict]) -> bool:
    if stored.get("prompt_cleared") or not prompt_id or prompt_id == "custom":
        return False
    preset = presets.get(prompt_id)
    if not preset:
        return False
    synced = float(stored.get("preset_updated_at") or stored.get("created_at") or 0)
    updated = float(preset.get("updated_at") or 0)
    if updated > synced + 0.001:
        return True
    old_hash = stored.get("prompt_text_hash") or ""
    if old_hash and old_hash != prompt_text_hash(preset.get("text", "")):
        return True
    return False


def gallery_entry(path: Path, meta_db: dict, presets: dict[str, dict] | None = None) -> dict:
    if presets is None:
        presets = presets_by_id()
    name = path.name
    stored = meta_db.get(name, {})
    parsed = meta_from_filename(name)
    prompt_cleared = bool(stored.get("prompt_cleared"))
    if prompt_cleared:
        prompt_id = ""
        prompt_label = ""
    else:
        prompt_id = stored.get("prompt_id") or parsed.get("prompt_id") or ""
        prompt_label = stored.get("prompt_label") or ""
    created_at = stored.get("created_at") or parsed.get("created_at") or path.stat().st_mtime
    entry = {
        "filename": name,
        "mtime": path.stat().st_mtime,
        "prompt_id": prompt_id,
        "prompt_label": prompt_label,
        "prompt_cleared": prompt_cleared,
        "created_at": created_at,
        "needs_regen": image_needs_regen(stored, prompt_id, presets),
        "preset_updated_at": stored.get("preset_updated_at"),
    }
    if stored.get("job_id"):
        entry["job_id"] = stored["job_id"]
    if prompt_id and prompt_id in presets:
        preset = presets[prompt_id]
        entry["current_preset_updated_at"] = preset.get("updated_at")
        sk, sl = preset_time_slot(float(preset.get("updated_at") or 0))
        entry["preset_slot"] = sk
        entry["preset_slot_label"] = sl
    entry["created_at_iso"] = iso_local(float(created_at or 0))
    gs, gl = preset_time_slot(float(created_at or 0))
    entry["generated_slot"] = gs
    entry["generated_slot_label"] = gl
    synced = stored.get("preset_updated_at")
    if synced:
        ss, ssl = preset_time_slot(float(synced))
        entry["synced_preset_slot"] = ss
        entry["synced_preset_slot_label"] = ssl
    return entry


def apply_pruna_to_qwen21_prompt(api_prompt: dict, prefix: str, seed: int) -> None:
    """Inject Pruna LoRA + manual sigma schedule (replaces KSampler 458)."""
    ks_key = f"{prefix}458"
    if ks_key in api_prompt:
        del api_prompt[ks_key]
    api_prompt[f"{prefix}450"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": [f"{prefix}451", 0],
            "lora_name": pruna_lora_filename(),
            "strength_model": 1.0,
        },
    }
    cache_key = f"{prefix}469"
    if cache_key in api_prompt:
        api_prompt[cache_key]["inputs"]["model"] = [f"{prefix}450", 0]
    api_prompt[f"{prefix}480"] = {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": "euler"},
    }
    api_prompt[f"{prefix}481"] = {
        "class_type": "ManualSigmas",
        "inputs": {"sigmas": PRUNA_SIGMAS_8},
    }
    api_prompt[f"{prefix}482"] = {
        "class_type": "SamplerCustom",
        "inputs": {
            "model": [cache_key, 0],
            "add_noise": True,
            "noise_seed": seed,
            "cfg": 1.0,
            "positive": [f"{prefix}474", 0],
            "negative": [f"{prefix}474", 1],
            "sampler": [f"{prefix}480", 0],
            "sigmas": [f"{prefix}481", 0],
            "latent_image": [f"{prefix}468", 0],
        },
    }
    api_prompt[f"{prefix}457"]["inputs"]["samples"] = [f"{prefix}482", 0]


def build_prompt_qwen21(
    image1: str,
    image2: str | None,
    prompt: str,
    steps: int,
    resolution: int,
    filename_prefix: str = "qwen_web",
    use_pruna: bool = False,
) -> dict:
    with WORKFLOW_QWEN21.open() as f:
        wf = json.load(f)

    outer_nodes = {n["id"]: n for n in wf["nodes"]}
    sg = wf["definitions"]["subgraphs"][0]
    sg_nodes = {n["id"]: n for n in sg["nodes"]}
    sg_links = {parse_link(l)[0]: parse_link(l) for l in sg.get("links", [])}
    subgraph_node = outer_nodes[459]
    subgraph_widget_map = dict(subgraph_node.get("widgets_values_named", {}))
    subgraph_widget_map["prompt"] = prompt
    subgraph_widget_map["steps"] = steps
    subgraph_widget_map["resolution"] = resolution
    subgraph_widget_map["seed"] = random.randint(0, 2**53)
    prefix = "459:"

    sg_input_map = {}
    for inp in sg.get("inputs", []):
        for link_id in inp.get("linkIds", []):
            sg_input_map[link_id] = inp["name"]

    def build_inputs(node):
        wvn = node.get("widgets_values_named", {})
        wv = node.get("widgets_values", [])
        ntype = node["type"]
        inputs = {}
        if ntype == "LoadImage":
            inputs["image"] = wvn.get("image", wv[0])
        elif ntype == "SaveImageAdvanced":
            inputs["filename_prefix"] = filename_prefix
            inputs["format"] = "png"
            inputs["format.bit_depth"] = "8-bit"
            inputs["format.input_color_space"] = "sRGB"
        elif ntype in ("UNETLoader", "UnetLoaderGGUF"):
            inputs["unet_name"] = subgraph_widget_map.get("unet_name", wvn.get("unet_name", wv[0]))
        elif ntype == "CLIPLoader":
            inputs["clip_name"] = subgraph_widget_map.get("clip_name", wvn.get("clip_name", wv[0]))
            inputs["type"] = wvn.get("type", wv[1] if len(wv) > 1 else "qwen_image")
        elif ntype == "VAELoader":
            inputs["vae_name"] = subgraph_widget_map.get("vae_name", wvn.get("vae_name", wv[0]))
        elif ntype == "EmptyLatentImage":
            inputs["width"] = subgraph_widget_map.get("width", 1024)
            inputs["height"] = subgraph_widget_map.get("height", 1024)
            inputs["batch_size"] = 1
        elif ntype == "KSampler":
            inputs["seed"] = subgraph_widget_map["seed"]
            inputs["steps"] = subgraph_widget_map.get("steps", 25)
            inputs["cfg"] = subgraph_widget_map.get("cfg", 1)
            inputs["sampler_name"] = subgraph_widget_map.get("scheduler", "euler")
            inputs["scheduler"] = subgraph_widget_map.get("scheduler_1", "simple")
            inputs["denoise"] = 1
        elif ntype == "ComfySwitchNode":
            inputs["switch"] = subgraph_widget_map.get("switch", False)
        elif ntype == "QwenImage21Cache":
            inputs["device"] = subgraph_widget_map.get("device", "auto")
            inputs["dtype"] = subgraph_widget_map.get("dtype", "default")
        elif ntype == "TextEncodeQwenImage21":
            inputs["prompt"] = subgraph_widget_map.get("prompt", "")
            inputs["negative_prompt"] = subgraph_widget_map.get("negative_prompt", "")
            inputs["resolution"] = subgraph_widget_map.get("resolution", 1024)
        return inputs

    api_prompt = {}
    for oid, node in outer_nodes.items():
        if node["type"] in ("MarkdownNote", "ImageCompare", "ResolutionSelector") or oid == 459:
            continue
        api_prompt[str(oid)] = {"class_type": node["type"], "inputs": build_inputs(node)}

    for sid, node in sg_nodes.items():
        if use_pruna and sid == 458:
            continue
        ctype = node["type"]
        if ctype == "UNETLoader" and str(subgraph_widget_map.get("unet_name", "")).endswith(".gguf"):
            ctype = "UnetLoaderGGUF"
        api_prompt[f"{prefix}{sid}"] = {"class_type": ctype, "inputs": build_inputs(node)}

    api_prompt["470"]["inputs"]["image"] = image1
    if image2:
        api_prompt["475"] = {"class_type": "LoadImage", "inputs": {"image": image2}}

    for lid, (_, src, src_slot, dst, dst_slot, _) in sg_links.items():
        if dst < 0 or src == -20:
            continue
        if use_pruna and dst == 458:
            continue
        dst_node = sg_nodes[dst]
        inp_name = dst_node["inputs"][dst_slot]["name"]
        if src == -10:
            ext = sg_input_map.get(lid)
            if ext == "images.image_1":
                api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = ["470", 0]
            elif ext == "images.image_2" and image2:
                api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = ["475", 0]
            elif ext and ext in subgraph_widget_map:
                api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = subgraph_widget_map[ext]
        else:
            api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = [f"{prefix}{src}", src_slot]

    api_prompt["461"]["inputs"]["images"] = ["459:457", 0]
    if use_pruna:
        apply_pruna_to_qwen21_prompt(api_prompt, prefix, subgraph_widget_map["seed"])
    return api_prompt


def build_prompt_rapid(
    image1: str,
    image2: str | None,
    prompt: str,
    steps: int,
    width: int,
    height: int,
    filename_prefix: str = "qwen_web",
) -> dict:
    seed = random.randint(0, 2**53)
    vae_name = rapid_vae_name()
    pos_inputs = {
        "clip": ["2", 0],
        "vae": ["3", 0],
        "prompt": prompt,
        "image1": ["10", 0],
    }
    if image2:
        pos_inputs["image2"] = ["11", 0]

    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": RAPID_UNET}},
        "2": {
            "class_type": "CLIPLoaderGGUF",
            "inputs": {"clip_name": RAPID_CLIP, "type": "qwen_image"},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "10": {"class_type": "LoadImage", "inputs": {"image": image1}},
        "11": {"class_type": "LoadImage", "inputs": {"image": image2 or image1}},
        "20": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": pos_inputs,
        },
        "21": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": ""},
        },
        "30": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "40": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["20", 0],
                "negative": ["21", 0],
                "latent_image": ["30", 0],
                "seed": seed,
                "steps": steps,
                "cfg": 1,
                "sampler_name": "sa_solver",
                "scheduler": "beta",
                "denoise": 1,
            },
        },
        "50": {"class_type": "VAEDecode", "inputs": {"samples": ["40", 0], "vae": ["3", 0]}},
        "60": {
            "class_type": "SaveImageAdvanced",
            "inputs": {
                "images": ["50", 0],
                "filename_prefix": filename_prefix,
                "format": "png",
                "format.bit_depth": "8-bit",
                "format.input_color_space": "sRGB",
            },
        },
    }


def build_prompt(
    model: str,
    image1: str,
    image2: str | None,
    prompt: str,
    steps: int,
    resolution: int,
    width: int,
    height: int,
    filename_prefix: str = "qwen_web",
) -> dict:
    if model == "rapid":
        return build_prompt_rapid(
            image1, image2, prompt, steps, width, height, filename_prefix
        )
    return build_prompt_qwen21(
        image1,
        image2,
        prompt,
        steps,
        resolution,
        filename_prefix,
        use_pruna=(model == "qwen21_pruna"),
    )


def comfy_post_prompt(api_prompt: dict) -> str:
    payload = json.dumps({"prompt": api_prompt, "client_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(
        f"{COMFY_API}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI não retornou prompt_id: {result}")
    if result.get("node_errors"):
        raise RuntimeError(json.dumps(result["node_errors"], ensure_ascii=False)[:2000])
    return prompt_id


def comfy_wait(prompt_id: str, timeout_s: int = 900) -> list[str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with urllib.request.urlopen(f"{COMFY_API}/history/{prompt_id}", timeout=15) as resp:
            hist = json.loads(resp.read())
        if prompt_id not in hist:
            time.sleep(3)
            continue
        entry = hist[prompt_id]
        status = entry.get("status", {})
        if not status.get("completed"):
            time.sleep(3)
            continue
        for msg in status.get("messages", []):
            if msg[0] == "execution_error":
                detail = msg[1] if len(msg) > 1 else {}
                raise RuntimeError(detail.get("exception_message", str(detail)))
        images = []
        for node_out in entry.get("outputs", {}).values():
            for img in node_out.get("images", []):
                images.append(img["filename"])
        return images
    raise TimeoutError("Geração demorou demais (timeout 15 min)")


def run_job(
    job_id: str,
    model: str,
    image1: str,
    image2: str | None,
    prompt: str,
    steps: int,
    resolution: int,
    width: int,
    height: int,
    filename_prefix: str,
    prompt_preset_id: str,
    prompt_preset_label: str,
    regenerated_from: str | None = None,
):
    def worker():
        try:
            with jobs_lock:
                jobs[job_id]["status"] = "running"
                jobs[job_id]["started_at"] = time.time()
            api_prompt = build_prompt(
                model,
                image1,
                image2,
                prompt,
                steps,
                resolution,
                width,
                height,
                filename_prefix,
            )
            prompt_id = comfy_post_prompt(api_prompt)
            with jobs_lock:
                jobs[job_id]["prompt_id"] = prompt_id
            filenames = comfy_wait(prompt_id)
            finished = time.time()
            snap = finished
            ph = prompt_text_hash(prompt)
            if prompt_preset_id and prompt_preset_id != "custom":
                preset = presets_by_id().get(prompt_preset_id)
                if preset:
                    snap = float(preset.get("updated_at") or finished)
                    ph = prompt_text_hash(preset.get("text", ""))
            for fn in filenames:
                record_image_meta(
                    fn,
                    prompt_preset_id=prompt_preset_id,
                    prompt_label=prompt_preset_label,
                    job_id=job_id,
                    created_at=finished,
                    preset_updated_at=snap,
                    prompt_text_hash_value=ph,
                    regenerated_from=regenerated_from,
                )
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["images"] = filenames
                jobs[job_id]["finished_at"] = finished
        except Exception as e:
            with jobs_lock:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["finished_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def load_raw_prompts() -> list[dict]:
    if not PROMPTS_FILE.is_file():
        return []
    with PROMPTS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    return data


def save_raw_prompts(items: list[dict]) -> None:
    PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROMPTS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(PROMPTS_FILE)


def ensure_prompt_timestamps(items: list[dict]) -> list[dict]:
    now = time.time()
    fallback = PROMPTS_FILE.stat().st_mtime if PROMPTS_FILE.is_file() else now
    changed = False
    for item in items:
        if not item.get("created_at"):
            item["created_at"] = fallback
            changed = True
        if not item.get("updated_at"):
            item["updated_at"] = item["created_at"]
            changed = True
    if changed:
        save_raw_prompts(items)
    return items


def ensure_prompt_ids(items: list[dict]) -> list[dict]:
    changed = False
    for item in items:
        if not item.get("id"):
            item["id"] = uuid.uuid4().hex[:12]
            changed = True
    if changed:
        save_raw_prompts(items)
    return ensure_prompt_timestamps(items)


def normalize_prompt_item(data: dict, fallback_label: str = "Novo prompt") -> dict:
    category = str(data.get("category") or "Outros").strip() or "Outros"
    label = str(data.get("label") or fallback_label).strip() or fallback_label
    text = str(data.get("text") or "").strip()
    if not text:
        raise ValueError("Texto do prompt obrigatório")
    return {"category": category, "label": label, "text": text}


def load_prompt_presets() -> list[dict]:
    with prompts_lock:
        items = ensure_prompt_ids(load_raw_prompts())
    out: list[dict] = []
    for i, item in enumerate(items):
        if not item.get("text"):
            continue
        updated = float(item.get("updated_at") or 0)
        created = float(item.get("created_at") or updated)
        slot_key, slot_label = preset_time_slot(updated)
        c_slot_key, c_slot_label = preset_time_slot(created)
        out.append(
            {
                "id": item["id"],
                "category": item.get("category", "Outros"),
                "label": item.get("label", f"Prompt {i + 1}"),
                "text": item.get("text", ""),
                "created_at": created,
                "updated_at": updated,
                "created_at_iso": iso_local(created),
                "updated_at_iso": iso_local(updated),
                "updated_slot": slot_key,
                "updated_slot_label": slot_label,
                "created_slot": c_slot_key,
                "created_slot_label": c_slot_label,
            }
        )
    return out


def ordered_prompt_presets() -> list[dict]:
    """Same order as dropdown: categories A→Z, original file order within each."""
    raw = load_prompt_presets()
    by_category: dict[str, list[dict]] = {}
    for item in raw:
        by_category.setdefault(item["category"], []).append(item)
    ordered: list[dict] = []
    for category in sorted(by_category.keys()):
        ordered.extend(by_category[category])
    return [{**item, "order": i + 1} for i, item in enumerate(ordered)]


def create_prompt(data: dict) -> dict:
    item = normalize_prompt_item(data)
    item["id"] = uuid.uuid4().hex[:12]
    now = time.time()
    item["created_at"] = now
    item["updated_at"] = now
    with prompts_lock:
        items = ensure_prompt_ids(load_raw_prompts())
        items.append(item)
        save_raw_prompts(items)
    return item


def update_prompt(prompt_id: str, data: dict) -> dict | None:
    with prompts_lock:
        items = ensure_prompt_ids(load_raw_prompts())
        for i, item in enumerate(items):
            if item.get("id") != prompt_id:
                continue
            updated = normalize_prompt_item(data, item.get("label", "Prompt"))
            updated["id"] = prompt_id
            updated["created_at"] = float(item.get("created_at") or time.time())
            updated["updated_at"] = time.time()
            items[i] = updated
            save_raw_prompts(items)
            return updated
    return None


def delete_prompt(prompt_id: str) -> bool:
    with prompts_lock:
        items = ensure_prompt_ids(load_raw_prompts())
        new_items = [item for item in items if item.get("id") != prompt_id]
        if len(new_items) == len(items):
            return False
        save_raw_prompts(new_items)
        return True


def render_index_html() -> bytes:
    template = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    presets = ordered_prompt_presets()
    by_category: dict[str, list[dict]] = {}
    for preset in presets:
        by_category.setdefault(preset["category"], []).append(preset)

    options = ['<option value="">— Escolha um prompt salvo —</option>']
    for category in sorted(by_category.keys()):
        options.append(f'<optgroup label="{html_module.escape(category)}">')
        for item in by_category[category]:
            label = html_module.escape(f"{item['order']}. {item['label']}")
            options.append(f'<option value="{html_module.escape(str(item["id"]))}">{label}</option>')
        options.append("</optgroup>")

    model_options = []
    for mid, cfg in models_for_ui().items():
        available = model_available(mid)
        label = cfg["label"] + (" ✓" if available else " (baixando...)")
        disabled = "" if available else " disabled"
        model_options.append(f'<option value="{mid}"{disabled}>{html_module.escape(label)}</option>')

    page = template.replace("<!--PROMPT_OPTIONS-->", "\n          ".join(options))
    page = page.replace("<!--PROMPT_DATA-->", json.dumps(presets, ensure_ascii=False))
    page = page.replace("<!--MODEL_OPTIONS-->", "\n          ".join(model_options))
    page = page.replace("<!--MODEL_DATA-->", json.dumps(model_status(), ensure_ascii=False))
    return page.encode("utf-8")


def safe_output_path(filename: str) -> Path | None:
    name = Path(filename).name
    if not name.startswith("qwen_web_") or not name.endswith(".png"):
        return None
    path = (COMFY_OUTPUT / name).resolve()
    if path.parent != COMFY_OUTPUT.resolve():
        return None
    return path


def delete_output_image(filename: str) -> bool:
    path = safe_output_path(filename)
    if path is None or not path.is_file():
        return False
    path.unlink()
    remove_image_meta(path.name)
    return True


def clear_image_prompt(filename: str) -> dict:
    path = safe_output_path(filename)
    if path is None or not path.is_file():
        return {"ok": False}
    with meta_lock:
        meta = load_image_meta()
        stored = meta.get(path.name, {})
        parsed = meta_from_filename(path.name)
        prompt_id = str(stored.get("prompt_id") or parsed.get("prompt_id") or "")
        entry = dict(stored)
        entry["prompt_cleared"] = True
        entry.pop("prompt_id", None)
        entry.pop("prompt_label", None)
        meta[path.name] = entry
        save_image_meta(meta)
    preset_deleted = False
    if prompt_id and prompt_id != "custom":
        preset_deleted = delete_prompt(prompt_id)
    return {
        "ok": True,
        "prompt_id": prompt_id,
        "preset_deleted": preset_deleted,
    }


def list_gallery(
    limit: int = 48,
    only_stale: bool = False,
    preset_slot: str | None = None,
) -> list[dict]:
    meta_db = load_image_meta()
    presets = presets_by_id()
    files = sorted(
        COMFY_OUTPUT.glob("qwen_web_*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    entries = [gallery_entry(f, meta_db, presets) for f in files]
    if only_stale:
        entries = [e for e in entries if e.get("needs_regen")]
    if preset_slot:
        entries = [e for e in entries if e.get("preset_slot") == preset_slot]
    if limit <= 0:
        return entries
    return entries[:limit]


def list_stale_gallery_entries(preset_slot: str | None = None) -> list[dict]:
    return list_gallery(limit=0, only_stale=True, preset_slot=preset_slot)


def collect_preset_slots() -> list[dict]:
    """Unique 5h slots from preset updated_at, newest first."""
    counts: dict[str, dict] = {}
    for p in load_prompt_presets():
        key = p.get("updated_slot") or ""
        if not key:
            continue
        if key not in counts:
            counts[key] = {
                "key": key,
                "label": p.get("updated_slot_label") or key,
                "count": 0,
            }
        counts[key]["count"] += 1
    return sorted(counts.values(), key=lambda x: x["key"], reverse=True)


def resolve_generation_settings(data: dict, model_default: str = "qwen21_pruna") -> dict:
    model = str(data.get("model") or model_default).strip()
    if model not in models_for_ui():
        model = model_default
    presets = models_for_ui()[model]["presets"]
    quality = str(data.get("quality") or "normal").strip()
    preset_q = presets.get(quality, presets["normal"])
    resolution = int(data.get("resolution") or preset_q["resolution"])
    resolution = max(512, min(resolution, 1536))
    steps = int(data.get("steps") or preset_q["steps"])
    if model == "qwen21_pruna":
        steps = PRUNA_STEPS
    else:
        steps = max(4, min(steps, 50))
    return {"model": model, "quality": quality, "resolution": resolution, "steps": steps}


def enqueue_regeneration_from_output(
    filename: str,
    settings: dict,
) -> str:
    path = safe_output_path(filename)
    if path is None or not path.is_file():
        raise ValueError(f"Imagem não encontrada: {filename}")
    meta_db = load_image_meta()
    stored = meta_db.get(path.name, {})
    parsed = meta_from_filename(path.name)
    prompt_id = str(stored.get("prompt_id") or parsed.get("prompt_id") or "")
    if not prompt_id or prompt_id == "custom":
        raise ValueError(f"Sem preset associado: {filename}")
    if stored.get("prompt_cleared"):
        raise ValueError(f"Preset removido desta imagem: {filename}")
    preset = presets_by_id().get(prompt_id)
    if not preset:
        raise ValueError(f"Preset {prompt_id} não existe mais")
    model = settings["model"]
    resolution = settings["resolution"]
    steps = settings["steps"]
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())[:8]
    image1_path = COMFY_INPUT / f"web_{job_id}_main.png"
    shutil.copy(path, image1_path)
    orig = resize_image_for_qwen(image1_path, resolution)
    image1_name = image1_path.with_suffix(".png").name
    out_w, out_h = orig[2], orig[3]
    filename_prefix = make_filename_prefix(prompt_id)
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "model": model,
            "prompt": preset["text"],
            "image1": image1_name,
            "image2": None,
            "steps": steps,
            "resolution": resolution,
            "created_at": time.time(),
            "prompt_preset_id": prompt_id,
            "prompt_preset_label": preset.get("label", ""),
            "filename_prefix": filename_prefix,
            "regenerated_from": path.name,
        }
    run_job(
        job_id,
        model,
        image1_name,
        None,
        preset["text"],
        steps,
        resolution,
        out_w,
        out_h,
        filename_prefix,
        prompt_id,
        preset.get("label", ""),
        regenerated_from=path.name,
    )
    return job_id


def delete_all_output_images() -> int:
    count = 0
    for path in COMFY_OUTPUT.glob("qwen_web_*.png"):
        path.unlink()
        count += 1
    with meta_lock:
        save_image_meta({})
    return count


class Handler(BaseHTTPRequestHandler):
    server_version = "QwenEditor/1.1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def _json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str):
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            body = render_index_html()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/gallery":
            qs = urllib.parse.parse_qs(parsed.query)
            all_images = qs.get("all", ["0"])[0] in ("1", "true", "yes")
            only_stale = qs.get("stale", ["0"])[0] in ("1", "true", "yes")
            preset_slot = (qs.get("slot", [""])[0] or "").strip() or None
            limit = 0 if all_images else 48
            images = list_gallery(
                limit=limit, only_stale=only_stale, preset_slot=preset_slot
            )
            stale_count = len(list_stale_gallery_entries(preset_slot=preset_slot))
            return self._json(
                {
                    "images": images,
                    "stale_count": stale_count,
                    "preset_slot": preset_slot,
                }
            )

        if path == "/api/models":
            return self._json({"models": model_status()})

        if path == "/api/prompts":
            return self._json(
                {
                    "prompts": ordered_prompt_presets(),
                    "time_slots": collect_preset_slots(),
                    "slot_hours": PRESET_SLOT_HOURS,
                    "timezone": "America/Sao_Paulo" if LOCAL_TZ else "local",
                }
            )

        if path.startswith("/api/status/"):
            job_id = path.split("/")[-1]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                return self._json({"error": "Job não encontrado"}, HTTPStatus.NOT_FOUND)
            return self._json(job)

        if path.startswith("/output/"):
            name = Path(path.removeprefix("/output/")).name
            safe = safe_output_path(name)
            if safe is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            return self._file(safe, "image/png")

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/images":
                deleted = delete_all_output_images()
                return self._json({"ok": True, "deleted": deleted})
            if parsed.path.startswith("/api/prompts/"):
                prompt_id = Path(parsed.path.removeprefix("/api/prompts/")).name
                if not prompt_id:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not delete_prompt(prompt_id):
                    return self._json({"error": "Prompt não encontrado"}, HTTPStatus.NOT_FOUND)
                return self._json({"ok": True, "prompts": ordered_prompt_presets()})
            if parsed.path.startswith("/api/image/") and parsed.path.endswith("/prompt"):
                filename = Path(parsed.path.removeprefix("/api/image/").removesuffix("/prompt")).name
                path = safe_output_path(filename)
                result = clear_image_prompt(filename)
                if not result.get("ok") or path is None:
                    return self._json({"error": "Imagem não encontrada"}, HTTPStatus.NOT_FOUND)
                payload = {
                    "ok": True,
                    "filename": filename,
                    "image": gallery_entry(path, load_image_meta()),
                    "prompt_id": result.get("prompt_id") or "",
                    "preset_deleted": bool(result.get("preset_deleted")),
                }
                if result.get("preset_deleted"):
                    payload["prompts"] = ordered_prompt_presets()
                return self._json(payload)
            if not parsed.path.startswith("/api/image/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            filename = Path(parsed.path.removeprefix("/api/image/")).name
            if not delete_output_image(filename):
                return self._json({"error": "Imagem não encontrada"}, HTTPStatus.NOT_FOUND)
            return self._json({"ok": True, "filename": filename})
        except Exception as e:
            return self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith("/api/prompts/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            prompt_id = Path(parsed.path.removeprefix("/api/prompts/")).name
            if not prompt_id:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = read_json_body(self)
            updated = update_prompt(prompt_id, data)
            if not updated:
                return self._json({"error": "Prompt não encontrado"}, HTTPStatus.NOT_FOUND)
            return self._json({"ok": True, "prompt": updated, "prompts": ordered_prompt_presets()})
        except ValueError as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            return self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/prompts":
                data = read_json_body(self)
                created = create_prompt(data)
                return self._json(
                    {"ok": True, "prompt": created, "prompts": ordered_prompt_presets()},
                    HTTPStatus.CREATED,
                )
            if parsed.path == "/api/regenerate-stale":
                data = read_json_body(self)
                settings = resolve_generation_settings(data)
                slot = (data.get("preset_slot") or data.get("slot") or "").strip() or None
                stale = list_stale_gallery_entries(preset_slot=slot)
                one = data.get("filename")
                if one:
                    stale = [e for e in stale if e["filename"] == str(one)]
                if not stale:
                    return self._json(
                        {
                            "ok": True,
                            "queued": 0,
                            "remaining": 0,
                            "message": "Nenhuma imagem com preset desatualizado.",
                        }
                    )
                entry = stale[0]
                try:
                    job_id = enqueue_regeneration_from_output(entry["filename"], settings)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return self._json(
                    {
                        "ok": True,
                        "queued": 1,
                        "remaining": max(0, len(stale) - 1),
                        "job": {
                            "job_id": job_id,
                            "filename": entry["filename"],
                            "prompt_id": entry.get("prompt_id"),
                            "prompt_label": entry.get("prompt_label"),
                        },
                    }
                )
            if parsed.path != "/api/generate":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._json({"error": "Envie multipart/form-data"}, HTTPStatus.BAD_REQUEST)

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
            )

            prompt = form_text(form, "prompt")
            if not prompt:
                return self._json({"error": "Prompt obrigatório"}, HTTPStatus.BAD_REQUEST)

            image1_field = form["image1"] if "image1" in form else None
            if not field_has_upload(image1_field):
                return self._json({"error": "Imagem principal obrigatória"}, HTTPStatus.BAD_REQUEST)

            model = (form.getvalue("model") or "qwen21").strip()
            if model not in models_for_ui():
                return self._json({"error": "Modelo inválido"}, HTTPStatus.BAD_REQUEST)
            if model == "rapid" and not rapid_assets_ready():
                return self._json(
                    {"error": "Modelo Rapid ainda está baixando. Tente de novo em alguns minutos."},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            if model == "qwen21_pruna" and not pruna_assets_ready():
                return self._json(
                    {
                        "error": "LoRA Pruna ainda não está no servidor. Rode download_pruna_lora.py ou desative em pruna_config.json.",
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )

            presets = models_for_ui()[model]["presets"]
            quality = (form.getvalue("quality") or "normal").strip()
            preset = presets.get(quality, presets["normal"])
            resolution = int(form.getvalue("resolution") or preset["resolution"])
            resolution = max(512, min(resolution, 1536))
            steps = int(form.getvalue("steps") or preset["steps"])
            if model == "qwen21_pruna":
                steps = PRUNA_STEPS
            else:
                steps = max(4, min(steps, 50))

            prompt_preset_id = form_text(form, "prompt_preset_id")
            prompt_preset_label = form_text(form, "prompt_preset_label")
            filename_prefix = make_filename_prefix(prompt_preset_id or None)

            COMFY_INPUT.mkdir(parents=True, exist_ok=True)
            job_id = str(uuid.uuid4())[:8]
            image1_name = f"web_{job_id}_main.png"
            image1_path = COMFY_INPUT / image1_name
            with image1_path.open("wb") as f:
                shutil.copyfileobj(image1_field.file, f)
            orig1 = resize_image_for_qwen(image1_path, resolution)
            image1_name = image1_path.with_suffix(".png").name
            out_w, out_h = orig1[2], orig1[3]

            image2_name = None
            resize_info = {"image1": {"from": [orig1[0], orig1[1]], "to": [out_w, out_h]}}
            image2_field = form["image2"] if "image2" in form else None
            if field_has_upload(image2_field):
                image2_name = f"web_{job_id}_ref.png"
                image2_path = COMFY_INPUT / image2_name
                with image2_path.open("wb") as f:
                    shutil.copyfileobj(image2_field.file, f)
                orig2 = resize_image_for_qwen(image2_path, resolution)
                image2_name = image2_path.with_suffix(".png").name
                resize_info["image2"] = {"from": [orig2[0], orig2[1]], "to": [orig2[2], orig2[3]]}

            with jobs_lock:
                jobs[job_id] = {
                    "status": "queued",
                    "model": model,
                    "prompt": prompt,
                    "image1": image1_name,
                    "image2": image2_name,
                    "steps": steps,
                    "resolution": resolution,
                    "resize": resize_info,
                    "created_at": time.time(),
                    "prompt_preset_id": prompt_preset_id or "custom",
                    "prompt_preset_label": prompt_preset_label,
                    "filename_prefix": filename_prefix,
                }

            run_job(
                job_id,
                model,
                image1_name,
                image2_name,
                prompt,
                steps,
                resolution,
                out_w,
                out_h,
                filename_prefix,
                prompt_preset_id or "custom",
                prompt_preset_label,
            )
            return self._json({
                "job_id": job_id,
                "status": "queued",
                "model": model,
                "prompt": prompt,
                "resolution": resolution,
                "resize": resize_info,
                "prompt_preset_id": prompt_preset_id or "custom",
            })
        except ValueError as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:
            return self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main():
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    COMFY_OUTPUT.mkdir(parents=True, exist_ok=True)
    with prompts_lock:
        ensure_prompt_timestamps(ensure_prompt_ids(load_raw_prompts()))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Qwen Editor em http://0.0.0.0:{PORT}")
    print(f"ComfyUI API: {COMFY_API}")
    print(f"Rapid ready: {rapid_assets_ready()}")
    print(f"Pruna enabled: {pruna_feature_enabled()}, ready: {pruna_assets_ready()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
