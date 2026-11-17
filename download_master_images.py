#!/usr/bin/env python3
"""
download_master_images.py

Baixa as imagens geradas (qwen_web_*.png) do Master para uma pasta local
(ex.: /Volumes/3Tb/aiphotos) SEM tocar na fila e SEM risco de re-geração.

Por que é seguro:
  • Usa apenas GET (/api/gallery?all=1 e /output/<arquivo>) — nenhuma escrita,
    nenhum DELETE, nenhuma alteração em queue.json ou image_meta.json.
  • Não mexe em arquivos no servidor (nada de SSH/rm), então nada é removido
    da galeria e nada pode ser re-enfileirado pelo recover_queue_on_boot().
  • Download atômico: grava em .part e só renomeia após validar o cabeçalho PNG.
  • Idempotente: arquivos já presentes e válidos são pulados, nunca apagados.
  • Purge é opt-in e só acontece no final, arquivo por arquivo
    (DELETE /api/image/<nome>), e apenas para imagens que já existem válidas
    no destino — nunca usa DELETE /api/images (que apagaria tudo, inclusive o
    que ainda não foi baixado). Sem --purge, nada é apagado no servidor.

Uso:
  python3 download_master_images.py --dest /Volumes/3Tb/aiphotos
  python3 download_master_images.py --dest /Volumes/3Tb/aiphotos --watch 60
  python3 download_master_images.py --dest /Volumes/3Tb/aiphotos --purge

--watch N repete a sincronização a cada N segundos (Ctrl+C para parar),
o que permite deixar rodando enquanto a fila continua gerando.
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

MASTER_HOSTS = ["http://192.168.2.183:3000", "http://100.89.150.119:3000"]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_VALID_BYTES = 10_000


def get_active_master_url(hosts: list[str]) -> str:
    for host in hosts:
        try:
            r = requests.get(f"{host}/api/system/gpu", timeout=4)
            if r.status_code == 200:
                return host
        except Exception:
            continue
    raise RuntimeError(
        "Não foi possível conectar ao Master em nenhum endereço conhecido "
        f"({', '.join(hosts)}). Verifique rede/Tailscale e se o serviço está no ar."
    )


def create_session(workers: int) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=workers + 4, pool_maxsize=workers + 4, max_retries=3)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def fetch_master_image_list(master_url: str) -> list[str]:
    r = requests.get(f"{master_url}/api/gallery?all=1", timeout=60)
    r.raise_for_status()
    images = r.json().get("images", [])
    return [img["filename"] for img in images if img.get("filename")]


def local_file_ok(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < MIN_VALID_BYTES:
        return False
    try:
        with path.open("rb") as f:
            return f.read(8) == PNG_MAGIC
    except OSError:
        return False


def download_single_image(
    master_url: str, filename: str, dest_dir: Path, session: requests.Session
) -> tuple[str, bool, int, str]:
    dest_path = dest_dir / filename
    tmp_path = dest_dir / f"{filename}.part"

    try:
        if local_file_ok(dest_path):
            return (filename, True, 0, "already_exists")

        with session.get(f"{master_url}/output/{filename}", stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as out_f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        out_f.write(chunk)

        if not tmp_path.is_file() or tmp_path.stat().st_size < MIN_VALID_BYTES:
            tmp_path.unlink(missing_ok=True)
            return (filename, False, 0, "arquivo menor que 10KB ou incompleto")

        with tmp_path.open("rb") as f:
            if f.read(8) != PNG_MAGIC:
                tmp_path.unlink()
                return (filename, False, 0, "cabeçalho PNG inválido")

        size = tmp_path.stat().st_size
        tmp_path.replace(dest_path)
        return (filename, True, size, "")

    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return (filename, False, 0, str(exc))


def sync_once(
    master_url: str, dest_dir: Path, session: requests.Session, workers: int
) -> tuple[int, int, int, int, list[str]]:
    """Uma passada: baixa tudo que está no Master e ainda não existe local. Retorna estatísticas."""
    filenames = fetch_master_image_list(master_url)
    pending = [fn for fn in filenames if not local_file_ok(dest_dir / fn)]

    downloaded = skipped = errors = total_bytes = 0
    failed: list[str] = []

    if not pending:
        return (0, len(filenames), 0, 0, [])

    print(f"[*] {len(filenames)} imagens na galeria do Master · {len(pending)} novas para baixar.")
    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download_single_image, master_url, fn, dest_dir, session): fn
            for fn in pending
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            fn, ok, size, msg = fut.result()
            if ok:
                if msg == "already_exists":
                    skipped += 1
                else:
                    downloaded += 1
                    total_bytes += size
            else:
                errors += 1
                failed.append(fn)
                print(f"[!] Falha em {fn}: {msg}", flush=True)

            if i % 200 == 0 or i == len(pending):
                elapsed = max(0.001, time.time() - start)
                speed = (total_bytes / (1024 * 1024)) / elapsed
                print(
                    f"[{i}/{len(pending)}] baixadas: {downloaded} · "
                    f"já existiam: {skipped} · {speed:.1f} MB/s · erros: {errors}",
                    flush=True,
                )

    return (downloaded, skipped, errors, total_bytes, failed)


def purge_downloaded(
    master_url: str, dest_dir: Path, session: requests.Session, workers: int
) -> tuple[int, int, int, int]:
    """Apaga no Master, arquivo por arquivo, apenas o que já está salvo e válido localmente."""
    print("\n[Purge] Comparando galeria do Master com os arquivos locais...")
    filenames = fetch_master_image_list(master_url)
    candidates = [fn for fn in filenames if local_file_ok(dest_dir / fn)]
    not_backed_up = len(filenames) - len(candidates)
    print(
        f"[Purge] {len(filenames)} na galeria · {len(candidates)} já salvas (apagando) · "
        f"{not_backed_up} sem cópia local (mantidas no Master)"
    )
    if not candidates:
        return (0, 0, 0, not_backed_up)

    deleted = errors = 0
    start = time.time()

    def delete_one(fn: str) -> tuple[bool, str]:
        try:
            r = session.delete(f"{master_url}/api/image/{quote(fn)}", timeout=60)
            if r.status_code == 200:
                return (True, "")
            return (False, f"HTTP {r.status_code}")
        except Exception as exc:
            return (False, str(exc))

    with ThreadPoolExecutor(max_workers=min(8, workers)) as executor:
        futures = {executor.submit(delete_one, fn): fn for fn in candidates}
        for i, fut in enumerate(as_completed(futures), start=1):
            ok, msg = fut.result()
            if ok:
                deleted += 1
            else:
                errors += 1
                print(f"[!] Falha ao apagar {futures[fut]}: {msg}", flush=True)
            if i % 500 == 0 or i == len(candidates):
                elapsed = max(0.001, time.time() - start)
                print(
                    f"[{i}/{len(candidates)}] apagadas: {deleted} · erros: {errors} · "
                    f"{i / elapsed:.0f} itens/s",
                    flush=True,
                )

    try:
        remaining = len(fetch_master_image_list(master_url))
        print(f"[Purge] Galeria do Master agora: {remaining} imagem(ns) restante(s).")
    except Exception:
        pass
    return (deleted, errors, not_backed_up, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Baixa imagens geradas do Master com segurança.")
    parser.add_argument("--dest", default=str(Path(__file__).resolve().parent / "imgs"),
                        help="pasta local de destino (padrão: ./imgs)")
    parser.add_argument("--workers", type=int, default=12, help="downloads paralelos (padrão: 12)")
    parser.add_argument("--watch", type=int, default=0,
                        help="repetir a sincronização a cada N segundos (0 = uma passada só)")
    parser.add_argument("--purge", action="store_true",
                        help="apagar no Master (arquivo por arquivo) só as imagens já salvas e válidas locais")
    parser.add_argument("--host", action="append", default=None,
                        help="URL do Master (pode repetir; padrão: IP local + Tailscale)")
    args = parser.parse_args()

    dest_dir = Path(args.dest).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest_dir.is_dir():
        raise SystemExit(f"Destino inválido: {dest_dir}")

    hosts = args.host or MASTER_HOSTS
    master_url = get_active_master_url(hosts)
    session = create_session(args.workers)

    print("═" * 65)
    print("🚀 DOWNLOAD DE IMAGENS GERADAS (somente leitura no servidor)")
    print(f"[*] Master: {master_url}")
    print(f"[*] Destino: {dest_dir}")
    print(f"[*] Workers: {args.workers} · purge: {'SIM' if args.purge else 'não'}")
    print("═" * 65)

    total_downloaded = total_bytes = 0
    first = True
    while True:
        try:
            downloaded, skipped, errors, nbytes, failed = sync_once(
                master_url, dest_dir, session, args.workers
            )
        except Exception as exc:
            print(f"[!] Passada falhou: {exc}")
            downloaded = skipped = errors = nbytes = 0
            failed = []

        total_downloaded += downloaded
        total_bytes += nbytes
        print(
            f"✓ Passada: {downloaded} baixadas · {skipped} já existiam · "
            f"{nbytes / (1024 * 1024):.1f} MB · {errors} erro(s)"
        )

        if first and args.purge:
            purge_downloaded(master_url, dest_dir, session, args.workers)
        first = False

        if not args.watch:
            break

        print(f"⏳ aguardando {args.watch}s para a próxima passada (Ctrl+C para sair)...")
        time.sleep(args.watch)
        try:
            master_url = get_active_master_url(hosts)
        except Exception:
            pass

    print("═" * 65)
    print(f"Total baixado nesta sessão: {total_downloaded} imagens · "
          f"{total_bytes / (1024 * 1024):.1f} MB")
    print(f"Pasta: {dest_dir}")
    print("Fila do Master: intacta (nenhuma alteração em queue.json).")
    print("═" * 65)


if __name__ == "__main__":
    main()
