#!/usr/bin/env python3
"""
Worker Satélite Qwen - Processamento Distribuído com Buffer Offline
Executa ComfyUI localmente com --lowvram na RTX 3060 6GB e sincroniza com o Master (192.168.2.183).
"""

import os
import sys
import time
import json
import math
import uuid
import shutil
import urllib.request
import urllib.error
from pathlib import Path
from PIL import Image

def _load_env_file():
    """Carrega variáveis do arquivo .env local caso exista."""
    env_paths = [
        Path(__file__).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]
    for p in env_paths:
        if p.is_file():
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                            v = v[1:-1]
                        if k and k not in os.environ:
                            os.environ[k] = v
                break
            except Exception:
                pass

_load_env_file()

MASTER_HOST = os.environ.get("MASTER_HOST", "192.168.2.183")
MASTER_PORT = os.environ.get("MASTER_PORT", "3000")
MASTER_URL = os.environ.get("MASTER_URL", f"http://{MASTER_HOST}:{MASTER_PORT}").rstrip("/")
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
WORKER_ID = os.environ.get("WORKER_ID", "predator-3060")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "15"))

MASTER_SSH_USER = os.environ.get("MASTER_SSH_USER", "user")
MASTER_SSH_PASS = os.environ.get("MASTER_SSH_PASS", "password")

BASE_DIR = Path(os.environ.get("BASE_DIR", Path.home() / "satellite_worker"))
OUTBOX_DIR = BASE_DIR / "outbox"
POOL_CACHE_DIR = BASE_DIR / "pool"
RESIZED_DIR = BASE_DIR / "resized"
REFS_CACHE_DIR = BASE_DIR / "preset_refs"
LOCAL_QUEUE_FILE = BASE_DIR / "local_queue.json"

COMFY_DIR = Path(os.environ.get("COMFY_DIR", Path.home() / "ComfyUI"))
COMFY_INPUT = COMFY_DIR / "input"
COMFY_OUTPUT = COMFY_DIR / "output"
WORKFLOW_FILE = COMFY_DIR / "user/default/workflows/image_qwen_image_2_1_image_edit.json"

MAX_REFERENCE_IMAGES = 9
QWEN21_REF_LOADER_IDS = ["475", "476", "477", "478", "479", "480", "481", "482", "483"]
QWEN21_REF_EXT_NAMES = [
    "images.image_2", "images.image_3", "images.image_4", "images.image_5_1",
    "images.image_6", "images.image_7", "images.image_8", "images.image_9", "images.image_10"
]
PRUNA_SIGMAS_8 = "1, 0.9333333333, 0.8571428571, 0.7692307692, 0.6666666667, 0.5454545455, 0.4, 0.2222222222, 0"


def ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    POOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RESIZED_DIR.mkdir(parents=True, exist_ok=True)
    REFS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    COMFY_OUTPUT.mkdir(parents=True, exist_ok=True)


def is_master_online() -> bool:
    try:
        req = urllib.request.Request(f"{MASTER_URL}/api/queue", headers={"User-Agent": "SatelliteWorker/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def flush_outbox():
    """Se o master estiver online, envia todas as imagens acumuladas no outbox."""
    meta_files = sorted(OUTBOX_DIR.glob("*.json"))
    if not meta_files:
        return
    if not is_master_online():
        return

    print(f"[*] Outbox: {len(meta_files)} item(ns) acumulado(s) para sincronizar com o Master...")
    for mf in meta_files:
        try:
            with mf.open("r", encoding="utf-8") as f:
                data = json.load(f)
            item_id = data["item_id"]
            filename = data["filename"]
            img_path = OUTBOX_DIR / filename
            if not img_path.is_file():
                mf.unlink(missing_ok=True)
                continue

            boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
            body = bytearray()
            def add_field(name, val):
                body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode("utf-8"))

            add_field("item_id", item_id)
            add_field("filename", filename)
            add_field("finished_at", str(data.get("finished_at", time.time())))

            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
            body.extend(img_path.read_bytes())
            body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

            req = urllib.request.Request(
                f"{MASTER_URL}/api/worker/complete",
                data=bytes(body),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    img_path.unlink(missing_ok=True)
                    mf.unlink(missing_ok=True)
                    print(f"  [✓] Sincronizado do outbox: {item_id} -> {filename}")
        except Exception as e:
            print(f"  [!] Erro ao enviar outbox item {mf.name}: {e}")
            break


def load_local_queue() -> list[dict]:
    if not LOCAL_QUEUE_FILE.is_file():
        return []
    try:
        with LOCAL_QUEUE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_local_queue(items: list[dict]):
    tmp = LOCAL_QUEUE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    tmp.replace(LOCAL_QUEUE_FILE)


def fetch_master_lease() -> list[dict]:
    if not is_master_online():
        return []
    payload = json.dumps({"worker_id": WORKER_ID, "count": BATCH_SIZE}).encode("utf-8")
    req = urllib.request.Request(
        f"{MASTER_URL}/api/worker/lease",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("items") or []
    except Exception as e:
        print(f"[!] Erro ao pedir lease ao master: {e}")
        return []


def notify_master_fail(item_id: str, error_msg: str):
    if not is_master_online():
        return
    try:
        payload = json.dumps({"item_id": item_id, "error": error_msg}).encode("utf-8")
        req = urllib.request.Request(
            f"{MASTER_URL}/api/worker/fail",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def download_pool_image(pool_id: str) -> Path | None:
    if not pool_id or pool_id.startswith("blank_"):
        return None
    dest = POOL_CACHE_DIR / f"{pool_id}.png"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    url = f"{MASTER_URL}/api/pool/{pool_id}/file"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SatelliteWorker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest.write_bytes(resp.read())
        return dest
    except Exception as e:
        print(f"[!] Erro ao baixar imagem do pool {pool_id}: {e}")
        return None


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
    return link


def build_qwen_workflow_prompt(
    item: dict,
    image1_name: str,
    ref_names: list[str],
    out_w: int,
    out_h: int,
    filename_prefix: str,
) -> dict:
    with WORKFLOW_FILE.open("r", encoding="utf-8") as f:
        wf = json.load(f)

    outer_nodes = {n["id"]: n for n in wf["nodes"]}
    sg = wf["definitions"]["subgraphs"][0]
    sg_nodes = {n["id"]: n for n in sg["nodes"]}
    sg_links = {parse_link(l)[0]: parse_link(l) for l in sg.get("links", [])}

    subgraph_node = outer_nodes[459]
    subgraph_widget_map = dict(subgraph_node.get("widgets_values_named", {}))
    subgraph_widget_map["prompt"] = item.get("prompt") or ""
    subgraph_widget_map["steps"] = int(item.get("steps") or 8)
    subgraph_widget_map["resolution"] = int(item.get("resolution") or 1024)
    import random
    subgraph_widget_map["seed"] = random.randint(0, 2**53)
    is_t2i = bool(item.get("is_text_to_image") or str(item.get("pool_id", "")).startswith("blank_"))
    subgraph_widget_map["switch"] = is_t2i
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
            inputs["image"] = wvn.get("image", wv[0] if wv else "")
        elif ntype == "SaveImageAdvanced":
            inputs["filename_prefix"] = filename_prefix
            inputs["format"] = "png"
            inputs["format.bit_depth"] = "8-bit"
            inputs["format.input_color_space"] = "sRGB"
        elif ntype in ("UNETLoader", "UnetLoaderGGUF"):
            inputs["unet_name"] = "qwen-image-2.1-UC-Q4_K_M.gguf"
        elif ntype == "CLIPLoader":
            inputs["clip_name"] = "qwen3vl_8b_int8_convrot.safetensors"
            inputs["type"] = "qwen_image"
        elif ntype == "VAELoader":
            inputs["vae_name"] = "qwen_image_2.1_vae_bf16.safetensors"
        elif ntype == "EmptyLatentImage":
            inputs["width"] = out_w
            inputs["height"] = out_h
            inputs["batch_size"] = 1
        elif ntype == "KSampler":
            inputs["seed"] = subgraph_widget_map["seed"]
            inputs["steps"] = 8
            inputs["cfg"] = 1.0
            inputs["sampler_name"] = "euler"
            inputs["scheduler"] = "simple"
            inputs["denoise"] = 1
        elif ntype == "ComfySwitchNode":
            inputs["switch"] = is_t2i
        elif ntype == "QwenImage21Cache":
            inputs["device"] = "auto"
            inputs["dtype"] = "default"
        elif ntype == "TextEncodeQwenImage21":
            inputs["prompt"] = subgraph_widget_map["prompt"]
            inputs["negative_prompt"] = ""
            inputs["resolution"] = subgraph_widget_map["resolution"]
        return inputs

    api_prompt = {}
    for oid, node in outer_nodes.items():
        if node["type"] in ("MarkdownNote", "ImageCompare", "ResolutionSelector") or oid == 459:
            continue
        api_prompt[str(oid)] = {"class_type": node["type"], "inputs": build_inputs(node)}

    for sid, node in sg_nodes.items():
        if sid == 458:  # Pruna sampler bypass
            continue
        ctype = node["type"]
        if ctype == "UNETLoader":
            ctype = "UnetLoaderGGUF"
        api_prompt[f"{prefix}{sid}"] = {"class_type": ctype, "inputs": build_inputs(node)}

    api_prompt["470"]["inputs"]["image"] = image1_name
    for idx, img in enumerate(ref_names[:len(QWEN21_REF_LOADER_IDS)]):
        loader_id = QWEN21_REF_LOADER_IDS[idx]
        api_prompt[loader_id] = {"class_type": "LoadImage", "inputs": {"image": img}}

    for lid, (_, src, src_slot, dst, dst_slot, _) in sg_links.items():
        if dst < 0 or src == -20 or dst == 458:
            continue
        dst_node = sg_nodes[dst]
        inp_name = dst_node["inputs"][dst_slot]["name"]
        if src == -10:
            ext = sg_input_map.get(lid)
            if ext == "images.image_1":
                api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = ["470", 0]
            elif ext in QWEN21_REF_EXT_NAMES:
                ridx = QWEN21_REF_EXT_NAMES.index(ext)
                if ridx < len(ref_names):
                    loader_id = QWEN21_REF_LOADER_IDS[ridx]
                    api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = [loader_id, 0]
            elif ext and ext in subgraph_widget_map:
                api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = subgraph_widget_map[ext]
        else:
            api_prompt[f"{prefix}{dst}"]["inputs"][inp_name] = [f"{prefix}{src}", src_slot]

    api_prompt["461"]["inputs"]["images"] = ["459:457", 0]

    # Pruna LoRA 8-step setup
    seed = subgraph_widget_map["seed"]
    api_prompt[f"{prefix}450"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": [f"{prefix}451", 0],
            "lora_name": "p_qwen_image_2.1_8step_v0.1.safetensors",
            "strength_model": 1.0,
        },
    }
    api_prompt[f"{prefix}469"]["inputs"]["model"] = [f"{prefix}450", 0]
    api_prompt[f"{prefix}480"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    api_prompt[f"{prefix}481"] = {
        "class_type": "ManualSigmas",
        "inputs": {"sigmas": PRUNA_SIGMAS_8},
    }
    api_prompt[f"{prefix}482"] = {
        "class_type": "SamplerCustom",
        "inputs": {
            "model": [f"{prefix}469", 0],
            "positive": [f"{prefix}474", 0],
            "negative": [f"{prefix}474", 1],
            "sampler": [f"{prefix}480", 0],
            "sigmas": [f"{prefix}481", 0],
            "latent_image": [f"{prefix}468", 0],
            "add_noise": True,
            "noise_seed": seed,
            "cfg": 1.0,
        },
    }
    api_prompt[f"{prefix}457"]["inputs"]["samples"] = [f"{prefix}482", 0]
    return api_prompt


def comfy_submit_and_wait(prompt_dict: dict) -> list[str]:
    payload = json.dumps({"prompt": prompt_dict, "client_id": WORKER_ID}).encode("utf-8")
    req = urllib.request.Request(f"{COMFY_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    prompt_id = res.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI local erro ao submeter prompt: {res}")

    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(2)
        try:
            with urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}", timeout=10) as hresp:
                hist = json.loads(hresp.read().decode("utf-8"))
            if prompt_id not in hist:
                continue
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if not status.get("completed"):
                continue
            for msg in status.get("messages", []):
                if msg[0] == "execution_error":
                    raise RuntimeError(str(msg[1]))
            out_files = []
            for n_out in entry.get("outputs", {}).values():
                for im in n_out.get("images", []):
                    fn = im.get("filename")
                    if fn:
                        out_files.append(fn)
            return out_files
        except urllib.error.URLError:
            continue
    raise TimeoutError("Timeout aguardando ComfyUI local")


def deliver_or_outbox(item_id: str, filename: str, finished_at: float):
    src = COMFY_OUTPUT / filename
    if not src.is_file():
        print(f"[!] Arquivo gerado não encontrado: {src}")
        return

    delivered = False
    if is_master_online():
        try:
            boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
            body = bytearray()
            def add_field(name, val):
                body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode("utf-8"))

            add_field("item_id", item_id)
            add_field("filename", filename)
            add_field("finished_at", str(finished_at))
            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"))
            body.extend(src.read_bytes())
            body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

            req = urllib.request.Request(
                f"{MASTER_URL}/api/worker/complete",
                data=bytes(body),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    delivered = True
                    print(f"[✓] Entregue com sucesso ao Master ({MASTER_HOST}): {item_id} -> {filename}")
        except Exception as e:
            print(f"[!] Falha ao entregar direto ao Master (guardando no outbox): {e}")

    if not delivered:
        dest = OUTBOX_DIR / filename
        shutil.copy(src, dest)
        meta = {"item_id": item_id, "filename": filename, "finished_at": finished_at}
        with (OUTBOX_DIR / f"{item_id}.json").open("w", encoding="utf-8") as f:
            json.dump(meta, f)
        print(f"[+] Master offline ou indisponível. Guardado no outbox: {item_id} -> {filename}")


def sync_assets_from_master():
    """Sincroniza presets e imagens de referência do master via rsync rápido se online."""
    if not is_master_online():
        return
    try:
        import subprocess
        # Sincroniza preset_refs e prompts.json
        cmd = [
            "sshpass", "-p", MASTER_SSH_PASS,
            "rsync", "-az", "--update",
            f"{MASTER_SSH_USER}@{MASTER_HOST}:/home/{MASTER_SSH_USER}/qwen-editor/preset_refs/",
            str(REFS_CACHE_DIR) + "/"
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception:
        pass


def main():
    ensure_dirs()
    print("==================================================================")
    print(f"[*] Iniciando Worker Satélite ({WORKER_ID})")
    print(f"[*] Master: {MASTER_URL} | ComfyUI local: {COMFY_URL}")
    print(f"[*] VRAM otimizada para RTX 3060 6GB com suporte offline (Outbox)")
    print("==================================================================")

    while True:
        try:
            # 1. Flush de qualquer item que ficou no outbox
            flush_outbox()

            # 2. Carrega fila local
            queue = load_local_queue()
            if not queue:
                if is_master_online():
                    print(f"[*] Fila local vazia. Sincronizando presets do Master...")
                    sync_assets_from_master()

                    print(f"[*] Solicitando bloco de {BATCH_SIZE} tarefas ao Master...")
                    items = fetch_master_lease()
                    if items:
                        # Pré-baixa imagens do pool para todos os itens recebidos enquanto master está online
                        print(f"[*] Pré-carregando imagens do pool para o lote ({len(items)} itens)...")
                        for it in items:
                            pid = it.get("pool_id")
                            if pid and not pid.startswith("blank_"):
                                download_pool_image(pid)

                        save_local_queue(items)
                        queue = items
                        print(f"[✓] Recebidas e preparadas {len(items)} tarefas para processamento.")
                    else:
                        print(f"[*] Fila do Master sem tarefas pendentes. Aguardando 15s...")
                        time.sleep(15)
                        continue
                else:
                    print(f"[*] Master ({MASTER_URL}) offline e sem tarefas locais pendentes. Aguardando 10s...")
                    time.sleep(10)
                    continue

            # 3. Processa próximo item da fila local
            item = queue[0]
            item_id = item["id"]
            pool_id = item.get("pool_id") or ""
            resolution = int(item.get("resolution") or 1024)
            preset_label = item.get("prompt_preset_label") or "custom"
            print(f"\n[>] Processando item {item_id} (Preset: {preset_label}, Res: {resolution})...")

            # Prepara imagem base
            image1_name = f"sat_{item_id}_main.png"
            dest_image1 = COMFY_INPUT / image1_name
            is_t2i = bool(item.get("is_text_to_image") or pool_id.startswith("blank_"))
            out_w, out_h = resolution, resolution

            if is_t2i or not pool_id:
                im = Image.new("RGB", (out_w, out_h), color=(0, 0, 0))
                im.save(dest_image1, format="PNG")
            else:
                cache_resized = RESIZED_DIR / f"{pool_id}_{resolution}.png"
                if cache_resized.is_file():
                    shutil.copy(cache_resized, dest_image1)
                    with Image.open(dest_image1) as cim:
                        out_w, out_h = cim.size
                else:
                    src_path = POOL_CACHE_DIR / f"{pool_id}.png"
                    if not src_path.is_file():
                        src_path = download_pool_image(pool_id)
                    if not src_path or not src_path.is_file():
                        print(f"[!] Imagem do pool {pool_id} não encontrada localmente nem no Master. Pulando...")
                        notify_master_fail(item_id, f"Pool image {pool_id} missing")
                        queue.pop(0)
                        save_local_queue(queue)
                        continue

                    raw_dest = COMFY_INPUT / f"sat_{item_id}_raw.png"
                    shutil.copy(src_path, raw_dest)
                    _, _, out_w, out_h = resize_image_for_qwen(raw_dest, resolution)
                    shutil.copy(raw_dest, dest_image1)
                    shutil.copy(raw_dest, cache_resized)
                    raw_dest.unlink(missing_ok=True)

            # Prepara referências do preset
            ref_names = []
            prompt_preset_id = item.get("prompt_preset_id") or ""
            if prompt_preset_id:
                pdir = REFS_CACHE_DIR / prompt_preset_id
                if pdir.is_dir():
                    ref_files = sorted(pdir.glob("ref_*.*"))
                    want_ids = set(item.get("ref_ids") or [])
                    for idx, rf in enumerate(ref_files[:MAX_REFERENCE_IMAGES]):
                        if want_ids and rf.name not in want_ids and rf.stem not in want_ids:
                            continue
                        rname = f"sat_{item_id}_pref{idx}.png"
                        rdest = COMFY_INPUT / rname
                        shutil.copy(rf, rdest)
                        resize_image_for_qwen(rdest, resolution)
                        ref_names.append(rname)

            filename_prefix = f"qwen_web_{time.strftime('%Y%m%d_%H%M%S')}_{prompt_preset_id or 'custom'}"

            # Monta prompt e submete ao ComfyUI local
            prompt_dict = build_qwen_workflow_prompt(
                item,
                image1_name,
                ref_names,
                out_w,
                out_h,
                filename_prefix,
            )

            t0 = time.time()
            out_files = comfy_submit_and_wait(prompt_dict)
            elapsed = int(time.time() - t0)
            print(f"[✓] Geração concluída localmente em {elapsed}s: {out_files}")

            if out_files:
                deliver_or_outbox(item_id, out_files[0], time.time())

            # Limpa temporários de input
            dest_image1.unlink(missing_ok=True)
            for rn in ref_names:
                (COMFY_INPUT / rn).unlink(missing_ok=True)

            # Remove da fila local
            queue.pop(0)
            save_local_queue(queue)

        except Exception as exc:
            print(f"[!] Erro no loop do worker: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
