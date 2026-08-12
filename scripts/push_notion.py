#!/usr/bin/env python3
"""Push the monthly views table + per-video audit list to the Notion
"Monthly views record" page.

Runs AFTER update_stats.py (reads the monthly_breakdown.json it wrote) and
AFTER the site commit step — so a Notion hiccup fails the workflow loudly
(owner gets an email) without ever blocking the website update.

The page carries two layers:
  1. the monthly summary table (what the hiring site's headline number sums to)
  2. one collapsed toggle per client listing EVERY video counted, with a link
     — so the count can be spot-checked by hand against what we actually made.

Env: NOTION_TOKEN — an integration token with access to the target page.
Stdlib only.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
NOTION_BASE = "https://api.notion.com/v1"
MN = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
      7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
CHUNK = 90   # Notion allows 100 children per request; stay under it


def notion_req(method, path, token, payload=None):
    req = urllib.request.Request(
        f"{NOTION_BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def rt(text, bold=False, link=None):
    seg = {"type": "text", "text": {"content": text}}
    if link:
        seg["text"]["link"] = {"url": link}
    if bold:
        seg["annotations"] = {"bold": True}
    return seg


def table_row(cells):
    return {"type": "table_row",
            "table_row": {"cells": [[rt(c[0], bold=c[1])] if isinstance(c, tuple) else [rt(c)]
                                    for c in cells]}}


def filter_desc(spec):
    t = spec.get("type")
    if t == "caption_contains":
        return f"caption 含「{spec['value']}」"
    if t == "any_video":
        return "整個帳號嘅片都當我哋出品"
    if t == "video_not_sponsor":
        return "片，但剔走第三方 brand ad"
    return t or "?"


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN not set")
    cfg = json.loads((HERE / "stats_config.json").read_text(encoding="utf-8"))
    page_id = cfg["notion_page_id"]
    acc_by_label = {a["label"]: a for a in cfg["accounts"]}
    bd = json.loads((REPO_ROOT / "monthly_breakdown.json").read_text(encoding="utf-8"))

    labels = bd["labels"]
    frozen = set(bd["frozen_labels"])
    monthly = {lbl: {int(m): cv for m, cv in bm.items()} for lbl, bm in bd["monthly"].items()}
    per_video = bd.get("per_video", {})

    def cell(lbl, m):
        c, v = monthly.get(lbl, {}).get(m, [0, 0])
        return f"{v:,} ({c})" if c else "—"

    rows = [table_row([("月份", True)] + [(l, True) for l in labels] + [("每月總 Views", True)])]
    tot = {l: [0, 0] for l in labels}
    grand = 0
    for m in range(1, 13):
        if all(monthly.get(l, {}).get(m, [0, 0])[0] == 0 for l in labels):
            continue
        mt = sum(monthly.get(l, {}).get(m, [0, 0])[1] for l in labels)
        for l in labels:
            c, v = monthly.get(l, {}).get(m, [0, 0])
            tot[l][0] += c
            tot[l][1] += v
        grand += mt
        rows.append(table_row([MN[m]] + [cell(l, m) for l in labels] + [(f"{mt:,}", True)]))
    rows.append(table_row([("全期總計", True)]
                          + [(f"{tot[l][1]:,} ({tot[l][0]})", True) for l in labels]
                          + [(f"{grand:,}", True)]))
    grand_videos = sum(tot[l][0] for l in labels)

    frozen_note = ("；" + "、".join(sorted(frozen)) + " 已停 collaboration，凍結 snapshot 唔再更新") if frozen else ""
    head = [
        {"type": "heading_1",
         "heading_1": {"rich_text": [rt(f"{bd['year']} 每月總 Views — 我哋整嘅片")]}},
        {"type": "quote",
         "quote": {"rich_text": [rt(
             f"Snapshot {bd['snapshot']}｜自動更新：GitHub Actions 逢星期一 03:00 HKT"
             f"（repo ev-hiring-site），同一組數字同步出 hiring.ev-hk.com。"
             f"Views = 累計終身數（IG videoPlayCount），按 post 月份歸類；"
             f"cross-post 帳號（Yoru）＝ IG＋Facebook 加總{frozen_note}。")]}},
        {"type": "table",
         "table": {"table_width": len(labels) + 2, "has_column_header": True,
                   "children": rows}},
        {"type": "paragraph",
         "paragraph": {"rich_text": [rt(f"括號 = 條數。全期合共 {grand_videos} 條片、{grand:,} views。"
                                        "手動即時更新：GitHub → ev-hiring-site → Actions → Update stats → Run workflow。")]}},
        {"type": "heading_2",
         "heading_2": {"rich_text": [rt("逐條片明細（人手覆核用）")]}},
        {"type": "paragraph",
         "paragraph": {"rich_text": [rt(
             "下面列晒每條被計入嘅片，撳日期入返原 post。抽查方法：撳開一個 client，"
             "對下條數同你記得嗰個月交咗幾多條；見到唔應該計嘅片（例如 client 自己出、"
             "或者 brand ad），話我知邊條，我改返 filter。")]}},
    ]

    # Full replace: delete existing blocks, then append fresh content.
    while True:
        kids = notion_req("GET", f"/blocks/{page_id}/children?page_size=100", token)
        for b in kids["results"]:
            notion_req("DELETE", f"/blocks/{b['id']}", token)
        if not kids.get("has_more"):
            break
    notion_req("PATCH", f"/blocks/{page_id}/children", token, {"children": head})

    # One toggle per client. Appended separately so no request exceeds the
    # 100-children limit, and one client's failure is easy to localise.
    for lbl in labels:
        acc = acc_by_label.get(lbl, {})
        vids = per_video.get(lbl, [])
        n = tot[lbl][0]
        if lbl in frozen:
            bullets = [{"type": "paragraph", "paragraph": {"rich_text": [rt(
                f"已停 collaboration，凍結喺 {n} 條 / {tot[lbl][1]:,} views 嘅 snapshot，"
                "唔再逐條拉——所以冇明細。")]}}]
        else:
            desc = filter_desc(acc.get("filter", {}))
            bullets = [{"type": "paragraph", "paragraph": {"rich_text": [rt(
                f"計入條件：{desc}，2026 年內 post。")]}}]
            for v in vids:
                fb = f"（含 FB {v['fb']:,}）" if v.get("fb") else ""
                line = [rt(v["date"], link=v["url"] or None),
                        rt(f" · {v['views']:,} views{fb} · {v['caption']}")]
                bullets.append({"type": "bulleted_list_item",
                                "bulleted_list_item": {"rich_text": line}})

        first, rest = bullets[:CHUNK], bullets[CHUNK:]
        res = notion_req("PATCH", f"/blocks/{page_id}/children", token, {"children": [
            {"type": "toggle", "toggle": {
                "rich_text": [rt(f"{lbl} — {n} 條 / {tot[lbl][1]:,} views", bold=True)],
                "children": first}}]})
        toggle_id = res["results"][0]["id"]
        for i in range(0, len(rest), CHUNK):
            notion_req("PATCH", f"/blocks/{toggle_id}/children", token,
                       {"children": rest[i:i + CHUNK]})
        print(f"  {lbl}: {len(vids)} rows", flush=True)

    print(f"[OK] Notion updated ({bd['snapshot']}): {grand_videos} videos / {grand:,} views")


if __name__ == "__main__":
    main()
