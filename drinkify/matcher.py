"""The pairing algorithm. Pure functions, no I/O, no AI.

Given an artist's MusicBrainz tags (with vote counts), country, start year and type:

1. Genre tags are mapped to genre profiles (10 flavor axes, preferred spirits, vibes) and mixed by
   vote share. Descriptive tags ("brutal", "atmospheric", "gore", "finnish", ...) then nudge the mix.
2. A small per-artist "fingerprint" derived from the MusicBrainz id shifts the target a little, so
   two bands with identical tags still probe slightly different corners of the menu.
3. Every drink is scored:

       score = W_FLAVOR * flavor + W_SPIRIT * spirit + W_VIBE * vibe + W_MODIFIER * modifier

   flavor   = similarity of the drink's flavor profile to the artist's target profile
   spirit   = how much the genres (or the artist's country) favor the drink's base spirit
   vibe     = weighted Jaccard overlap between the artist's vibes and the drink's tags
   modifier = country -> regional spirit, era -> classic/modern drinks, big ensembles -> punch

4. The good fits (within SHORTLIST_WINDOW of the best, at most SHORTLIST_MAX, no category hogging
   the list) form a shortlist, and one is drawn with a hash of the artist id, weighted toward the
   better ranks. The same artist always gets the same drink, and so does its twist.
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

SHORTLIST_WINDOW = 0.15    # drinks this close to the best score are all good fits...
SHORTLIST_MAX = 10         # ...up to this many of them
CATEGORY_SHARE = 0.6       # no category (shot, beer, ...) may fill more than this share of the shortlist
RANK_DECAY = 0.85          # draw weight of the n-th shortlisted drink is RANK_DECAY ** n
FINGERPRINT_FLAVOR = 0.15  # per-artist shift on each flavor axis (+/-)
FINGERPRINT_SPIRIT = 0.25  # per-artist shift on each preferred spirit (+/-)
MAX_TAGS = 10              # only the most-voted tags count
BACKOFF_PENALTY = 0.8      # weight multiplier for a tag matched by keyword back-off
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


@dataclass(frozen=True)
class Descriptor:
    name: str
    deltas: tuple[float, ...]
    vibes: frozenset[str]
    spirits: dict[str, float]
    country: str | None = None


@dataclass(frozen=True)
class Twist:
    text: str
    filters: dict


@dataclass
class Catalog:
    drinks: list[Drink]
    genres: dict[str, Genre]
    aliases: dict[str, str]
    countries: dict[str, dict]  # code -> {"name": str, "spirits": {spirit: weight}}
    descriptors: dict[str, Descriptor] = field(default_factory=dict)  # tag -> descriptor
    twists: list[Twist] = field(default_factory=list)
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
    share: float  # fraction of the genre mix this tag contributed


@dataclass
class Profile:
    vec: tuple[float, ...]
    spirits: dict[str, float]
    vibes: dict[str, float]
    matches: list[GenreMatch]
    descriptors: list[str] = field(default_factory=list)  # descriptor names that nudged the profile
    country: str | None = None  # country implied by a tag like "finnish", if MusicBrainz had none


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
    twist: str | None = None


# ---------------------------------------------------------------- hashing helpers

def _digest(*parts: str) -> bytes:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).digest()


def stable_index(key: str, n: int) -> int:
    return int.from_bytes(_digest(key), "big") % n if n else 0


def stable_unit(*parts: str) -> float:
    """A deterministic number in [0, 1) for these inputs."""
    return int.from_bytes(_digest(*parts)[:8], "big") / 2**64


def _signed(*parts: str) -> float:
    return stable_unit(*parts) * 2 - 1


def artist_key(artist: Artist) -> str:
    return artist.mbid or normalize_tag(artist.name)


# ---------------------------------------------------------------- tag resolution

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
    leading words: "swedish melodic death metal" -> "melodic death metal".
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


def resolve_descriptor(tag: str, catalog: Catalog) -> Descriptor | None:
    t = normalize_tag(tag)
    for v in _variants(t):
        if v in catalog.descriptors:
            return catalog.descriptors[v]
    return None


def leading_descriptors(tag: str, catalog: Catalog) -> list[Descriptor]:
    """Descriptors hidden in front of a backed-off genre: "brutal death metal" is a genre, but
    "raw atmospheric black metal" -> black metal + raw + atmospheric."""
    words = normalize_tag(tag).replace("-", " ").split()
    found = []
    for w in words[:-1]:
        d = catalog.descriptors.get(w)
        if d and d not in found:
            found.append(d)
    return found


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
    nudges: list[tuple[Descriptor, float]] = []  # (descriptor, raw tag weight)
    for tag, weight in _weighted_tags(artist.tags):
        hit = resolve_genre(tag, catalog)
        if hit:
            name, exact = hit
            resolved.append((tag, catalog.genres[name], weight * (1.0 if exact else BACKOFF_PENALTY)))
            if not exact:
                nudges.extend((d, weight * 0.5) for d in leading_descriptors(tag, catalog))
            continue
        d = resolve_descriptor(tag, catalog)
        if d:
            nudges.append((d, weight))
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

    # Descriptor nudges, scaled by how strongly the tag is voted relative to the genres.
    used: list[str] = []
    country = None
    best_country_weight = 0.0
    for d, w in nudges:
        share = min(1.0, w / total)
        for i, delta in enumerate(d.deltas):
            vec[i] += share * delta
        for v in d.vibes:
            vibes[v] = vibes.get(v, 0.0) + share
        for s, sw in d.spirits.items():
            spirits[s] = spirits.get(s, 0.0) + share * sw
        if d.country and w > best_country_weight:
            country, best_country_weight = d.country, w
        if d.name not in used and (any(d.deltas) or d.vibes or d.spirits):
            used.append(d.name)

    matches = sorted(
        (GenreMatch(tag=t, genre=g, share=s) for (t, g), s in by_tag.items()),
        key=lambda m: (-m.share, m.tag),
    )
    return Profile(vec=tuple(min(1.0, max(0.0, x)) for x in vec), spirits=_scale_to_one(spirits),
                   vibes=_scale_to_one(vibes), matches=matches, descriptors=used, country=country)


def apply_fingerprint(profile: Profile, key: str) -> Profile:
    """Shift the target a little, deterministically per artist."""
    vec = tuple(min(1.0, max(0.0, x + FINGERPRINT_FLAVOR * _signed(key, "axis", a)))
                for x, a in zip(profile.vec, AXES))
    spirits = {s: max(0.0, w + FINGERPRINT_SPIRIT * _signed(key, "spirit", s)) for s, w in profile.spirits.items()}
    return Profile(vec=vec, spirits=spirits, vibes=profile.vibes, matches=profile.matches,
                   descriptors=profile.descriptors, country=profile.country)


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


def effective_country(artist: Artist, profile: Profile | None = None) -> str | None:
    return artist.country or (profile.country if profile else None)


def country_spirits(artist: Artist, catalog: Catalog, profile: Profile | None = None
                    ) -> tuple[str | None, dict[str, float]]:
    code = effective_country(artist, profile)
    if code and code in catalog.countries:
        c = catalog.countries[code]
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


def rank(artist: Artist, catalog: Catalog, profile: Profile) -> list[tuple[float, Drink]]:
    _, local = country_spirits(artist, catalog, profile)
    return sorted(((score_drink(d, profile, artist, catalog, local), d) for d in catalog.drinks),
                  key=lambda sd: (-sd[0], sd[1].slug))


def shortlist(scored: list[tuple[float, Drink]]) -> list[tuple[float, Drink]]:
    """The good fits: close to the best score, capped in size, no single category hogging it."""
    best = scored[0][0]
    cap = max(2, int(SHORTLIST_MAX * CATEGORY_SHARE))
    per_cat: dict[str, int] = {}
    out = []
    for s, d in scored:
        if best - s > SHORTLIST_WINDOW or len(out) >= SHORTLIST_MAX:
            break
        if per_cat.get(d.category, 0) >= cap:
            continue
        per_cat[d.category] = per_cat.get(d.category, 0) + 1
        out.append((s, d))
    return out


def weighted_draw(candidates: list[tuple[float, Drink]], key: str) -> int:
    """Deterministic draw from the shortlist, better-ranked drinks more likely (weight RANK_DECAY ** rank)."""
    weights = [RANK_DECAY ** i for i in range(len(candidates))]
    u = stable_unit(key, "draw") * sum(weights)
    for i, w in enumerate(weights):
        u -= w
        if u < 0:
            return i
    return len(candidates) - 1


# ---------------------------------------------------------------- twists

def twist_fits(t: Twist, drink: Drink) -> bool:
    f = t.filters
    if "methods" in f and drink.method not in f["methods"]:
        return False
    if drink.method in f.get("not_methods", ()):
        return False
    if "categories" in f and drink.category not in f["categories"]:
        return False
    if drink.category in f.get("not_categories", ()):
        return False
    if "spirits" in f and drink.base_spirit not in f["spirits"]:
        return False
    if drink.base_spirit in f.get("not_spirits", ()):
        return False
    if "needs" in f and not any(f["needs"] in name for _, name in drink.ingredients):
        return False
    return True


def pick_twist(artist: Artist, drink: Drink, catalog: Catalog) -> str | None:
    fits = [t for t in catalog.twists if twist_fits(t, drink)]
    if not fits:
        return None
    return fits[stable_index(artist_key(artist) + "|" + drink.slug, len(fits))].text


# ---------------------------------------------------------------- the main entry point

def pair(artist: Artist, catalog: Catalog) -> Pairing:
    if not catalog.drinks:
        raise ValueError("drink catalog is empty; run `flask --app drinkify init-db`")

    base = build_profile(artist, catalog)
    if base is None:
        return _fallback(artist, catalog)

    key = artist_key(artist)
    profile = apply_fingerprint(base, key)
    scored = rank(artist, catalog, profile)
    short = shortlist(scored)
    i = weighted_draw(short, key)
    score, drink = short[i]
    alternates = [d for _, d in short if d.slug != drink.slug][:2]
    if len(alternates) < 2:
        alternates += [d for _, d in scored if d.slug != drink.slug and d not in alternates][:2 - len(alternates)]

    country_name, local = country_spirits(artist, catalog, base)
    top_genre = catalog.genres[base.matches[0].genre]
    return Pairing(
        artist=artist,
        drink=drink,
        alternates=alternates,
        matches=base.matches,
        reasons=_reasons(artist, drink, base, country_name, local),
        quip=_quip(top_genre, catalog),
        fallback=False,
        score=round(score, 4),
        twist=pick_twist(artist, drink, catalog),
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
    if profile.descriptors:
        reasons.append("also: " + ", ".join(profile.descriptors[:4]))
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
    i = stable_index(artist_key(artist), len(pool))
    drink = pool[i]
    alternates = [pool[(i + k) % len(pool)] for k in (1, 2) if len(pool) > k]
    reasons = ["we couldn't find much on them, so here's the house pour"]
    if country_name and spirit_affinity(drink.base_spirit, local) >= 0.8:
        reasons.append(f"from {country_name}, so {spirit_label(drink.base_spirit)}")
    return Pairing(artist=artist, drink=drink, alternates=alternates, matches=[], reasons=reasons,
                   quip="drink it while you listen and decide for yourself.", fallback=True,
                   twist=pick_twist(artist, drink, catalog))
