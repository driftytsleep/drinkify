"""SQLite access, seeding from the JSON files in drinkify/data, and catalog loading."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import click
from flask import current_app, g

from .matcher import AXES, Catalog, Descriptor, Drink, Genre, Twist

DATA_DIR = Path(__file__).parent / "data"
SCHEMA = Path(__file__).parent / "schema.sql"


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------- seeding

def slugify(name: str) -> str:
    s = name.lower().replace("'", "").replace("é", "e").replace("ç", "c")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _vec(f: list[int | float]) -> list[float]:
    if len(f) != len(AXES):
        raise ValueError(f"flavor vector must have {len(AXES)} values, got {f}")
    return [round(float(x) / 10.0, 3) for x in f]


def resolve_genres(raw: dict[str, dict]) -> dict[str, dict]:
    """Fill each genre's missing fields from its parent chain."""
    out: dict[str, dict] = {}

    def resolve(name: str, stack: tuple[str, ...] = ()) -> dict:
        if name in out:
            return out[name]
        if name not in raw:
            raise ValueError(f"unknown parent genre {name!r}")
        if name in stack:
            raise ValueError(f"genre parent cycle: {' -> '.join(stack + (name,))}")
        spec = dict(raw[name])
        parent = spec.get("parent")
        if parent:
            base = resolve(parent, stack + (name,))
            for key in ("f", "spirits", "vibes", "quip"):
                spec.setdefault(key, base.get(key))
        for key in ("f", "spirits", "vibes"):
            if spec.get(key) is None:
                raise ValueError(f"genre {name!r} has no {key!r} and no parent to inherit it from")
        out[name] = spec
        return spec

    for name in raw:
        resolve(name)
    return out


def seed(conn: sqlite3.Connection, data_dir: Path = DATA_DIR) -> dict[str, int]:
    drinks = json.loads((data_dir / "drinks.json").read_text())["drinks"]
    genres = resolve_genres(json.loads((data_dir / "genres.json").read_text())["genres"])
    countries = json.loads((data_dir / "countries.json").read_text())["countries"]

    ingredient_ids: dict[str, int] = {}
    slugs = set()
    for d in drinks:
        slug = d.get("slug") or slugify(d["name"])
        if slug in slugs:
            raise ValueError(f"duplicate drink slug {slug!r}")
        slugs.add(slug)
        cur = conn.execute(
            f"""INSERT INTO drinks (slug, name, category, base_spirit, glass, method, instructions, era, house,
                                    {", ".join(AXES)})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, {", ".join("?" * len(AXES))})""",
            (slug, d["name"], d["cat"], d["spirit"], d["glass"], d["method"], d["steps"], d.get("era"),
             1 if d.get("house") else 0, *_vec(d["f"])),
        )
        drink_id = cur.lastrowid
        for pos, (amount, ingredient) in enumerate(d["ing"]):
            key = ingredient.lower()
            if key not in ingredient_ids:
                ingredient_ids[key] = conn.execute(
                    "INSERT INTO ingredients (name) VALUES (?)", (key,)).lastrowid
            conn.execute("INSERT INTO drink_ingredients VALUES (?, ?, ?, ?)",
                         (drink_id, ingredient_ids[key], amount, pos))
        for tag in sorted(set(d.get("tags", []))):
            conn.execute("INSERT INTO drink_tags VALUES (?, ?)", (drink_id, tag))

    aliases: dict[str, str] = {}
    for name, spec in genres.items():
        conn.execute(
            f"""INSERT INTO genres (name, parent, {", ".join(AXES)}, spirits_json, vibes_json, quip)
                VALUES (?, ?, {", ".join("?" * len(AXES))}, ?, ?, ?)""",
            (name, spec.get("parent"), *_vec(spec["f"]), json.dumps(spec["spirits"]),
             json.dumps(sorted(set(spec["vibes"]))), spec.get("quip")),
        )
        for alias in spec.get("aliases", []):
            alias = alias.lower()
            if alias in genres or (alias in aliases and aliases[alias] != name):
                raise ValueError(f"alias {alias!r} on {name!r} clashes with another genre or alias")
            aliases[alias] = name
    conn.executemany("INSERT INTO genre_aliases VALUES (?, ?)", sorted(aliases.items()))

    for code, c in countries.items():
        conn.execute("INSERT INTO countries VALUES (?, ?, ?)", (code, c["name"], json.dumps(c["spirits"])))

    descriptors = json.loads((data_dir / "descriptors.json").read_text())["descriptors"]
    seen_tags: dict[str, str] = {}
    for name, spec in descriptors.items():
        unknown = set(spec.get("d", {})) - set(AXES)
        if unknown:
            raise ValueError(f"descriptor {name!r} has unknown axes {sorted(unknown)}")
        if spec.get("country") and spec["country"] not in countries:
            raise ValueError(f"descriptor {name!r} points at unknown country {spec['country']!r}")
        deltas = [round(spec.get("d", {}).get(a, 0) / 10.0, 3) for a in AXES]
        for tag in [name] + spec.get("aliases", []):
            tag = tag.lower()
            if tag in genres or tag in aliases:
                raise ValueError(f"descriptor tag {tag!r} is already a genre; genres win, so drop it")
            if tag in seen_tags:
                raise ValueError(f"descriptor tag {tag!r} used by both {seen_tags[tag]!r} and {name!r}")
            seen_tags[tag] = name
            conn.execute("INSERT INTO descriptors VALUES (?, ?, ?, ?, ?, ?)",
                         (tag, name, json.dumps(deltas), json.dumps(sorted(set(spec.get("vibes", [])))),
                          json.dumps(spec.get("spirits", {})), spec.get("country")))

    twists = json.loads((data_dir / "twists.json").read_text())["twists"]
    allowed = {"text", "methods", "not_methods", "categories", "not_categories", "spirits", "not_spirits", "needs"}
    for t in twists:
        if set(t) - allowed:
            raise ValueError(f"twist {t.get('text')!r} has unknown keys {sorted(set(t) - allowed)}")
        filters = {k: v for k, v in t.items() if k != "text"}
        conn.execute("INSERT INTO twists (text, filters_json) VALUES (?, ?)", (t["text"], json.dumps(filters)))

    return {"drinks": len(drinks), "ingredients": len(ingredient_ids), "genres": len(genres),
            "aliases": len(aliases), "descriptors": len(descriptors), "twists": len(twists),
            "countries": len(countries)}


def init_db(conn: sqlite3.Connection, reset_cache: bool = False) -> dict[str, int]:
    if reset_cache:
        conn.executescript("DROP TABLE IF EXISTS artist_cache; DROP TABLE IF EXISTS search_cache;"
                           "DROP TABLE IF EXISTS pairings;")
    conn.executescript(SCHEMA.read_text())
    counts = seed(conn)
    conn.commit()
    return counts


def is_seeded(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT COUNT(*) FROM drinks").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------- catalog

def load_catalog(conn: sqlite3.Connection) -> Catalog:
    tags: dict[int, set[str]] = {}
    for row in conn.execute("SELECT drink_id, tag FROM drink_tags"):
        tags.setdefault(row["drink_id"], set()).add(row["tag"])
    ingredients: dict[int, list[tuple[str, str]]] = {}
    for row in conn.execute(
        """SELECT di.drink_id, di.amount, i.name FROM drink_ingredients di
           JOIN ingredients i ON i.id = di.ingredient_id ORDER BY di.drink_id, di.position"""):
        ingredients.setdefault(row["drink_id"], []).append((row["amount"], row["name"]))

    drinks = [
        Drink(slug=r["slug"], name=r["name"], category=r["category"], base_spirit=r["base_spirit"],
              glass=r["glass"], method=r["method"], instructions=r["instructions"], era=r["era"],
              house=bool(r["house"]), vec=tuple(r[a] for a in AXES),
              tags=frozenset(tags.get(r["id"], ())), ingredients=tuple(ingredients.get(r["id"], ())))
        for r in conn.execute("SELECT * FROM drinks ORDER BY slug")
    ]
    genres = {
        r["name"]: Genre(name=r["name"], parent=r["parent"], vec=tuple(r[a] for a in AXES),
                         spirits=json.loads(r["spirits_json"]), vibes=frozenset(json.loads(r["vibes_json"])),
                         quip=r["quip"])
        for r in conn.execute("SELECT * FROM genres")
    }
    aliases = {r["alias"]: r["genre"] for r in conn.execute("SELECT alias, genre FROM genre_aliases")}
    countries = {r["code"]: {"name": r["name"], "spirits": json.loads(r["spirits_json"])}
                 for r in conn.execute("SELECT * FROM countries")}
    descriptors = {
        r["tag"]: Descriptor(name=r["name"], deltas=tuple(json.loads(r["deltas_json"])),
                             vibes=frozenset(json.loads(r["vibes_json"])),
                             spirits=json.loads(r["spirits_json"]), country=r["country"])
        for r in conn.execute("SELECT * FROM descriptors")
    }
    twists = [Twist(text=r["text"], filters=json.loads(r["filters_json"]))
              for r in conn.execute("SELECT * FROM twists ORDER BY id")]
    return Catalog(drinks=drinks, genres=genres, aliases=aliases, countries=countries,
                   descriptors=descriptors, twists=twists)


def get_catalog() -> Catalog:
    """The catalog is read-only at runtime, so it is loaded once per app and kept in memory."""
    cat = current_app.extensions.get("drinkify_catalog")
    if cat is None:
        cat = load_catalog(get_db())
        current_app.extensions["drinkify_catalog"] = cat
    return cat


# ---------------------------------------------------------------- CLI

@click.command("init-db")
@click.option("--reset-cache", is_flag=True, help="Also clear cached MusicBrainz data and recent pairings.")
def init_db_command(reset_cache: bool):
    """Rebuild the drink/genre catalog from drinkify/data/*.json."""
    counts = init_db(get_db(), reset_cache=reset_cache)
    current_app.extensions.pop("drinkify_catalog", None)
    click.echo("seeded " + ", ".join(f"{v} {k}" for k, v in counts.items()))


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
