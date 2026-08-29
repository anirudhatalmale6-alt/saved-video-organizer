"""Step 2 - fetch creator, caption, hashtags, duration and thumbnail per video.

    python -m pipeline.enrich --email you@example.com --limit 500

Designed to be stopped and restarted. Every video is committed as soon as it
resolves, and only rows still marked `pending` are picked up on the next run,
so an overnight pass over 40k that dies at 3am simply resumes where it stopped
instead of starting over.

Videos the creator has since deleted or made private are marked `unavailable`
rather than retried forever - they stay in the library with their saved date,
they just have nothing left to fetch.
"""

import argparse
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, now  # noqa: E402

HASHTAG_RE = re.compile(r"#(\w+)")

GONE_MARKERS = (
    "video not available", "content isn't available", "removed", "private",
    "unavailable", "404", "does not exist", "deleted", "status_deleted",
)

_write_lock = threading.Lock()


def make_extractor(quiet=True):
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise SystemExit("pip install yt-dlp")

    opts = {
        "quiet": quiet,
        "no_warnings": quiet,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 25,
        "retries": 2,
    }
    return YoutubeDL(opts)


def fetch_meta(ydl, url):
    """Return (fields dict, status, note)."""
    try:
        info = ydl.extract_info(url, download=False)
    except Exception as e:              # noqa: BLE001 - yt-dlp raises many types
        msg = str(e).lower()
        if any(m in msg for m in GONE_MARKERS):
            return {}, "unavailable", str(e)[:200]
        return {}, "error", str(e)[:200]

    caption = info.get("description") or info.get("title") or ""
    handle = info.get("uploader_id") or info.get("uploader") or ""
    if handle and not handle.startswith("@"):
        handle = "@" + handle.lstrip("@")

    return (
        {
            "tiktok_id": str(info.get("id") or "") or None,
            "creator_handle": handle,
            "creator_name": info.get("uploader") or info.get("channel") or "",
            "caption": caption,
            "hashtags": sorted(set(HASHTAG_RE.findall(caption))),
            "duration": int(info.get("duration") or 0) or None,
            "thumbnail_url": info.get("thumbnail") or None,
        },
        "ok",
        None,
    )


def main():
    ap = argparse.ArgumentParser(description="Fetch metadata for pending videos")
    ap.add_argument("--email", required=True)
    ap.add_argument("--db", default=os.environ.get("ORGANIZER_DB", "organizer.db"))
    ap.add_argument("--limit", type=int, default=0, help="0 = everything pending")
    ap.add_argument("--workers", type=int, default=3,
                    help="keep this low; TikTok throttles hard above ~4")
    ap.add_argument("--delay", type=float, default=1.2,
                    help="seconds between requests per worker")
    ap.add_argument("--retry-errors", action="store_true",
                    help="also re-try rows that previously errored")
    args = ap.parse_args()

    conn = connect(args.db)
    user = conn.execute("SELECT id FROM users WHERE email=?", (args.email,)).fetchone()
    if not user:
        raise SystemExit(f"No account for {args.email}")

    statuses = ("pending", "error") if args.retry_errors else ("pending",)
    sql = ("SELECT id, url FROM videos WHERE user_id=? AND status IN "
           f"({','.join('?' * len(statuses))}) ORDER BY id")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, (user["id"], *statuses)).fetchall()
    conn.close()

    total = len(rows)
    print(f"{total:,} videos to enrich  ({args.workers} workers, {args.delay}s delay)")
    if not total:
        return

    counts = {"ok": 0, "unavailable": 0, "error": 0}
    started = time.time()

    def worker(chunk):
        ydl = make_extractor()
        # Each thread gets its own connection: sqlite3 objects are not safe to
        # share across threads, and the writes are serialised by _write_lock.
        local = connect(args.db)
        for row in chunk:
            fields, status, note = fetch_meta(ydl, row["url"])
            sets = {"status": status, "status_note": note, "enriched_at": now()}
            if status == "ok":
                import json as _json
                fields["hashtags"] = _json.dumps(fields["hashtags"])
                sets.update(fields)
            with _write_lock:
                local.execute(
                    f"UPDATE videos SET {','.join(f'{k}=?' for k in sets)} WHERE id=?",
                    list(sets.values()) + [row["id"]],
                )
                local.commit()
                counts[status] = counts.get(status, 0) + 1
                done = sum(counts.values())
                if done % 25 == 0 or done == total:
                    rate = done / max(1e-6, time.time() - started)
                    eta = (total - done) / rate / 60
                    print(f"  {done:,}/{total:,}  ok={counts['ok']:,} "
                          f"gone={counts['unavailable']:,} err={counts['error']:,} "
                          f"~{eta:.0f} min left")
            # Jittered so N workers don't march in lockstep and look like a bot.
            time.sleep(args.delay * random.uniform(0.7, 1.4))
        local.close()

    chunks = [rows[i::args.workers] for i in range(args.workers)]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(worker, chunks))

    print(f"\nDone in {(time.time() - started) / 60:.1f} min: "
          f"{counts['ok']:,} ok, {counts['unavailable']:,} gone, {counts['error']:,} errors")
    print("Next: python -m pipeline.classify --email", args.email)


if __name__ == "__main__":
    main()
