#!/usr/bin/env python3
"""每週將「新下載嘅封面」上載去 Notion「📸 Cover 圖庫（sales kit）」頁。

鐵律：一律用 **file block**（一行一個撳得嘅 link）——絕對唔用 image block，
      圖庫頁先唔會成頁展開 preview。

流程（跟喺 update_stats.py 之後）：
  1. update_stats.py 每週 download 新封面入 covers/{label}/{shortCode}.jpg，
     並寫 covers_manifest.json（label / shortCode / views / date）
  2. 本 script 對照 covers_pushed.json（已上載記錄），淨係處理未上載過嘅
  3. 每張新封面：Notion File Upload API 直傳 → append 一個 file block
     落頁尾「🤖 每週自動新增」heading 下面、該 client 嘅 toggle 入面
  4. 上載完寫返 covers_pushed.json（workflow 會 commit 佢，下週唔會重複）

Env: NOTION_TOKEN — 同 push_notion.py 同一個 integration。
     ⚠️ 個 integration 必須連埋「Cover 圖庫」頁（頁面 ⋯ → Connections），
     唔係會 404，本 script 會用人話提你。
用法：python scripts/push_covers_notion.py [--check]（--check 淨係驗頁面接通）
Stdlib only。
"""
import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
COVERS_DIR = REPO_ROOT / "covers"
MANIFEST = REPO_ROOT / "covers_manifest.json"
STATE = REPO_ROOT / "covers_pushed.json"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VER = "2022-06-28"
AUTO_HEADING = "🤖 每週自動新增（file link 式）"


def req(method, path, token, payload=None, raw_body=None, content_type=None):
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VER}
    data = raw_body
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    r = urllib.request.Request(f"{NOTION_BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


def rt(text):
    return {"type": "text", "text": {"content": text}}


def upload_file(token, local_path, filename):
    """Notion File Upload API（single part）→ 回 file_upload id。"""
    fu = req("POST", "/file_uploads", token, payload={"filename": filename, "mode": "single_part"})
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n").encode() \
        + local_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req("POST", f"/file_uploads/{fu['id']}/send", token,
        raw_body=body, content_type=f"multipart/form-data; boundary={boundary}")
    return fu["id"]


def append_children(token, block_id, children):
    return req("PATCH", f"/blocks/{block_id}/children", token, payload={"children": children})


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN not set")
    cfg = json.loads((HERE / "stats_config.json").read_text(encoding="utf-8"))
    page_id = cfg.get("notion_covers_page_id")
    if not page_id:
        sys.exit("stats_config.json 冇 notion_covers_page_id")

    # 接通測試（兼 --check 模式）：404 = integration 未連到呢頁
    try:
        req("GET", f"/blocks/{page_id}", token)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            sys.exit("[FAIL] integration 連唔到 Cover 圖庫頁——去 Notion 開嗰頁 → ⋯ menu → "
                     "Connections → 加返 stats integration，再 re-run。")
        raise
    if "--check" in sys.argv:
        print("[check OK] Cover 圖庫頁接通")
        return

    if not MANIFEST.exists():
        print("[skip] 冇 covers_manifest.json（今週冇新封面）")
        return
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else \
        {"heading_done": False, "toggles": {}, "pushed": []}
    pushed = set(state["pushed"])

    todo = [e for e in manifest
            if e["shortCode"] not in pushed
            and (COVERS_DIR / e["label"] / f"{e['shortCode']}.jpg").exists()]
    if not todo:
        print("[skip] 冇新封面要上 Notion")
        return

    # 頁尾 auto section heading（開一次，之後記住）
    if not state["heading_done"]:
        append_children(token, page_id, [
            {"type": "heading_2", "heading_2": {"rich_text": [rt(AUTO_HEADING)]}},
            {"type": "paragraph", "paragraph": {"rich_text": [rt(
                "以下由每週 workflow 自動加入（新片先有）；一律 file link，撳先開圖。人手精選版喺上面。")]}},
        ])
        state["heading_done"] = True

    n_ok = n_fail = 0
    for e in sorted(todo, key=lambda x: (x["label"], x.get("date", ""))):
        label = e["label"]
        # 每個 client 一個 toggle（開一次，記住 block id）
        if label not in state["toggles"]:
            res = append_children(token, page_id, [
                {"type": "toggle", "toggle": {"rich_text": [rt(f"{label} — 自動新增")]}}])
            state["toggles"][label] = res["results"][0]["id"]
        try:
            views = e.get("views") or 0
            fname = f"{label}_{views // 1000}k_{e['shortCode']}.jpg"
            fid = upload_file(token, COVERS_DIR / label / f"{e['shortCode']}.jpg", fname)
            caption = f"{e.get('date', '')} · {views:,} views · instagram.com/reel/{e['shortCode']}"
            # ⚠️ file block（link 式）——唔准改做 image block
            append_children(token, state["toggles"][label], [
                {"type": "file", "file": {"type": "file_upload",
                                          "file_upload": {"id": fid},
                                          "caption": [rt(caption)]}}])
            pushed.add(e["shortCode"])
            n_ok += 1
            print(f"[up] {label}/{e['shortCode']} ({views:,})", flush=True)
        except Exception as ex:
            n_fail += 1
            print(f"[warn] {label}/{e['shortCode']}: {ex}", flush=True)

    state["pushed"] = sorted(pushed)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] {n_ok} uploaded, {n_fail} failed")
    if n_fail and not n_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
