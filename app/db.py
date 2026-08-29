"""SQLite storage layer.

Everything lives in one file so the whole library is portable: copy the .db
somewhere else and it still works. FTS5 gives us the full-text search over
captions + transcripts + creator names without needing Elasticsearch for what
is, at 40k rows, still a small dataset.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.environ.get("ORGANIZER_DB", "organizer.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tiktok_id       TEXT,
    url             TEXT NOT NULL,
    creator_handle  TEXT,
    creator_name    TEXT,
    caption         TEXT DEFAULT '',
    hashtags        TEXT DEFAULT '[]',   -- json array
    duration        INTEGER,             -- seconds
    thumbnail_url   TEXT,
    saved_at        TEXT,                -- when the user saved it on TikTok
    -- pending: never fetched.  ok: metadata fetched.  unavailable: deleted or
    -- made private by the creator since it was saved.
    status          TEXT NOT NULL DEFAULT 'pending',
    status_note     TEXT,
    transcript      TEXT DEFAULT '',
    transcript_lang TEXT,
    segments        TEXT DEFAULT '[]',   -- json: [{start, end, text}, ...]
    genre           TEXT,
    genre_confidence REAL,
    is_demo         INTEGER NOT NULL DEFAULT 0,
    enriched_at     TEXT,
    transcribed_at  TEXT,
    UNIQUE(user_id, url)
);

CREATE INDEX IF NOT EXISTS idx_videos_user    ON videos(user_id);
CREATE INDEX IF NOT EXISTS idx_videos_creator ON videos(user_id, creator_handle);
CREATE INDEX IF NOT EXISTS idx_videos_genre   ON videos(user_id, genre);
CREATE INDEX IF NOT EXISTS idx_videos_status  ON videos(user_id, status);
CREATE INDEX IF NOT EXISTS idx_videos_saved   ON videos(user_id, saved_at);

-- External-content FTS index.  We keep it in sync with triggers so a plain
-- INSERT/UPDATE on `videos` is all the pipeline scripts ever need to do.
CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    caption,
    transcript,
    creator,
    hashtags,
    content='videos',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, caption, transcript, creator, hashtags)
    VALUES (new.id, new.caption, new.transcript,
            COALESCE(new.creator_handle,'') || ' ' || COALESCE(new.creator_name,''),
            new.hashtags);
END;

CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, caption, transcript, creator, hashtags)
    VALUES ('delete', old.id, old.caption, old.transcript,
            COALESCE(old.creator_handle,'') || ' ' || COALESCE(old.creator_name,''),
            old.hashtags);
END;

CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, caption, transcript, creator, hashtags)
    VALUES ('delete', old.id, old.caption, old.transcript,
            COALESCE(old.creator_handle,'') || ' ' || COALESCE(old.creator_name,''),
            old.hashtags);
    INSERT INTO videos_fts(rowid, caption, transcript, creator, hashtags)
    VALUES (new.id, new.caption, new.transcript,
            COALESCE(new.creator_handle,'') || ' ' || COALESCE(new.creator_name,''),
            new.hashtags);
END;

-- User-editable genre list.  Genres are suggested by the classifier but the
-- user owns them: rename, merge and delete all happen here.
CREATE TABLE IF NOT EXISTS genres (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    color    TEXT,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS import_runs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT,
    total       INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    duplicates  INTEGER DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None):
    path = path or DEFAULT_DB
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------- search ----

def _fts_query(raw):
    """Turn what a human typed into something FTS5 will accept.

    FTS5 treats bare punctuation as syntax and throws on a malformed query, so
    a search for `chef's knife (best)` would 500 the page. We strip it down to
    quoted terms and let a trailing word prefix-match so results update
    sensibly while the user is still typing.
    """
    tokens, buf, in_quote = [], [], False
    for ch in raw:
        if ch == '"':
            if in_quote and buf:
                tokens.append('"' + "".join(buf).replace('"', "") + '"')
                buf = []
            in_quote = not in_quote
            continue
        if in_quote:
            buf.append(ch)
        elif ch.isalnum() or ch in "_#@":
            buf.append(ch)
        elif buf:
            tokens.append('"' + "".join(buf) + '"')
            buf = []
    if buf:
        tokens.append('"' + "".join(buf) + '"')
    if not tokens:
        return None
    # Last token prefix-matches, so "kni" already finds "knife". A trailing
    # space or quote means the user finished the word, so match it exactly.
    if not raw.endswith(('"', " ")):
        tokens[-1] = tokens[-1][:-1] + '"*'
    return " ".join(tokens)


SORTS = {
    "relevance": None,  # only meaningful with a query; falls back to recent
    "recent": "v.saved_at DESC",
    "oldest": "v.saved_at ASC",
    "creator": "v.creator_handle COLLATE NOCASE ASC, v.saved_at DESC",
    "longest": "v.duration DESC",
}


def search_videos(conn, user_id, q="", creator=None, genre=None, status=None,
                  has_transcript=None, sort="relevance", limit=48, offset=0):
    """Filtered + ranked search. Returns (rows, total_count)."""
    params = [user_id]
    joins = ""
    where = ["v.user_id = ?"]
    order = SORTS.get(sort) or "v.saved_at DESC"

    match = _fts_query(q) if q and q.strip() else None
    if match:
        joins = "JOIN videos_fts f ON f.rowid = v.id"
        where.append("videos_fts MATCH ?")
        params.append(match)
        if sort == "relevance":
            # Weight the caption and creator above the transcript: a word in a
            # 6-word caption says more about the video than the same word
            # dropped once in three minutes of speech.
            order = "bm25(videos_fts, 8.0, 1.0, 6.0, 4.0) ASC"

    if creator:
        where.append("v.creator_handle = ?")
        params.append(creator)
    if genre:
        where.append("v.genre = ?")
        params.append(genre)
    if status:
        where.append("v.status = ?")
        params.append(status)
    if has_transcript is True:
        where.append("v.transcript <> ''")
    elif has_transcript is False:
        where.append("v.transcript = ''")

    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM videos v {joins} WHERE {clause}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT v.* FROM videos v {joins} WHERE {clause} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return rows, total


def facets(conn, user_id, genre=None, creator=None):
    """Counts for the sidebar. Each facet is counted with the *other* filter
    applied, so picking a genre narrows the creator list and vice versa."""
    g_params, g_where = [user_id], "user_id = ?"
    if creator:
        g_where += " AND creator_handle = ?"
        g_params.append(creator)
    genres = conn.execute(
        f"SELECT genre AS name, COUNT(*) AS n FROM videos WHERE {g_where} "
        "AND genre IS NOT NULL AND genre <> '' GROUP BY genre ORDER BY n DESC",
        g_params,
    ).fetchall()

    c_params, c_where = [user_id], "user_id = ?"
    if genre:
        c_where += " AND genre = ?"
        c_params.append(genre)
    creators = conn.execute(
        f"SELECT creator_handle AS handle, COALESCE(MAX(creator_name),'') AS name, "
        f"COUNT(*) AS n FROM videos WHERE {c_where} AND creator_handle IS NOT NULL "
        "AND creator_handle <> '' GROUP BY creator_handle ORDER BY n DESC, handle ASC",
        c_params,
    ).fetchall()
    return genres, creators


def library_stats(conn, user_id):
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(status='ok')          AS ok,
                  SUM(status='pending')     AS pending,
                  SUM(status='unavailable') AS unavailable,
                  SUM(transcript <> '')     AS transcribed,
                  COUNT(DISTINCT creator_handle) AS creators,
                  COUNT(DISTINCT genre)          AS genres,
                  COALESCE(SUM(duration),0)      AS seconds
           FROM videos WHERE user_id = ?""",
        (user_id,),
    ).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def upsert_video(conn, user_id, url, **fields):
    """Insert a saved video, or fill in blanks on one we already have.

    Re-importing a newer TikTok export must not wipe transcripts we already
    paid to generate, so an existing row only takes values it is missing.
    """
    existing = conn.execute(
        "SELECT * FROM videos WHERE user_id = ? AND url = ?", (user_id, url)
    ).fetchone()
    if existing is None:
        cols = ["user_id", "url"] + list(fields.keys())
        vals = [user_id, url] + [
            json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()
        ]
        conn.execute(
            f"INSERT INTO videos ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        return "inserted"

    updates = {}
    for k, v in fields.items():
        if v in (None, "", [], {}):
            continue
        if not existing[k] or existing[k] in ("[]", "pending"):
            updates[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
    if updates:
        conn.execute(
            f"UPDATE videos SET {','.join(f'{k}=?' for k in updates)} WHERE id = ?",
            list(updates.values()) + [existing["id"]],
        )
    return "duplicate"
