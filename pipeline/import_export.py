"""Step 1 - read the TikTok data export and load the saved-video list.

Usage:
    python -m pipeline.import_export user_data.json --email you@example.com
    python -m pipeline.import_export "TikTok Data.zip" --email you@example.com --section likes

TikTok has changed the shape of this file more than once (Activity vs "Your
Activity", FavoriteVideoList vs ItemFavoriteList, JSON vs TXT). Rather than
hard-coding one layout and breaking on the next export, we walk the whole
document and pick up anything that looks like {date, link} - so a future
rename of the wrapper key doesn't cost you a re-import.
"""

import argparse
import io
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db, now, upsert_video  # noqa: E402

LINK_KEYS = ("link", "videolink", "url", "sharedlink")
DATE_KEYS = ("date", "createtime", "time", "timestamp")

# Which part of the export we pull from. TikTok files "saved" videos under
# Favorite Videos; Like List is a different thing and usually much larger.
SECTION_HINTS = {
    "favorites": ("favorite", "favourite", "saved", "collect"),
    "likes": ("like",),
    "history": ("browsing", "history", "watch"),
}

VIDEO_ID_RE = re.compile(r"/video/(\d+)|/(\d{15,})")


def _norm_date(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):  # unix seconds
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    s = str(value).strip()
    return s.replace(" ", "T") if re.match(r"^\d{4}-\d\d-\d\d \d\d:", s) else s


def _tiktok_id(url):
    m = VIDEO_ID_RE.search(url or "")
    return (m.group(1) or m.group(2)) if m else None


def walk_json(node, path=""):
    """Yield (path, {date, link}) for every entry anywhere in the document."""
    if isinstance(node, dict):
        lower = {k.lower(): v for k, v in node.items()}
        link = next((lower[k] for k in LINK_KEYS if isinstance(lower.get(k), str)), None)
        if link and "http" in link:
            date = next((lower[k] for k in DATE_KEYS if lower.get(k)), None)
            yield path, {"link": link.strip(), "date": _norm_date(date)}
            return
        for k, v in node.items():
            yield from walk_json(v, f"{path}/{k}")
    elif isinstance(node, list):
        for item in node:
            yield from walk_json(item, path)


TXT_BLOCK = re.compile(
    r"Date:\s*(?P<date>[^\n]+)\s*\n\s*(?:Link|Video Link):\s*(?P<link>\S+)", re.I
)


def parse_txt(text, path="txt"):
    for m in TXT_BLOCK.finditer(text):
        yield path, {"link": m.group("link").strip(),
                     "date": _norm_date(m.group("date"))}


def read_export(path, section="favorites"):
    """Return a de-duplicated list of {link, date} from a .json, .txt or .zip."""
    hints = SECTION_HINTS.get(section, SECTION_HINTS["favorites"])
    other = [h for k, v in SECTION_HINTS.items() if k != section for h in v]
    found, seen = [], set()

    def take(src_path, entry):
        p = src_path.lower()
        # A path has to name our section and must not name a competing one -
        # otherwise "Favorite Videos" inside "Video Browsing History" leaks in.
        if not any(h in p for h in hints):
            return
        # Exclusions ignore the final path segment. That segment is the name of
        # the list itself, and TikTok calls the *likes* list "ItemFavoriteList"
        # - matching "favorite" there would make every likes import come back
        # empty.
        container = p.rsplit("/", 1)[0] if "/" in p else p
        if any(h in container for h in other if h not in hints):
            return
        if entry["link"] in seen:
            return
        seen.add(entry["link"])
        found.append(entry)

    def handle(name, raw):
        if name.lower().endswith(".json"):
            try:
                doc = json.loads(raw)
            except ValueError as e:
                print(f"  ! {name}: not valid JSON ({e})")
                return
            for p, entry in walk_json(doc, name):
                take(p, entry)
        elif name.lower().endswith(".txt"):
            for p, entry in parse_txt(raw.decode("utf-8", "replace")
                                      if isinstance(raw, bytes) else raw, name):
                take(p, entry)

    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > 200 * 1024 * 1024:
                    continue
                if info.filename.lower().endswith((".json", ".txt")):
                    handle(info.filename, z.read(info))
    else:
        with io.open(path, "rb") as fh:
            handle(path, fh.read())

    return found


def import_into_db(db_path, email, entries):
    conn = init_db(db_path)
    user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        raise SystemExit(
            f"No account for {email}. Create it in the web UI first (/register)."
        )
    uid = user["id"]

    run = conn.execute(
        "INSERT INTO import_runs (user_id, source, total, started_at) VALUES (?,?,?,?)",
        (uid, "tiktok-export", len(entries), now()),
    ).lastrowid

    inserted = dupes = 0
    for e in entries:
        result = upsert_video(
            conn, uid, e["link"],
            tiktok_id=_tiktok_id(e["link"]),
            saved_at=e.get("date"),
            status="pending",
        )
        if result == "inserted":
            inserted += 1
        else:
            dupes += 1
    conn.execute(
        "UPDATE import_runs SET inserted=?, duplicates=?, finished_at=? WHERE id=?",
        (inserted, dupes, now(), run),
    )
    conn.commit()
    conn.close()
    return inserted, dupes


def main():
    ap = argparse.ArgumentParser(description="Import a TikTok data export")
    ap.add_argument("export", help="user_data.json, a .txt export, or the whole .zip")
    ap.add_argument("--email", required=True, help="account to import into")
    ap.add_argument("--db", default=os.environ.get("ORGANIZER_DB", "organizer.db"))
    ap.add_argument("--section", default="favorites",
                    choices=sorted(SECTION_HINTS), help="which list to import")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = read_export(args.export, args.section)
    print(f"Found {len(entries):,} {args.section} entries in {args.export}")
    if not entries:
        print("Nothing matched. Run with --section likes/history, or check the file.")
        return
    if args.dry_run:
        for e in entries[:5]:
            print("  ", e["date"], e["link"])
        print("  ... (dry run, nothing written)")
        return

    inserted, dupes = import_into_db(args.db, args.email, entries)
    print(f"Imported {inserted:,} new, {dupes:,} already present.")
    print("Next: python -m pipeline.enrich --email", args.email)


if __name__ == "__main__":
    main()
