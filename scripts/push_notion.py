#!/usr/bin/env python3
"""Push the monthly views table to the Notion "Monthly views record" page.

Runs AFTER update_stats.py (reads the monthly_breakdown.json it wrote) and
AFTER the site commit step — so a Notion hiccup fails the workflow loudly
(owner gets an email) without ever blocking the website update.

Env: NOTION_TOKEN — an internal-integration token that has been given access
to the target page. Stdlib only.
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


def rt(text, bold=False):
    seg = {"type": "text", "text": {"content": text}}
    if bold:
        seg["annotations"] = {"bold": True}
    return [seg]


def table_row(cells):
    return {"type": "table_row",
            "table_row": {"cells": [rt(c[0], bold=c[1]) if isinstance(c, tuple) else rt(c)
                                    for c in cells]}}


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN not set")
    cfg = json.loads((HERE / "stats_config.json").read_text(encoding="utf-8"))
    page_id = cfg["notion_page_id"]
    bd = json.loads((REPO_ROOT / "monthly_breakdown.json").read_text(encoding="utf-8"))

    labels = bd["labels"]
    frozen = set(bd["frozen_labels"])
    monthly = {lbl: {int(m): cv for m, cv in bm.items()} for lbl, bm in bd["monthly"].items()}

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

    frozen_note = ("；" + "、".join(sorted(frozen)) + " = 已停 collaboration，凍結 snapshot 唔再更新") if frozen else ""
    blocks = [
        {"type": "heading_1",
         "heading_1": {"rich_text": rt(f"{bd['year']} 每月總 Views — 我哋整嘅片")}},
        {"type": "quote",
         "quote": {"rich_text": rt(
             f"Snapshot: {bd['snapshot']}（自動更新：GitHub Actions 逢星期一 03:00 HKT，"
             f"repo = ev-hiring-site）。Views = 累計終身數（IG videoPlayCount），按 post 月份歸類；"
             f"有 cross-post 嘅 client（Yoru）＝ IG＋Facebook 加總{frozen_note}。")}},
        {"type": "table",
         "table": {"table_width": len(labels) + 2, "has_column_header": True,
                   "children": rows}},
        {"type": "paragraph",
         "paragraph": {"rich_text": rt("括號 = 條數。手動跑：GitHub → ev-hiring-site → Actions → Update stats → Run workflow。")}},
    ]

    # Full replace: delete existing blocks, then append the fresh report.
    while True:
        kids = notion_req("GET", f"/blocks/{page_id}/children?page_size=100", token)
        for b in kids["results"]:
            notion_req("DELETE", f"/blocks/{b['id']}", token)
        if not kids.get("has_more"):
            break
    notion_req("PATCH", f"/blocks/{page_id}/children", token, {"children": blocks})
    print(f"[OK] Notion page updated ({bd['snapshot']}): grand total {grand:,} views")


if __name__ == "__main__":
    main()
