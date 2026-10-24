# AGENTS.md — QwenSwarm

How QwenSwarm works and how we operate it (implementation history as of October 2026).

## Structure

- `qwen-editor/` — Web UI (stdlib HTTP) controlling ComfyUI for image editing with Qwen.
  - `app.py` — Single server (Python, port **3000**). Contains core logic: pool, queue, worker, gallery, and cluster lease coordinator.
  - `index.html` — Zero-build SPA (inline HTML+CSS+JS). Served directly by `app.py`.
  - `prompts.json` — Prompt presets (text + reference images per preset stored in `preset_refs/`).
  - `image_meta.json` — Generated image metadata (preset, job, timestamps).
  - `staging/pool/` + `pool.json` — **Image pool** (persisted uploads + 256px thumbnails).
  - `staging/queue_refs/<item_id>/` — Reference images uploaded during on-the-fly "quick" edits.
  - `staging/resized/<pool_id>_<resolution>.png` — Resize cache for pool images.
  - `queue.json` — **Persistent queue** (state of active batch, updated on each transition).
- `satellite-worker/` — Autonomous worker for distributed secondary nodes (e.g. RTX 3060 6GB Laptop).
  - `worker.py` — Leases tasks from Master, caches assets, drives local ComfyUI, and buffers completions offline in `outbox/`.
- `h3-video-editor/` — Standalone video editing UI (untouched).
- `imgs/` — Local copy downloaded via SSH from server output (manual backup, purged from the server later).

## Queue & Worker Architecture (qwen-editor)

- **Dedicated local worker** (daemon thread) consumes 1 `pending` item at a time, builds the workflow, submits it to ComfyUI (`POST /prompt`), awaits completion (`/history/{id}`), and writes metadata. ComfyUI only executes 1 job at a time per GPU instance.
- **Prefetching**: The worker submits the next item while the current one is still rendering (max 2 in flight) — eliminates idle polling/build/POST gaps between jobs. Pause/cancel only apply to items not yet submitted; in-flight jobs finish cleanly (the cancel button UI dialogue clarifies this).
- **Resize Cache**: `staging/resized/<pool_id>_<resolution>.png` — Each pool image is resized only once per target resolution; all subsequent jobs for the same preset reuse the cached file. Combined with prefetching, execution cadence dropped from ~48.5s to ~37.5s per image (-23%).
- **`quick` Lane**: "Generate image" / priority edits take precedence over active `batch` jobs (they run right after the currently rendering item without aborting it).
- **Persistence & Recovery**: `queue.json` + `recover_queue_on_boot()` reconciles `running` items against ComfyUI history on startup (completed → `done`; missing → reverted to `pending` and re-submitted). The browser tab can close safely; the queue lives entirely on the server. Pausing does **not** persist across restarts (boots back in running state). Booting with huge queues (>130k items) takes a few seconds to parse JSON before serving requests.
- **Retry Logic**: 1 automatic retry on failure, then transitions to `error` and moves forward.
- **Save Prompt Enqueueing**: `POST/PUT /api/prompts` automatically enqueues the preset × all pool images in append mode (added to the end of the active queue). If pool size > 500 images, auto-enqueueing is skipped with a warning.
- **Batch Guardrail**: `POST /api/batch` with > 500 items requires `force=1` (the frontend only sends this after user confirmation showing the exact math: images × presets and estimated completion time).
- **Windowed `GET /api/queue`**: Capped at 120 items (running + next pending items + recently completed/errors, with `items_total` and `truncated` flag in the summary). Never returns the entire batch — with 136k items, an unwindowed response (~50MB JSON) crashed the web client.

## Distributed Cluster & Satellite Nodes

- **Batch Leasing**: Satellite workers (`satellite-worker/worker.py`) lease blocks of pending tasks from Master via `POST /api/worker/lease`. Leased items are locked with an automatic 45-minute expiration failsafe.
- **Pre-caching**: Upon receiving a lease, the worker immediately pre-downloads pool images and preset references while the Master is online.
- **Offline Resilience (`outbox/`)**: If the Master server (`192.168.2.183`) goes offline or is turned off:
  - The satellite continues rendering its leased batch uninterrupted.
  - Generated images and metadata are stored in `/home/joao/satellite_worker/outbox/`.
  - As soon as the Master comes back online, the worker automatically flushes the outbox via `POST /api/worker/complete` and requests the next block of tasks.
- **Low-VRAM Hardware Optimization**: For constrained GPUs (e.g. RTX 3060 6GB Laptop), ComfyUI runs with `--lowvram --vram-headroom 1.2 --reserve-vram 0.8`. UNet weights fit within ~3.9GB VRAM while remaining weights and CLIP dynamically offload to system RAM without OOM errors.

## Key API Endpoints

- `GET /api/pool` · `POST /api/pool` (multipart `images`) · `GET /api/pool/{id}/thumb|file` · `DELETE /api/pool/{id}` (blocked if referenced in an active queue)
- `POST /api/batch` — `pool_ids` (ordered by priority) + `preset_start_id`/`preset_end_id` + settings + `force`
- `POST /api/quick` — Priority item (textarea prompt + 1st pool image + references)
- `POST /api/edit` — Highest-priority image editing (next in queue). Accepts `multipart/form-data` (`prompt`, `image`/`pool_id`, and references in `ref_image`/`references`/`ref_pool_ids`) or `application/json` (`prompt`, `image_base64`/`pool_id`, and `references`/`ref_pool_ids`). Returns `id`, `status: pending`, `image_url`, and `status_url`.
- `GET /api/edit/{id}` — Item status polling (`pending`, `running`, `done`, `error`), elapsed time, and result image URLs. Also supports multi-ID lookup via `GET /api/edit?ids=id1,id2`.
- `GET /api/edit/{id}/image` — Direct binary image download (`image/png`). Supports `?idx=N` if multiple images were generated.
- `DELETE /api/edit/{id}` — Cancels if pending, or permanently removes generated file, metadata, and queue record. Supports batch deletion via `?ids=id1,id2` or JSON body `{"ids": [...]}`.
- `GET /api/queue` · `POST /api/queue/pause|resume|cancel` · `DELETE /api/queue` (cleans done/error/cancelled; keeps pool intact)
- `POST /api/worker/lease` — Leases $N$ pending items to an authenticated satellite worker.
- `POST /api/worker/complete` — Receives completed image and metadata from a satellite worker.
- `POST /api/worker/fail` — Reports a failed item and re-enqueues it for retry.
- Gallery: `GET /api/gallery`, `DELETE /api/images` (deletes all `qwen_web_*.png` files **and** resets metadata)

## Operations / Deployment (Linux Servers)

### Master Server (`192.168.2.183`)
- User: `user@192.168.2.183` (password used during deploy: `govegogo00` — SSH may occasionally fail 1-2 times consecutively; retrying resolves it).
- Directory: `/home/user/qwen-editor/`. ComfyUI at `/home/user/ComfyUI/` (API `127.0.0.1:8188`).
- Service: `systemctl --user restart qwen-editor` (unit `qwen-editor.service`, `enabled`, `ExecStart=start.sh`). Does **not** have sudo; use `sshpass` + `systemctl --user`.
- Deploy: Server backup (`cp app.py app.py.bak-<ts>`) → `scp app.py index.html` → restart service → verify `curl /api/queue`.
- **Python**: ComfyUI virtualenv is **Python 3.12** — `app.py` uses `cgi` (deprecated/removed in 3.13+). Do not upgrade to 3.13 without replacing multipart form parsing.
- Paths in `app.py` are relative to `WEB_ROOT` (script directory) or `/home/user/ComfyUI` — no hardcoded macOS paths.

### Satellite Worker (`192.168.2.191` - Predator Laptop 3060 6GB)
- User: `joao@192.168.2.191` (password & sudo: `govegogo00`).
- ComfyUI unit: `/etc/systemd/system/comfyui.service` (`--listen 127.0.0.1 --port 8188 --lowvram --vram-headroom 1.2 --reserve-vram 0.8`).
- Worker unit: `/etc/systemd/system/qwen-satellite.service` (`ExecStart=/home/joao/ComfyUI/venv/bin/python -u /home/joao/satellite_worker/worker.py`).
- Logs: `journalctl -u qwen-satellite.service -f` and `journalctl -u comfyui.service -f`.

## Local Testing (without real ComfyUI)

- Disposable script (outside repo, in `/tmp`): Imports `app.py`, mocks `COMFY_*`, `POOL_*`, `QUEUE_FILE`, and `model_available`; launches an HTTP ComfyUI stub (`/prompt` completes immediately, `/history/{id}` returns completed with 1 PNG).
- Run with **Python ≤3.12 + Pillow** (e.g. venv via `uv venv --python3.12 && uv pip install pillow`).
- Tested scenarios: Pool uploads, image-major batching, active batch guardrails, execution and metadata synchronization, restart recovery (done & reprocess), pause/resume, cancellation, active item deletion locks, quick lane priority, append mode with active queue, auto-enqueue on prompt save, and quick edits with additional references.

## Known Pitfalls & Gotchas

- **Never launch massive batches without confirmation**: A pool of 401 images × 340 presets = 136k generations (~months of compute). If triggered accidentally, cancel pending items and call `DELETE /api/queue` to keep `queue.json` lightweight (with hundreds of thousands of items, every state transition rewires a ~40MB JSON).
- Images only enter the pool **after upload completion** (auto-upload on drag-and-drop); files existing only in browser memory are lost on page refresh.
- Deleting output PNGs directly from disk via SSH **does not** trigger regeneration; they disappear from the gallery leaving orphaned metadata entries (harmless). Deleting through the Web UI cleans both files and metadata together.
- "Regenerate new presets" only considers images **already present** in the gallery whose preset was modified afterwards.
- The "Generate image" form uses the 1st selected image in the pool (uploads a new photo to the pool if needed).
