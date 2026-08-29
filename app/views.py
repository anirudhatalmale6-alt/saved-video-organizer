"""Library browsing, search and the video detail page."""

import json
import math
import re

from flask import (Blueprint, abort, g, jsonify, redirect, render_template,
                   request, url_for)

from . import db as store
from .auth import get_db, login_required

bp = Blueprint("views", __name__)

PER_PAGE = 48


@bp.app_template_filter("hashtags")
def _hashtags(raw):
    try:
        return json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []


@bp.app_template_filter("segments")
def _segments(raw):
    try:
        return json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []


@bp.app_template_filter("mmss")
def _mmss(seconds):
    if seconds in (None, ""):
        return "--:--"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


@bp.app_template_filter("shortdate")
def _shortdate(value):
    return (value or "")[:10]


@bp.app_template_filter("thumbhue")
def _thumbhue(value):
    """Stable colour per creator so the grid stays visually grouped even when
    a thumbnail hasn't been fetched yet."""
    return sum(ord(c) * 7 for c in (value or "x")) % 360


def _highlight(text, query, radius=110):
    """Trim `text` to the region around the first query word and mark hits.

    Returns HTML-safe markup: everything is escaped first, then <mark> tags are
    added, so a caption containing '<script>' can't inject anything.
    """
    from markupsafe import Markup, escape

    if not text:
        return Markup("")
    words = [w for w in re.findall(r"[\w#@]+", query or "") if len(w) > 1]
    idx = -1
    if words:
        low = text.lower()
        hits = [low.find(w.lower()) for w in words]
        hits = [h for h in hits if h >= 0]
        idx = min(hits) if hits else -1

    if idx < 0:
        start, end = 0, min(len(text), radius * 2)
    else:
        start = max(0, idx - radius // 2)
        end = min(len(text), idx + radius)

    # Snap to whitespace so a snippet never opens or closes mid-word - the
    # window is chosen by character offset, and "...ency fund" reads as a bug.
    if start > 0:
        space = text.find(" ", start, start + 25)
        start = space + 1 if space != -1 else start
    if end < len(text):
        space = text.rfind(" ", end - 25, end)
        end = space if space != -1 else end

    snippet = text[start:end]
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""

    out = escape(snippet)
    for w in sorted(set(words), key=len, reverse=True):
        out = Markup(
            re.sub(
                f"({re.escape(escape(w))})",
                r"<mark>\1</mark>",
                str(out),
                flags=re.IGNORECASE,
            )
        )
    return Markup(prefix) + out + Markup(suffix)


bp.add_app_template_global(_highlight, "highlight")


@bp.app_template_global("url_with")
def url_with(**overrides):
    """Current URL with some query params replaced.

    Passing an empty string drops the param, which is how the templates build
    both "filter by this" and "clear this filter" links from one helper.
    Anything that changes the result set also resets the page.
    """
    args = request.args.to_dict()
    if any(k != "page" for k in overrides):
        args.pop("page", None)
    for k, v in overrides.items():
        if v in (None, "", 0):
            args.pop(k, None)
        else:
            args[k] = v
    return url_for(request.endpoint, **args)


@bp.route("/")
@login_required
def library():
    conn = get_db()
    q = (request.args.get("q") or "").strip()
    creator = request.args.get("creator") or None
    genre = request.args.get("genre") or None
    status = request.args.get("status") or None
    sort = request.args.get("sort") or ("relevance" if q else "recent")
    # A hand-edited or truncated URL must not 500 the library.
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1

    tr = request.args.get("transcript")
    has_transcript = {"yes": True, "no": False}.get(tr)

    rows, total = store.search_videos(
        conn, g.user["id"], q=q, creator=creator, genre=genre, status=status,
        has_transcript=has_transcript, sort=sort,
        limit=PER_PAGE, offset=(page - 1) * PER_PAGE,
    )
    genres, creators = store.facets(conn, g.user["id"], genre=genre, creator=creator)
    stats = store.library_stats(conn, g.user["id"])

    return render_template(
        "library.html",
        videos=rows, total=total, page=page,
        pages=max(1, math.ceil(total / PER_PAGE)),
        q=q, creator=creator, genre=genre, status=status, sort=sort,
        transcript=tr, genres=genres, creators=creators, stats=stats,
    )


@bp.route("/video/<int:video_id>")
@login_required
def video(video_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND user_id = ?", (video_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)

    more = conn.execute(
        "SELECT id, caption, thumbnail_url, creator_handle, duration FROM videos "
        "WHERE user_id = ? AND creator_handle = ? AND id <> ? "
        "ORDER BY saved_at DESC LIMIT 8",
        (g.user["id"], row["creator_handle"], video_id),
    ).fetchall()
    return render_template("video.html", v=row, more=more, q=request.args.get("q", ""))


@bp.route("/creators")
@login_required
def creators():
    conn = get_db()
    rows = conn.execute(
        """SELECT creator_handle AS handle,
                  COALESCE(MAX(creator_name),'') AS name,
                  COUNT(*) AS n,
                  SUM(transcript <> '') AS transcribed,
                  MAX(saved_at) AS last_saved,
                  (SELECT genre FROM videos v2 WHERE v2.user_id = v.user_id
                     AND v2.creator_handle = v.creator_handle AND v2.genre IS NOT NULL
                     GROUP BY genre ORDER BY COUNT(*) DESC LIMIT 1) AS top_genre
           FROM videos v WHERE user_id = ? AND creator_handle <> ''
           GROUP BY creator_handle ORDER BY n DESC, handle ASC""",
        (g.user["id"],),
    ).fetchall()
    return render_template("creators.html", creators=rows)


@bp.route("/genres")
@login_required
def genres():
    conn = get_db()
    rows = conn.execute(
        """SELECT genre AS name, COUNT(*) AS n,
                  COUNT(DISTINCT creator_handle) AS creators,
                  COALESCE(SUM(duration),0) AS seconds
           FROM videos WHERE user_id = ? AND genre IS NOT NULL AND genre <> ''
           GROUP BY genre ORDER BY n DESC""",
        (g.user["id"],),
    ).fetchall()
    unsorted_n = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE user_id = ? AND (genre IS NULL OR genre = '')",
        (g.user["id"],),
    ).fetchone()[0]
    return render_template("genres.html", genres=rows, unsorted_n=unsorted_n)


@bp.route("/api/suggest")
@login_required
def suggest():
    """Type-ahead for the search box: matching creators and genres."""
    term = (request.args.get("q") or "").strip()
    if len(term) < 2:
        return jsonify([])
    like = f"%{term}%"
    conn = get_db()
    out = []
    for r in conn.execute(
        "SELECT creator_handle AS h, COUNT(*) n FROM videos WHERE user_id = ? "
        "AND creator_handle LIKE ? GROUP BY creator_handle ORDER BY n DESC LIMIT 5",
        (g.user["id"], like),
    ):
        out.append({"type": "creator", "label": r["h"], "count": r["n"]})
    for r in conn.execute(
        "SELECT genre AS gname, COUNT(*) n FROM videos WHERE user_id = ? "
        "AND genre LIKE ? GROUP BY genre ORDER BY n DESC LIMIT 4",
        (g.user["id"], like),
    ):
        out.append({"type": "genre", "label": r["gname"], "count": r["n"]})
    return jsonify(out)


@bp.app_context_processor
def demo_flag():
    """Flag a library made of seeded rows so the UI can say so on every page.

    Generated captions and handles are convincing enough to be mistaken for a
    real export, which is exactly why the banner is not optional.
    """
    if getattr(g, "user", None) is None:
        return {}
    n = get_db().execute(
        "SELECT COUNT(*) FROM videos WHERE user_id = ? AND is_demo = 1", (g.user["id"],)
    ).fetchone()[0]
    return {"demo_count": n}


@bp.route("/health")
def health():
    return {"ok": True}
