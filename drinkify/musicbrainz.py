"""Tiny MusicBrainz client: artist search + artist lookup with tags/genres.

Follows https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting: a descriptive User-Agent and
at most one request per second. Results are cached in SQLite for CACHE_TTL seconds.

With offline=True, responses are read from JSON fixture files instead of the network (same raw
MusicBrainz format), so tests and local development work without network access.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import requests

from .matcher import Artist, normalize_tag

API = "https://musicbrainz.org/ws/2"
CACHE_TTL = 30 * 24 * 3600
MIN_INTERVAL = 1.0
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SUBDIVISIONS = {"Scotland", "England", "Wales", "Northern Ireland"}

_lock = threading.Lock()
_last_request = 0.0


class MusicBrainzError(Exception):
    """The music database could not be reached or answered with an error."""


class NotFound(MusicBrainzError):
    pass


def _year(life_span: dict | None) -> int | None:
    begin = (life_span or {}).get("begin") or ""
    return int(begin[:4]) if begin[:4].isdigit() else None


def _country(raw: dict) -> str | None:
    area = (raw.get("area") or {}).get("name")
    if area in SUBDIVISIONS:
        return area
    return raw.get("country") or None


def parse_candidate(raw: dict) -> dict:
    return {
        "mbid": raw["id"],
        "name": raw.get("name", ""),
        "disambiguation": raw.get("disambiguation") or "",
        "country": _country(raw),
        "type": raw.get("type"),
        "begin_year": _year(raw.get("life-span")),
        "score": int(raw.get("score", 0) or 0),
    }


def parse_artist(raw: dict) -> Artist:
    tags: dict[str, int] = {}
    for item in (raw.get("genres") or []) + (raw.get("tags") or []):
        name = item.get("name")
        if name:
            tags[name] = max(tags.get(name, -10**9), int(item.get("count", 0) or 0))
    return Artist(
        mbid=raw["id"],
        name=raw.get("name", ""),
        tags=tuple(sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))),
        country=_country(raw),
        begin_year=_year(raw.get("life-span")),
        type=raw.get("type"),
        disambiguation=raw.get("disambiguation") or None,
    )


_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def lucene_escape(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


def fixture_slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_tag(query)).strip("-")


class MusicBrainz:
    def __init__(self, conn: sqlite3.Connection, user_agent: str, offline: bool = False,
                 fixtures: str | Path | None = None, timeout: float = 5.0, session=None):
        self.conn = conn
        self.user_agent = user_agent
        self.offline = offline
        self.fixtures = Path(fixtures) if fixtures else None
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------ transport

    def _get(self, path: str, params: dict) -> dict:
        global _last_request
        with _lock:
            wait = MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        try:
            resp = self.session.get(f"{API}/{path}", params={**params, "fmt": "json"}, timeout=self.timeout,
                                    headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        except requests.RequestException as exc:
            raise MusicBrainzError(str(exc)) from exc
        if resp.status_code in (400, 404):
            raise NotFound(path)
        if resp.status_code != 200:
            raise MusicBrainzError(f"HTTP {resp.status_code} from MusicBrainz")
        try:
            return resp.json()
        except ValueError as exc:
            raise MusicBrainzError("bad JSON from MusicBrainz") from exc

    def _fixture(self, kind: str, key: str) -> dict:
        if not self.fixtures:
            raise MusicBrainzError("offline mode without a fixtures directory")
        path = self.fixtures / kind / f"{key}.json"
        if not path.exists():
            if kind == "search":
                return {"artists": []}
            raise NotFound(key)
        return json.loads(path.read_text())

    # ------------------------------------------------------------ public API

    def search(self, query: str, limit: int = 8) -> list[dict]:
        key = normalize_tag(query)
        if not key:
            return []
        row = self.conn.execute("SELECT results_json, fetched_at FROM search_cache WHERE query = ?",
                                (key,)).fetchone()
        if row and time.time() - row["fetched_at"] < CACHE_TTL:
            return json.loads(row["results_json"])

        if self.offline:
            raw = self._fixture("search", fixture_slug(query))
        else:
            raw = self._get("artist", {"query": lucene_escape(query.strip()), "limit": limit})
        results = [parse_candidate(a) for a in raw.get("artists", [])[:limit]]
        self.conn.execute("INSERT OR REPLACE INTO search_cache VALUES (?, ?, ?)",
                          (key, json.dumps(results), int(time.time())))
        self.conn.commit()
        return results

    def artist(self, mbid: str) -> Artist:
        if not UUID_RE.match(mbid):
            raise NotFound(mbid)
        row = self.conn.execute("SELECT * FROM artist_cache WHERE mbid = ?", (mbid,)).fetchone()
        if row and time.time() - row["fetched_at"] < CACHE_TTL:
            return Artist(mbid=row["mbid"], name=row["name"],
                          tags=tuple((t, c) for t, c in json.loads(row["tags_json"])),
                          country=row["country"], begin_year=row["begin_year"], type=row["type"],
                          disambiguation=row["disambiguation"])

        if self.offline:
            raw = self._fixture("artist", mbid)
        else:
            raw = self._get(f"artist/{mbid}", {"inc": "tags+genres"})
        artist = parse_artist(raw)
        self.conn.execute(
            "INSERT OR REPLACE INTO artist_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (artist.mbid, artist.name, artist.disambiguation, artist.country, artist.begin_year, artist.type,
             json.dumps([list(t) for t in artist.tags]), int(time.time())),
        )
        self.conn.commit()
        return artist


def pick(query: str, results: list[dict]) -> dict | list[dict] | None:
    """Decide whether a search is unambiguous.

    Returns one candidate dict to go straight to, a list of candidates to choose from, or None.
    """
    if not results:
        return None
    q = normalize_tag(query)
    exact = [r for r in results if normalize_tag(r["name"]) == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact
    top = results[0]
    if top["score"] >= 95 and (len(results) == 1 or results[1]["score"] <= top["score"] - 10):
        return top
    return results
