#!/usr/bin/env python3
"""QwenSwarm Unified AI Suite CLI Client.

Allows the AI Agent or human users to interact directly with all AI capabilities
running on the server (Image, Video, LLM, TTS, Whisper, GPU Telemetry).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_HOST = os.environ.get("QWENSWARM_HOST", "http://192.168.2.183:3000")


def request(
    endpoint: str,
    method: str = "GET",
    data: dict | bytes | None = None,
    headers: dict | None = None,
    host: str = DEFAULT_HOST,
    timeout: int = 60,
) -> tuple[int, bytes, dict]:
    url = f"{host.rstrip('/')}/{endpoint.lstrip('/')}"
    req_headers = headers or {}
    body = None

    if isinstance(data, dict):
        body = json.dumps(data).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    elif isinstance(data, bytes):
        body = data

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read()
            resp_headers = dict(resp.headers)
            return resp.status, resp_body, resp_headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def cmd_gpu(args):
    code, body, _ = request("/api/system/gpu", host=args.host)
    if code != 200:
        print(f"Error ({code}): {body.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return 1
    data = json.loads(body.decode("utf-8"))
    used_gb = round(data.get("vram_used_mb", 0) / 1024, 2)
    total_gb = round(data.get("vram_total_mb", 0) / 1024, 2)
    print("=== GPU & CLUSTER STATUS ===")
    print(f"Mode:          {data.get('mode', 'unknown')} ({data.get('description', '')})")
    print(f"Queue Paused:  {data.get('queue_paused')}")
    print(f"VRAM:          {used_gb} GB / {total_gb} GB ({round((used_gb/max(total_gb, 0.1))*100)}%)")
    print(f"GPU Util:      {data.get('gpu_util_pct', 0)}%")
    print(f"Temperature:   {data.get('temperature_c', 0)}°C")
    print(f"LLM Running:   {data.get('llm_running', False)}")
    print(f"Video Active:  {data.get('video_active', False)}")
    return 0


def cmd_image(args):
    prompt = args.prompt
    image_path = Path(args.image) if args.image else None
    out_path = Path(args.out) if args.out else Path(f"output_img_{int(time.time())}.png")

    req_data = {
        "prompt": prompt,
        "model": args.model or "qwen21_pruna",
        "resolution": args.resolution or 768,
    }
    if args.pool_id:
        req_data["pool_id"] = args.pool_id
    elif image_path and image_path.is_file():
        req_data["image_base64"] = base64.b64encode(image_path.read_bytes()).decode("utf-8")

    if args.ref:
        req_data["references"] = [
            base64.b64encode(Path(p).read_bytes()).decode("utf-8")
            for p in args.ref
            if Path(p).is_file()
        ]

    print(f"Enfileirando edição de imagem: '{prompt}'...")
    code, body, _ = request("/api/edit", method="POST", data=req_data, host=args.host)
    if code not in (200, 201):
        print(f"Erro ao enfileirar: {body.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return 1

    res = json.loads(body.decode("utf-8"))
    job_id = res.get("id")
    print(f"Job ID: {job_id}. Aguardando conclusão na fila...")

    # Poll status
    while True:
        time.sleep(3)
        code, body, _ = request(f"/api/edit/{job_id}", host=args.host)
        if code != 200:
            continue
        st = json.loads(body.decode("utf-8"))
        status = st.get("status")
        if status == "running":
            print("Status: renderizando na GPU...", end="\r", flush=True)
        elif status == "done":
            print("\nConcluído com sucesso!")
            code, img_data, _ = request(f"/api/edit/{job_id}/image", host=args.host)
            if code == 200:
                out_path.write_bytes(img_data)
                print(f"Imagem salva em: {out_path.resolve()}")
                return 0
            break
        elif status == "error":
            print(f"\nErro no job: {st.get('error')}", file=sys.stderr)
            return 1


def cmd_video(args):
    prompt = args.prompt
    mode = args.mode or "t2v"
    size = args.size or "preview"
    seconds = args.seconds or 3
    seed = args.seed if args.seed is not None else -1
    out_path = Path(args.out) if args.out else Path(f"output_video_{int(time.time())}.mp4")

    req_data = {
        "prompt": prompt,
        "mode": mode,
        "size": size,
        "seconds": seconds,
        "seed": seed,
    }
    if args.pool_id:
        req_data["pool_id"] = args.pool_id

    print(f"Enfileirando vídeo ({mode}): '{prompt}' ({seconds}s)...")
    print("(Aviso: A fila de imagens será pausada automaticamente para liberar a GPU)")
    code, body, _ = request("/api/video/generate", method="POST", data=req_data, host=args.host)
    if code != 200:
        print(f"Erro: {body.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return 1

    res = json.loads(body.decode("utf-8"))
    job_id = res.get("job_id")
    print(f"Job ID: {job_id}. Acompanhando renderização...")

    while True:
        time.sleep(4)
        code, body, _ = request(f"/api/video/job/{job_id}", host=args.host)
        if code != 200:
            continue
        job = json.loads(body.decode("utf-8"))
        status = job.get("status")
        if status == "waiting_gpu":
            print("Status: aguardando GPU liberar...", end="\r", flush=True)
        elif status == "running":
            print("Status: renderizando vídeo (MiniMax H3 8 passos)...", end="\r", flush=True)
        elif status == "done":
            print("\nVídeo concluído com sucesso!")
            vurl = job.get("video_url")
            if vurl:
                code, vdata, _ = request(vurl, host=args.host)
                if code == 200:
                    out_path.write_bytes(vdata)
                    print(f"Vídeo salvo em: {out_path.resolve()}")
                    return 0
            break
        elif status == "error":
            print(f"\nErro: {job.get('error')}", file=sys.stderr)
            return 1


def cmd_llm(args):
    prompt = args.prompt
    system = args.system or "Você é um assistente de IA especialista em engenharia de prompt para imagem e vídeo."
    temp = args.temp or 0.7

    req_data = {
        "messages": [{"role": "user", "content": prompt}],
        "system_prompt": system,
        "temperature": temp,
        "max_tokens": args.max_tokens or 1024,
    }

    print(f"Enviando para Qwen 3.8 27B (assumindo GPU)...")
    code, body, _ = request("/api/llm/chat", method="POST", data=req_data, host=args.host, timeout=180)
    if code != 200:
        print(f"Erro: {body.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return 1

    res = json.loads(body.decode("utf-8"))
    reply = res.get("choices", [{}])[0].get("message", {}).get("content", "")
    print("\n--- RESPOSTA DA LLM ---")
    print(reply)
    print("------------------------\n")

    if not args.keep_alive:
        print("Liberando GPU de volta para a fila de imagens...")
        request("/api/llm/release", method="POST", host=args.host)
    return 0


def cmd_tts(args):
    text = args.text
    voice = args.voice or "pf_dora"
    speed = args.speed or 1.0
    out_path = Path(args.out) if args.out else Path(f"output_speech_{int(time.time())}.wav")

    req_data = {"text": text, "voice": voice, "speed": speed}
    print(f"Sintetizando áudio no Kokoro TTS com voz '{voice}'...")
    code, body, _ = request("/api/tts", method="POST", data=req_data, host=args.host)
    if code != 200:
        print(f"Erro: {body.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return 1

    out_path.write_bytes(body)
    print(f"Áudio WAV gerado com sucesso: {out_path.resolve()} ({len(body)} bytes)")
    return 0


def cmd_transcribe(args):
    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"Arquivo não encontrado: {file_path}", file=sys.stderr)
        return 1

    req_data = {
        "audio_base64": base64.b64encode(file_path.read_bytes()).decode("utf-8"),
        "lang": args.lang or "auto",
    }
    print(f"Transcrevendo '{file_path.name}' via Whisper.cpp...")
    code, body, _ = request("/api/transcribe", method="POST", data=req_data, host=args.host, timeout=120)
    if code != 200:
        print(f"Erro: {body.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return 1

    res = json.loads(body.decode("utf-8"))
    print(f"Idioma Detectado: {res.get('language')}")
    print(f"Texto Completo:   {res.get('text')}\n")
    if args.verbose:
        print("Segmentos:")
        for s in res.get("segments", []):
            ts = s.get("timestamps", {})
            print(f"  [{ts.get('from')} -> {ts.get('to')}] {s.get('text')}")
    return 0


def cmd_queue(args):
    action = args.action
    if action == "status":
        code, body, _ = request("/api/queue", host=args.host)
        if code == 200:
            q = json.loads(body.decode("utf-8"))
            s = q.get("summary", {})
            print("=== RESUMO DA FILA ===")
            print(f"Total:      {s.get('total')}")
            print(f"Pendentes:  {s.get('pending')}")
            print(f"Rodando:    {s.get('running')}")
            print(f"Leased:     {s.get('leased')}")
            print(f"Concluídas: {s.get('done')}")
            print(f"Erros:      {s.get('error')}")
            print(f"Pausada:    {s.get('paused')}")
            return 0
    elif action in ("pause", "resume", "cancel"):
        code, body, _ = request(f"/api/queue/{action}", method="POST", host=args.host)
        print(body.decode("utf-8", errors="ignore"))
        return 0 if code == 200 else 1
    elif action == "clear":
        code, body, _ = request("/api/queue", method="DELETE", host=args.host)
        print(body.decode("utf-8", errors="ignore"))
        return 0 if code == 200 else 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="QwenSwarm Unified AI Suite Client")
    parser.add_argument("--host", default=DEFAULT_HOST, help="API Base URL")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # gpu
    p_gpu = subparsers.add_parser("gpu", help="Status da GPU e do Arbiter")
    p_gpu.set_defaults(func=cmd_gpu)

    # image
    p_img = subparsers.add_parser("image", help="Geração / Edição de Imagem (Qwen 2.1)")
    p_img.add_argument("--prompt", required=True, help="Texto do prompt de edição")
    p_img.add_argument("--image", help="Caminho da imagem inicial")
    p_img.add_argument("--pool-id", help="ID da imagem no pool")
    p_img.add_argument("--ref", action="append", help="Imagem de referência (pode repetir)")
    p_img.add_argument("--resolution", type=int, default=768, help="Resolução")
    p_img.add_argument("--model", default="qwen21_pruna", help="Modelo")
    p_img.add_argument("--out", help="Arquivo de saída")
    p_img.set_defaults(func=cmd_image)

    # video
    p_vid = subparsers.add_parser("video", help="Geração de Vídeo (MiniMax H3)")
    p_vid.add_argument("--prompt", required=True, help="Prompt do vídeo")
    p_vid.add_argument("--mode", choices=["t2v", "i2v"], default="t2v", help="t2v ou i2v")
    p_vid.add_argument("--pool-id", help="ID da foto inicial do pool (para i2v)")
    p_vid.add_argument("--size", choices=["preview", "native", "square"], default="preview")
    p_vid.add_argument("--seconds", type=float, default=3.0, help="Duração (3-5s)")
    p_vid.add_argument("--seed", type=int, default=-1)
    p_vid.add_argument("--out", help="Arquivo MP4 de saída")
    p_vid.set_defaults(func=cmd_video)

    # llm
    p_llm = subparsers.add_parser("llm", help="Chat / Texto com Qwen 3.8 27B")
    p_llm.add_argument("--prompt", required=True, help="Mensagem do usuário")
    p_llm.add_argument("--system", help="Prompt do sistema")
    p_llm.add_argument("--temp", type=float, default=0.7, help="Temperatura")
    p_llm.add_argument("--max-tokens", type=int, default=1024)
    p_llm.add_argument("--keep-alive", action="store_true", help="Não libera a GPU após responder")
    p_llm.set_defaults(func=cmd_llm)

    # tts
    p_tts = subparsers.add_parser("tts", help="Síntese de Voz (Kokoro TTS)")
    p_tts.add_argument("--text", required=True, help="Texto para sintetizar")
    p_tts.add_argument("--voice", default="pf_dora", help="Voz (pf_dora, pm_alex, pm_santa, af_heart, am_adam)")
    p_tts.add_argument("--speed", type=float, default=1.0)
    p_tts.add_argument("--out", help="Arquivo WAV de saída")
    p_tts.set_defaults(func=cmd_tts)

    # transcribe
    p_tr = subparsers.add_parser("transcribe", help="Transcrição de Áudio (Whisper)")
    p_tr.add_argument("--file", required=True, help="Arquivo de áudio/vídeo")
    p_tr.add_argument("--lang", default="auto", help="Idioma (auto, pt, en)")
    p_tr.add_argument("-v", "--verbose", action="store_true", help="Exibe timestamps")
    p_tr.set_defaults(func=cmd_transcribe)

    # queue
    p_q = subparsers.add_parser("queue", help="Gerenciamento da Fila de Imagens")
    p_q.add_argument("action", choices=["status", "pause", "resume", "cancel", "clear"])
    p_q.set_defaults(func=cmd_queue)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
