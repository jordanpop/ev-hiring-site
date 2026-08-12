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


def floor_to(n, step):
    return (n // step) * step


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
    total_views = total_videos = 0

    for acc in accounts:
        label, handle = acc["label"], acc["handle"]
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
        for r in items:
            if post_year(r) == year and filt(r):
                v = views(r)
                ep = ts_epoch(r.get("timestamp"))
                if fb_items and ep is not None:
                    v += match_fb(ep, fb_items, fb_used)
                n += 1
                v_sum += v

        floor = acc.get("min_expected", 1)
        if n < floor:
            errors.append(f"{label}: got {n} videos, below floor {floor} — scrape/filter drift?")
        per_account[label] = {"videos": n, "views": v_sum}
        total_videos += n
        total_views += v_sum
        print(f"       {n} videos, {v_sum:,} views", flush=True)

    print("[pull] follower counts ...", flush=True)
    handles = [a["handle"] for a in accounts if a.get("count_followers", True)]
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

    stats = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "views": total_views,
        "views_display": floor_to(total_views, 100_000),
        "videos": total_videos,
        "videos_display": floor_to(total_videos, 10),
        "followers": total_followers,
        "followers_display": floor_to(total_followers, 1_000),
        "per_account": per_account,
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n[OK] stats.json updated: {total_views:,} views / {total_videos} videos / {total_followers:,} followers")


if __name__ == "__main__":
    main()
