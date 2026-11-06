import json
from unittest import mock

import pytest
import requests

from drinkify import musicbrainz as mbmod
from drinkify.musicbrainz import MusicBrainz, MusicBrainzError, NotFound, parse_artist, pick

from .conftest import FIXTURES

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(mbmod, "MIN_INTERVAL", 0)


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def online(conn, responses):
    session = mock.Mock()
    session.get.side_effect = responses
    return MusicBrainz(conn, user_agent="Drinkify-test/1.0", session=session), session


def test_parse_artist_merges_genres_and_tags():
    raw = json.loads((FIXTURES / "artist" / f"{METALLICA}.json").read_text())
    a = parse_artist(raw)
    assert a.name == "Metallica" and a.country == "US" and a.begin_year == 1981 and a.type == "Group"
    assert a.tags[0] == ("thrash metal", 20)
    assert len({t for t, _ in a.tags}) == len(a.tags)


def test_subdivision_beats_country_code():
    a = parse_artist({"id": "x", "name": "Y", "country": "GB", "area": {"name": "Scotland"}})
    assert a.country == "Scotland"


def test_lookup_is_cached(conn):
    raw = json.loads((FIXTURES / "artist" / f"{METALLICA}.json").read_text())
    client, session = online(conn, [FakeResp(200, raw)])
    first = client.artist(METALLICA)
    second = client.artist(METALLICA)
    assert first == second
    assert session.get.call_count == 1
    _, kwargs = session.get.call_args
    assert kwargs["headers"]["User-Agent"] == "Drinkify-test/1.0"
    assert kwargs["params"]["inc"] == "tags+genres"


def test_search_is_cached_and_normalized(conn):
    payload = {"artists": [{"id": METALLICA, "name": "Metallica", "score": 100}]}
    client, session = online(conn, [FakeResp(200, payload)])
    assert client.search("Metallica")[0]["mbid"] == METALLICA
    assert client.search("  metallica ")[0]["mbid"] == METALLICA
    assert session.get.call_count == 1


def test_search_escapes_lucene(conn):
    client, session = online(conn, [FakeResp(200, {"artists": []})])
    client.search("AC/DC")
    _, kwargs = session.get.call_args
    assert kwargs["params"]["query"] == "AC\\/DC"


def test_network_errors_become_musicbrainz_error(conn):
    client, _ = online(conn, requests.ConnectionError("down"))
    with pytest.raises(MusicBrainzError):
        client.search("anyone")


def test_server_error(conn):
    client, _ = online(conn, [FakeResp(503, {})])
    with pytest.raises(MusicBrainzError):
        client.artist(METALLICA)


def test_not_found(conn):
    client, _ = online(conn, [FakeResp(404, {})])
    with pytest.raises(NotFound):
        client.artist(METALLICA)


def test_bad_mbid_never_hits_network(conn):
    client, session = online(conn, [])
    with pytest.raises(NotFound):
        client.artist("../../etc/passwd")
    session.get.assert_not_called()


def test_offline_mode_reads_fixtures(conn):
    client = MusicBrainz(conn, user_agent="x", offline=True, fixtures=FIXTURES)
    assert client.search("Metallica")[0]["mbid"] == METALLICA
    assert client.search("nobody at all") == []
    assert client.artist(METALLICA).name == "Metallica"


def c(name, score=100, mbid=None):
    return {"mbid": mbid or name, "name": name, "score": score}


def test_pick_rules():
    assert pick("x", []) is None
    assert pick("Metallica", [c("Metallica"), c("Metallica Tribute")])["name"] == "Metallica"
    both = pick("nirvana", [c("Nirvana", mbid="a"), c("Nirvana", mbid="b")])
    assert isinstance(both, list) and len(both) == 2
    assert pick("metalica", [c("Metallica", 98), c("Metalika", 70)])["name"] == "Metallica"
    assert isinstance(pick("the", [c("The The", 90), c("The Who", 88)]), list)
