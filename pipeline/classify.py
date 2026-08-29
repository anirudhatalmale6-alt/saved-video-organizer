"""Genre tagging.

Two modes:

  rules  (default) - weighted keyword scoring over hashtags, caption and
                     transcript. Free, instant, no network, and on hashtag-rich
                     TikTok captions it is right most of the time.
  llm              - send caption + hashtags + the first slice of transcript to
                     a model and let it pick from the same genre list.

The rule pass runs first regardless; --mode llm only re-checks the ones the
rules were not confident about, which keeps the API bill proportional to the
genuinely ambiguous tail rather than the whole library.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, now  # noqa: E402

# keyword -> weight is implicit: hashtag hits count 3x, caption 2x, transcript 1x
GENRES = {
    "Cooking & Recipes": ["recipe", "cooking", "cook", "baking", "bake", "kitchen",
                          "meal", "mealprep", "food", "foodtok", "dinner", "pasta",
                          "airfryer", "ingredients", "tablespoon", "preheat", "sauce"],
    "Fitness & Training": ["workout", "gym", "fitness", "reps", "sets", "squat",
                           "deadlift", "cardio", "training", "muscle", "gymtok",
                           "form check", "hypertrophy", "warm up"],
    "Money & Finance": ["money", "invest", "investing", "stocks", "budget", "salary",
                        "savings", "debt", "credit", "tax", "finance", "financetok",
                        "portfolio", "interest rate", "retirement", "401k"],
    "Beauty & Skincare": ["makeup", "skincare", "serum", "moisturizer", "routine",
                          "foundation", "concealer", "retinol", "spf", "beautytok",
                          "hair", "haircare", "nails"],
    "Fashion & Style": ["outfit", "ootd", "style", "fashion", "wardrobe", "thrift",
                        "haul", "capsule", "styling", "fit check", "denim"],
    "Home & DIY": ["diy", "home", "renovation", "declutter", "organize", "organizing",
                   "cleaning", "cleantok", "hack", "furniture", "apartment", "storage"],
    "Travel": ["travel", "trip", "flight", "hotel", "itinerary", "airbnb", "packing",
               "backpacking", "visa", "traveltok", "layover", "hostel"],
    "Comedy & Skits": ["comedy", "skit", "joke", "funny", "prank", "humor", "pov",
                       "meme", "bit", "punchline"],
    "Tech & Gadgets": ["tech", "iphone", "android", "app", "laptop", "gadget",
                       "software", "ai", "coding", "code", "setup", "keyboard", "gpu"],
    "Education & Explainers": ["explain", "history", "science", "fact", "learn",
                               "study", "physics", "biology", "psychology", "actually",
                               "research", "did you know", "here's why"],
    "Health & Wellness": ["sleep", "anxiety", "therapy", "mental health", "stress",
                          "vitamin", "gut", "hydration", "mindfulness", "burnout"],
    "Pets & Animals": ["dog", "cat", "puppy", "kitten", "pet", "vet", "adopt",
                       "dogtok", "cattok", "training my dog", "rescue"],
    "Parenting": ["toddler", "baby", "kids", "parenting", "mom", "dad", "newborn",
                  "nap", "daycare", "momtok"],
    "Music & Dance": ["song", "music", "dance", "choreo", "singing", "cover",
                      "guitar", "piano", "producer", "beat", "lyrics"],
    "Gaming": ["game", "gaming", "gameplay", "minecraft", "fortnite", "speedrun",
               "console", "playstation", "xbox", "loadout", "boss fight"],
    "Career & Productivity": ["career", "job", "interview", "resume", "linkedin",
                              "productivity", "notion", "workflow", "manager",
                              "promotion", "remote work", "burnout at work"],
    "Books & Reading": ["book", "booktok", "reading", "novel", "author", "chapter",
                        "tbr", "audiobook", "library"],
    "Crafts & Art": ["craft", "crochet", "knitting", "sewing", "painting", "drawing",
                     "art", "resin", "pottery", "sketch"],
    "Cars & Motors": ["car", "engine", "motor", "detailing", "tyre", "tire", "ev",
                      "carsoftiktok", "garage", "brake"],
    "Relationships": ["relationship", "dating", "boyfriend", "girlfriend", "marriage",
                      "breakup", "red flag", "green flag", "couple"],
}

# Multi-word keys must be matched as substrings; single words as whole words, so
# "ai" doesn't fire on "said" and "art" doesn't fire on "start".
_PATTERNS = {
    g: [(k, re.compile(re.escape(k) if " " in k else rf"\b{re.escape(k)}\b", re.I))
        for k in kws]
    for g, kws in GENRES.items()
}


def score(caption="", hashtags=(), transcript=""):
    """Return (genre, confidence 0-1, full score table)."""
    hay_tags = " ".join(hashtags or ())
    # Only the opening stretch of a long transcript is topical; the tail drifts.
    hay_tr = (transcript or "")[:1500]
    totals = {}
    for genre, pats in _PATTERNS.items():
        s = 0
        for _kw, pat in pats:
            if pat.search(hay_tags):
                s += 3
            if pat.search(caption or ""):
                s += 2
            if pat.search(hay_tr):
                s += 1
        if s:
            totals[genre] = s
    if not totals:
        return None, 0.0, {}

    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0
    # Confidence is about the *margin*: a video scoring 9 for Cooking and 8 for
    # Health is a coin flip, not a confident call, however high the raw score.
    margin = (best_score - runner) / best_score
    confidence = round(min(1.0, (0.45 * min(best_score, 8) / 8) + 0.55 * margin), 2)
    return best, confidence, totals


def classify_llm(rows, model="claude-sonnet-5"):
    """Optional second pass for the ambiguous tail. Needs ANTHROPIC_API_KEY."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("pip install anthropic  (or run without --mode llm)")

    client = anthropic.Anthropic()
    out = {}
    batch_size = 20
    names = list(GENRES) + ["Other"]
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        listing = "\n".join(
            f'{r["id"]}. caption={r["caption"][:160]!r} '
            f'tags={json.loads(r["hashtags"] or "[]")[:8]} '
            f'transcript={r["transcript"][:300]!r}'
            for r in chunk
        )
        msg = client.messages.create(
            model=model,
            max_tokens=1500,
            system=("You label short videos by genre. Reply with JSON only: "
                    '{"<id>": "<genre>"}. Choose each genre from this exact list: '
                    + ", ".join(names) + ". Use Other only if none fit."),
            messages=[{"role": "user", "content": listing}],
        )
        text = msg.content[0].text.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        try:
            for k, v in json.loads(text).items():
                if v in names:
                    out[int(k)] = v
        except (ValueError, TypeError):
            print(f"  ! could not parse model reply for batch {i // batch_size}")
        print(f"  labelled {min(i + batch_size, len(rows)):,}/{len(rows):,}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Tag videos with a genre")
    ap.add_argument("--email", required=True)
    ap.add_argument("--db", default=os.environ.get("ORGANIZER_DB", "organizer.db"))
    ap.add_argument("--mode", default="rules", choices=("rules", "llm"))
    ap.add_argument("--min-confidence", type=float, default=0.35,
                    help="below this the genre is left blank for the llm pass")
    ap.add_argument("--redo", action="store_true", help="re-tag already-tagged videos")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = connect(args.db)
    user = conn.execute("SELECT id FROM users WHERE email=?", (args.email,)).fetchone()
    if not user:
        raise SystemExit(f"No account for {args.email}")

    where = "user_id = ?" if args.redo else "user_id = ? AND (genre IS NULL OR genre='')"
    sql = f"SELECT * FROM videos WHERE {where} ORDER BY id"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, (user["id"],)).fetchall()
    print(f"{len(rows):,} videos to tag")

    unsure, tagged = [], 0
    for r in rows:
        genre, conf, _ = score(r["caption"], json.loads(r["hashtags"] or "[]"),
                               r["transcript"])
        if genre and conf >= args.min_confidence:
            conn.execute("UPDATE videos SET genre=?, genre_confidence=? WHERE id=?",
                         (genre, conf, r["id"]))
            tagged += 1
        else:
            unsure.append(r)
    conn.commit()
    print(f"Rules tagged {tagged:,}; {len(unsure):,} unclear")

    if args.mode == "llm" and unsure:
        for vid, genre in classify_llm(unsure).items():
            conn.execute("UPDATE videos SET genre=?, genre_confidence=? WHERE id=?",
                         (genre, 0.8, vid))
        conn.commit()
        print("LLM pass done")

    conn.close()


if __name__ == "__main__":
    main()
