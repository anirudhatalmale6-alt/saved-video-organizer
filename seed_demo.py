"""Fill the database with generated demo videos so the UI can be tried out.

Everything this creates is invented: the handles, captions and transcripts are
written by this script, not scraped from TikTok, and every creator handle is
prefixed `@demo.` so a demo row can never be mistaken for a real account. The
links point at tiktok.com/@demo.* and will not resolve - that is deliberate.

    python seed_demo.py --email demo@example.com --password demo12345 --count 1500
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone

from passlib.hash import bcrypt

from app.db import init_db, now

# genre -> (creator name stems, caption templates, spoken lines, hashtags)
CONTENT = {
    "Cooking & Recipes": {
        "stems": ["chef", "kitchen", "hungry", "skillet", "spice", "batch", "pantry"],
        "captions": [
            "the 15 minute {thing} that ruined takeaway for me",
            "stop boiling your {thing}. do this instead",
            "3 ingredient {thing} - no oven, no mixer",
            "my grandmother's {thing} recipe, finally written down",
            "meal prep sunday: five {thing} lunches under 20 minutes",
            "why restaurant {thing} tastes better than yours",
        ],
        "things": ["pasta", "garlic bread", "curry", "fried rice", "soup", "dumplings",
                   "risotto", "noodles", "chicken", "sauce"],
        "lines": [
            "So the first thing everybody gets wrong here is the heat.",
            "You want the pan smoking before anything touches it.",
            "Two tablespoons of oil, and don't crowd the pan or it steams.",
            "Salt the water until it tastes like the sea, I mean it.",
            "Give that about four minutes and you'll see the edges catch.",
            "Off the heat before you add the garlic or it turns bitter.",
            "Taste it now. If it's flat, it needs acid, not more salt.",
            "That's it. Fifteen minutes, one pan, and no one has to know.",
        ],
        "tags": ["recipe", "cooking", "foodtok", "easyrecipes", "mealprep", "kitchen"],
    },
    "Fitness & Training": {
        "stems": ["lift", "coach", "strong", "reps", "iron", "form", "train"],
        "captions": [
            "your {thing} isn't growing because of this one thing",
            "form check: the {thing} mistake I see every single day",
            "3 sets of this beats an hour of {thing}",
            "beginner {thing} program - week one",
            "stop training {thing} like this",
        ],
        "things": ["squat", "deadlift", "bench", "pull up", "core", "cardio", "back"],
        "lines": [
            "Alright, if your knees cave in on the way up, watch this.",
            "Brace like someone's about to punch you in the stomach.",
            "Three sets of eight, and the last two should be genuinely hard.",
            "You don't need more volume, you need more tension.",
            "Slow the negative down to three seconds and it changes everything.",
            "Rest ninety seconds. Yes, actually time it.",
            "Progressive overload just means slightly more than last week.",
        ],
        "tags": ["gymtok", "fitness", "workout", "formcheck", "training", "strength"],
    },
    "Money & Finance": {
        "stems": ["money", "ledger", "budget", "invest", "coin", "fiscal"],
        "captions": [
            "the {thing} rule nobody explains properly",
            "I tracked every pound for a year. here's what {thing} actually cost me",
            "why your {thing} is quietly losing money",
            "{thing} explained in 60 seconds",
        ],
        "things": ["savings", "emergency fund", "index fund", "credit score",
                   "pension", "tax return", "budget"],
        "lines": [
            "Okay so the fifty thirty twenty rule is a starting point, not a law.",
            "Your emergency fund goes in a savings account, not the market.",
            "Compound interest only looks boring until year seven.",
            "If the interest rate is above about six percent, pay that first.",
            "I'm not a financial advisor, this is just what worked for me.",
            "Automate it on payday and you'll never notice it leaving.",
        ],
        "tags": ["moneytok", "finance", "budgeting", "investing", "savings"],
    },
    "Beauty & Skincare": {
        "stems": ["glow", "derm", "skin", "beauty", "serum", "blush"],
        "captions": [
            "the {thing} step everyone skips",
            "I used {thing} for 30 days - honest results",
            "drugstore {thing} that beats the expensive one",
            "my full morning {thing} routine",
        ],
        "things": ["retinol", "sunscreen", "moisturiser", "vitamin C", "cleanser"],
        "lines": [
            "Sunscreen. Every single day. Yes, even in December.",
            "Start retinol twice a week, not every night, or you'll wreck your barrier.",
            "Damp skin first, then the moisturiser, it makes a real difference.",
            "Give any product twelve weeks before you decide it isn't working.",
            "Patch test on your jaw for three days first.",
        ],
        "tags": ["skincare", "beautytok", "skincareroutine", "retinol", "spf"],
    },
    "Home & DIY": {
        "stems": ["fix", "home", "tidy", "renovate", "corner", "nest"],
        "captions": [
            "the {thing} hack that saved my rental deposit",
            "I organised my whole {thing} for under 30 quid",
            "renters: you can actually fix your {thing} yourself",
            "small {thing}? do these four things",
        ],
        "things": ["kitchen", "wardrobe", "bathroom", "hallway", "shelf", "desk"],
        "lines": [
            "Everything you own that you use daily should live at eye level.",
            "Command strips, not screws, if you want your deposit back.",
            "Empty the whole drawer first. Every time. It's the only way.",
            "Label the box or in six weeks it's a mystery box.",
            "This whole thing cost me about twenty eight pounds.",
        ],
        "tags": ["cleantok", "diy", "organising", "homehacks", "renting"],
    },
    "Travel": {
        "stems": ["wander", "roam", "atlas", "nomad", "transit", "voyage"],
        "captions": [
            "3 days in {thing} on a real budget",
            "the {thing} mistake that cost me a flight",
            "how I pack for two weeks in one carry on for {thing}",
            "{thing}: what I'd do differently",
        ],
        "things": ["Lisbon", "Tokyo", "Rome", "Bangkok", "Reykjavik", "Porto"],
        "lines": [
            "Book the flight on a Tuesday, it's not a myth, I've checked.",
            "Always leave three hours between connecting flights internationally.",
            "Roll, don't fold, and use the packing cubes properly.",
            "The free walking tour on day one saves you two days of wandering.",
            "Get the transit card at the airport, not in town.",
        ],
        "tags": ["traveltok", "budgettravel", "packing", "solotravel", "carryon"],
    },
    "Tech & Gadgets": {
        "stems": ["byte", "circuit", "pixel", "stack", "cache", "signal"],
        "captions": [
            "this {thing} setting is on by default and it shouldn't be",
            "{thing} tips I wish I knew three years ago",
            "cheap {thing} that actually works",
            "stop paying for {thing}, do this",
        ],
        "things": ["iPhone", "laptop", "router", "keyboard", "cloud storage", "VPN"],
        "lines": [
            "Go into settings, privacy, and turn that second one off right now.",
            "You're paying monthly for something your machine already does.",
            "Back it up in three places or you don't have a backup.",
            "This costs about forty quid and does ninety percent of the job.",
            "Restart it properly, not sleep. There's a difference.",
        ],
        "tags": ["techtok", "techtips", "gadgets", "productivity", "settings"],
    },
    "Education & Explainers": {
        "stems": ["curio", "why", "atlas", "footnote", "margin", "archive"],
        "captions": [
            "why {thing} is not what you were taught",
            "the {thing} thing nobody mentions in school",
            "{thing}, explained without the jargon",
            "here's why {thing} actually happened",
        ],
        "things": ["gravity", "the Roman empire", "sleep", "memory", "inflation",
                   "the printing press"],
        "lines": [
            "So the version you got in school is roughly a hundred years out of date.",
            "Here's the bit that actually matters, and it's not the date.",
            "Two things had to be true at the same time for this to work.",
            "Everyone remembers the outcome and forgets the cause.",
            "There's a paper from 2019 that changed how we read this entirely.",
        ],
        "tags": ["learnontiktok", "history", "science", "explainer", "facts"],
    },
    "Comedy & Skits": {
        "stems": ["bit", "sketch", "deadpan", "punchline", "sofa"],
        "captions": [
            "pov: your {thing} at 3pm on a friday",
            "every {thing} conversation ever",
            "when your {thing} does this and you can't say anything",
            "{thing}. that's the whole video.",
        ],
        "things": ["manager", "flatmate", "group chat", "gym buddy", "landlord"],
        "lines": [
            "No no no, I'm not saying that. I'm saying the other thing.",
            "And then she just looked at me. Full eye contact. Nothing.",
            "Right, so we're doing this now. Okay. Cool. Cool cool cool.",
            "I have never in my life been so calm about anything.",
        ],
        "tags": ["comedy", "skit", "pov", "funny", "relatable"],
    },
    "Pets & Animals": {
        "stems": ["paw", "bark", "whisker", "kennel", "furry", "tail"],
        "captions": [
            "teaching my {thing} this took four days",
            "the {thing} behaviour everyone misreads",
            "3 things I'd tell any new {thing} owner",
            "my {thing} has opinions",
        ],
        "things": ["dog", "puppy", "cat", "rescue", "kitten"],
        "lines": [
            "Reward the calm, not the excitement. That's the whole trick.",
            "Four short sessions a day beats one long one, every time.",
            "If they're doing this with their tail, they are not happy.",
            "Never call them over to do something they don't like.",
        ],
        "tags": ["dogtok", "cattok", "petsoftiktok", "dogtraining", "rescue"],
    },
    "Career & Productivity": {
        "stems": ["desk", "focus", "deep", "notion", "inbox", "career"],
        "captions": [
            "the {thing} answer that got me the offer",
            "I stopped doing {thing} and got more done",
            "how to say no to {thing} without sounding difficult",
            "{thing} system I've actually stuck to for two years",
        ],
        "things": ["interview", "to do list", "extra work", "meetings", "email"],
        "lines": [
            "Say what you did, what happened, and what the number was.",
            "Two minute rule: if it's under two minutes, it never gets a list entry.",
            "Block the calendar first, then agree to things.",
            "Nobody is coming to prioritise it for you. That's your job.",
        ],
        "tags": ["careertok", "productivity", "interviewtips", "worklife", "notion"],
    },
    "Music & Dance": {
        "stems": ["chord", "tempo", "reverb", "beat", "loop", "encore"],
        "captions": [
            "this {thing} is four chords and you already know them",
            "{thing} for absolute beginners",
            "the {thing} everyone plays wrong",
            "one take, no edits: {thing}",
        ],
        "things": ["riff", "chord progression", "drum beat", "vocal take", "melody"],
        "lines": [
            "Four chords. That's it. You can play about six hundred songs now.",
            "Count it in your head, don't tap, tapping makes you rush.",
            "The space between the notes is doing most of the work here.",
            "Record it once and listen back. It's never what you think.",
        ],
        "tags": ["musictok", "guitar", "beginner", "chords", "cover"],
    },
}

FIRST = ["ana", "milo", "priya", "jonas", "ife", "sara", "dev", "noor", "kai",
         "lena", "omar", "tess", "arun", "mira", "finn", "zoe", "hugo", "rina"]


def build_creators(rng, per_genre=7):
    creators = []
    for genre, data in CONTENT.items():
        for i in range(per_genre):
            stem = data["stems"][i % len(data["stems"])]
            name = rng.choice(FIRST)
            handle = f"@demo.{stem}.{name}{rng.randint(1, 99)}"
            creators.append({"handle": handle,
                             "name": f"{name.title()} ({stem})",
                             "genre": genre})
    return creators


def make_transcript(rng, data, duration):
    n = max(3, min(9, duration // 7))
    lines = rng.sample(data["lines"], k=min(n, len(data["lines"])))
    segs, t = [], 1.0
    for line in lines:
        span = round(len(line) / 15.0 + rng.uniform(0.4, 1.2), 2)
        segs.append({"start": round(t, 2), "end": round(t + span, 2), "text": line})
        t += span + rng.uniform(0.1, 0.5)
    return " ".join(s["text"] for s in segs), segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="demo@example.com")
    ap.add_argument("--password", default="demo12345")
    ap.add_argument("--db", default="organizer.db")
    ap.add_argument("--count", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = init_db(args.db)

    row = conn.execute("SELECT id FROM users WHERE email=?", (args.email,)).fetchone()
    if row:
        uid = row["id"]
        conn.execute("DELETE FROM videos WHERE user_id=?", (uid,))
        print(f"Reusing account {args.email}, cleared its videos")
    else:
        uid = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            (args.email, bcrypt.hash(args.password), now()),
        ).lastrowid
        print(f"Created account {args.email}")

    creators = build_creators(rng)
    base = datetime.now(timezone.utc) - timedelta(days=900)
    made = 0

    for i in range(args.count):
        c = rng.choice(creators)
        # A tenth of what a creator posts sits outside their usual lane, which
        # is what makes the genre facet worth having rather than a creator list.
        genre = c["genre"] if rng.random() > 0.1 else rng.choice(list(CONTENT))
        data = CONTENT[genre]

        thing = rng.choice(data["things"])
        caption = rng.choice(data["captions"]).format(thing=thing)
        tags = rng.sample(data["tags"], k=rng.randint(2, 4))
        caption_full = caption + " " + " ".join("#" + t for t in tags)

        duration = rng.choice([12, 18, 23, 29, 34, 41, 52, 67, 88, 121])
        saved = base + timedelta(days=rng.uniform(0, 900), hours=rng.uniform(0, 24))

        # Mirror the real shape of a big saved list: some never transcribed,
        # some gone from TikTok entirely.
        roll = rng.random()
        if roll < 0.06:
            status, transcript, segs = "unavailable", "", []
        elif roll < 0.30:
            status, transcript, segs = "ok", "", []
        else:
            status = "ok"
            transcript, segs = make_transcript(rng, data, duration)

        conn.execute(
            """INSERT INTO videos (user_id, tiktok_id, url, creator_handle, creator_name,
               caption, hashtags, duration, saved_at, status, status_note, transcript,
               transcript_lang, segments, genre, genre_confidence, is_demo, enriched_at,
               transcribed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (uid, str(7000000000000000000 + i),
             f"https://www.tiktok.com/{c['handle']}/video/{7000000000000000000 + i}",
             c["handle"], c["name"], caption_full, json.dumps(tags), duration,
             saved.isoformat(timespec="seconds"), status,
             "Creator removed this video or set the account to private."
             if status == "unavailable" else None,
             transcript, "en" if transcript else None, json.dumps(segs),
             genre if status != "unavailable" or rng.random() > 0.5 else None,
             round(rng.uniform(0.55, 0.98), 2), now(),
             now() if transcript else None),
        )
        made += 1

    conn.commit()
    stats = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT creator_handle) c, "
        "COUNT(DISTINCT genre) g, SUM(transcript<>'') t FROM videos WHERE user_id=?",
        (uid,),
    ).fetchone()
    conn.close()

    print(f"Seeded {made:,} demo videos: {stats['c']} creators, {stats['g']} genres, "
          f"{stats['t']:,} with transcripts")
    print(f"Sign in at /login with {args.email} / {args.password}")


if __name__ == "__main__":
    sys.exit(main())
