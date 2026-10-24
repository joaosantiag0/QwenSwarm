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
PRESET_REFS_DIR = WEB_ROOT / "preset_refs"
META_FILE = WEB_ROOT / "image_meta.json"
MAX_REFERENCE_IMAGES = 9
QWEN21_REF_EXT_NAMES = (
    "images.image_2",
    "images.image_3",
    "images.image_4",
    "images.image_5_1",
    "images.image_6",
    "images.image_7",
    "images.image_8",
    "images.image_9",
    "images.image_10",
)
QWEN21_REF_LOADER_IDS = ("475", "477", "478", "479", "480", "481", "482", "483", "484")
PRUNA_CONFIG_FILE = WEB_ROOT / "pruna_config.json"
STAGING_DIR = WEB_ROOT / "staging"
POOL_DIR = STAGING_DIR / "pool"
POOL_FILE = STAGING_DIR / "pool.json"
QUEUE_REFS_DIR = STAGING_DIR / "queue_refs"
RESIZED_DIR = STAGING_DIR / "resized"
QUEUE_FILE = WEB_ROOT / "queue.json"
POOL_THUMB_SIZE = 256
QUEUE_MAX_RETRIES = 1
QUEUE_BATCH_LIMIT = 500
POOL_EXTS = (".png", ".jpg", ".jpeg", ".webp")
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
pool_lock = threading.Lock()
queue_lock = threading.Condition()
queue_items: dict[str, dict] = {}
queue_current_batch = ""
queue_paused = False
_worker_thread: threading.Thread | None = None


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


def form_file_list(form, key: str) -> list:
    if key not in form:
        return []
    field = form[key]
    if isinstance(field, list):
        return [f for f in field if field_has_upload(f)]
    return [field] if field_has_upload(field) else []


def safe_preset_id(prompt_id: str) -> str | None:
    pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(prompt_id or ""))
    return pid or None


def preset_ref_dir(prompt_id: str) -> Path:
    pid = safe_preset_id(prompt_id)
    if not pid:
        raise ValueError("ID de preset inválido")
    return PRESET_REFS_DIR / pid


def safe_preset_ref_path(prompt_id: str, filename: str) -> Path | None:
    pid = safe_preset_id(prompt_id)
    if not pid:
        return None
    name = Path(filename).name
    if not re.fullmatch(r"ref_[a-zA-Z0-9_-]+\.(png|jpg|jpeg|webp)", name):
        return None
    path = (preset_ref_dir(pid) / name).resolve()
    if path.parent != preset_ref_dir(pid).resolve():
        return None
    return path


def public_preset_references(prompt_id: str, records: list[dict]) -> list[dict]:
    pid = safe_preset_id(prompt_id)
    if not pid:
        return []
    out = []
    for rec in records or []:
        fn = rec.get("filename")
        if not fn or not safe_preset_ref_path(pid, fn):
            continue
        out.append(
            {
                "id": rec.get("id") or fn,
                "label": str(rec.get("label") or ""),
                "filename": fn,
                "url": f"/preset-refs/{pid}/{fn}",
            }
        )
    return out


def preset_reference_records(prompt_id: str) -> list[dict]:
    with prompts_lock:
        for item in ensure_prompt_ids(load_raw_prompts()):
            if item.get("id") == prompt_id:
                return list(item.get("reference_images") or [])
    return []


def add_preset_reference(prompt_id: str, upload, label: str = "") -> dict:
    if not field_has_upload(upload):
        raise ValueError("Arquivo de imagem obrigatório")
    pid = safe_preset_id(prompt_id)
    if not pid:
        raise ValueError("Preset inválido")
    ref_id = uuid.uuid4().hex[:10]
    dest_dir = preset_ref_dir(pid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(upload.filename or "ref.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    filename = f"ref_{ref_id}{ext}"
    dest = dest_dir / filename
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    record = {"id": ref_id, "filename": filename, "label": label.strip()}
    with prompts_lock:
        items = ensure_prompt_ids(load_raw_prompts())
        found = False
        for i, item in enumerate(items):
            if item.get("id") != prompt_id:
                continue
            refs = list(item.get("reference_images") or [])
            if len(refs) >= MAX_REFERENCE_IMAGES:
                dest.unlink(missing_ok=True)
                raise ValueError(f"Máximo de {MAX_REFERENCE_IMAGES} referências por preset")
            refs.append(record)
            item["reference_images"] = refs
            item["updated_at"] = time.time()
            items[i] = item
            found = True
            break
        if not found:
            dest.unlink(missing_ok=True)
            raise ValueError("Preset não encontrado")
        save_raw_prompts(items)
    return public_preset_references(prompt_id, [record])[0]


def delete_preset_reference(prompt_id: str, ref_id: str) -> bool:
    with prompts_lock:
        items = ensure_prompt_ids(load_raw_prompts())
        for i, item in enumerate(items):
            if item.get("id") != prompt_id:
                continue
            refs = list(item.get("reference_images") or [])
            new_refs = []
            removed = None
            for rec in refs:
                if rec.get("id") == ref_id:
                    removed = rec
                else:
                    new_refs.append(rec)
            if not removed:
                return False
            item["reference_images"] = new_refs
            item["updated_at"] = time.time()
            items[i] = item
            save_raw_prompts(items)
            fn = removed.get("filename")
            if fn:
                path = safe_preset_ref_path(prompt_id, fn)
                if path and path.is_file():
                    path.unlink()
            return True
    return False


def copy_preset_refs_to_comfy(
    prompt_id: str,
    job_id: str,
    resolution: int,
    ref_ids: list[str] | None = None,
) -> tuple[list[str], dict]:
    records = preset_reference_records(prompt_id)
    if ref_ids:
        want = set(ref_ids)
        records = [r for r in records if r.get("id") in want]
    names: list[str] = []
    resize: dict = {}
    for idx, rec in enumerate(records[:MAX_REFERENCE_IMAGES]):
        src = safe_preset_ref_path(prompt_id, rec.get("filename", ""))
        if src is None or not src.is_file():
            continue
        dest = COMFY_INPUT / f"web_{job_id}_pref{idx}.png"
        shutil.copy(src, dest)
        orig = resize_image_for_qwen(dest, resolution)
        name = dest.with_suffix(".png").name
        names.append(name)
        resize[name] = {"from": [orig[0], orig[1]], "to": [orig[2], orig[3]]}
    return names, resize


def stage_uploaded_refs(
    uploads: list,
    job_id: str,
    resolution: int,
    start_index: int = 0,
) -> tuple[list[str], dict]:
    names: list[str] = []
    resize: dict = {}
    for i, field in enumerate(uploads):
        if len(names) + start_index >= MAX_REFERENCE_IMAGES:
            break
        dest = COMFY_INPUT / f"web_{job_id}_ref{start_index + i}.png"
        with dest.open("wb") as f:
            shutil.copyfileobj(field.file, f)
        orig = resize_image_for_qwen(dest, resolution)
        name = dest.with_suffix(".png").name
        names.append(name)
        resize[name] = {"from": [orig[0], orig[1]], "to": [orig[2], orig[3]]}
    return names, resize


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
    ref_images: list[str] | None,
    prompt: str,
    steps: int,
    resolution: int,
    filename_prefix: str = "qwen_web",
    use_pruna: bool = False,
    switch: bool = False,
) -> dict:
    ref_images = [r for r in (ref_images or []) if r][:MAX_REFERENCE_IMAGES]
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
    subgraph_widget_map["switch"] = switch
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
    for idx, img in enumerate(ref_images):
        if idx >= len(QWEN21_REF_LOADER_IDS):
            break
        loader_id = QWEN21_REF_LOADER_IDS[idx]
        api_prompt[loader_id] = {"class_type": "LoadImage", "inputs": {"image": img}}

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
            elif ext in QWEN21_REF_EXT_NAMES:
                ridx = QWEN21_REF_EXT_NAMES.index(ext)
                if ridx < len(ref_images):
                    loader_id = QWEN21_REF_LOADER_IDS[ridx]
                    api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = [loader_id, 0]
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
    ref_images: list[str] | None,
    prompt: str,
    steps: int,
    resolution: int,
    width: int,
    height: int,
    filename_prefix: str = "qwen_web",
    switch: bool = False,
) -> dict:
    refs = [r for r in (ref_images or []) if r]
    if model == "rapid":
        return build_prompt_rapid(
            image1, refs[0] if refs else None, prompt, steps, width, height, filename_prefix
        )
    return build_prompt_qwen21(
        image1,
        refs,
        prompt,
        steps,
        resolution,
        filename_prefix,
        use_pruna=(model == "qwen21_pruna"),
        switch=switch,
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
            time.sleep(1)
            continue
        entry = hist[prompt_id]
        status = entry.get("status", {})
        if not status.get("completed"):
            time.sleep(1)
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
    ref_images: list[str] | None,
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
                ref_images,
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
                "reference_images": public_preset_references(
                    item["id"], item.get("reference_images") or []
                ),
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
    item["reference_images"] = []
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
            updated["reference_images"] = list(item.get("reference_images") or [])
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
    try:
        ref_dir = preset_ref_dir(prompt_id)
        if ref_dir.is_dir():
            shutil.rmtree(ref_dir, ignore_errors=True)
    except ValueError:
        pass
    return True


def gather_job_reference_images(
    form,
    prompt_preset_id: str,
    job_id: str,
    resolution: int,
) -> tuple[list[str], dict]:
    """Preset refs + session uploads (+ legado image2)."""
    resize_info: dict = {}
    ref_names: list[str] = []
    if prompt_preset_id and prompt_preset_id != "custom":
        ids_raw = form_text(form, "preset_ref_ids")
        if "preset_ref_ids" in form or ids_raw:
            ref_ids = [x.strip() for x in ids_raw.split(",") if x.strip()]
        else:
            ref_ids = None
        preset_names, preset_resize = copy_preset_refs_to_comfy(
            prompt_preset_id, job_id, resolution, ref_ids
        )
        ref_names.extend(preset_names)
        resize_info.update(preset_resize)

    uploads = form_file_list(form, "ref_image")
    image2_field = form["image2"] if "image2" in form else None
    if field_has_upload(image2_field):
        uploads = [image2_field] + uploads

    extra_names, extra_resize = stage_uploaded_refs(
        uploads, job_id, resolution, start_index=len(ref_names)
    )
    ref_names.extend(extra_names)
    resize_info.update(extra_resize)
    return ref_names[:MAX_REFERENCE_IMAGES], resize_info


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
    ref_names, _ref_resize = copy_preset_refs_to_comfy(prompt_id, job_id, resolution, None)
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "model": model,
            "prompt": preset["text"],
            "image1": image1_name,
            "ref_images": ref_names,
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
        ref_names,
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


# ---------------------------------------------------------------------------
# Pool de imagens (uploads persistidos em staging/pool)
# ---------------------------------------------------------------------------


def load_pool() -> list[dict]:
    if not POOL_FILE.is_file():
        return []
    try:
        with POOL_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_pool(items: list[dict]) -> None:
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = POOL_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(POOL_FILE)


def safe_pool_id(pool_id: str) -> str | None:
    pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(pool_id or ""))
    return pid or None


def pool_main_path(rec: dict) -> Path | None:
    pid = safe_pool_id(str(rec.get("id") or ""))
    name = str(rec.get("file") or "")
    if not pid or not name or Path(name).name != name:
        return None
    path = (POOL_DIR / name).resolve()
    if path.parent != POOL_DIR.resolve():
        return None
    return path


def pool_thumb_path(pool_id: str) -> Path | None:
    pid = safe_pool_id(pool_id)
    if not pid:
        return None
    path = (POOL_DIR / f"{pid}_thumb.jpg").resolve()
    if path.parent != POOL_DIR.resolve():
        return None
    return path


def public_pool_item(rec: dict) -> dict:
    pid = rec.get("id")
    return {
        "id": pid,
        "name": rec.get("original_name") or "",
        "width": rec.get("width"),
        "height": rec.get("height"),
        "added_at": rec.get("added_at"),
        "added_at_iso": iso_local(float(rec.get("added_at") or 0)),
        "thumb_url": f"/api/pool/{pid}/thumb",
        "file_url": f"/api/pool/{pid}/file",
    }


def make_pool_thumb(src: Path, thumb_path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        im.thumbnail((POOL_THUMB_SIZE, POOL_THUMB_SIZE), Image.Resampling.LANCZOS)
        im.save(thumb_path, format="JPEG", quality=82)
    return w, h


def add_pool_images(uploads: list) -> list[dict]:
    out: list[dict] = []
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    with pool_lock:
        items = load_pool()
        for upload in uploads:
            if not field_has_upload(upload):
                continue
            pool_id = uuid.uuid4().hex[:12]
            ext = Path(upload.filename or "img.png").suffix.lower()
            if ext not in POOL_EXTS:
                ext = ".png"
            main_name = f"{pool_id}_main{ext}"
            dest = POOL_DIR / main_name
            with dest.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            try:
                w, h = make_pool_thumb(dest, POOL_DIR / f"{pool_id}_thumb.jpg")
            except Exception:
                dest.unlink(missing_ok=True)
                continue
            rec = {
                "id": pool_id,
                "file": main_name,
                "original_name": Path(upload.filename or main_name).name,
                "width": w,
                "height": h,
                "added_at": time.time(),
            }
            items.append(rec)
            out.append(rec)
        save_pool(items)
    return out


def save_pool_image_bytes(data_bytes: bytes, filename: str = "image.png") -> dict:
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    pool_id = uuid.uuid4().hex[:12]
    ext = Path(filename).suffix.lower()
    if ext not in POOL_EXTS:
        ext = ".png"
    main_name = f"{pool_id}_main{ext}"
    dest = POOL_DIR / main_name
    dest.write_bytes(data_bytes)
    try:
        w, h = make_pool_thumb(dest, POOL_DIR / f"{pool_id}_thumb.jpg")
    except Exception:
        dest.unlink(missing_ok=True)
        raise ValueError("Arquivo de imagem inválido ou corrompido")
    rec = {
        "id": pool_id,
        "file": main_name,
        "original_name": Path(filename).name,
        "width": w,
        "height": h,
        "added_at": time.time(),
    }
    with pool_lock:
        items = load_pool()
        items.append(rec)
        save_pool(items)
    return rec


def delete_pool_image(pool_id: str) -> bool:
    pid = safe_pool_id(pool_id)
    if not pid:
        return False
    with queue_lock:
        active = any(
            it.get("pool_id") == pid and it.get("status") in ("pending", "running")
            for it in queue_items.values()
        )
    if active:
        raise ValueError("Imagem em uso na fila ativa — cancele a fila antes de removê-la")
    removed = False
    with pool_lock:
        items = load_pool()
        keep: list[dict] = []
        for rec in items:
            if rec.get("id") == pid:
                removed = True
                p = pool_main_path(rec)
                if p and p.is_file():
                    p.unlink()
                continue
            keep.append(rec)
        if removed:
            save_pool(keep)
    tp = pool_thumb_path(pid)
    if tp and tp.is_file():
        tp.unlink()
    return removed


def pool_image_record(pool_id: str) -> dict | None:
    pid = safe_pool_id(pool_id)
    if not pid:
        return None
    with pool_lock:
        for rec in load_pool():
            if rec.get("id") == pid:
                return dict(rec)
    return None


def get_or_create_blank_canvas_pool_id(resolution: int = 1024) -> str:
    """Retorna o pool_id de um canvas neutro vazio para geração text-to-image sem imagem base."""
    from PIL import Image

    pid = f"blank_{resolution}"
    rec = pool_image_record(pid)
    if rec:
        p = pool_main_path(rec)
        if p and p.is_file():
            return pid
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    main_name = f"{pid}_main.png"
    dest = POOL_DIR / main_name
    im = Image.new("RGB", (resolution, resolution), color=(0, 0, 0))
    im.save(dest, format="PNG")
    make_pool_thumb(dest, POOL_DIR / f"{pid}_thumb.jpg")
    rec = {
        "id": pid,
        "file": main_name,
        "original_name": "blank_canvas.png",
        "width": resolution,
        "height": resolution,
        "added_at": time.time(),
        "is_blank": True,
    }
    with pool_lock:
        items = load_pool()
        items = [x for x in items if x.get("id") != pid]
        items.append(rec)
        save_pool(items)
    return pid


def stage_queue_item_uploads(item_id: str, uploads: list) -> list[str]:
    """Refs enviadas na hora do quick ficam em staging/queue_refs/<item_id>/."""
    if not uploads:
        return []
    dest_dir = QUEUE_REFS_DIR / item_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for i, field in enumerate(uploads):
        if len(names) >= MAX_REFERENCE_IMAGES:
            break
        if not field_has_upload(field):
            continue
        ext = Path(field.filename or f"ref{i}.png").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        name = f"ref_{i}{ext}"
        with (dest_dir / name).open("wb") as f:
            shutil.copyfileobj(field.file, f)
        names.append(name)
    return names


def stage_queue_item_bytes(item_id: str, byte_list: list[bytes]) -> list[str]:
    """Salva referências fornecidas em bytes/base64 em staging/queue_refs/<item_id>/."""
    if not byte_list:
        return []
    dest_dir = QUEUE_REFS_DIR / item_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = [f.name for f in dest_dir.iterdir() if f.is_file()]
    start_idx = len(existing)
    names: list[str] = []
    for i, raw in enumerate(byte_list):
        if len(names) + start_idx >= MAX_REFERENCE_IMAGES:
            break
        if not raw:
            continue
        idx = start_idx + len(names)
        name = f"ref_{idx}.png"
        (dest_dir / name).write_bytes(raw)
        names.append(name)
    return names


def stage_queue_item_from_pool_ids(item_id: str, pool_ids: list[str]) -> list[str]:
    """Copia referências do pool de imagens para staging/queue_refs/<item_id>/."""
    if not pool_ids:
        return []
    dest_dir = QUEUE_REFS_DIR / item_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = [f.name for f in dest_dir.iterdir() if f.is_file()]
    start_idx = len(existing)
    names: list[str] = []
    for pid in pool_ids:
        if len(names) + start_idx >= MAX_REFERENCE_IMAGES:
            break
        rec = pool_image_record(pid)
        if not rec:
            continue
        p = pool_main_path(rec)
        if not p or not p.is_file():
            continue
        idx = start_idx + len(names)
        ext = p.suffix.lower() or ".png"
        name = f"ref_{idx}{ext}"
        shutil.copy(p, dest_dir / name)
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# Fila persistente (queue.json) com worker único
# ---------------------------------------------------------------------------


def load_queue_data() -> dict:
    if not QUEUE_FILE.is_file():
        return {"current_batch_id": "", "items": []}
    try:
        with QUEUE_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"current_batch_id": "", "items": []}
        items = data.get("items")
        return {
            "current_batch_id": str(data.get("current_batch_id") or ""),
            "items": items if isinstance(items, list) else [],
        }
    except (json.JSONDecodeError, OSError):
        return {"current_batch_id": "", "items": []}


def save_queue_locked() -> None:
    """Persiste a fila. Deve ser chamado segurando queue_lock."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current_batch_id": queue_current_batch,
        "items": list(queue_items.values()),
    }
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(QUEUE_FILE)


def queue_item_priority(it: dict) -> int:
    try:
        return int(it.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def queue_next_pending() -> dict | None:
    """Próximo item: quick primeiro (por prioridade e inserção), depois batch por prioridade e inserção."""
    quick = None
    quick_priority = None
    batch = None
    batch_priority = None
    for it in queue_items.values():
        if it.get("status") != "pending":
            continue
        if it.get("lane") == "quick":
            pr = queue_item_priority(it)
            if quick is None or quick_priority is None or pr > quick_priority:
                quick = it
                quick_priority = pr
        else:
            pr = queue_item_priority(it)
            if batch is None or batch_priority is None or pr > batch_priority:
                batch = it
                batch_priority = pr
    return quick or batch


def update_queue_item(item_id: str, **fields) -> None:
    with queue_lock:
        it = queue_items.get(item_id)
        if it is None:
            return
        it.update(fields)
        save_queue_locked()
        queue_lock.notify_all()


def worker_lease_items(worker_id: str, count: int = 20) -> list[dict]:
    """Reserva até `count` itens pendentes para um worker satélite (ex: notebook 3060)."""
    leased = []
    now = time.time()
    with queue_lock:
        # Recupera itens com lease expirado (> 45 minutos)
        for it in queue_items.values():
            if it.get("status") == "leased":
                leased_at = float(it.get("leased_at") or 0)
                if now - leased_at > 2700:
                    it["status"] = "pending"
                    it.pop("leased_to", None)
                    it.pop("leased_at", None)

        candidates = [it for it in queue_items.values() if it.get("status") == "pending"]
        candidates.sort(
            key=lambda x: (
                0 if x.get("lane") == "quick" else 1,
                -queue_item_priority(x),
                x.get("order") or 0,
            )
        )

        selected = candidates[:count]
        for it in selected:
            it["status"] = "leased"
            it["leased_to"] = worker_id
            it["leased_at"] = now
            leased.append({
                "id": it["id"],
                "batch_id": it.get("batch_id"),
                "order": it.get("order"),
                "lane": it.get("lane"),
                "pool_id": it.get("pool_id"),
                "prompt": it.get("prompt"),
                "prompt_preset_id": it.get("prompt_preset_id"),
                "prompt_preset_label": it.get("prompt_preset_label"),
                "model": it.get("model") or "qwen21_pruna",
                "resolution": it.get("resolution") or 1024,
                "steps": it.get("steps") or 25,
                "ref_ids": it.get("ref_ids"),
                "extra_refs": it.get("extra_refs"),
                "is_text_to_image": bool(it.get("is_text_to_image")),
            })
        if selected:
            save_queue_locked()
            queue_lock.notify_all()
    return leased


def worker_complete_item(item_id: str, filename: str, finished_at: float | None = None) -> bool:
    """Registra conclusão de um item gerado por um worker satélite."""
    finished = finished_at or time.time()
    with queue_lock:
        it = queue_items.get(item_id)
        if it is None:
            return False
        it["status"] = "done"
        it["images"] = [filename]
        it["finished_at"] = finished
        it.pop("leased_to", None)
        it.pop("leased_at", None)
        sync_queue_meta(it, [filename], finished)
        save_queue_locked()
        queue_lock.notify_all()
    return True


def worker_fail_item(item_id: str, error: str = "") -> bool:
    """Devolve item para pending em caso de erro no worker satélite."""
    with queue_lock:
        it = queue_items.get(item_id)
        if it is None:
            return False
        it["status"] = "pending"
        it["error"] = error or "Worker failure"
        it.pop("leased_to", None)
        it.pop("leased_at", None)
        save_queue_locked()
        queue_lock.notify_all()
    return True


def queue_item_public(it: dict, pool_map: dict) -> dict:
    pool_id = it.get("pool_id") or ""
    rec = pool_map.get(pool_id, {})
    started = it.get("started_at")
    finished = it.get("finished_at")
    elapsed = int((finished or time.time()) - started) if started else 0
    return {
        "id": it.get("id"),
        "batch_id": it.get("batch_id"),
        "order": it.get("order"),
        "priority": queue_item_priority(it),
        "lane": it.get("lane") or "batch",
        "status": it.get("status"),
        "pool_id": pool_id,
        "image_name": rec.get("original_name") or "",
        "thumb_url": f"/api/pool/{pool_id}/thumb" if pool_id and pool_id in pool_map else None,
        "prompt_preset_id": it.get("prompt_preset_id"),
        "prompt_preset_label": it.get("prompt_preset_label"),
        "model": it.get("model"),
        "resolution": it.get("resolution"),
        "prompt_id": it.get("prompt_id"),
        "images": it.get("images") or [],
        "error": it.get("error"),
        "retries": it.get("retries") or 0,
        "elapsed_s": elapsed,
        "created_at": it.get("created_at"),
        "started_at": started,
        "finished_at": finished,
    }


def queue_view(limit: int = 120) -> dict:
    with queue_lock:
        snapshot = [
            dict(it)
            for it in sorted(
                (x for x in queue_items.values() if x.get("batch_id") == queue_current_batch),
                key=lambda x: x.get("order") or 0,
            )
        ]
        all_items = list(queue_items.values())
        summary = {
            "total": len(all_items),
            "pending": sum(1 for x in all_items if x.get("status") == "pending"),
            "running": sum(1 for x in all_items if x.get("status") == "running"),
            "leased": sum(1 for x in all_items if x.get("status") == "leased"),
            "done": sum(1 for x in all_items if x.get("status") == "done"),
            "error": sum(1 for x in all_items if x.get("status") == "error"),
            "cancelled": sum(1 for x in all_items if x.get("status") == "cancelled"),
            "paused": queue_paused,
            "active": any(x.get("status") in ("pending", "running", "leased") for x in all_items),
        }
        batch_id = queue_current_batch
    pool_map = {r["id"]: r for r in load_pool()}
    total = len(snapshot)
    if total > limit:
        running = [x for x in snapshot if x.get("status") in ("running", "leased")]
        pending = [x for x in snapshot if x.get("status") == "pending"]
        finished = [x for x in snapshot if x.get("status") in ("done", "error", "cancelled")]
        take_fin = min(len(finished), max(20, limit // 4))
        head_fin = finished[-take_fin:] if finished else []
        rest = max(0, limit - len(head_fin) - len(running))
        head_pending = pending[:rest]
        window = head_fin + running + head_pending
        window.sort(key=lambda x: x.get("order") or 0)
    else:
        window = snapshot
    summary["items_total"] = total
    summary["window_size"] = len(window)
    summary["truncated"] = total > len(window)
    return {
        "batch_id": batch_id,
        "summary": summary,
        "items": [queue_item_public(it, pool_map) for it in window],
    }


def sync_queue_meta(it: dict, filenames: list[str], finished: float) -> None:
    preset_id = it.get("prompt_preset_id") or "custom"
    snap = finished
    ph = prompt_text_hash(it.get("prompt") or "")
    if preset_id and preset_id != "custom":
        preset = presets_by_id().get(preset_id)
        if preset:
            snap = float(preset.get("updated_at") or finished)
            ph = prompt_text_hash(preset.get("text", ""))
    for fn in filenames:
        record_image_meta(
            fn,
            prompt_preset_id=preset_id,
            prompt_label=it.get("prompt_preset_label") or "",
            job_id=it.get("id") or "",
            created_at=finished,
            preset_updated_at=snap,
            prompt_text_hash_value=ph,
        )


def submit_queue_item(item: dict) -> None:
    """Estagia, monta o workflow e submete ao ComfyUI. Lança em erro (caller trata)."""
    item_id = item["id"]
    with queue_lock:
        it = queue_items.get(item_id)
        if it is None or it.get("status") not in ("pending", "running"):
            raise ValueError("Item não está mais pendente")
        if it.get("prompt_id"):
            return
        it["status"] = "running"
        it["started_at"] = time.time()
        it["error"] = None
        save_queue_locked()
        queue_lock.notify_all()
    rec = pool_image_record(item.get("pool_id") or "")
    if not rec:
        raise ValueError("Imagem do pool não encontrada")
    src = pool_main_path(rec)
    if src is None or not src.is_file():
        raise ValueError("Arquivo da imagem do pool não existe mais")
    resolution = int(item.get("resolution") or 1024)
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    RESIZED_DIR.mkdir(parents=True, exist_ok=True)
    image1_name = f"web_{item_id}_main.png"
    dest = COMFY_INPUT / image1_name
    cache = RESIZED_DIR / f"{safe_pool_id(str(rec.get('id')))}_{resolution}.png"
    if cache.is_file():
        shutil.copy(cache, dest)
        from PIL import Image

        with Image.open(dest) as im:
            out_w, out_h = im.size
    else:
        raw = COMFY_INPUT / f"web_{item_id}_main{src.suffix.lower() or '.png'}"
        shutil.copy(src, raw)
        orig = resize_image_for_qwen(raw, resolution)
        out_w, out_h = orig[2], orig[3]
        if dest.is_file() and not cache.is_file():
            shutil.copy(dest, cache)
    ref_names, _ref_resize = copy_preset_refs_to_comfy(
        item.get("prompt_preset_id") or "",
        item_id,
        resolution,
        item.get("ref_ids"),
    )
    extra_dir = QUEUE_REFS_DIR / item_id
    if extra_dir.is_dir():
        for i, name in enumerate(sorted(item.get("extra_refs") or [])):
            if len(ref_names) >= MAX_REFERENCE_IMAGES:
                break
            xsrc = (extra_dir / name).resolve()
            if xsrc.parent != extra_dir.resolve() or not xsrc.is_file():
                continue
            xdest = COMFY_INPUT / f"web_{item_id}_xref{i}{xsrc.suffix.lower()}"
            shutil.copy(xsrc, xdest)
            resize_image_for_qwen(xdest, resolution)
            ref_names.append(xdest.with_suffix(".png").name)
    filename_prefix = make_filename_prefix(item.get("prompt_preset_id"))
    is_t2i = bool(item.get("is_text_to_image") or str(item.get("pool_id", "")).startswith("blank_"))
    api_prompt = build_prompt(
        item["model"],
        image1_name,
        ref_names,
        item["prompt"],
        int(item.get("steps") or 25),
        resolution,
        out_w,
        out_h,
        filename_prefix,
        switch=is_t2i,
    )
    prompt_id = comfy_post_prompt(api_prompt)
    update_queue_item(item_id, prompt_id=prompt_id)


def finish_queue_item(item: dict) -> None:
    item_id = item["id"]
    prompt_id = item.get("prompt_id")
    if not prompt_id:
        raise ValueError("Item sem prompt_id submetido")
    filenames = comfy_wait(prompt_id)
    finished = time.time()
    it = queue_items.get(item_id)
    if it is not None:
        sync_queue_meta(it, filenames, finished)
    update_queue_item(item_id, status="done", images=filenames, finished_at=finished)


def fail_queue_item(item_id: str, exc: Exception) -> None:
    with queue_lock:
        it = queue_items.get(item_id)
        if it is None or it.get("status") != "running":
            return
        if int(it.get("retries") or 0) < QUEUE_MAX_RETRIES:
            it["retries"] = int(it.get("retries") or 0) + 1
            it["status"] = "pending"
            it["error"] = str(exc)
            it["prompt_id"] = None
            it["started_at"] = None
        else:
            it["status"] = "error"
            it["error"] = str(exc)
            it["finished_at"] = time.time()
        save_queue_locked()
        queue_lock.notify_all()


def queue_worker() -> None:
    """Worker único com prefetch: submete o próximo item enquanto o atual roda,
    eliminando a folga (poll+build+POST) entre execuções do ComfyUI.
    Pausa/cancel só valem para itens ainda não submetidos; no máx. 2 ficam em voo."""
    prefetch_id: str | None = None
    while True:
        item = None
        with queue_lock:
            if prefetch_id:
                it = queue_items.get(prefetch_id)
                if it is not None and it.get("status") == "running" and it.get("prompt_id"):
                    item = it
                prefetch_id = None
            if item is None and not queue_paused:
                item = queue_next_pending()
            if item is None:
                queue_lock.wait(timeout=2.0)
                continue
        try:
            if not item.get("prompt_id"):
                submit_queue_item(item)
            with queue_lock:
                nxt = None if queue_paused else queue_next_pending()
            if nxt is not None and nxt["id"] != item["id"]:
                try:
                    submit_queue_item(nxt)
                    prefetch_id = nxt["id"]
                except Exception as e:
                    fail_queue_item(nxt["id"], e)
            finish_queue_item(item)
        except Exception as e:
            fail_queue_item(item["id"], e)


def ensure_queue_worker() -> None:
    global _worker_thread
    with queue_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            queue_lock.notify_all()
            return
        _worker_thread = threading.Thread(target=queue_worker, name="queue-worker", daemon=True)
        _worker_thread.start()


def create_batch(
    pool_ids_in_order: list[str],
    preset_start_id: str,
    preset_end_id: str | None,
    settings: dict,
    ref_ids: list[str] | None,
    append: bool = False,
    force: bool = False,
) -> dict:
    global queue_current_batch
    with queue_lock:
        active = any(it.get("status") in ("pending", "running") for it in queue_items.values())
        if active and not append:
            raise ValueError("Já existe uma fila ativa. Aguarde terminar ou cancele os pendentes.")
    presets = ordered_prompt_presets()
    ids = [p["id"] for p in presets]
    if preset_start_id not in ids:
        raise ValueError("Preset inicial não encontrado")
    start = ids.index(preset_start_id)
    if preset_end_id:
        if preset_end_id not in ids:
            raise ValueError("Preset final não encontrado")
        end = ids.index(preset_end_id)
        if end < start:
            start, end = end, start
    else:
        end = len(presets) - 1
    selected = presets[start : end + 1]
    if not selected:
        raise ValueError("Nenhum preset no intervalo escolhido")
    total_items = len(pool_ids_in_order) * len(selected)
    if total_items > QUEUE_BATCH_LIMIT and not force:
        raise ValueError(
            f"Lote muito grande: {len(pool_ids_in_order)} imagem(ns) × {len(selected)} prompt(s) "
            f"= {total_items} gerações (limite {QUEUE_BATCH_LIMIT}). Confirme para criar mesmo assim."
        )
    pool_recs: dict[str, dict] = {}
    for pool_id in pool_ids_in_order:
        rec = pool_image_record(pool_id)
        if not rec:
            raise ValueError(f"Imagem do pool não encontrada: {pool_id}")
        pool_recs[pool_id] = rec
    batch_id = uuid.uuid4().hex[:8]
    now = time.time()
    created: list[dict] = []
    with queue_lock:
        if (
            append
            and queue_current_batch
            and any(it.get("batch_id") == queue_current_batch for it in queue_items.values())
        ):
            batch_id = queue_current_batch
        else:
            queue_current_batch = batch_id
        order = 1 + max(
            (
                it.get("order") or 0
                for it in queue_items.values()
                if it.get("batch_id") == batch_id
            ),
            default=0,
        )
        for pool_id in pool_ids_in_order:
            rec = pool_recs[pool_id]
            for preset in selected:
                item = {
                    "id": uuid.uuid4().hex[:8],
                    "batch_id": batch_id,
                    "order": order,
                    "lane": "batch",
                    "status": "pending",
                    "pool_id": pool_id,
                    "prompt": preset["text"],
                    "prompt_preset_id": preset["id"],
                    "prompt_preset_label": preset.get("label") or "",
                    "ref_ids": ref_ids,
                    "extra_refs": [],
                    "model": settings["model"],
                    "quality": settings["quality"],
                    "resolution": settings["resolution"],
                    "steps": settings["steps"],
                    "width": rec.get("width"),
                    "height": rec.get("height"),
                    "prompt_id": None,
                    "images": [],
                    "error": None,
                    "retries": 0,
                    "created_at": now,
                    "started_at": None,
                    "finished_at": None,
                }
                queue_items[item["id"]] = item
                created.append(item)
                order += 1
        save_queue_locked()
        queue_lock.notify_all()
    ensure_queue_worker()
    return {
        "batch_id": batch_id,
        "total": len(created),
        "images": len(pool_ids_in_order),
        "presets": len(selected),
    }


def create_quick_item(
    pool_id: str,
    prompt_text: str,
    preset_id: str | None,
    preset_label: str,
    settings: dict,
    ref_ids: list[str] | None,
    extra_uploads: list,
    priority: int | None = None,
    extra_ref_bytes: list[bytes] | None = None,
    extra_ref_pool_ids: list[str] | None = None,
    is_text_to_image: bool = False,
) -> dict:
    """Item prioritário: roda depois do item em curso, na frente dos pendentes."""
    global queue_current_batch
    rec = pool_image_record(pool_id)
    if not rec:
        raise ValueError(f"Imagem do pool não encontrada: {pool_id}")
    if not (prompt_text or "").strip():
        raise ValueError("Prompt obrigatório")
    item_id = uuid.uuid4().hex[:8]
    extra = stage_queue_item_uploads(item_id, extra_uploads)
    if extra_ref_bytes:
        extra.extend(stage_queue_item_bytes(item_id, extra_ref_bytes))
    if extra_ref_pool_ids:
        extra.extend(stage_queue_item_from_pool_ids(item_id, extra_ref_pool_ids))
    now = time.time()
    with queue_lock:
        batch_id = queue_current_batch
        if not batch_id or not any(
            it.get("batch_id") == batch_id for it in queue_items.values()
        ):
            batch_id = uuid.uuid4().hex[:8]
            queue_current_batch = batch_id
        order = 1 + max(
            (
                it.get("order") or 0
                for it in queue_items.values()
                if it.get("batch_id") == batch_id
            ),
            default=0,
        )
        if priority is None:
            priority = 1 + max(
                (
                    queue_item_priority(it)
                    for it in queue_items.values()
                    if it.get("status") == "pending" and it.get("lane") == "quick"
                ),
                default=0,
            )
        item = {
            "id": item_id,
            "batch_id": batch_id,
            "order": order,
            "priority": priority,
            "lane": "quick",
            "status": "pending",
            "is_text_to_image": is_text_to_image or str(pool_id).startswith("blank_"),
            "pool_id": pool_id,
            "prompt": prompt_text.strip(),
            "prompt_preset_id": preset_id or "custom",
            "prompt_preset_label": preset_label,
            "ref_ids": ref_ids,
            "extra_refs": extra,
            "model": settings["model"],
            "quality": settings["quality"],
            "resolution": settings["resolution"],
            "steps": settings["steps"],
            "width": rec.get("width"),
            "height": rec.get("height"),
            "prompt_id": None,
            "images": [],
            "error": None,
            "retries": 0,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        }
        queue_items[item_id] = item
        save_queue_locked()
        queue_lock.notify_all()
    ensure_queue_worker()
    return dict(item)


def get_edit_job(item_id: str) -> dict | None:
    with queue_lock:
        it = queue_items.get(item_id)
        if it is not None:
            it = dict(it)
    if it is not None:
        started = it.get("started_at")
        finished = it.get("finished_at")
        elapsed = int((finished or time.time()) - started) if started else 0
        status = it.get("status")
        images = it.get("images") or []
        pool_id = it.get("pool_id") or ""
        return {
            "id": it.get("id"),
            "status": status,
            "lane": it.get("lane") or "quick",
            "priority": queue_item_priority(it),
            "pool_id": pool_id,
            "prompt": it.get("prompt"),
            "model": it.get("model"),
            "resolution": it.get("resolution"),
            "error": it.get("error"),
            "elapsed_s": elapsed,
            "created_at": it.get("created_at"),
            "started_at": started,
            "finished_at": finished,
            "ref_count": len(it.get("extra_refs") or []) + len(it.get("ref_ids") or []),
            "images": images,
            "image_url": f"/api/edit/{item_id}/image" if status == "done" and images else None,
            "image_urls": [f"/api/edit/{item_id}/image?idx={i}" if len(images) > 1 else f"/api/edit/{item_id}/image" for i in range(len(images))] if status == "done" and images else [],
        }

    meta = load_image_meta()
    found = [fn for fn, m in meta.items() if m.get("job_id") == item_id]
    if found:
        return {
            "id": item_id,
            "status": "done",
            "lane": "quick",
            "priority": 0,
            "pool_id": "",
            "prompt": "",
            "model": "",
            "resolution": 0,
            "error": None,
            "elapsed_s": 0,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "images": found,
            "image_url": f"/api/edit/{item_id}/image",
            "image_urls": [f"/api/edit/{item_id}/image?idx={i}" if len(found) > 1 else f"/api/edit/{item_id}/image" for i in range(len(found))],
        }
    return None


def delete_edit_job(item_id: str) -> dict:
    found = False
    deleted_images = []
    with queue_lock:
        it = queue_items.get(item_id)
        if it is not None:
            found = True
            status = it.get("status")
            if status == "pending":
                it["status"] = "cancelled"
                it["finished_at"] = time.time()
                queue_items.pop(item_id, None)
                save_queue_locked()
                queue_lock.notify_all()
            elif status == "running":
                it["status"] = "cancelled"
                save_queue_locked()
                queue_lock.notify_all()
            else:
                images = list(it.get("images") or [])
                queue_items.pop(item_id, None)
                save_queue_locked()
                queue_lock.notify_all()
                for fn in images:
                    if delete_output_image(fn):
                        deleted_images.append(fn)

    ref_dir = QUEUE_REFS_DIR / item_id
    if ref_dir.is_dir():
        shutil.rmtree(ref_dir, ignore_errors=True)

    meta = load_image_meta()
    for fn, m in list(meta.items()):
        if m.get("job_id") == item_id:
            found = True
            if delete_output_image(fn):
                deleted_images.append(fn)

    if not found:
        return {"ok": False, "error": "Item não encontrado", "id": item_id}
    return {"ok": True, "deleted": True, "id": item_id, "deleted_images": deleted_images}


def enqueue_preset_for_all_pool(settings: dict, preset_id: str) -> dict:
    """Ao salvar um prompt: enfileira ele para todas as imagens do pool (append)."""
    with pool_lock:
        pool = load_pool()
    pool_ids = [r["id"] for r in pool]
    if not pool_ids:
        return {"queued": 0, "message": "Prompt salvo. Pool vazio — nada a gerar."}
    if len(pool_ids) > QUEUE_BATCH_LIMIT:
        return {
            "queued": 0,
            "message": (
                f"Prompt salvo. Pool com {len(pool_ids)} imagens — geração automática "
                f"cancelada (limite {QUEUE_BATCH_LIMIT}). Selecione as desejadas e use a fila."
            ),
        }
    if not model_available(settings["model"]):
        return {"queued": 0, "message": "Prompt salvo. Modelo indisponível — nada a gerar."}
    info = create_batch(pool_ids, preset_id, preset_id, settings, None, append=True)
    return {
        "queued": info["total"],
        "batch_id": info["batch_id"],
        "message": f"Salvo e enfileirado: {info['total']} imagem(ns) × este prompt.",
    }


def generation_settings_from_mapping(data: dict) -> dict:
    return resolve_generation_settings(
        {
            "model": data.get("model") or "",
            "quality": data.get("quality") or "",
            "resolution": data.get("resolution") or "",
            "steps": data.get("steps") or "",
        }
    )


def cancel_pending_items() -> int:
    with queue_lock:
        n = 0
        for it in queue_items.values():
            if it.get("status") == "pending":
                it["status"] = "cancelled"
                it["finished_at"] = time.time()
                n += 1
        save_queue_locked()
        queue_lock.notify_all()
    return n


def clear_finished_items() -> int:
    with queue_lock:
        drop = [
            k
            for k, it in queue_items.items()
            if it.get("status") in ("done", "error", "cancelled")
        ]
        for k in drop:
            del queue_items[k]
        save_queue_locked()
        queue_lock.notify_all()
    return len(drop)


def comfy_completed_images(prompt_id: str) -> list[str] | None:
    """Imagens de um prompt concluído no ComfyUI, ou None se não concluído/desconhecido."""
    with urllib.request.urlopen(f"{COMFY_API}/history/{prompt_id}", timeout=15) as resp:
        hist = json.loads(resp.read())
    entry = hist.get(prompt_id)
    if not entry:
        return None
    status = entry.get("status", {})
    if not status.get("completed"):
        return None
    for msg in status.get("messages", []):
        if msg[0] == "execution_error":
            detail = msg[1] if len(msg) > 1 else {}
            raise RuntimeError(detail.get("exception_message", str(detail)))
    images = []
    for node_out in entry.get("outputs", {}).values():
        for img in node_out.get("images", []):
            images.append(img["filename"])
    return images


def recover_queue_on_boot() -> None:
    """Recarrega queue.json e reconcilia itens 'running' com o histórico do ComfyUI."""
    global queue_current_batch
    data = load_queue_data()
    queue_items.clear()
    for it in data["items"]:
        if isinstance(it, dict) and it.get("id"):
            queue_items[str(it["id"])] = it
    queue_current_batch = str(data.get("current_batch_id") or "")
    recovered = 0
    for it in list(queue_items.values()):
        if it.get("status") != "running":
            continue
        prompt_id = it.get("prompt_id")
        images: list[str] | None = None
        if prompt_id:
            try:
                images = comfy_completed_images(prompt_id)
            except Exception:
                images = None
        if images is not None:
            finished = time.time()
            it["status"] = "done"
            it["images"] = images
            it["finished_at"] = finished
            sync_queue_meta(it, images, finished)
            recovered += 1
        else:
            it["status"] = "pending"
            it["prompt_id"] = None
            it["started_at"] = None
    with queue_lock:
        save_queue_locked()
        queue_lock.notify_all()
    if recovered:
        print(f"Fila: {recovered} item(ns) concluído(s) recuperado(s) do ComfyUI")


class Handler(BaseHTTPRequestHandler):
    server_version = "QwenEditor/1.1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def _json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

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

        if path == "/api/pool":
            with pool_lock:
                items = sorted(
                    load_pool(),
                    key=lambda r: float(r.get("added_at") or 0),
                    reverse=True,
                )
            return self._json({"images": [public_pool_item(r) for r in items]})

        if path == "/api/queue":
            return self._json(queue_view())

        if path.startswith("/api/pool/"):
            rest = path.removeprefix("/api/pool/").strip("/")
            parts = rest.split("/", 1)
            if len(parts) != 2:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            pid, kind = parts[0], parts[1]
            if kind == "thumb":
                file_path = pool_thumb_path(pid)
                ctype = "image/jpeg"
            elif kind == "file":
                rec = pool_image_record(pid)
                file_path = pool_main_path(rec) if rec else None
                name = str((rec or {}).get("file") or "").lower()
                ctype = "image/png"
                if name.endswith((".jpg", ".jpeg")):
                    ctype = "image/jpeg"
                elif name.endswith(".webp"):
                    ctype = "image/webp"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if file_path is None or not file_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            return self._file(file_path, ctype)

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

        if path.startswith("/preset-refs/"):
            rest = path.removeprefix("/preset-refs/").strip("/")
            parts = rest.split("/", 1)
            if len(parts) != 2:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            pid, fn = parts[0], parts[1]
            ref_path = safe_preset_ref_path(pid, fn)
            if ref_path is None or not ref_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            ctype = "image/png"
            if fn.lower().endswith((".jpg", ".jpeg")):
                ctype = "image/jpeg"
            elif fn.lower().endswith(".webp"):
                ctype = "image/webp"
            return self._file(ref_path, ctype)

        if path in ("/api/edit", "/api/edit/images"):
            qs = urllib.parse.parse_qs(parsed.query)
            raw_ids = qs.get("ids", [""])[0]
            if raw_ids:
                ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
                results = [get_edit_job(i) or {"id": i, "status": "not_found"} for i in ids]
                return self._json({"items": results})
            return self._json({"error": "Informe ?ids=id1,id2"}, HTTPStatus.BAD_REQUEST)

        if path.startswith("/api/edit/"):
            rest = path.removeprefix("/api/edit/").strip("/")
            parts = rest.split("/")
            if len(parts) == 2 and parts[1] == "image":
                item_id = parts[0]
                job = get_edit_job(item_id)
                if not job:
                    return self._json({"error": "Item não encontrado", "id": item_id}, HTTPStatus.NOT_FOUND)
                if job.get("status") != "done":
                    return self._json(
                        {"error": "Imagem ainda não está pronta", "id": item_id, "status": job.get("status")},
                        HTTPStatus.CONFLICT,
                    )
                images = job.get("images") or []
                if not images:
                    return self._json({"error": "Nenhuma imagem gerada para este item", "id": item_id}, HTTPStatus.NOT_FOUND)
                qs = urllib.parse.parse_qs(parsed.query)
                idx = 0
                try:
                    idx = int(qs.get("idx", ["0"])[0])
                except (ValueError, TypeError):
                    idx = 0
                if idx < 0 or idx >= len(images):
                    idx = 0
                fn = images[idx]
                safe = safe_output_path(fn)
                if safe is None or not safe.is_file():
                    return self._json({"error": "Arquivo não encontrado no servidor", "filename": fn}, HTTPStatus.NOT_FOUND)
                ctype = "image/png"
                if fn.lower().endswith((".jpg", ".jpeg")):
                    ctype = "image/jpeg"
                elif fn.lower().endswith(".webp"):
                    ctype = "image/webp"
                return self._file(safe, ctype)
            elif len(parts) == 1 and parts[0]:
                item_id = parts[0]
                job = get_edit_job(item_id)
                if not job:
                    return self._json({"error": "Item não encontrado", "id": item_id}, HTTPStatus.NOT_FOUND)
                return self._json(job)

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/api/edit", "/api/edit/"):
                qs = urllib.parse.parse_qs(parsed.query)
                raw_ids = qs.get("ids", [""])[0]
                ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
                if not ids:
                    try:
                        data = read_json_body(self)
                        ids = [str(x).strip() for x in (data.get("ids") or []) if str(x).strip()]
                    except Exception:
                        pass
                if not ids:
                    return self._json({"error": "Informe ?ids=id1,id2 ou body com {'ids': [...]}"}, HTTPStatus.BAD_REQUEST)
                results = [delete_edit_job(i) for i in ids]
                return self._json({"ok": True, "results": results})

            if parsed.path.startswith("/api/edit/"):
                item_id = Path(parsed.path.removeprefix("/api/edit/")).name
                if not item_id:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                res = delete_edit_job(item_id)
                status = HTTPStatus.OK if res.get("ok") else HTTPStatus.NOT_FOUND
                return self._json(res, status)
            if parsed.path == "/api/queue":
                removed = clear_finished_items()
                return self._json({"ok": True, "removed": removed, "queue": queue_view()})
            if parsed.path.startswith("/api/pool/"):
                pid = Path(parsed.path.removeprefix("/api/pool/")).name
                if not pid:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    removed = delete_pool_image(pid)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                if not removed:
                    return self._json({"error": "Imagem não encontrada"}, HTTPStatus.NOT_FOUND)
                return self._json({"ok": True, "id": pid})
            if parsed.path == "/api/images":
                deleted = delete_all_output_images()
                return self._json({"ok": True, "deleted": deleted})
            if parsed.path.startswith("/api/prompts/") and "/references/" in parsed.path:
                rest = parsed.path.removeprefix("/api/prompts/").strip("/")
                prompt_id, _, ref_id = rest.partition("/references/")
                if not prompt_id or not ref_id:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not delete_preset_reference(prompt_id, ref_id):
                    return self._json({"error": "Referência não encontrada"}, HTTPStatus.NOT_FOUND)
                return self._json({"ok": True, "prompts": ordered_prompt_presets()})
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
            gen = enqueue_preset_for_all_pool(
                generation_settings_from_mapping(data), prompt_id
            )
            return self._json(
                {
                    "ok": True,
                    "prompt": updated,
                    "prompts": ordered_prompt_presets(),
                    "generated": gen,
                }
            )
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
                gen = enqueue_preset_for_all_pool(
                    generation_settings_from_mapping(data), created["id"]
                )
                return self._json(
                    {
                        "ok": True,
                        "prompt": created,
                        "prompts": ordered_prompt_presets(),
                        "generated": gen,
                    },
                    HTTPStatus.CREATED,
                )
            if parsed.path.startswith("/api/prompts/") and parsed.path.endswith("/references"):
                prompt_id = Path(
                    parsed.path.removeprefix("/api/prompts/").removesuffix("/references")
                ).name
                if not prompt_id:
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
                upload = form["file"] if "file" in form else None
                label = form_text(form, "label")
                try:
                    ref = add_preset_reference(prompt_id, upload, label)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return self._json(
                    {"ok": True, "reference": ref, "prompts": ordered_prompt_presets()},
                    HTTPStatus.CREATED,
                )
            if parsed.path == "/api/pool":
                ctype_pool = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype_pool:
                    return self._json({"error": "Envie multipart/form-data"}, HTTPStatus.BAD_REQUEST)
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype_pool},
                )
                uploads = form_file_list(form, "images") or form_file_list(form, "files")
                if not uploads:
                    return self._json({"error": "Nenhuma imagem enviada"}, HTTPStatus.BAD_REQUEST)
                added = add_pool_images(uploads)
                if not added:
                    return self._json(
                        {"error": "Nenhuma imagem válida (aceita png/jpg/webp)"},
                        HTTPStatus.BAD_REQUEST,
                    )
                return self._json(
                    {"ok": True, "items": [public_pool_item(r) for r in added]},
                    HTTPStatus.CREATED,
                )

            if parsed.path == "/api/batch":
                ctype_batch = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype_batch:
                    return self._json({"error": "Envie multipart/form-data"}, HTTPStatus.BAD_REQUEST)
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype_batch},
                )
                settings = resolve_generation_settings(
                    {
                        "model": form_text(form, "model") or "qwen21_pruna",
                        "quality": form_text(form, "quality") or "normal",
                        "resolution": form_text(form, "resolution") or "",
                        "steps": form_text(form, "steps") or "",
                    }
                )
                if not model_available(settings["model"]):
                    return self._json(
                        {"error": "Modelo indisponível no momento (download em andamento?)"},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                uploads = form_file_list(form, "images")
                added = add_pool_images(uploads) if uploads else []
                new_ids = [r["id"] for r in added]
                listed = [x.strip() for x in form_text(form, "pool_ids").split(",") if x.strip()]
                seen: set[str] = set()
                ordered_ids: list[str] = []
                for pid in new_ids + listed:
                    if pid in seen:
                        continue
                    seen.add(pid)
                    ordered_ids.append(pid)
                if not ordered_ids:
                    return self._json(
                        {"error": "Selecione ao menos uma imagem"}, HTTPStatus.BAD_REQUEST
                    )
                preset_start_id = form_text(form, "preset_start_id")
                if not preset_start_id:
                    return self._json(
                        {"error": "preset_start_id obrigatório"}, HTTPStatus.BAD_REQUEST
                    )
                preset_end_id = form_text(form, "preset_end_id") or None
                ref_raw = form_text(form, "preset_ref_ids")
                ref_ids = [x.strip() for x in ref_raw.split(",") if x.strip()] or None
                force = form_text(form, "force").lower() in ("1", "true", "yes")
                try:
                    info = create_batch(
                        ordered_ids,
                        preset_start_id,
                        preset_end_id,
                        settings,
                        ref_ids,
                        force=force,
                    )
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return self._json({"ok": True, **info}, HTTPStatus.CREATED)

            if parsed.path == "/api/quick":
                ctype_q = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype_q:
                    return self._json({"error": "Envie multipart/form-data"}, HTTPStatus.BAD_REQUEST)
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype_q},
                )
                prompt = form_text(form, "prompt")
                if not prompt:
                    return self._json({"error": "Prompt obrigatório"}, HTTPStatus.BAD_REQUEST)
                settings = resolve_generation_settings(
                    {
                        "model": form_text(form, "model") or "qwen21_pruna",
                        "quality": form_text(form, "quality") or "normal",
                        "resolution": form_text(form, "resolution") or "",
                        "steps": form_text(form, "steps") or "",
                    }
                )
                if not model_available(settings["model"]):
                    return self._json(
                        {"error": "Modelo indisponível no momento"},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                pool_id = form_text(form, "image_pool_id")
                if not pool_id:
                    uploads_main = form_file_list(form, "image1")
                    if not uploads_main:
                        return self._json(
                            {"error": "Selecione uma imagem no pool"}, HTTPStatus.BAD_REQUEST
                        )
                    added = add_pool_images(uploads_main[:1])
                    if not added:
                        return self._json({"error": "Imagem inválida"}, HTTPStatus.BAD_REQUEST)
                    pool_id = added[0]["id"]
                ref_raw = form_text(form, "preset_ref_ids")
                ref_ids = [x.strip() for x in ref_raw.split(",") if x.strip()] or None
                extra = form_file_list(form, "ref_image")
                preset_id = form_text(form, "prompt_preset_id")
                try:
                    item = create_quick_item(
                        pool_id,
                        prompt,
                        preset_id or None,
                        form_text(form, "prompt_preset_label"),
                        settings,
                        ref_ids,
                        extra,
                    )
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return self._json(
                    {"ok": True, "item_id": item["id"], "queue": queue_view()},
                    HTTPStatus.CREATED,
                )

            if parsed.path == "/api/edit":
                ctype = self.headers.get("Content-Type", "")
                prompt = ""
                pool_id = ""
                extra = []
                ref_bytes = []
                ref_pool_ids = None
                settings_in = {}
                ref_ids = None

                if "multipart/form-data" in ctype:
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
                    )
                    prompt = form_text(form, "prompt")
                    pool_id = form_text(form, "pool_id") or form_text(form, "image_pool_id")
                    uploads = form_file_list(form, "image") or form_file_list(form, "image1")
                    extra = (
                        form_file_list(form, "ref_image")
                        or form_file_list(form, "ref_images")
                        or form_file_list(form, "reference")
                        or form_file_list(form, "references")
                    )
                    if uploads:
                        added = add_pool_images(uploads[:1])
                        if added:
                            pool_id = added[0]["id"]
                    elif extra and not pool_id:
                        # Se não enviou imagem principal, a primeira referência se torna a imagem base!
                        first_ref = extra.pop(0)
                        added = add_pool_images([first_ref])
                        if added:
                            pool_id = added[0]["id"]

                    ref_pool_raw = form_text(form, "ref_pool_ids") or form_text(form, "reference_pool_ids")
                    if ref_pool_raw:
                        ref_pool_ids = [x.strip() for x in ref_pool_raw.split(",") if x.strip()] or None
                        if ref_pool_ids and not pool_id:
                            pool_id = ref_pool_ids.pop(0)
                    ref_raw = form_text(form, "preset_ref_ids")
                    if ref_raw:
                        ref_ids = [x.strip() for x in ref_raw.split(",") if x.strip()] or None
                    settings_in = {
                        "model": form_text(form, "model") or "qwen21_pruna",
                        "quality": form_text(form, "quality") or "normal",
                        "resolution": form_text(form, "resolution") or "",
                        "steps": form_text(form, "steps") or "",
                    }
                else:
                    data = read_json_body(self)
                    prompt = str(data.get("prompt") or "").strip()
                    img_b64 = data.get("image_base64") or data.get("image")
                    if img_b64 and isinstance(img_b64, str) and (img_b64.startswith("data:image/") or len(img_b64) > 100):
                        import base64

                        if "," in img_b64:
                            img_b64 = img_b64.split(",", 1)[1]
                        try:
                            raw_bytes = base64.b64decode(img_b64)
                            rec = save_pool_image_bytes(raw_bytes, data.get("image_name") or "edit_upload.png")
                            pool_id = rec["id"]
                        except Exception as e:
                            return self._json({"error": f"Base64 inválido: {e}"}, HTTPStatus.BAD_REQUEST)
                    else:
                        pool_id = str(data.get("pool_id") or data.get("image_pool_id") or "").strip()

                    raw_refs = data.get("references") or data.get("references_base64") or data.get("ref_images")
                    if isinstance(raw_refs, str):
                        raw_refs = [raw_refs]
                    if isinstance(raw_refs, list):
                        import base64

                        for r_b64 in raw_refs:
                            if not isinstance(r_b64, str):
                                continue
                            if "," in r_b64:
                                r_b64 = r_b64.split(",", 1)[1]
                            try:
                                ref_bytes.append(base64.b64decode(r_b64))
                            except Exception:
                                pass

                    raw_ref_pools = data.get("ref_pool_ids") or data.get("reference_pool_ids")
                    if isinstance(raw_ref_pools, list):
                        ref_pool_ids = [str(x).strip() for x in raw_ref_pools if str(x).strip()] or None
                    elif isinstance(raw_ref_pools, str) and raw_ref_pools.strip():
                        ref_pool_ids = [x.strip() for x in raw_ref_pools.split(",") if x.strip()] or None

                    if not pool_id:
                        if ref_bytes:
                            # Se não passou imagem principal, a primeira referência vira a imagem base!
                            first_bytes = ref_bytes.pop(0)
                            rec = save_pool_image_bytes(first_bytes, "edit_main_from_ref.png")
                            pool_id = rec["id"]
                        elif ref_pool_ids:
                            pool_id = ref_pool_ids.pop(0)

                    ref_raw = data.get("preset_ref_ids")
                    if isinstance(ref_raw, list):
                        ref_ids = [str(x).strip() for x in ref_raw if str(x).strip()] or None
                    elif isinstance(ref_raw, str) and ref_raw.strip():
                        ref_ids = [x.strip() for x in ref_raw.split(",") if x.strip()] or None
                    settings_in = {
                        "model": data.get("model") or "qwen21_pruna",
                        "quality": data.get("quality") or "normal",
                        "resolution": data.get("resolution") or "",
                        "steps": data.get("steps") or "",
                    }

                if not prompt:
                    return self._json({"error": "Prompt obrigatório (campo 'prompt')"}, HTTPStatus.BAD_REQUEST)

                settings = resolve_generation_settings(settings_in)
                if not model_available(settings["model"]):
                    return self._json(
                        {"error": "Modelo indisponível no momento"},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )

                is_text_to_image = False
                if not pool_id:
                    # Geração a partir do texto puro: NUNCA usa imagens da fila ou pool!
                    # Cria um canvas neutro e aciona geração pura via EmptyLatentImage.
                    res_val = int(settings.get("resolution") or 1024)
                    pool_id = get_or_create_blank_canvas_pool_id(res_val)
                    is_text_to_image = True

                try:
                    item = create_quick_item(
                        pool_id=pool_id,
                        prompt_text=prompt,
                        preset_id=None,
                        preset_label="API Edit" if not is_text_to_image else "API Text2Img",
                        settings=settings,
                        ref_ids=ref_ids,
                        extra_uploads=extra,
                        extra_ref_bytes=ref_bytes if ref_bytes else None,
                        extra_ref_pool_ids=ref_pool_ids,
                        is_text_to_image=is_text_to_image,
                    )
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

                return self._json(
                    {
                        "ok": True,
                        "id": item["id"],
                        "status": item["status"],
                        "lane": item.get("lane"),
                        "priority": queue_item_priority(item),
                        "mode": "text_to_image" if is_text_to_image else "image_edit",
                        "references_count": len(item.get("extra_refs") or []) + len(item.get("ref_ids") or []),
                        "image_url": f"/api/edit/{item['id']}/image",
                        "status_url": f"/api/edit/{item['id']}",
                        "message": "Item enfileirado com sucesso como próximo da fila",
                    },
                    HTTPStatus.CREATED,
                )

            if parsed.path == "/api/worker/lease":
                data = read_json_body(self)
                worker_id = str(data.get("worker_id") or "satellite_worker").strip()
                try:
                    count = min(100, max(1, int(data.get("count") or 20)))
                except (TypeError, ValueError):
                    count = 20
                items = worker_lease_items(worker_id, count)
                return self._json({"ok": True, "worker_id": worker_id, "count": len(items), "items": items})

            if parsed.path == "/api/worker/complete":
                ctype_wc = self.headers.get("Content-Type", "")
                if "multipart/form-data" in ctype_wc:
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype_wc},
                    )
                    item_id = form_text(form, "item_id")
                    if not item_id:
                        return self._json({"error": "item_id obrigatório"}, HTTPStatus.BAD_REQUEST)
                    image_field = form["image"] if "image" in form else None
                    if not field_has_upload(image_field):
                        return self._json({"error": "image obrigatória"}, HTTPStatus.BAD_REQUEST)
                    orig_name = Path(image_field.filename or f"qwen_worker_{item_id}.png").name
                    fn = form_text(form, "filename") or orig_name
                    dest = COMFY_OUTPUT / fn
                    with dest.open("wb") as f:
                        shutil.copyfileobj(image_field.file, f)
                    try:
                        finished_at = float(form_text(form, "finished_at") or time.time())
                    except (ValueError, TypeError):
                        finished_at = time.time()
                    ok = worker_complete_item(item_id, fn, finished_at)
                    return self._json({"ok": ok, "id": item_id, "filename": fn})
                else:
                    return self._json({"error": "Envie multipart/form-data com o arquivo da imagem"}, HTTPStatus.BAD_REQUEST)

            if parsed.path == "/api/worker/fail":
                data = read_json_body(self)
                item_id = str(data.get("item_id") or "").strip()
                error = str(data.get("error") or "")
                ok = worker_fail_item(item_id, error)
                return self._json({"ok": ok, "id": item_id})

            if parsed.path == "/api/queue/prioritize":
                data = read_json_body(self)
                prefixes = [str(x).strip().lower() for x in (data.get("prefixes") or []) if str(x).strip()]
                try:
                    prio = int(data.get("priority") or 1)
                except (TypeError, ValueError):
                    prio = 1
                clear = bool(data.get("clear"))
                pool_map = {r["id"]: r for r in load_pool()}
                updated = 0
                with queue_lock:
                    for it in queue_items.values():
                        if it.get("status") != "pending":
                            continue
                        rec = pool_map.get(it.get("pool_id") or "") or {}
                        name = (rec.get("original_name") or "").lower()
                        if not prefixes or any(p in name for p in prefixes):
                            if clear:
                                it.pop("priority", None)
                            else:
                                it["priority"] = prio
                            updated += 1
                    save_queue_locked()
                    queue_lock.notify_all()
                return self._json(
                    {"ok": True, "updated": updated, "priority": None if clear else prio, "queue": queue_view()}
                )

            if parsed.path in ("/api/queue/pause", "/api/queue/resume", "/api/queue/cancel"):
                global queue_paused
                with queue_lock:
                    if parsed.path == "/api/queue/pause":
                        queue_paused = True
                    elif parsed.path == "/api/queue/resume":
                        queue_paused = False
                    else:
                        for it in queue_items.values():
                            if it.get("status") == "pending":
                                it["status"] = "cancelled"
                                it["finished_at"] = time.time()
                    save_queue_locked()
                    queue_lock.notify_all()
                return self._json({"ok": True, "paused": queue_paused, "queue": queue_view()})

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

            resize_info = {"image1": {"from": [orig1[0], orig1[1]], "to": [out_w, out_h]}}
            ref_names, ref_resize = gather_job_reference_images(
                form, prompt_preset_id or "", job_id, resolution
            )
            if ref_resize:
                resize_info["references"] = ref_resize

            with jobs_lock:
                jobs[job_id] = {
                    "status": "queued",
                    "model": model,
                    "prompt": prompt,
                    "image1": image1_name,
                    "ref_images": ref_names,
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
                ref_names,
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
    PRESET_REFS_DIR.mkdir(parents=True, exist_ok=True)
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    RESIZED_DIR.mkdir(parents=True, exist_ok=True)
    with prompts_lock:
        ensure_prompt_timestamps(ensure_prompt_ids(load_raw_prompts()))
    recover_queue_on_boot()
    ensure_queue_worker()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Qwen Editor em http://0.0.0.0:{PORT}")
    print(f"ComfyUI API: {COMFY_API}")
    print(f"Rapid ready: {rapid_assets_ready()}")
    print(f"Pruna enabled: {pruna_feature_enabled()}, ready: {pruna_assets_ready()}")
    active = sum(1 for it in queue_items.values() if it.get("status") in ("pending", "running"))
    print(f"Fila: {len(queue_items)} item(ns) no total, {active} pendendo/rodando")
    server.serve_forever()


if __name__ == "__main__":
    main()
