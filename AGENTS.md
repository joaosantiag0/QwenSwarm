# AGENTS.md

Como o projeto funciona e como operamos ele (histórico da implementação de out/2026).

## Estrutura

- `qwen-editor/` — web UI (stdlib HTTP) que controla o ComfyUI pra edição de imagens com Qwen.
  - `app.py` — servidor único (Python, porta **3000**). Toda a lógica: pool, fila, worker, galeria.
  - `index.html` — SPA sem build (HTML+CSS+JS inline). Servido pelo próprio `app.py`.
  - `prompts.json` — presets de prompt (texto + refs por preset em `preset_refs/`).
  - `image_meta.json` — metadados das imagens geradas (preset, job, timestamps).
  - `staging/pool/` + `pool.json` — **pool de imagens** (uploads persistidos + thumbs 256px).
  - `staging/queue_refs/<item_id>/` — refs enviadas na hora do "quick".
  - `queue.json` — **fila persistente** (estado do lote atual, gravado a cada transição).
- `h3-video-editor/` — UI separada de vídeo (não mexemos).
- `imgs/` — cópia local baixada via SSH do output do servidor (backup manual, apagado do servidor depois).

## Arquitetura da fila (qwen-editor)

- **Worker único** (thread daemon) pega 1 item `pending` por vez, monta o workflow, manda pro
  ComfyUI (`POST /prompt`), espera (`/history/{id}`) e grava meta. ComfyUI só executa 1 por vez.
- **Prefetch**: o worker submete o próximo item enquanto o atual ainda roda (máx. 2 em voo) —
  elimina a folga poll+build+POST entre execuções. Pausa/cancel só valem para itens ainda não
  submetidos; os 2 em voo terminam (mensagem do botão de cancelar reflete isso).
- **Cache de resize**: `staging/resized/<pool_id>_<resolução>.png` — a mesma foto do pool é
  redimensionada uma vez por resolução; os demais ~300 itens do mesmo preset só copiam.
  Com essas 2 otimizações a cadência caiu de ~48,5s para ~37,5s por imagem (-23%).
- **Lane `quick`**: itens "Gerar imagem" priorizam sobre os `batch` (rodam depois do item em
  curso, nunca interrompem o running).
- **Persistência/restart**: `queue.json` + `recover_queue_on_boot()` reconcilia itens `running`
  com o histórico do ComfyUI (completou → `done`; sumiu → volta `pending` e resubmete). O
  navegador pode fechar; a fila é 100% do servidor. Pausa **não** persiste (boot volta rodando).
  Boot com fila grande (130k itens) demora alguns segundos (parse do JSON) antes de servir.
- **Retry**: 1 tentativa extra em erro, depois `error` e segue.
- **Salvou o prompt, gerou tudo**: `POST/PUT /api/prompts` enfileira o preset × todas as imagens
  do pool em modo append (entra no fim da fila ativa). Pool > 500 imagens: pula com aviso.
- **Guarda de lote**: `POST /api/batch` com > 500 itens exige `force=1` (o front só manda depois
  de confirmar o diálogo com a conta real: imagens × presets e tempo estimado).
- **`GET /api/queue` é janelado** (max 120 itens: running + pendentes próximos + últimos
  concluídos/erros, + `items_total`/`truncated` no resumo). Nunca devolve o lote inteiro —
  com 136k itens isso matou a página (JSON de ~50MB).

## Principais endpoints

- `GET /api/pool` · `POST /api/pool` (multipart `images`) · `GET /api/pool/{id}/thumb|file` · `DELETE /api/pool/{id}` (bloqueado se em fila ativa)
- `POST /api/batch` — `pool_ids` (ordenados = prioridade) + `preset_start_id`/`preset_end_id` + settings + `force`
- `POST /api/quick` — item prioritário (prompt do textarea + 1ª imagem do pool + refs)
- `POST /api/edit` — edição de imagem com prioridade máxima (próximo da fila). Aceita `multipart/form-data` (campos `prompt`, `image`/`pool_id`, e refs em `ref_image`/`references`/`ref_pool_ids`) ou `application/json` (`prompt`, `image_base64`/`pool_id`, e refs em `references`/`ref_pool_ids`). Retorna `id`, `status: pending`, `image_url` e `status_url`.
- `GET /api/edit/{id}` — polling de status do item (`pending`, `running`, `done`, `error`), tempo decorrido e URLs de resultado. Suporta também `GET /api/edit?ids=id1,id2`.
- `GET /api/edit/{id}/image` — download direto do binário da imagem gerada (`image/png`). Suporta `?idx=N` se houver múltiplas.
- `DELETE /api/edit/{id}` — cancela se pendente, ou exclui arquivo físico gerado + metadados e registro da fila. Suporta lote via `?ids=id1,id2` ou body `{"ids": [...]}`.
- `GET /api/queue` · `POST /api/queue/pause|resume|cancel` · `DELETE /api/queue` (limpa done/error/cancelled; não mexe no pool)
- `POST /api/regenerate-stale` e `/api/generate` continuam no caminho antigo (`run_job`, fora da fila persistente)
- Galeria: `GET /api/gallery`, `DELETE /api/images` (apaga todos os `qwen_web_*.png` **e** zera o meta)

## Operação / Deploy (servidor Linux)

- Host: `user@192.168.2.183` (senha usada no deploy: `govegogo00` — SSH às vezes falha 1-2x
  intercalado; repetir a conexão resolve; sshd/fail2ban aparentemente instável com senha).
- Arquivos em `/home/user/qwen-editor/`. ComfyUI em `/home/user/ComfyUI/` (API `127.0.0.1:8188`).
- Serviço: `systemctl --user restart qwen-editor` (unit `qwen-editor.service`, `enabled`,
  `ExecStart=start.sh`). **Não** tem sudo; usar `sshpass` + `systemctl --user`.
- Deploy: backup local no servidor (`cp app.py app.py.bak-<ts>`) → `scp app.py index.html` →
  restart do serviço → conferir `curl /api/queue`.
- **Python**: o venv do ComfyUI é **3.12** — `app.py` usa `cgi` (removido em 3.13+). Não migrar
  pra 3.13 sem trocar o parsing multipart.
- Todos os paths do `app.py` são relativos a `WEB_ROOT` (pasta do script) ou `/home/user/ComfyUI`
  — nada hardcoded de Mac.

## Teste local (sem ComfyUI real)

- Script descartável (fora do repo, em temp): importa `app.py`, mockia `COMFY_*`, `POOL_*`,
  `QUEUE_FILE` e `model_available`; sobe um stub HTTP de ComfyUI (`/prompt` completa na hora,
  `/history/{id}` retorna completed com 1 PNG).
- Rodar com **Python ≤3.12 + Pillow** (ex.: venv via `uv venv --python3.12` + `uv pip install pillow`).
- Cenários cobertos: pool, batch imagem-major, guarda de lote ativo, processamento+meta,
  recovery de restart (done e reprocessa), pausa/retomada, cancel, bloqueio de remoção na fila,
  quick priorizado, append com fila ativa, enqueue ao salvar prompt, quick com ref extra.

## Armadilhas conhecidas

- **Nunca criar lote gigante sem confirmação**: pool 401 × 340 presets = 136k gerações (~meses).
  Já aconteceu 2x; a 1ª derrubou a UI (fila sem janela) e foi cancelada via `POST /api/queue/cancel`.
  Se acontecer de novo: cancelar pendentes e `DELETE /api/queue` pra deixar o `queue.json` leve
  (com centenas de milhares de itens, cada transição regrava um JSON de ~40MB).
- Fotos só entram no pool **depois do upload** (auto-upload no drop); o que está só no navegador
  (objeto local) morre no reload.
- Apagar PNGs do `output/` via SSH **não** regera nada; some da galeria e deixa meta órfão
  (inofensivo). Apagar pela UI limpa arquivo + meta juntos.
- "Regerar presets novos" só considera imagens **existentes** na galeria com preset editado depois.
- O form "Gerar imagem" usa a 1ª imagem selecionada no pool (sobe foto nova pro pool se preciso).
