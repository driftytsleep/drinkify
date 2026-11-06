from drinkify import musicbrainz as mbmod

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
ABBA = "d87e52c5-bb8d-4da8-b941-9f4928627dc8"


def test_home(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "music to drink to." in html and 'name="artist"' in html
    assert "<script" not in html


def test_search_redirects_to_artist(client):
    r = client.get("/?artist=Metallica")
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/a/{METALLICA}")


def test_result_page(client):
    html = client.get(f"/a/{METALLICA}").get_data(as_text=True)
    assert "Metallica" in html and "drink:" in html and "sounds like:" in html and "also try:" in html


def test_different_artists_get_different_drinks(client, app):
    client.get(f"/a/{METALLICA}")
    client.get(f"/a/{ABBA}")
    with app.app_context():
        from drinkify.db import get_db
        rows = get_db().execute("SELECT mbid, drink_slug FROM pairings").fetchall()
    drinks = {r["mbid"]: r["drink_slug"] for r in rows}
    assert drinks[METALLICA] != drinks[ABBA]


def test_recent_list_shows_on_home_without_duplicates(client):
    client.get(f"/a/{METALLICA}")
    client.get(f"/a/{METALLICA}")
    html = client.get("/").get_data(as_text=True)
    assert "recently drinkified" in html
    assert html.count(f"/a/{METALLICA}") == 1


def test_ambiguous_search_shows_choices(client):
    r = client.get("/?artist=nirvana")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "which nirvana?" in html
    assert "US grunge band" in html and "60s band from the UK" in html


def test_unknown_artist(client):
    r = client.get("/?artist=zzqx nobody")
    assert r.status_code == 404
    assert "never heard of them" in r.get_data(as_text=True)


def test_artist_without_genres_gets_house_pour(client):
    html = client.get("/a/4b3c2d1e-0f9a-4b8c-9d7e-6f5a4b3c2d1e").get_data(as_text=True)
    assert "house pour" in html


def test_bad_mbid_is_404(client):
    assert client.get("/a/not-a-uuid").status_code == 404
    assert client.get("/a/00000000-0000-0000-0000-000000000000").status_code == 404


def test_musicbrainz_down(client, monkeypatch):
    def boom(*a, **k):
        raise mbmod.MusicBrainzError("down")
    monkeypatch.setattr(mbmod.MusicBrainz, "search", boom)
    r = client.get("/?artist=anyone")
    assert r.status_code == 503 and "unreachable" in r.get_data(as_text=True)


def test_drink_pages(client):
    assert "Negroni" in client.get("/drink/negroni").get_data(as_text=True)
    assert client.get("/drink/nope").status_code == 404
    menu = client.get("/drinks").get_data(as_text=True)
    assert "the menu" in menu and "/drink/old-fashioned" in menu


def test_about(client):
    html = client.get("/about").get_data(as_text=True)
    assert "no AI" in html


def test_html_is_escaped(client):
    html = client.get("/?artist=<script>alert(1)</script>").get_data(as_text=True)
    assert "<script>alert" not in html


def test_result_shows_twist(client):
    html = client.get(f"/a/{METALLICA}").get_data(as_text=True)
    assert "your twist:" in html


def test_death_metal_neighbors_differ(client):
    gore = client.get("/?artist=gorephilia", follow_redirects=True).get_data(as_text=True)
    faceless = client.get("/?artist=faceless burial", follow_redirects=True).get_data(as_text=True)

    def drink(html):
        return html.split('<h2 class="drink">', 1)[1].split("</h2>", 1)[0]

    assert drink(gore) != drink(faceless)
