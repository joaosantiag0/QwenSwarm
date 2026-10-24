# QwenSwarm: Distributed ComfyUI & Qwen Image Generation Cluster

A distributed, production-grade image generation and editing cluster powered by **ComfyUI** and **Qwen Image 2.1** (quantized GGUF with Pruna 8-step LoRA). Built for high-throughput batch generation, real-time priority editing, and resilient multi-GPU scaling across heterogeneous hardware.

---

## Architecture Overview

```
                      ┌──────────────────────────────────────────────┐
                      │              Web UI / Clients                │
                      │       (REST API / Web Studio UI)             │
                      └──────────────────────┬───────────────────────┘
                                             │ HTTP :3000
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │          MASTER SERVER (qwen-editor)         │
                      │  • Persistent Queue (queue.json)             │
                      │  • Image Pool & Preset Metadata Management   │
                      │  • Lease Coordinator (/api/worker/*)         │
                      │  • Embedded ComfyUI Worker (Local GPU)       │
                      └──────────────┬───────────────────────────────┘
                                     │
                 ┌───────────────────┴───────────────────┐
                 │ Lease & Sync                          │ Autonomous Offline Buffer
                 ▼                                       ▼
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│     LOCAL WORKER (Master)       │     │    SATELLITE WORKER (Remote)    │
│ • RTX 3060 12GB (Desktop)       │     │ • RTX 3060 6GB (Laptop / Node)  │
│ • In-flight Prefetching         │     │ • Low-VRAM Dynamic Offloading   │
│ • Local ComfyUI (127.0.0.1:8188)│     │ • Pre-cached Pool & Preset Refs │
└─────────────────────────────────┘     │ • Local Persistent Outbox Buffer│
                                        │ • Local ComfyUI (127.0.0.1:8188)│
                                        └─────────────────────────────────┘
```

The system coordinates work across multiple nodes:
- **Master Server (`qwen-editor`)**: Serves the single-page web UI, maintains image pool uploads, manages prompts and reference images, coordinates priority tasks, and runs a persistent job queue.
- **Satellite Workers (`satellite-worker`)**: Standalone worker instances running on remote nodes (such as low-VRAM 6GB laptops). They lease blocks of pending tasks, pre-cache input assets, generate images locally, and deliver results to the master.
- **Offline Fault Tolerance (`outbox/`)**: If the master server is turned off or disconnected, satellite workers continue rendering their leased tasks locally without interruptions. Generated images and metadata are stored in a persistent local `outbox/` directory, which automatically flushes to the master as soon as connectivity is restored.

---

## Key Features

- **Blazing Fast Pipeline**:
  - **Prefetching**: Submits the next prompt to ComfyUI while the current one is still finishing, eliminating idle gap between jobs.
  - **Resize Cache**: Resizes input pool images once per resolution and reuses them across batch runs.
  - Generates full 768p/1024p images in **~37s to 46s** per image.
- **Dual-Lane Prioritization**:
  - **Quick Lane**: Instant priority for single-image edits and API requests without stopping active batch runs.
  - **Batch Lane**: Handles hundreds or thousands of combinatorial preset generations cleanly in sequence.
- **Hardware-Adaptive Inference**:
  - Master node runs full-precision CLIP + GGUF UNet on 12GB VRAM.
  - Satellite nodes run on constrained hardware (RTX 3060 6GB) using `--lowvram`, `--vram-headroom 1.2`, and `--reserve-vram 0.8` with pinned CPU RAM offloading.
- **Safe Batch Guardrails**: Protects against accidental queue bloat when scaling large pools with multiple presets.
- **Pure Python Backend**: Zero external runtime dependencies beyond Python 3.12, standard library HTTP server, and Pillow.

---

## Repository Structure

```
.
├── qwen-editor/                 # Master Server & Web UI
│   ├── app.py                   # Master HTTP server, queue engine & API
│   ├── index.html               # Single-page web interface (no build step needed)
│   ├── download_pruna_lora.py   # Helper script to download 8-step LoRA
│   └── start.sh                 # Startup script for Master service
├── satellite-worker/            # Distributed Satellite Node
│   └── worker.py                # Autonomous worker with lease & outbox support
├── h3-video-editor/             # Video generation interface
├── AGENTS.md                    # Operational history, deployment notes & rules
└── README.md                    # Project documentation
```

---

## Models & Dependencies

### Required Models in ComfyUI (`models/`):
- `diffusion_models/qwen-image-2.1-UC-Q4_K_M.gguf` (~4.3 GB)
- `text_encoders/qwen3vl_8b_int8_convrot.safetensors` (~8.8 GB)
- `vae/qwen_image_2.1_vae_bf16.safetensors` (~645 MB)
- `loras/p_qwen_image_2.1_8step_v0.1.safetensors` (~321 MB)

### Required Custom Nodes in ComfyUI (`custom_nodes/`):
- `ComfyUI-GGUF`
- `ComfyUI-MiniMax-H3-Image-Studio`

---

## Getting Started

### 1. Master Server Setup

1. Start ComfyUI on the master machine (`127.0.0.1:8188`):
   ```bash
   python main.py --listen 127.0.0.1 --port 8188
   ```

2. Start the master editor server:
   ```bash
   cd qwen-editor
   python app.py
   ```
   The UI will be available at `http://<MASTER_IP>:3000`.

### 2. Satellite Worker Setup

1. Configure ComfyUI on the satellite node with low-VRAM optimizations:
   ```bash
   python main.py --listen 127.0.0.1 --port 8188 --lowvram --vram-headroom 1.2 --reserve-vram 0.8
   ```

2. Run the satellite worker pointing to the master server:
   ```bash
   cd satellite-worker
   export MASTER_URL="http://192.168.2.183:3000"
   export WORKER_ID="satellite-node-01"
   export BATCH_SIZE=15
   python worker.py
   ```

### 3. Running as Systemd Services

On Linux machines, services can be managed via systemd:

**/etc/systemd/system/comfyui.service**
```ini
[Unit]
Description=ComfyUI Service
After=network.target

[Service]
Type=simple
User=<USER>
WorkingDirectory=/home/<USER>/ComfyUI
ExecStart=/home/<USER>/ComfyUI/venv/bin/python main.py --listen 127.0.0.1 --port 8188 --lowvram --vram-headroom 1.2 --reserve-vram 0.8
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**/etc/systemd/system/qwen-satellite.service**
```ini
[Unit]
Description=Qwen Satellite Worker Service
After=network.target comfyui.service
Wants=comfyui.service

[Service]
Type=simple
User=<USER>
WorkingDirectory=/home/<USER>/satellite_worker
ExecStart=/home/<USER>/ComfyUI/venv/bin/python -u /home/<USER>/satellite_worker/worker.py
Restart=always
RestartSec=10
Environment="MASTER_URL=http://192.168.2.183:3000"
Environment="COMFY_URL=http://127.0.0.1:8188"
Environment="WORKER_ID=satellite-01"
Environment="BATCH_SIZE=15"

[Install]
WantedBy=multi-user.target
```

Enable and start services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable comfyui.service qwen-satellite.service
sudo systemctl start comfyui.service qwen-satellite.service
```

---

## API Reference

### Priority Image Editing
- **`POST /api/edit`**
  Submits an image edit with maximum priority into the queue.
  - Supports `multipart/form-data` and `application/json`.
  - Parameters: `prompt`, `image` / `image_base64` or `pool_id`, and optional `references` / `ref_image`.
  - Returns: `{ "id": "<item_id>", "status": "pending", "image_url": "...", "status_url": "..." }`

- **`GET /api/edit/<id>`**
  Poll item execution status (`pending`, `running`, `done`, `error`) and retrieve output image metadata.

- **`GET /api/edit/<id>/image`**
  Direct binary download of the generated PNG (`image/png`).

- **`DELETE /api/edit/<id>`**
  Cancels a pending task or removes generated output and metadata. Supports batch deletion via `?ids=id1,id2`.

### Queue Management
- **`GET /api/queue`**: Returns windowed active queue status, running item details, and summary stats.
- **`POST /api/queue/pause`**: Pauses queue dispatching.
- **`POST /api/queue/resume`**: Resumes queue processing.
- **`POST /api/queue/cancel`**: Cancels all pending tasks in the current batch.
- **`DELETE /api/queue`**: Cleans up finished and cancelled tasks.

### Distributed Worker Endpoints
- **`POST /api/worker/lease`**: Leases a block of $N$ pending items to an authenticated worker node with an automatic timeout.
- **`POST /api/worker/complete`**: Submits a completed image and metadata from a satellite node.
- **`POST /api/worker/fail`**: Re-queues a failed task for retry.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
