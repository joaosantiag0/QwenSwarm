#!/usr/bin/env python3
"""Web UI for FastVideo FastH3 Trim 8-step T2V (+ áudio) via ComfyUI."""

from __future__ import annotations

import cgi
import copy
import json
import mimetypes
import random
import shutil
import threading
import time
import uuid
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMFY_API = "http://127.0.0.1:8188"
COMFY_INPUT = Path("/home/user/ComfyUI/input")
COMFY_OUTPUT = Path("/home/user/ComfyUI/output")
COMFY_MODELS = Path("/home/user/ComfyUI/models")
WEB_ROOT = Path(__file__).resolve().parent
WORKFLOWS = WEB_ROOT / "workflows"
PORT = 3002
HF_REPO = "FastVideo/FastVideo-FastH3-8-Step-V2"
HF_REPO_COMFY = "FastVideo/FastVideo-FastH3-Comfy"
VIDEO_PREFIXES = ("h3_trim_video", "h3_web_video", "h3_kijai_video")

# UNet: repack Comfy do FastH3 8-Step V2 (Trim ainda sem .safetensors único no HF).
MODEL_FILES = {
    "unet": "diffusion_models/fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
    "clip": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "vae_video": "vae/minimax_h3_video_vae_int8_convrot.safetensors",
    "vae_audio": "vae/minimax_h3_audio_vae_fp32.safetensors",
}

# 16:9 @ ~0.4 MP (leve na 3060) e nativo 768p
SIZE_PRESETS = {
    "preview": {"label": "16:9 preview (864×480)", "width": 864, "height": 480},
    "native": {"label": "16:9 nativo (1344×768)", "width": 1344, "height": 768},
    "square": {"label": "1:1 (768×768)", "width": 768, "height": 768},
}

MODES = {
    "t2v": {
        "label": "FastH3 Trim — texto → vídeo + áudio (8 passos)",
        "workflow": "H3_T2V_API.json",
        "needs_image": False,
    },
}

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def model_path(rel: str) -> Path:
    return COMFY_MODELS / rel


def _missing(files: dict) -> list[str]:
    return [rel for rel in files.values() if not model_path(rel).is_file()]


def assets_status() -> dict:
    miss = _missing(MODEL_FILES)
    return {
        "ready": not miss,
        "missing": miss,
        "hf_repo": HF_REPO,
        "hf_url": f"https://huggingface.co/{HF_REPO}",
        "hf_comfy": HF_REPO_COMFY,
    }


def models_for_mode(mode: str) -> dict:
    return MODEL_FILES


def assets_ready_for_mode(mode: str) -> bool:
    return assets_status()["ready"]


def length_from_seconds(seconds: float) -> int:
    """H3 frame grid: snap to 17k+5 blocks at 24 fps (template ComfyUI)."""
    n = max(5, round(seconds * 24))
    return n + (5 - (n % 17)) % 17


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


def load_template(name: str) -> dict:
    return json.loads((WORKFLOWS / name).read_text(encoding="utf-8"))


def patch_workflow(
    template: dict,
    prompt: str,
    seed: int,
    width: int,
    height: int,
    length: int,
    image_name: str | None,
    model_files: dict,
    video_prefix: str,
) -> dict:
    graph = copy.deepcopy(template)
    clip_name = Path(model_files["clip"]).name
    unet_name = Path(model_files["unet"]).name
    vae_v = Path(model_files["vae_video"]).name
    vae_a = Path(model_files["vae_audio"]).name
    for node in graph.values():
        ct = node.get("class_type")
        inputs = node.get("inputs") or {}
        if ct == "UNETLoader":
            inputs["unet_name"] = unet_name
        elif ct == "CLIPLoader":
            inputs["clip_name"] = clip_name
        elif ct == "VAELoader":
            if inputs.get("vae_name", "").startswith("minimax_h3_audio"):
                inputs["vae_name"] = vae_a
            else:
                inputs["vae_name"] = vae_v
        elif ct == "BlockSparseAttention":
            sel = inputs.get("selection")
            if isinstance(sel, list):
                mode = sel[0] if sel else "vsa"
                inputs["selection"] = mode
                extra = sel[1] if len(sel) > 1 else {}
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        inputs[f"selection.{k}"] = v
            if inputs.get("selection") == "vsa" and "selection.keep_percent" not in inputs:
                inputs["selection.keep_percent"] = 10.0
        elif ct == "MiniMaxH3ImageToVideo":
            inputs["prompt"] = prompt
            inputs["width"] = width
            inputs["height"] = height
            inputs["length"] = length
        elif ct == "RandomNoise":
            inputs["noise_seed"] = seed
        elif ct == "SaveVideo":
            inputs["filename_prefix"] = video_prefix
        elif ct == "LoadImage" and image_name:
            inputs["image"] = image_name
        node["inputs"] = inputs
    return graph


def comfy_post_prompt(api_prompt: dict) -> str:
    payload = json.dumps({"prompt": api_prompt, "client_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(
        f"{COMFY_API}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI: {result}")
    if result.get("node_errors"):
        raise RuntimeError(json.dumps(result["node_errors"], ensure_ascii=False)[:2500])
    return prompt_id


def collect_output_files(entry: dict) -> list[dict]:
    found: list[dict] = []
    for node_out in entry.get("outputs", {}).values():
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key, []) or []:
                if isinstance(item, dict) and item.get("filename"):
                    found.append(
                        {
                            "filename": item["filename"],
                            "subfolder": item.get("subfolder") or "",
                            "type": item.get("type") or key,
                        }
                    )
    return found


def comfy_wait(prompt_id: str, timeout_s: int = 3600) -> list[dict]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with urllib.request.urlopen(f"{COMFY_API}/history/{prompt_id}", timeout=30) as resp:
            hist = json.loads(resp.read())
        if prompt_id not in hist:
            time.sleep(4)
            continue
        entry = hist[prompt_id]
        status = entry.get("status", {})
        if not status.get("completed"):
            time.sleep(4)
            continue
        for msg in status.get("messages", []):
            if msg[0] == "execution_error":
                detail = msg[1] if len(msg) > 1 else {}
                raise RuntimeError(detail.get("exception_message", str(detail)))
        files = collect_output_files(entry)
        if files:
            return files
        raise RuntimeError("ComfyUI terminou sem arquivo de vídeo na saída")
    raise TimeoutError("Timeout (60 min)")


def run_job(
    job_id: str,
    mode: str,
    prompt: str,
    seed: int,
    width: int,
    height: int,
    length: int,
    image_name: str | None,
):
    def worker():
        try:
            with jobs_lock:
                jobs[job_id]["status"] = "running"
                jobs[job_id]["started_at"] = time.time()
            wf = MODES[mode]["workflow"]
            mf = models_for_mode(mode)
            vprefix = "h3_trim_video"
            api_prompt = patch_workflow(
                load_template(wf),
                prompt,
                seed,
                width,
                height,
                length,
                image_name,
                mf,
                vprefix,
            )
            prompt_id = comfy_post_prompt(api_prompt)
            with jobs_lock:
                jobs[job_id]["prompt_id"] = prompt_id
            outputs = comfy_wait(prompt_id)
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["outputs"] = outputs
                jobs[job_id]["finished_at"] = time.time()
        except Exception as e:
            with jobs_lock:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["finished_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()


def resolve_media_path(filename: str, subfolder: str = "") -> Path | None:
    name = Path(filename).name
    if not any(name.startswith(p) for p in VIDEO_PREFIXES):
        return None
    base = COMFY_OUTPUT
    if subfolder:
        base = base / subfolder
    path = (base / name).resolve()
    if not str(path).startswith(str(COMFY_OUTPUT.resolve())):
        return None
    return path


def list_gallery() -> list[dict]:
    items: list[dict] = []
    patterns = []
    for prefix in VIDEO_PREFIXES:
        patterns.extend((f"{prefix}*.mp4", f"{prefix}*.webm", f"{prefix}*.mkv"))
    for pattern in patterns:
        for path in COMFY_OUTPUT.rglob(pattern):
            if path.is_file():
                rel = path.relative_to(COMFY_OUTPUT)
                items.append(
                    {
                        "filename": path.name,
                        "subfolder": str(rel.parent) if rel.parent != Path(".") else "",
                        "mtime": path.stat().st_mtime,
                    }
                )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def render_index() -> bytes:
    tpl = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    tpl = tpl.replace("<!--ASSETS-->", json.dumps(assets_status(), ensure_ascii=False))
    tpl = tpl.replace("<!--MODES-->", json.dumps(MODES, ensure_ascii=False))
    tpl = tpl.replace("<!--SIZES-->", json.dumps(SIZE_PRESETS, ensure_ascii=False))
    return tpl.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "H3VideoEditor/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = render_index()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/gallery":
            return self._json({"videos": list_gallery()})
        if parsed.path == "/api/assets":
            return self._json(assets_status())
        if parsed.path.startswith("/api/status/"):
            jid = parsed.path.split("/")[-1]
            with jobs_lock:
                job = jobs.get(jid)
            if not job:
                return self._json({"error": "Job não encontrado"}, HTTPStatus.NOT_FOUND)
            return self._json(job)
        if parsed.path.startswith("/media/"):
            qs = urllib.parse.parse_qs(parsed.query)
            name = Path(parsed.path.removeprefix("/media/")).name
            sub = (qs.get("subfolder") or [""])[0]
            path = resolve_media_path(name, sub)
            if path is None or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/generate":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._json({"error": "multipart/form-data"}, HTTP_STATUS.BAD_REQUEST)

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
        )
        mode = form_text(form, "mode") or "t2v"
        if mode not in MODES:
            return self._json({"error": "Modo inválido"}, HTTPStatus.BAD_REQUEST)
        st = assets_status()
        if not assets_ready_for_mode(mode):
            err = (
                "Modelos FastH3 Trim incompletos. No servidor: "
                "cd ~/h3-video-editor && ./download_fastvideo_trim.py"
            )
            return self._json({"error": err, "assets": st}, HTTPStatus.SERVICE_UNAVAILABLE)
        prompt = form_text(form, "prompt")
        if not prompt:
            return self._json({"error": "Prompt obrigatório"}, HTTPStatus.BAD_REQUEST)

        size_key = form_text(form, "size") or "preview"
        preset = SIZE_PRESETS.get(size_key, SIZE_PRESETS["preview"])
        width, height = preset["width"], preset["height"]

        try:
            duration = float(form_text(form, "duration") or "3")
        except ValueError:
            duration = 3.0
        duration = max(2.0, min(duration, 10.0))
        length = length_from_seconds(duration)

        seed_raw = form_text(form, "seed")
        seed = int(seed_raw) if seed_raw.isdigit() else random.randint(0, 2**32 - 1)

        job_id = str(uuid.uuid4())[:8]
        image_name = None
        if MODES[mode]["needs_image"]:
            field = form["image"] if "image" in form else None
            if not field_has_upload(field):
                return self._json({"error": "Envie uma imagem para I2V"}, HTTPStatus.BAD_REQUEST)
            COMFY_INPUT.mkdir(parents=True, exist_ok=True)
            image_name = f"h3_{job_id}_frame.png"
            with (COMFY_INPUT / image_name).open("wb") as f:
                shutil.copyfileobj(field.file, f)

        with jobs_lock:
            jobs[job_id] = {
                "status": "queued",
                "mode": mode,
                "prompt": prompt,
                "duration": duration,
                "length": length,
                "width": width,
                "height": height,
                "seed": seed,
                "created_at": time.time(),
            }
        run_job(job_id, mode, prompt, seed, width, height, length, image_name)
        return self._json({
            "job_id": job_id,
            "status": "queued",
            "length": length,
            "duration": duration,
        })


def main():
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    WORKFLOWS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"H3 Video Editor http://0.0.0.0:{PORT}")
    print(f"Assets ready: {assets_status()['ready']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
