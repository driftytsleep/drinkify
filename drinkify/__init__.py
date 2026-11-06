"""Drinkify: music to drink to. Enter an artist, get a drink. No AI, just a lookup table and some math."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parent.parent


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get("DRINKIFY_DB", str(Path(app.instance_path) / "drinkify.sqlite")),
        OFFLINE=os.environ.get("DRINKIFY_OFFLINE", "") not in ("", "0", "false"),
        FIXTURES=os.environ.get("DRINKIFY_FIXTURES", str(ROOT / "tests" / "fixtures" / "musicbrainz")),
        USER_AGENT=os.environ.get(
            "DRINKIFY_USER_AGENT", "Drinkify/1.0 (+https://github.com/driftytsleep/drinkify)"),
        MB_TIMEOUT=float(os.environ.get("DRINKIFY_MB_TIMEOUT", "5")),
    )
    if test_config:
        app.config.update(test_config)

    from . import db, routes

    db.init_app(app)
    app.register_blueprint(routes.bp)

    # First run convenience: build the catalog if the database is empty.
    with app.app_context():
        conn = db.get_db()
        if not db.is_seeded(conn):
            db.init_db(conn)

    return app
