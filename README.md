# drinkify

music to drink to. Type in an artist and get a drink.

This is a modern homage to the original drinkify.com. It has **no AI**. Each pairing comes from a SQLite database of drinks and genre profiles, scored with plain arithmetic. Artist data comes from [MusicBrainz](https://musicbrainz.org/), which is free and needs no API key.

## Run it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
flask --app drinkify run            # the database is built automatically on first run
```

Then open http://127.0.0.1:5000.

To develop without network access, run `DRINKIFY_OFFLINE=1 flask --app drinkify run`. MusicBrainz responses then come from `tests/fixtures/musicbrainz/`. Those fixtures are hand-written approximations for tests and local development; try `metallica`, `abba`, `bob dylan`, `miles davis`, `slowdive`, `mayhem`, `daft punk`, `bad bunny`, `the dubliners`, `nirvana` (ambiguous) or `basement tape hum` (no genre data).

Run the tests with `pytest`.

### Settings (environment variables)

| variable | default |
|---|---|
| `DRINKIFY_DB` | `instance/drinkify.sqlite` |
| `DRINKIFY_OFFLINE` | off |
| `DRINKIFY_USER_AGENT` | `Drinkify/1.0 (+https://github.com/driftytsleep/drinkify)`; MusicBrainz asks for a contact URL or email |
| `DRINKIFY_MB_TIMEOUT` | `5` seconds |

## Deploy (Render)

`render.yaml` is a Render Blueprint. In Render, choose **New → Blueprint** and pick this repo. When asked, set `DRINKIFY_USER_AGENT` to something like `Drinkify/1.0 (you@example.com)`, since MusicBrainz asks for a contact. Then deploy.

The free plan wipes the disk on each deploy or restart. That only resets the "recently drinkified" list and the MusicBrainz cache; the drink catalog is rebuilt from the JSON on every start. To keep that data, use a paid plan and uncomment the `disk` and `DRINKIFY_DB` lines in `render.yaml`.

On any other host, run `flask --app drinkify init-db && gunicorn "drinkify:create_app()" --bind 0.0.0.0:$PORT --workers 1 --threads 4`. Keep a single worker so the 1-request-per-second MusicBrainz limit holds.

## How a pairing is made

All of this lives in `drinkify/matcher.py`.

1. Look up the artist on MusicBrainz and read their genre/tag votes, country, start year and type. The client allows 1 request per second, and results are cached in SQLite for 30 days.
2. Match each tag to a genre in `genres.json`: exactly, by alias, or by backing off to the last words ("swedish melodic death metal" → "death metal"). Non-genre tags like "seen live" are ignored.
3. The artist's target profile is the vote-weighted mix of those genres. That covers a 10-axis flavor vector (strength, sweet, sour, bitter, smoky, fizzy, fruity, herbal, creamy, rich), preferred spirits and vibe tags.
4. Score every drink:
   `0.45 × flavor similarity + 0.25 × spirit match + 0.15 × vibe overlap + 0.15 × local color`.
   Local color covers the country's spirit (Mexico → tequila, Norway → aquavit, …), the era (pre-1970 artists → classic drinks) and orchestras/choirs (→ punch).
5. The best score wins. Drinks within 0.03 of the best score count as a tie, and the tie is broken by a hash of the artist's MusicBrainz id, so the same artist always gets the same drink.
6. An artist with no usable genre data gets a "house pour", picked the same deterministic way.

The result page shows the reasoning ("sounds like: shoegaze 50%, dream pop 39% · from England, so gin").

## Adding drinks and genres

The JSON files in `drinkify/data/` are the source of truth:

- `drinks.json` has a name, category, base spirit, glass, method, steps, ingredients, vibe tags, and `f`: 10 flavor values from 0 to 10.
- `genres.json` has a flavor profile, spirits, vibes, a quip and aliases. A genre with a `parent` inherits any field it leaves out.
- `countries.json` maps a country code to regional spirits.

After editing them, run `flask --app drinkify init-db` to rebuild the catalog. Add `--reset-cache` to also clear cached artists and recent pairings.

## Pages

| path | page |
|---|---|
| `/` | search, and the recently drinkified list |
| `/?artist=…` | search; redirects to the artist page or shows a "which one?" list |
| `/a/<mbid>` | permalink to an artist's pairing |
| `/drink/<slug>` | one drink |
| `/drinks` | the full menu |
| `/about` | how it works |
