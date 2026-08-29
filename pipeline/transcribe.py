"""Step 3 - speech-to-text for each video.

    python -m pipeline.transcribe --email you@example.com --limit 200
    python -m pipeline.transcribe --email you@example.com --engine api

This is the expensive step, so it is built to be run in slices. `--limit` and
`--creator` let you transcribe the part of the library you actually search
first and leave the long tail for later; nothing else in the app depends on a
video having a transcript.

Engines:
  local (default) - faster-whisper on this machine. Free, no data leaves the
                    box, speed depends entirely on your CPU/GPU.
  api             - OpenAI Whisper endpoint. Costs about $0.006 per audio
                    minute, but needs no local model and no GPU.

Audio is downloaded to a temp file, transcribed, and deleted immediately - the
library stores text, never video files.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json  # noqa: E402

from app.db import connect, now  # noqa: E402

_model = None


def get_local_model(size="small", device="auto", compute_type="auto"):
    """Load faster-whisper once and reuse it - loading costs seconds per call."""
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise SystemExit("pip install faster-whisper  (or use --engine api)")
        print(f"Loading whisper model '{size}' ...")
        _model = WhisperModel(size, device=device, compute_type=compute_type)
    return _model


def download_audio(url, workdir):
    """Pull the audio track only. Returns a path, or None if unavailable."""
    out = os.path.join(workdir, "audio.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "-x", "--audio-format", "mp3", "--audio-quality", "5",
        "--no-playlist", "--quiet", "--no-warnings",
        "-o", out, url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip()[:200]
    for f in os.listdir(workdir):
        if f.startswith("audio."):
            return os.path.join(workdir, f), None
    return None, "no audio file produced"


def transcribe_local(path, model_size, language=None):
    model = get_local_model(model_size)
    segments, info = model.transcribe(
        path, language=language, vad_filter=True, beam_size=1
    )
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2),
             "text": s.text.strip()} for s in segments]
    return " ".join(s["text"] for s in segs), info.language, segs


def transcribe_api(path, language=None):
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit("pip install openai  (or use --engine local)")
    client = OpenAI()
    with open(path, "rb") as fh:
        r = client.audio.transcriptions.create(
            model="whisper-1", file=fh, language=language,
            response_format="verbose_json", timestamp_granularities=["segment"],
        )
    segs = [{"start": round(s["start"], 2), "end": round(s["end"], 2),
             "text": s["text"].strip()}
            for s in (r.segments or [])] if getattr(r, "segments", None) else []
    return r.text, getattr(r, "language", None), segs


def main():
    ap = argparse.ArgumentParser(description="Transcribe video audio")
    ap.add_argument("--email", required=True)
    ap.add_argument("--db", default=os.environ.get("ORGANIZER_DB", "organizer.db"))
    ap.add_argument("--engine", default="local", choices=("local", "api"))
    ap.add_argument("--model", default="small",
                    help="faster-whisper size: tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default=None, help="force a language, e.g. en")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--creator", default=None, help="only this creator handle")
    ap.add_argument("--genre", default=None, help="only this genre")
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    user = conn.execute("SELECT id FROM users WHERE email=?", (args.email,)).fetchone()
    if not user:
        raise SystemExit(f"No account for {args.email}")

    where = ["user_id=?", "status='ok'"]
    params = [user["id"]]
    if not args.redo:
        where.append("transcript=''")
    if args.creator:
        where.append("creator_handle=?")
        params.append(args.creator)
    if args.genre:
        where.append("genre=?")
        params.append(args.genre)

    sql = f"SELECT id, url, duration FROM videos WHERE {' AND '.join(where)} ORDER BY id"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()

    total = len(rows)
    audio_min = sum((r["duration"] or 30) for r in rows) / 60
    print(f"{total:,} videos, roughly {audio_min:,.0f} minutes of audio")
    if args.engine == "api":
        print(f"Estimated API cost at $0.006/min: ${audio_min * 0.006:,.2f}")
    if not total:
        return

    ok = failed = 0
    started = time.time()
    for i, row in enumerate(rows, 1):
        with tempfile.TemporaryDirectory() as tmp:
            path, err = download_audio(row["url"], tmp)
            if path is None:
                failed += 1
                conn.execute(
                    "UPDATE videos SET status_note=? WHERE id=?",
                    (f"audio download failed: {err}", row["id"]),
                )
                conn.commit()
                continue
            try:
                if args.engine == "local":
                    text, lang, segs = transcribe_local(path, args.model, args.language)
                else:
                    text, lang, segs = transcribe_api(path, args.language)
            except Exception as e:                       # noqa: BLE001
                failed += 1
                print(f"  ! {row['url']}: {e}")
                continue

        conn.execute(
            "UPDATE videos SET transcript=?, transcript_lang=?, segments=?, "
            "transcribed_at=? WHERE id=?",
            (text or "", lang, json.dumps(segs), now(), row["id"]),
        )
        conn.commit()
        ok += 1
        if i % 10 == 0 or i == total:
            rate = i / max(1e-6, time.time() - started)
            print(f"  {i:,}/{total:,}  ok={ok:,} failed={failed:,} "
                  f"~{(total - i) / rate / 60:.0f} min left")

    print(f"\nDone: {ok:,} transcribed, {failed:,} failed")
    print("Re-run classify to fold the new transcripts into genre tagging:")
    print(f"  python -m pipeline.classify --email {args.email} --redo")


if __name__ == "__main__":
    main()
