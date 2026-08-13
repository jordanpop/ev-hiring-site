#!/usr/bin/env python3
"""Weekly stats refresh for hiring.ev-hk.com.

Pulls every configured IG account via Apify, filters down to the videos WE
produced, sums lifetime views (+ Facebook plays for cross-posted accounts),
counts videos, pulls follower counts, and rewrites stats.json in the repo
root. The site's hero counters read stats.json at page load.

Safety: if any account scrapes empty or lands below its min_expected floor,
the script exits non-zero WITHOUT touching stats.json — the GitHub Action
fails loudly (owner gets an email) and the site keeps serving the last good
numbers.

Env: APIFY_TOKEN (required). Stdlib only — no pip installs needed.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CONFIG_PATH = HERE / "stats_config.json"
STATS_PATH = REPO_ROOT / "stats.json"
COVERS_DIR = REPO_ROOT / "covers"   # 封面圖庫：covers/{label}/{shortCode}.jpg，增量下載

APIFY_BASE = "https://api.apify.com/v2"
IG_ACTOR = "apify~instagram-scraper"
FB_ACTOR = "apify~facebook-reels-scraper"
FB_JOIN_TOLERANCE_S = 300
RUN_TIMEOUT_S = 900
POLL_INTERVAL_S = 10


def http_json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def apify_run(actor, payload, token):
    """Start an actor run, poll to completion, return dataset items."""
    start = http_json(f"{APIFY_BASE}/acts/{actor}/runs?token={token}", payload)
    run = start["data"]
    run_id, dataset_id = run["id"], run["defaultDatasetId"]
    t0 = time.time()
    while True:
        status = http_json(f"{APIFY_BASE}/actor-runs/{run_id}?token={token}")["data"]["status"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {actor} ended {status}")
        if time.time() - t0 > RUN_TIMEOUT_S:
            raise RuntimeError(f"Apify run {actor} timed out after {RUN_TIMEOUT_S}s")
        time.sleep(POLL_INTERVAL_S)
    items, offset = [], 0
    while True:
        batch = http_json(
            f"{APIFY_BASE}/datasets/{dataset_id}/items?token={token}"
            f"&clean=true&format=json&offset={offset}&limit=1000")
        items.extend(batch)
        if len(batch) < 1000:
            return items
        offset += 1000


def make_filter(spec, sponsor_brands):
    def is_video(r):
        return r.get("type") == "Video" or (r.get("videoViewCount") or r.get("videoPlayCount") or 0) > 0

    ftype = spec["type"]
    if ftype == "caption_contains":
        sig = spec["value"]
        return lambda r: sig in (r.get("caption") or "")
    if ftype == "any_video":
        return is_video
    if ftype == "video_not_sponsor":
        brands = [b.lower() for b in sponsor_brands]
        return lambda r: is_video(r) and not any(b in (r.get("caption") or "").lower() for b in brands)
    raise ValueError(f"unknown filter type: {ftype!r}")


def views(r):
    # IG's public "Views" = videoPlayCount; videoViewCount is the old ~3x-lower metric.
    return r.get("videoPlayCount") or r.get("videoViewCount") or 0


def ts_epoch(ts):
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def post_year(r):
    try:
        return datetime.fromisoformat((r.get("timestamp") or "").replace("Z", "+00:00")).year
    except Exception:
        return None


def fb_plays(v):
    """playCountRounded: 44000 / '44K' / '1.2M' → int."""
    if v is None:
        return 0
    s = str(v).strip().upper().replace(",", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def match_fb(ig_epoch, fb_items, used):
    """Cross-posts land seconds after the IG post — join by nearest timestamp."""
    best, best_d = None, FB_JOIN_TOLERANCE_S + 1
    for i, (ep, plays) in enumerate(fb_items):
        if i in used:
            continue
        d = abs(ep - ig_epoch)
        if d < best_d:
            best, best_d = i, d
    if best is not None and best_d <= FB_JOIN_TOLERANCE_S:
        used.add(best)
        return fb_items[best][1]
    return 0


def main():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN not set")

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    year = cfg["year"]
    sponsor_brands = cfg.get("sponsor_brands", [])
    accounts = cfg["accounts"]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    if only:
        accounts = [a for a in accounts if a["handle"] == only]
        if not accounts:
            sys.exit(f"--only {only}: no such handle in config")

    errors = []
    per_account = {}
    monthly = {}          # label -> {month: [videos, views]} — feeds the Notion table
    per_video = {}        # label -> [{date, views, url, caption}] — for manual spot-checks
    total_views = total_videos = 0
    cover_jobs = []          # (label, shortCode, displayUrl) — 過濾後屬於我哋嘅片先入嚟

    for acc in accounts:
        label, handle = acc["label"], acc["handle"]
        if acc.get("frozen"):
            bm = {int(m): list(cv) for m, cv in acc["frozen_monthly"].items()}
            n = sum(c for c, _ in bm.values())
            v_sum = sum(v for _, v in bm.values())
            monthly[label] = bm
            per_account[label] = {"videos": n, "views": v_sum, "frozen": True}
            total_videos += n
            total_views += v_sum
            print(f"[frozen] {label}: {n} videos, {v_sum:,} views (not re-fetched)", flush=True)
            continue
        print(f"[pull] {label} (@{handle}) ...", flush=True)
        filt = make_filter(acc["filter"], sponsor_brands)
        items = apify_run(IG_ACTOR, {
            "directUrls": [f"https://www.instagram.com/{handle}/"],
            "resultsType": "posts", "resultsLimit": 250}, token)

        fb_items, fb_used = [], set()
        if acc.get("facebook_page"):
            print(f"[pull] {label} Facebook leg ...", flush=True)
            for it in apify_run(FB_ACTOR, {
                    "startUrls": [{"url": acc["facebook_page"]}],
                    "resultsLimit": 100}, token):
                ep = ts_epoch(it.get("time"))
                if ep is not None:
                    fb_items.append((ep, fb_plays(it.get("playCountRounded"))))

        n = v_sum = 0
        bm = {}
        vids = []
        for r in items:
            if post_year(r) == year and filt(r):
                v = views(r)
                ep = ts_epoch(r.get("timestamp"))
                fbv = 0
                if fb_items and ep is not None:
                    fbv = match_fb(ep, fb_items, fb_used)
                    v += fbv
                d = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                cell = bm.setdefault(d.month, [0, 0])
                cell[0] += 1
                cell[1] += v
                n += 1
                v_sum += v
                cap = " ".join((r.get("caption") or "").split())[:45]
                vids.append({"date": d.strftime("%Y-%m-%d"), "views": v, "fb": fbv,
                             "url": r.get("url") or "", "caption": cap})
                if r.get("shortCode") and r.get("displayUrl"):
                    cover_jobs.append({"label": label, "shortCode": r["shortCode"],
                                       "url": r["displayUrl"], "views": v,
                                       "date": d.strftime("%Y-%m-%d")})
        vids.sort(key=lambda x: x["date"])
        monthly[label] = bm
        per_video[label] = vids

        floor = acc.get("min_expected", 1)
        if n < floor:
            errors.append(f"{label}: got {n} videos, below floor {floor} — scrape/filter drift?")
        per_account[label] = {"videos": n, "views": v_sum}
        total_videos += n
        total_views += v_sum
        print(f"       {n} videos, {v_sum:,} views", flush=True)

    print("[pull] follower counts ...", flush=True)
    handles = [a["handle"] for a in accounts
               if a.get("count_followers", True) and not a.get("frozen")]
    details = apify_run(IG_ACTOR, {
        "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
        "resultsType": "details", "resultsLimit": 1}, token)
    total_followers = 0
    seen = set()
    for d in details:
        u = (d.get("username") or "").lower()
        f = d.get("followersCount") or 0
        if u and u not in seen:
            seen.add(u)
            total_followers += f
            print(f"       @{u}: {f:,} followers", flush=True)
    missing = {h.lower() for h in handles} - seen
    if missing:
        errors.append(f"followers pull missing: {', '.join(sorted(missing))}")

    if errors:
        print("\n[FAIL] not writing stats.json:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    if only:
        print("\n[smoke-test OK] --only mode never writes stats.json")
        return

    # 封面增量下載：IG displayUrl 係會過期嘅 signed URL，所以每週趁新鮮抓落嚟存底。
    # 已存在嘅 skip（shortCode 唯一）；individual 下載失敗只警告，唔會 fail 成個 run。
    import urllib.request as _rq
    new_covers = fail_covers = 0
    for job in cover_jobs:
        label, sc, url = job["label"], job["shortCode"], job["url"]
        dest = COVERS_DIR / label / f"{sc}.jpg"
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = _rq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            dest.write_bytes(_rq.urlopen(req, timeout=30).read())
            new_covers += 1
        except Exception as e:
            fail_covers += 1
            print(f"[warn] cover {label}/{sc}: {e}", flush=True)
    print(f"[covers] {new_covers} new downloaded, {fail_covers} failed, dir={COVERS_DIR}", flush=True)
    # manifest 俾 push_covers_notion.py 用（file link 式上圖庫頁）
    (REPO_ROOT / "covers_manifest.json").write_text(json.dumps(
        [{k: j[k] for k in ("label", "shortCode", "views", "date")} for j in cover_jobs],
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 實數出街，唔捨入 — 用戶 2026-08-12 拍板：「有幾多就出幾多，真實啲」
    stats = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "views": total_views,
        "views_display": total_views,
        "videos": total_videos,
        "videos_display": total_videos,
        "followers": total_followers,
        "followers_display": total_followers,
        "per_account": per_account,
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Workspace-only artifact for push_notion.py — gitignored, never committed
    # (per-client monthly numbers stay out of the public repo).
    breakdown = {
        "year": year,
        "snapshot": stats["updated"],
        "labels": [a["label"] for a in accounts],
        "frozen_labels": [a["label"] for a in accounts if a.get("frozen")],
        "monthly": {lbl: {str(m): cv for m, cv in bm.items()} for lbl, bm in monthly.items()},
        "per_video": per_video,
    }
    (REPO_ROOT / "monthly_breakdown.json").write_text(
        json.dumps(breakdown, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n[OK] stats.json updated: {total_views:,} views / {total_videos} videos / {total_followers:,} followers")


if __name__ == "__main__":
    main()
