import sqlite3
from pathlib import Path

import pytest

from drinkify import create_app
from drinkify import db as dbmod

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"


@pytest.fixture(scope="session")
def catalog():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod.init_db(conn)
    cat = dbmod.load_catalog(conn)
    conn.close()
    return cat


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    dbmod.init_db(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE": str(tmp_path / "test.sqlite"), "OFFLINE": True,
                       "FIXTURES": str(FIXTURES), "TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()
