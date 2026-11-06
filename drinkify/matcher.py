"""The pairing algorithm. Pure functions, no I/O, no AI.

Given an artist's MusicBrainz genre tags (with vote counts), country, start year and type,
build a target flavor profile from the genre table and score every drink in the catalog
against it. The same artist always gets the same drink.

Scoring per drink (all parts are 0..1):

    score = W_FLAVOR * flavor + W_SPIRIT * spirit + W_VIBE * vibe + W_MODIFIER * modifier

flavor   = similarity of the drink's 10-axis profile to the artist's target profile
spirit   = how much the artist's genres (and family, e.g. any whiskey) favor the base spirit
vibe     = weighted Jaccard overlap between genre vibe tags and the drink's tags
modifier = country -> regional spirit, era -> classic/modern drinks, big ensembles -> punch
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

AXES = ("strength", "sweet", "sour", "bitter", "smoky", "fizzy", "fruity", "herbal", "creamy", "rich")

W_FLAVOR = 0.45
W_SPIRIT = 0.25
W_VIBE = 0.15
W_MODIFIER = 0.15

TIE_WINDOW = 0.03      # drinks this close to the best score are treated as a tie...
MAX_TIED = 5           # ...and one of up to this many is picked by hashing the artist id
MAX_TAGS = 8           # only the most-voted tags count
BACKOFF_PENALTY = 0.8  # weight multiplier for a tag matched by keyword back-off
LOCAL_SPIRIT_WEIGHT = 0.85  # how strongly an artist's country's spirit competes with the genre's

SPIRIT_FAMILIES = {
    "whiskey": {"bourbon", "rye", "scotch", "irish whiskey", "japanese whisky", "moonshine"},
    "wine": {"wine", "sparkling wine"},
    "agave": {"tequila", "mezcal"},
    "anise": {"absinthe", "ouzo"},
}
FAMILY_CREDIT = 0.5

SPIRIT_LABELS = {"cachaca": "cachaça", "liqueur": "liqueurs", "amaro": "amaro", "beer": "beer"}


@dataclass(frozen=True)
class Drink:
    slug: str
    name: str
    category: str
    base_spirit: str
    glass: str
    method: str
    instructions: str
    era: int | None
    house: bool
    vec: tuple[float, ...]
    tags: frozenset[str]
    ingredients: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Genre:
    name: str
    parent: str | None
    vec: tuple[float, ...]
    spirits: dict[str, float]
    vibes: frozenset[str]
    quip: str | None


@dataclass
class Catalog:
    drinks: list[Drink]
    genres: dict[str, Genre]
    aliases: dict[str, str]
    countries: dict[str, dict]  # code -> {"name": str, "spirits": {spirit: weight}}
    _mean: tuple[float, ...] | None = field(default=None, repr=False)

    @property
    def mean_vec(self) -> tuple[float, ...]:
        if self._mean is None:
            n = max(len(self.drinks), 1)
            self._mean = tuple(sum(d.vec[i] for d in self.drinks) / n for i in range(len(AXES)))
        return self._mean


@dataclass(frozen=True)
class Artist:
    mbid: str
    name: str
    tags: tuple[tuple[str, int], ...] = ()  # (tag name, vote count)
    country: str | None = None
    begin_year: int | None = None
    type: str | None = None
    disambiguation: str | None = None


@dataclass(frozen=True)
class GenreMatch:
    tag: str
    genre: str
    share: float  # fraction of the profile this tag contributed


@dataclass
class Profile:
    vec: tuple[float, ...]
    spirits: dict[str, float]
    vibes: dict[str, float]
    matches: list[GenreMatch]


@dataclass
class Pairing:
    artist: Artist
    drink: Drink
    alternates: list[Drink]
    matches: list[GenreMatch]
    reasons: list[str]
    quip: str | None
    fallback: bool
    score: float | None = None


# ---------------------------------------------------------------- genre resolution

_ws = re.compile(r"\s+")
_junk = re.compile(r"[^\w\s&'/\-!]", re.UNICODE)


def normalize_tag(tag: str) -> str:
    t = tag.strip().lower().replace("_", " ")
    t = _junk.sub("", t)
    return _ws.sub(" ", t).strip()


def _variants(t: str) -> list[str]:
    out = [t, t.replace("-", " "), t.replace(" ", "-"), t.replace(" and ", " & "), t.replace(" & ", " and ")]
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def _lookup(t: str, catalog: Catalog) -> str | None:
    for v in _variants(t):
        if v in catalog.genres:
            return v
        if v in catalog.aliases:
            return catalog.aliases[v]
    return None


def resolve_genre(tag: str, catalog: Catalog) -> tuple[str, bool] | None:
    """Map a free-form tag to a known genre.

    Returns (genre name, exact) or None. Tries exact/alias first, then backs off by dropping
    leading words: "swedish melodic death metal" -> "melodic death metal" -> "death metal".
    """
    t = normalize_tag(tag)
    if not t:
        return None
    hit = _lookup(t, catalog)
    if hit:
        return hit, True
    words = t.replace("-", " ").split()
    for i in range(1, len(words)):
        hit = _lookup(" ".join(words[i:]), catalog)
        if hit:
            return hit, False
    return None


# ---------------------------------------------------------------- profile

def _weighted_tags(tags) -> list[tuple[str, float]]:
    merged: dict[str, tuple[str, int]] = {}
    for name, count in tags:
        key = normalize_tag(name)
        if not key:
            continue
        if key not in merged or count > merged[key][1]:
            merged[key] = (name, count)
    items = sorted(merged.values(), key=lambda kv: (-kv[1], kv[0]))
    positive = [(n, float(c)) for n, c in items if c > 0]
    if positive:
        return positive[:MAX_TAGS]
    return [(n, 1.0) for n, _ in items[:MAX_TAGS]]


def build_profile(artist: Artist, catalog: Catalog) -> Profile | None:
    resolved: list[tuple[str, Genre, float]] = []
    for tag, weight in _weighted_tags(artist.tags):
        hit = resolve_genre(tag, catalog)
        if not hit:
            continue
        name, exact = hit
        resolved.append((tag, catalog.genres[name], weight * (1.0 if exact else BACKOFF_PENALTY)))
    if not resolved:
        return None

    total = sum(w for _, _, w in resolved)
    vec = [0.0] * len(AXES)
    spirits: dict[str, float] = {}
    vibes: dict[str, float] = {}
    by_tag: dict[tuple[str, str], float] = {}
    for tag, g, w in resolved:
        share = w / total
        for i, x in enumerate(g.vec):
            vec[i] += share * x
        for s, sw in g.spirits.items():
            spirits[s] = spirits.get(s, 0.0) + share * sw
        for v in g.vibes:
            vibes[v] = vibes.get(v, 0.0) + share
        key = (normalize_tag(tag), g.name)
        by_tag[key] = by_tag.get(key, 0.0) + share

    matches = sorted(
        (GenreMatch(tag=t, genre=g, share=s) for (t, g), s in by_tag.items()),
        key=lambda m: (-m.share, m.tag),
    )
    return Profile(vec=tuple(vec), spirits=_scale_to_one(spirits), vibes=_scale_to_one(vibes), matches=matches)


def _scale_to_one(d: dict[str, float]) -> dict[str, float]:
    top = max(d.values(), default=0.0)
    return {k: v / top for k, v in d.items()} if top > 0 else {}


# ---------------------------------------------------------------- scoring

def flavor_similarity(a, b, mean) -> float:
    """Blend of centered cosine (same *direction* of flavor) and plain distance (same intensity)."""
    ca = [x - m for x, m in zip(a, mean)]
    cb = [x - m for x, m in zip(b, mean)]
    na = math.sqrt(sum(x * x for x in ca))
    nb = math.sqrt(sum(x * x for x in cb))
    cos = sum(x * y for x, y in zip(ca, cb)) / (na * nb) if na and nb else 0.0
    dist = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b))) / math.sqrt(len(a))
    return 0.7 * (cos + 1) / 2 + 0.3 * (1 - dist)


def _family(spirit: str) -> str | None:
    for fam, members in SPIRIT_FAMILIES.items():
        if spirit in members:
            return fam
    return None


def spirit_affinity(spirit: str, prefs: dict[str, float]) -> float:
    direct = prefs.get(spirit, 0.0)
    fam = _family(spirit)
    if fam:
        cousins = max((w for s, w in prefs.items() if s != spirit and s in SPIRIT_FAMILIES[fam]), default=0.0)
        direct = max(direct, FAMILY_CREDIT * cousins)
    return direct


def vibe_overlap(vibes: dict[str, float], tags: frozenset[str]) -> float:
    """Weighted Jaccard between the vibe weights and the drink's (binary) tag set."""
    if not vibes or not tags:
        return 0.0
    inter = sum(vibes.get(t, 0.0) for t in tags)
    union = len(tags) + sum(w for t, w in vibes.items() if t not in tags)
    return inter / union if union else 0.0


def country_spirits(artist: Artist, catalog: Catalog) -> tuple[str | None, dict[str, float]]:
    if artist.country and artist.country in catalog.countries:
        c = catalog.countries[artist.country]
        return c["name"], c["spirits"]
    return None, {}


def era_bonus(artist: Artist, drink: Drink) -> float:
    y = artist.begin_year
    if not y:
        return 0.0
    if y < 1970 and (drink.tags & {"classic", "retro"}):
        return 1.0
    if y >= 2010 and "modern" in drink.tags:
        return 1.0
    if 1970 <= y < 2010 and drink.era and abs(drink.era - y) <= 15:
        return 0.5
    return 0.0


def crowd_bonus(artist: Artist, drink: Drink) -> float:
    big = {"orchestra": 1.0, "choir": 1.0, "group": 0.3}.get((artist.type or "").lower(), 0.0)
    if big and (drink.category == "punch" or "crowd" in drink.tags):
        return big
    return 0.0


def score_drink(drink: Drink, profile: Profile, artist: Artist, catalog: Catalog,
                local: dict[str, float]) -> float:
    flavor = flavor_similarity(profile.vec, drink.vec, catalog.mean_vec)
    regional = spirit_affinity(drink.base_spirit, local)
    # The local spirit competes with the genre's favorite spirit, slightly discounted.
    spirit = max(spirit_affinity(drink.base_spirit, profile.spirits), LOCAL_SPIRIT_WEIGHT * regional)
    vibe = vibe_overlap(profile.vibes, drink.tags)
    modifier = min(1.0, 0.5 * regional + 0.3 * era_bonus(artist, drink) + 0.2 * crowd_bonus(artist, drink))
    return W_FLAVOR * flavor + W_SPIRIT * spirit + W_VIBE * vibe + W_MODIFIER * modifier


def stable_index(key: str, n: int) -> int:
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % n if n else 0


# ---------------------------------------------------------------- the main entry point

def pair(artist: Artist, catalog: Catalog) -> Pairing:
    if not catalog.drinks:
        raise ValueError("drink catalog is empty; run `flask --app drinkify init-db`")

    profile = build_profile(artist, catalog)
    if profile is None:
        return _fallback(artist, catalog)

    country_name, local = country_spirits(artist, catalog)
    scored = sorted(
        ((score_drink(d, profile, artist, catalog, local), d) for d in catalog.drinks),
        key=lambda sd: (-sd[0], sd[1].slug),
    )
    best = scored[0][0]
    tied = [d for s, d in scored[:MAX_TIED] if best - s <= TIE_WINDOW]
    drink = tied[stable_index(artist.mbid or artist.name, len(tied))]
    alternates = [d for _, d in scored if d.slug != drink.slug][:2]

    top_genre = catalog.genres[profile.matches[0].genre]
    return Pairing(
        artist=artist,
        drink=drink,
        alternates=alternates,
        matches=profile.matches,
        reasons=_reasons(artist, drink, profile, country_name, local),
        quip=_quip(top_genre, catalog),
        fallback=False,
        score=round(next(s for s, d in scored if d.slug == drink.slug), 4),
    )


def _quip(genre: Genre, catalog: Catalog) -> str | None:
    g: Genre | None = genre
    while g is not None:
        if g.quip:
            return g.quip
        g = catalog.genres.get(g.parent) if g.parent else None
    return None


def spirit_label(spirit: str) -> str:
    return SPIRIT_LABELS.get(spirit, spirit)


def _reasons(artist, drink, profile, country_name, local) -> list[str]:
    reasons = []
    parts = []
    for m in profile.matches[:3]:
        label = m.tag if normalize_tag(m.tag) == m.genre else f"{m.tag} ({m.genre})"
        parts.append(f"{label} {round(m.share * 100)}%")
    reasons.append("sounds like: " + ", ".join(parts))
    if profile.spirits.get(drink.base_spirit, 0) >= 0.5:
        reasons.append(f"that sound leans {spirit_label(drink.base_spirit)}")
    if country_name and spirit_affinity(drink.base_spirit, local) > 0:
        reasons.append(f"from {country_name}, so {spirit_label(drink.base_spirit)}")
    if era_bonus(artist, drink) >= 1.0:
        reasons.append(f"around since {artist.begin_year}")
    if crowd_bonus(artist, drink) >= 1.0:
        reasons.append("big ensemble, big batch")
    return reasons


def _fallback(artist: Artist, catalog: Catalog) -> Pairing:
    country_name, local = country_spirits(artist, catalog)
    pool = [d for d in catalog.drinks if d.house] or list(catalog.drinks)
    if local:
        regional = [d for d in catalog.drinks if spirit_affinity(d.base_spirit, local) >= 0.8
                    and d.category in ("neat", "beer", "wine", "cocktail")]
        if regional:
            pool = sorted(regional, key=lambda d: d.slug)[:6]
    pool = sorted(pool, key=lambda d: d.slug)
    i = stable_index(artist.mbid or artist.name, len(pool))
    drink = pool[i]
    alternates = [pool[(i + k) % len(pool)] for k in (1, 2) if len(pool) > k]
    reasons = ["we couldn't find much on them, so here's the house pour"]
    if country_name and spirit_affinity(drink.base_spirit, local) >= 0.8:
        reasons.append(f"from {country_name}, so {spirit_label(drink.base_spirit)}")
    return Pairing(artist=artist, drink=drink, alternates=alternates, matches=[], reasons=reasons,
                   quip="drink it while you listen and decide for yourself.", fallback=True)
