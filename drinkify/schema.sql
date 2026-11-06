-- Drinkify schema. Seed data lives in drinkify/data/*.json; `flask --app drinkify init-db`
-- drops and rebuilds the catalog tables. Cache and pairing tables are kept unless --reset.

DROP TABLE IF EXISTS drink_tags;
DROP TABLE IF EXISTS drink_ingredients;
DROP TABLE IF EXISTS ingredients;
DROP TABLE IF EXISTS drinks;
DROP TABLE IF EXISTS genre_aliases;
DROP TABLE IF EXISTS genres;
DROP TABLE IF EXISTS countries;

CREATE TABLE drinks (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL CHECK (category IN ('cocktail','shot','beer','wine','neat','punch')),
    base_spirit  TEXT NOT NULL,
    glass        TEXT NOT NULL,
    method       TEXT NOT NULL,
    instructions TEXT NOT NULL,
    era          INTEGER,
    house        INTEGER NOT NULL DEFAULT 0,  -- 1 = eligible as a fallback "house drink"
    strength REAL NOT NULL, sweet REAL NOT NULL, sour REAL NOT NULL, bitter REAL NOT NULL,
    smoky    REAL NOT NULL, fizzy REAL NOT NULL, fruity REAL NOT NULL, herbal REAL NOT NULL,
    creamy   REAL NOT NULL, rich  REAL NOT NULL
);

CREATE TABLE ingredients (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE drink_ingredients (
    drink_id      INTEGER NOT NULL REFERENCES drinks(id),
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    amount        TEXT NOT NULL,
    position      INTEGER NOT NULL,
    PRIMARY KEY (drink_id, position)
);

CREATE TABLE drink_tags (
    drink_id INTEGER NOT NULL REFERENCES drinks(id),
    tag      TEXT NOT NULL,
    PRIMARY KEY (drink_id, tag)
);

CREATE TABLE genres (
    name     TEXT PRIMARY KEY,
    parent   TEXT,
    strength REAL NOT NULL, sweet REAL NOT NULL, sour REAL NOT NULL, bitter REAL NOT NULL,
    smoky    REAL NOT NULL, fizzy REAL NOT NULL, fruity REAL NOT NULL, herbal REAL NOT NULL,
    creamy   REAL NOT NULL, rich  REAL NOT NULL,
    spirits_json TEXT NOT NULL,   -- {"gin": 1.0, "vodka": 0.5}
    vibes_json   TEXT NOT NULL,   -- ["dark", "angry"]
    quip         TEXT
);

CREATE TABLE genre_aliases (
    alias TEXT PRIMARY KEY,
    genre TEXT NOT NULL REFERENCES genres(name)
);

CREATE TABLE countries (
    code         TEXT PRIMARY KEY,  -- ISO 3166-1 alpha-2 (or MusicBrainz area code)
    name         TEXT NOT NULL,
    spirits_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artist_cache (
    mbid       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    disambiguation TEXT,
    country    TEXT,       -- ISO code, or a subdivision name like 'Scotland' when MusicBrainz gives one
    begin_year INTEGER,
    type       TEXT,
    tags_json  TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    query        TEXT PRIMARY KEY,
    results_json TEXT NOT NULL,
    fetched_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pairings (
    id          INTEGER PRIMARY KEY,
    mbid        TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    drink_slug  TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pairings_created ON pairings(created_at DESC);
