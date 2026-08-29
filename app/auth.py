"""Email + password authentication.

Passwords are hashed with bcrypt (via passlib). Sessions are Flask's signed
cookie sessions, which is enough for a single-user personal library; if this
ever gets shared with other people, swap SECRET_KEY for a real one from the
environment (see README).
"""

import functools
import re

from flask import (Blueprint, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from passlib.hash import bcrypt

from .db import connect, now

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@bp.before_app_request
def load_user():
    uid = session.get("user_id")
    g.user = None
    if uid is not None:
        g.user = get_db().execute(
            "SELECT id, email FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)

    return wrapped


@bp.route("/register", methods=("GET", "POST"))
def register():
    db = get_db()
    # First account to exist owns the library. After that, registration is
    # closed unless ALLOW_SIGNUP is on - this is a personal tool, not a SaaS.
    have_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    if have_users and not current_app.config.get("ALLOW_SIGNUP"):
        flash("Registration is closed on this instance.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        error = None
        if not EMAIL_RE.match(email):
            error = "That doesn't look like a valid email address."
        elif len(password) < MIN_PASSWORD:
            error = f"Password needs to be at least {MIN_PASSWORD} characters."
        elif password != confirm:
            error = "The two passwords don't match."
        elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            error = "An account with that email already exists."

        if error is None:
            cur = db.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                (email, bcrypt.hash(password), now()),
            )
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
            return redirect(url_for("views.library"))
        flash(error, "error")

    return render_template("register.html", first_run=not have_users)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        # One message for both cases so the form can't be used to find out
        # which email addresses have accounts.
        if user is None or not bcrypt.verify(password, user["password_hash"]):
            flash("Wrong email or password.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next") or ""
            # Only ever redirect within this app - an absolute URL here would
            # let a crafted login link bounce the user off-site after auth.
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                            else url_for("views.library"))
    no_users = get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    return render_template("login.html", no_users=no_users)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
