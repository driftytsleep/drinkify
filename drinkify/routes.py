from __future__ import annotations

import time

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from . import matcher
from .db import get_catalog, get_db
from .musicbrainz import UUID_RE, MusicBrainz, MusicBrainzError, NotFound, pick

bp = Blueprint("drinkify", __name__)

CATEGORY_ORDER = ["cocktail", "shot", "beer", "wine", "neat", "punch"]


def mb() -> MusicBrainz:
    cfg = current_app.config
    return MusicBrainz(get_db(), user_agent=cfg["USER_AGENT"], offline=cfg["OFFLINE"],
                       fixtures=cfg["FIXTURES"], timeout=cfg["MB_TIMEOUT"])


def recent_pairings(limit: int = 8):
    return get_db().execute(
        """SELECT p.mbid, p.artist_name, p.drink_slug, d.name AS drink_name
           FROM pairings p JOIN drinks d ON d.slug = p.drink_slug
           WHERE p.id IN (SELECT MAX(id) FROM pairings GROUP BY mbid)
           ORDER BY p.id DESC LIMIT ?""", (limit,)).fetchall()


@bp.app_template_filter("describe")
def describe_candidate(c: dict) -> str:
    bits = [b for b in (c.get("disambiguation"), c.get("country"),
                        str(c["begin_year"]) if c.get("begin_year") else None,
                        (c.get("type") or "").lower() or None) if b]
    return ", ".join(bits)


@bp.get("/")
def index():
    query = (request.args.get("artist") or "").strip()[:200]
    if not query:
        return render_template("index.html", recent=recent_pairings())
    try:
        results = mb().search(query)
    except MusicBrainzError:
        return render_template("error.html", query=query,
                               message="the music database is unreachable right now. try again in a minute."), 503
    choice = pick(query, results)
    if choice is None:
        return render_template("error.html", query=query,
                               message="never heard of them. check the spelling?"), 404
    if isinstance(choice, dict):
        return redirect(url_for("drinkify.artist", mbid=choice["mbid"]))
    return render_template("choose.html", query=query, candidates=choice)


@bp.get("/a/<mbid>")
def artist(mbid: str):
    if not UUID_RE.match(mbid):
        abort(404)
    try:
        a = mb().artist(mbid)
    except NotFound:
        abort(404)
    except MusicBrainzError:
        return render_template("error.html", query="",
                               message="the music database is unreachable right now. try again in a minute."), 503

    result = matcher.pair(a, get_catalog())
    db = get_db()
    last = db.execute("SELECT drink_slug FROM pairings WHERE mbid = ? ORDER BY id DESC LIMIT 1",
                      (mbid,)).fetchone()
    if not last or last["drink_slug"] != result.drink.slug:
        db.execute("INSERT INTO pairings (mbid, artist_name, drink_slug, created_at) VALUES (?, ?, ?, ?)",
                   (mbid, a.name, result.drink.slug, int(time.time())))
    else:
        db.execute("UPDATE pairings SET created_at = ? WHERE id = (SELECT MAX(id) FROM pairings WHERE mbid = ?)",
                   (int(time.time()), mbid))
    db.commit()
    return render_template("result.html", p=result, spirit_label=matcher.spirit_label)


@bp.get("/drink/<slug>")
def drink(slug: str):
    d = next((d for d in get_catalog().drinks if d.slug == slug), None)
    if d is None:
        abort(404)
    return render_template("drink.html", d=d)


@bp.get("/drinks")
def drinks():
    by_cat: dict[str, list] = {c: [] for c in CATEGORY_ORDER}
    for d in sorted(get_catalog().drinks, key=lambda d: d.name.lower()):
        by_cat.setdefault(d.category, []).append(d)
    return render_template("drinks.html", by_cat=by_cat)


@bp.get("/about")
def about():
    cat = get_catalog()
    return render_template("about.html", n_drinks=len(cat.drinks), n_genres=len(cat.genres),
                           n_aliases=len(cat.aliases), weights=(matcher.W_FLAVOR, matcher.W_SPIRIT,
                                                                matcher.W_VIBE, matcher.W_MODIFIER))


@bp.app_errorhandler(404)
def not_found(_e):
    return render_template("error.html", query="", message="nothing here. not even a drink."), 404
