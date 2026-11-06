import json

import pytest

from drinkify import matcher as M
from drinkify.musicbrainz import parse_artist

from .conftest import FIXTURES


def artist(tags, mbid="00000000-0000-0000-0000-000000000001", **kw):
    return M.Artist(mbid=mbid, name=kw.pop("name", "Test"), tags=tuple(tags), **kw)


def fixture_artist(mbid):
    return parse_artist(json.loads((FIXTURES / "artist" / f"{mbid}.json").read_text()))


# ---------------------------------------------------------------- genre resolution

@pytest.mark.parametrize("tag,expected,exact", [
    ("Thrash Metal", "thrash metal", True),
    ("hip-hop", "hip hop", True),
    ("Hip Hop", "hip hop", True),
    ("drum and bass", "drum and bass", True),
    ("drum & bass", "drum and bass", True),
    ("dream pop", "shoegaze", True),
    ("swedish melodic death metal", "death metal", False),
    ("japanese noise rock", "experimental", False),
    ("true norwegian black metal", "black metal", False),
])
def test_resolve_genre(catalog, tag, expected, exact):
    assert M.resolve_genre(tag, catalog) == (expected, exact)


@pytest.mark.parametrize("tag", ["seen live", "american", "male vocalists", "90s", "", "   "])
def test_non_genre_tags_are_ignored(catalog, tag):
    assert M.resolve_genre(tag, catalog) is None


def test_every_parent_and_alias_points_at_a_real_genre(catalog):
    for g in catalog.genres.values():
        assert g.parent is None or g.parent in catalog.genres, g.name
    for alias, g in catalog.aliases.items():
        assert g in catalog.genres, alias


def test_catalog_shape(catalog):
    assert len(catalog.drinks) >= 150
    assert len(catalog.genres) >= 100
    assert {d.category for d in catalog.drinks} == {"cocktail", "shot", "beer", "wine", "neat", "punch"}
    for d in catalog.drinks:
        assert len(d.vec) == len(M.AXES) and all(0 <= x <= 1 for x in d.vec), d.slug
        assert d.ingredients and d.tags, d.slug


# ---------------------------------------------------------------- profile

def test_profile_is_vote_weighted(catalog):
    p = M.build_profile(artist([("black metal", 30), ("disco", 10)]), catalog)
    shares = {m.genre: m.share for m in p.matches}
    assert shares["black metal"] == pytest.approx(0.75)
    assert shares["disco"] == pytest.approx(0.25)
    strength = M.AXES.index("strength")
    bm, disco = catalog.genres["black metal"].vec, catalog.genres["disco"].vec
    assert p.vec[strength] == pytest.approx(0.75 * bm[strength] + 0.25 * disco[strength])


def test_negative_votes_are_dropped(catalog):
    p = M.build_profile(artist([("jazz", 5), ("metal", -3)]), catalog)
    assert [m.genre for m in p.matches] == ["jazz"]


def test_no_genre_tags_gives_no_profile(catalog):
    assert M.build_profile(artist([("seen live", 3)]), catalog) is None


# ---------------------------------------------------------------- pairing behaviour

def axis(drink, name):
    return drink.vec[M.AXES.index(name)]


def test_metal_gets_something_strong_and_dark(catalog):
    p = M.pair(artist([("death metal", 10), ("black metal", 5)]), catalog)
    assert axis(p.drink, "strength") >= 0.7
    assert p.drink.tags & {"dark", "angry", "gritty", "heavy"}
    assert axis(p.drink, "fruity") <= 0.3 and axis(p.drink, "sour") <= 0.3


def test_disco_gets_something_fizzy_and_sweet(catalog):
    p = M.pair(artist([("disco", 10)]), catalog)
    assert axis(p.drink, "fizzy") >= 0.7
    assert axis(p.drink, "sweet") >= 0.2
    assert p.drink.tags & {"party", "retro"}


def test_folk_gets_something_mellow_and_earthy(catalog):
    p = M.pair(artist([("folk", 10), ("singer-songwriter", 6)]), catalog)
    assert p.drink.tags & {"earthy", "mellow", "cozy", "sad"}
    assert axis(p.drink, "fizzy") <= 0.4


def test_reggae_gets_rum(catalog):
    p = M.pair(artist([("reggae", 10), ("dub", 4)], country="JM"), catalog)
    assert p.drink.base_spirit == "rum"
    assert any("Jamaica" in r for r in p.reasons)


def _agave_rank(a, catalog):
    profile = M.build_profile(a, catalog)
    _, local = M.country_spirits(a, catalog)
    ranked = sorted(catalog.drinks, key=lambda d: -M.score_drink(d, profile, a, catalog, local))
    return next(i for i, d in enumerate(ranked) if d.base_spirit in {"tequila", "mezcal"})


def test_country_nudges_the_spirit(catalog):
    tags = [("rock", 10)]
    assert _agave_rank(artist(tags, country="MX"), catalog) < _agave_rank(artist(tags), catalog)
    assert _agave_rank(artist([("folk", 10)], country="MX"), catalog) <= 2


def test_same_artist_always_gets_same_drink(catalog):
    a = artist([("indie rock", 7), ("shoegaze", 3)], mbid="5b11f4ce-a62d-471e-81fc-a69a8278c7da")
    first = M.pair(a, catalog).drink.slug
    assert all(M.pair(a, catalog).drink.slug == first for _ in range(5))


def test_tie_break_depends_on_artist_id(catalog):
    # Same tags, many different ids: the hash spreads them over the tied drinks.
    tags = [("pop", 10)]
    picks = {M.pair(artist(tags, mbid=f"00000000-0000-0000-0000-{i:012d}"), catalog).drink.slug for i in range(40)}
    assert 1 <= len(picks) <= M.MAX_TIED


def test_genres_produce_varied_drinks(catalog):
    picks = {M.pair(artist([(g, 10)], mbid=f"00000000-0000-0000-0000-{i:012d}"), catalog).drink.slug
             for i, g in enumerate(catalog.genres)}
    assert len(picks) >= 40, "the catalog should be well used across genres"


def test_alternates_are_distinct(catalog):
    p = M.pair(artist([("techno", 10)]), catalog)
    slugs = [p.drink.slug] + [a.slug for a in p.alternates]
    assert len(slugs) == 3 == len(set(slugs))


def test_reasons_explain_the_match(catalog):
    p = M.pair(artist([("dream pop", 10)], country="England"), catalog)
    assert p.reasons[0].startswith("sounds like: dream pop (shoegaze) 100%")
    assert p.quip


def test_fallback_for_artist_without_genres(catalog):
    a = artist([("seen live", 1)], mbid="4b3c2d1e-0f9a-4b8c-9d7e-6f5a4b3c2d1e")
    p = M.pair(a, catalog)
    assert p.fallback and p.drink.house
    assert M.pair(a, catalog).drink.slug == p.drink.slug


def test_fallback_uses_regional_drink_when_known(catalog):
    p = M.pair(artist([], country="MX"), catalog)
    assert p.fallback and p.drink.base_spirit in {"tequila", "mezcal"}


def test_fixture_artists_pair_sensibly(catalog):
    metallica = M.pair(fixture_artist("65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"), catalog).drink
    abba = M.pair(fixture_artist("d87e52c5-bb8d-4da8-b941-9f4928627dc8"), catalog).drink
    miles = M.pair(fixture_artist("561d854a-6a28-4aa7-8c99-323e6ce46c2a"), catalog).drink
    assert len({metallica.slug, abba.slug, miles.slug}) == 3
    assert axis(metallica, "sweet") < axis(abba, "sweet")
    assert miles.tags & {"classic", "elegant", "slick"}
