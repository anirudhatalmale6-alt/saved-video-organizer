import os

from flask import Flask

from .db import init_db


def create_app(db_path=None):
    app = Flask(__name__)
    app.config.update(
        DB_PATH=db_path or os.environ.get("ORGANIZER_DB", "organizer.db"),
        # A generated key logs everyone out on restart; set ORGANIZER_SECRET in
        # the environment for anything that needs to survive a reboot.
        SECRET_KEY=os.environ.get("ORGANIZER_SECRET") or os.urandom(32),
        ALLOW_SIGNUP=os.environ.get("ORGANIZER_ALLOW_SIGNUP") == "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=256 * 1024 * 1024,
    )
    init_db(app.config["DB_PATH"]).close()

    from . import auth, views
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.teardown_appcontext(auth.close_db)
    return app
