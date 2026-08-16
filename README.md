# Popcorn Check

A small Python/FastAPI app for the home LAN that combines Rotten Tomatoes
scores (and synopses) with YouTube TV movie discovery. Built for household
phone use — large tap targets and expandable "Movie summary" rows.

## Components

| File              | Purpose                                                        |
|-------------------|----------------------------------------------------------------|
| `server.py`       | FastAPI app; serves `/` (RT lookup) and `/yt` (YT TV browser)  |
| `rt.py`           | Rotten Tomatoes search + scorecard parsing + 7-day SQLite cache|
| `ytv.py`          | JustWatch snapshot fetch + RT audience-score enrichment         |
| `sync_ytv.py`     | Batch/nightly sync entry point                                 |
| `pc.py`           | CLI Rotten Tomatoes lookup                                     |
| `cache.db`        | RT lookup cache (search + movie scorecards)                    |
| `yttv.db`         | YouTube TV catalog, ratings, meta                              |

## Endpoints

- `GET /` — Rotten Tomatoes lookup UI
- `GET /yt` — YouTube TV movie browser UI (genre filter + critics/popcorn sort)
- `GET /api/lookup?q=<title>` — RT search candidates
- `GET /api/movie?slug=<slug>` — full RT scorecard
- `GET /api/yttv` — full rated catalog (all titles, one shot; filtering/sorting is client-side)

## Sync

```bash
.venv/bin/python sync_ytv.py            # catalog refresh + ~150 enrichments
.venv/bin/python sync_ytv.py --catalog  # catalog only
.venv/bin/python sync_ytv.py --backfill 600   # 600 enrichment attempts
.venv/bin/python sync_ytv.py --all      # catalog + 2000-title backfill
```

An example cron entry that runs `sync_ytv.py` nightly at 4:00 AM is provided
in `examples/popcorn-check.cron.example`. Update its installation path and log
destination before adding it with `crontab -e`.

The catalog is the general US inventory that JustWatch associates with its
YouTube TV package; it is not personalized for a subscriber's location,
add-ons, recordings, or account entitlements. JustWatch supplies baseline
metadata and critic scores. Rotten Tomatoes is queried for audience scores and
missing critic data.

Genre filters use JustWatch's technical genre keys and English display names.
RT genres are normalized into that vocabulary, supplemented by explicit Anime,
Biography, Faith & Spirituality, Holiday, and LGBTQ+ categories. Movies without
genre data from either source appear under `No genre listed`.

## Run

```bash
uv sync
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8790
```

Or customize and install the example systemd service (LAN access on port 8790):

```bash
cp examples/popcorn-check.service.example /tmp/popcorn-check.service
# Edit /tmp/popcorn-check.service for your user and installation path.
sudo cp /tmp/popcorn-check.service /etc/systemd/system/popcorn-check.service
sudo systemctl daemon-reload
sudo systemctl enable --now popcorn-check
sudo systemctl status popcorn-check
```

After code changes: `sudo systemctl restart popcorn-check`.
