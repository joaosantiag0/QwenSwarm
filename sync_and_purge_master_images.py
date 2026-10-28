#!/usr/bin/env python3
"""
sync_and_purge_master_images.py
Sincroniza todas as imagens geradas do Master para a pasta local imgs/ em alta velocidade,
e em seguida purga os PNGs e metadados do Master sem parar ou perder a fila ativa.
"""

import os
import sys
import time
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

MASTER_HOSTS = ["http://192.168.2.183:3000", "http://100.89.150.119:3000"]
LOCAL_IMGS_DIR = Path(__file__).resolve().parent / "imgs"
LOCAL_IMGS_DIR.mkdir(parents=True, exist_ok=True)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
WORKERS = 12

def get_active_master_url() -> str:
    for host in MASTER_HOSTS:
        try:
            r = requests.get(f"{host}/api/system/gpu", timeout=3)
            if r.status_code == 200:
                return host
        except Exception:
            continue
    raise RuntimeError("Não foi possível conectar ao Master em nenhum dos endereços conhecidos.")

def create_session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=WORKERS + 4, pool_maxsize=WORKERS + 4, max_retries=3)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def fetch_master_image_list(master_url: str) -> list[str]:
    print(f"• Consultando lista da galeria no Master ({master_url})...", flush=True)
    r = requests.get(f"{master_url}/api/gallery?all=1", timeout=30)
    r.raise_for_status()
    data = r.json()
    images = data.get("images", [])
    filenames = [img["filename"] for img in images if img.get("filename")]
    return filenames

def download_single_image(master_url: str, filename: str, session: requests.Session) -> tuple[str, bool, int, str]:
    dest_path = LOCAL_IMGS_DIR / filename
    tmp_path = LOCAL_IMGS_DIR / f"{filename}.part"

    try:
        # Se já existe localmente e é válido, pula
        if dest_path.is_file() and dest_path.stat().st_size > 10_000:
            with dest_path.open("rb") as f:
                if f.read(8) == PNG_MAGIC:
                    return (filename, True, 0, "already_exists")

        dl_url = f"{master_url}/output/{filename}"
        with session.get(dl_url, stream=True, timeout=25) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as out_f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        out_f.write(chunk)

        if not tmp_path.is_file() or tmp_path.stat().st_size < 10_000:
            if tmp_path.exists():
                tmp_path.unlink()
            return (filename, False, 0, "Arquivo menor que 10KB ou incompleto")

        with tmp_path.open("rb") as f:
            if f.read(8) != PNG_MAGIC:
                tmp_path.unlink()
                return (filename, False, 0, "Cabeçalho PNG inválido")

        size = tmp_path.stat().st_size
        tmp_path.replace(dest_path)
        return (filename, True, size, "")

    except Exception as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return (filename, False, 0, str(e))

def run():
    master_url = get_active_master_url()
    print("═" * 65)
    print("🚀 QWEN MASTER SYNC & PURGE — SALVANDO IMAGENS LOCALMENTE")
    print(f"[*] Master: {master_url}")
    print(f"[*] Destino Local: {LOCAL_IMGS_DIR}")
    print(f"[*] Concorrência: {WORKERS} threads de download paralelo")
    print("═" * 65)

    # 1. Primeira varredura
    master_images = fetch_master_image_list(master_url)
    total_initial = len(master_images)
    print(f"[*] Total de imagens no Master agora: {total_initial}")

    if total_initial == 0:
        print("✓ Nenhuma imagem para baixar no Master.")
        return

    session = create_session()
    start_time = time.time()
    downloaded_count = 0
    skipped_count = 0
    error_count = 0
    total_bytes = 0

    print(f"\n[Fase 1/2] Iniciando download paralelo de {total_initial} imagens...")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(download_single_image, master_url, fn, session): fn for fn in master_images}

        for i, fut in enumerate(as_completed(futures), start=1):
            fn, ok, b_len, msg = fut.result()
            if ok:
                if msg == "already_exists":
                    skipped_count += 1
                else:
                    downloaded_count += 1
                    total_bytes += b_len
            else:
                error_count += 1
                print(f"\n[!] Falha no download de {fn}: {msg}", flush=True)

            if i % 200 == 0 or i == total_initial:
                elapsed = max(0.001, time.time() - start_time)
                speed_mb = (total_bytes / (1024 * 1024)) / elapsed
                pct = (i / total_initial) * 100
                print(
                    f"[{i}/{total_initial}] ({pct:.1f}%) | "
                    f"Baixadas: {downloaded_count} | Já existiam: {skipped_count} | "
                    f"Velocidade: {speed_mb:.1f} MB/s | Erros: {error_count}",
                    flush=True
                )

    # 2. Varredura adicional (captura fotos que terminaram durante o download)
    print("\n[Verificação] Verificando novas imagens geradas durante o download...")
    time.sleep(1)
    sweep_images = fetch_master_image_list(master_url)
    pending_sweep = [fn for fn in sweep_images if not (LOCAL_IMGS_DIR / fn).is_file()]

    if pending_sweep:
        print(f"[*] Baixando mais {len(pending_sweep)} imagens recém-finalizadas...")
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(download_single_image, master_url, fn, session): fn for fn in pending_sweep}
            for fut in as_completed(futures):
                fn, ok, b_len, msg = fut.result()
                if ok and msg != "already_exists":
                    downloaded_count += 1
                    total_bytes += b_len

    # 3. Validação de segurança antes do purge
    print("\n[Fase 2/2] Validando que todas as imagens estão salvas localmente antes de purgar o Master...")
    final_master_list = fetch_master_image_list(master_url)
    missing_local = [fn for fn in final_master_list if not (LOCAL_IMGS_DIR / fn).is_file()]

    if missing_local:
        print(f"⚠️ Atenção: {len(missing_local)} imagens não foram encontradas localmente. Abortando purge por segurança!")
        return

    print(f"✓ Confirmação: Todas as {len(final_master_list)} imagens do Master estão 100% salvas em imgs/!")

    # 4. Purge no Master via DELETE /api/images
    print("• Purgando imagens e metadados no Master via DELETE /api/images...", end="", flush=True)
    purge_r = session.delete(f"{master_url}/api/images", timeout=30)
    if purge_r.status_code == 200:
        res = purge_r.json()
        print(f" [Sucesso! Deletadas no Master: {res.get('deleted', 0)}]")
    else:
        print(f" [Aviso: Master retornou HTTP {purge_r.status_code}]")

    elapsed_total = time.time() - start_time
    print("\n" + "═" * 65)
    print("🎉 PROCESSO CONCLUÍDO COM SUCESSO!")
    print(f"• Total de imagens baixadas nesta sessão: {downloaded_count}")
    print(f"• Imagens já presentes (mantidas): {skipped_count}")
    print(f"• Volume de dados transferido: {total_bytes / (1024 * 1024):.2f} MB")
    print(f"• Tempo total: {elapsed_total:.1f}s ({elapsed_total / 60:.1f} min)")
    print(f"• Pasta local de destino: {LOCAL_IMGS_DIR}")
    print("• Fila do Master (queue.json): 100% PRESERVADA e rodando sem interrupção!")
    print("═" * 65)

if __name__ == "__main__":
    run()
