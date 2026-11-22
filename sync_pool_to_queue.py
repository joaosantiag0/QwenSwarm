#!/usr/bin/env python3
"""Sync all pool images to the active queue and shuffle pending items."""

import json
import random
import time
import uuid
from pathlib import Path

WEB_ROOT = Path("/home/user/qwen-editor")
POOL_FILE = WEB_ROOT / "staging" / "pool.json"
QUEUE_FILE = WEB_ROOT / "queue.json"
PROMPTS_FILE = WEB_ROOT / "prompts.json"

def main():
    pool = json.load(POOL_FILE.open(encoding="utf-8"))
    queue_data = json.load(QUEUE_FILE.open(encoding="utf-8"))
    prompts = json.load(PROMPTS_FILE.open(encoding="utf-8"))

    items = queue_data.get("items", [])
    batch_id = queue_data.get("current_batch_id") or (items[0].get("batch_id") if items else "bdc945a8")

    # Map pool_id -> set of prompt_preset_id already in queue
    pool_has_presets = {}
    for it in items:
        pid = it.get("pool_id")
        psid = it.get("prompt_preset_id")
        if pid and psid:
            pool_has_presets.setdefault(pid, set()).add(psid)

    now = time.time()
    new_items = []
    max_order = max((it.get("order") or 0 for it in items), default=0)

    for p in pool:
        pid = p["id"]
        existing_presets = pool_has_presets.get(pid, set())
        w = p.get("width")
        h = p.get("height")
        
        for preset in prompts:
            psid = preset["id"]
            if psid in existing_presets:
                continue
            max_order += 1
            item = {
                "id": uuid.uuid4().hex[:8],
                "batch_id": batch_id,
                "order": max_order,
                "lane": "batch",
                "status": "pending",
                "pool_id": pid,
                "prompt": preset["text"],
                "prompt_preset_id": psid,
                "prompt_preset_label": preset.get("label") or "",
                "ref_ids": None,
                "extra_refs": [],
                "model": "qwen21_pruna",
                "quality": "normal",
                "resolution": 768,
                "steps": 8,
                "width": w,
                "height": h,
                "prompt_id": None,
                "images": [],
                "error": None,
                "retries": 0,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
            }
            new_items.append(item)

    print(f"Adding {len(new_items)} new queue items for missing pool image presets...")
    all_items = items + new_items

    # Shuffle all pending items
    pending_items = [it for it in all_items if it.get("status") == "pending" and it.get("lane") != "quick"]
    orders = list(range(1, len(pending_items) + 1))
    random.shuffle(orders)
    for it, ord_val in zip(pending_items, orders):
        it["order"] = ord_val

    # Sort queue items so iteration order matches quick first, priority, then order
    all_items.sort(
        key=lambda x: (
            0 if x.get("lane") == "quick" else 1,
            -int(x.get("priority") or 0),
            x.get("order") or 0,
        )
    )

    queue_data["items"] = all_items
    queue_data["current_batch_id"] = batch_id

    # Atomic write to queue.json
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(queue_data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(QUEUE_FILE)

    print(f"DONE! Queue updated: Total={len(all_items)}, Pending={len(pending_items)}, Done={sum(1 for x in all_items if x.get('status') == 'done')}")

if __name__ == "__main__":
    main()
