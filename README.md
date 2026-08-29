# Saved Video Organizer

A private, self-hosted library for your saved TikTok videos: grouped by creator,
tagged by genre, and searchable down to the words spoken in the audio.

Built for libraries in the tens of thousands. Everything lives in one SQLite
file — no server to rent, no account with anyone, and the database is portable.

## What it does

- **Import** your saved list straight from TikTok's official data export.
- **Enrich** each video with creator, caption, hashtags, duration and thumbnail.
- **Transcribe** the audio with Whisper, stored timecoded and searchable.
- **Tag** each video with a genre from caption + hashtags + transcript.
- **Search** across all of it at once, with creator and genre filters.
- **Email + password login**, bcrypt-hashed, registration closed after the first
  account.

## Quick look

```bash
pip install -r requirements.txt
python seed_demo.py --email demo@example.com --password demo12345 --count 1500
python run.py --db organizer.db
```

Open http://127.0.0.1:5000 and sign in with those details.

`seed_demo.py` generates fake videos so you can try the interface — invented
handles, captions and transcripts, with a banner on every page saying so. Nothing
in it is scraped and the links do not resolve.

## Using it with your real library

### 1. Get your data out of TikTok

In the app: **Profile → Settings and privacy → Account → Download your data**,
choose **JSON**, request it. TikTok takes a day or two, then gives you a zip.

### 2. Import the saved list

```bash
python run.py                      # register your account at /register first
python -m pipeline.import_export "TikTok Data.zip" --email you@example.com
```

Add `--dry-run` to see what it found without writing anything. `--section likes`
or `--section history` import those lists instead of favourites.

TikTok has changed this file's layout several times, so the parser walks the
whole document looking for date + link pairs rather than expecting one fixed
shape. It reads `.json`, `.txt` and the zip directly.

### 3. Fetch the metadata

```bash
python -m pipeline.enrich --email you@example.com --workers 3 --delay 1.2
```

Stop and restart it whenever you like — only videos still marked `pending` get
picked up, so an overnight run that dies at 3am resumes where it stopped.

Videos the creator has since deleted or made private get marked `unavailable`.
They stay in your library with their saved date and are filterable, they just
have nothing left to fetch. On a list saved over several years this is normally
a noticeable slice, and there is no way around it — the video is gone.

### 4. Transcribe (the expensive step)

```bash
python -m pipeline.transcribe --email you@example.com --limit 500          # local
python -m pipeline.transcribe --email you@example.com --engine api         # hosted
```

Local runs faster-whisper on your machine: free, nothing leaves the box, speed
depends entirely on your hardware. `--engine api` uses OpenAI's Whisper endpoint
at roughly $0.006 per audio minute and needs no GPU.

Nothing else depends on a video having a transcript, so `--limit`, `--creator`
and `--genre` let you transcribe the part of the library you actually search
first and leave the tail for later. Audio is downloaded to a temp file and
deleted straight after — the library stores text, never video.

### 5. Tag genres

```bash
python -m pipeline.classify --email you@example.com                # keyword rules
python -m pipeline.classify --email you@example.com --mode llm     # ambiguous tail
```

Rules mode is free and instant, and on hashtag-rich captions it is right most of
the time. `--mode llm` only re-checks the ones the rules were unsure about, so
the API cost tracks the genuinely ambiguous videos rather than the whole library.

Re-run with `--redo` after transcribing to fold the new text into the tagging.

## Scale

Rough shape for a 40,000-video library, assuming ~30s average. These are
estimates from the design, not measured on your data:

| Step        | Time                              | Cost                     |
|-------------|-----------------------------------|--------------------------|
| Import      | seconds                           | free                     |
| Enrich      | overnight-ish, 3 workers @ ~1.2s  | free                     |
| Transcribe  | many hours (local, hardware-bound) | free                     |
| Transcribe  | — (hosted)                        | ~$120–150 at $0.006/min  |
| Classify    | minutes (rules)                   | free                     |

Search itself stays fast at this size: SQLite FTS5 with an external-content
index, kept in sync by triggers.

## Search notes

- One box searches captions, transcripts, creators and hashtags together.
- The caption and creator are weighted above the transcript — a word in a
  six-word caption says more about a video than the same word dropped once in
  three minutes of speech.
- The last word prefix-matches, so `kni` already finds `knife`.
- `"quoted phrases"` match exactly.
- Accents fold both ways: `cafe` finds `café` and vice versa.
- Punctuation is stripped rather than passed to FTS5, so `chef's knife (best)`
  searches instead of erroring.

## Layout

```
app/                 Flask app: auth, search, templates, static
  db.py              schema, FTS5 index, search + facet queries
pipeline/
  import_export.py   step 1 - read the TikTok export
  enrich.py          step 2 - metadata per video
  transcribe.py      step 3 - Whisper
  classify.py        step 4 - genre tagging
seed_demo.py         generate demo data
run.py               dev server
```

## Deploying it properly

The bundled server is Flask's development one. For anything long-lived:

```bash
pip install gunicorn
export ORGANIZER_SECRET="$(python -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')"
export ORGANIZER_DB=/path/to/organizer.db
gunicorn -w 2 -b 127.0.0.1:5000 "app:create_app()"
```

Set `ORGANIZER_SECRET` — without it a fresh key is generated at startup and
every restart signs you out. Put it behind HTTPS if it is reachable from the
internet. `ORGANIZER_ALLOW_SIGNUP=1` re-opens registration beyond the first
account.
