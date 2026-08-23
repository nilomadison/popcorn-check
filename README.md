# Popcorn Check

A small Python/FastAPI app for the home LAN that combines Rotten Tomatoes
scores (and synopses) with streaming-provider movie discovery. Built for
household phone use — large tap targets and expandable "Movie summary" rows.

## Components

| File              | Purpose                                                        |
|-------------------|----------------------------------------------------------------|
| `server.py`       | FastAPI app; serves `/` (RT lookup) and `/yt` (provider browser)|
| `rt.py`           | Rotten Tomatoes search + scorecard parsing + 7-day SQLite cache|
| `ytv.py`          | Provider-aware JustWatch snapshots + RT score enrichment        |
| `sync_ytv.py`     | Batch/nightly sync entry point                                 |
| `pc.py`           | CLI Rotten Tomatoes lookup                                     |
| `cache.db`        | RT lookup cache (search + movie scorecards)                    |
| `yttv.db`         | Streaming-provider catalog, ratings, meta                      |

## Endpoints

- `GET /` — Rotten Tomatoes lookup UI
- `GET /yt` — multi-provider movie browser UI (genre filter + score sorting)
- `GET /api/lookup?q=<title>` — RT search candidates
- `GET /api/movie?slug=<slug>` — full RT scorecard
- `GET /api/yttv` — active provider catalog (one shot; filtering/sorting is client-side)

## Sync

```bash
.venv/bin/python sync_ytv.py            # provider refresh + ~150 enrichments
.venv/bin/python sync_ytv.py --catalog  # provider catalogs only
.venv/bin/python sync_ytv.py --backfill 600   # 600 enrichment attempts
.venv/bin/python sync_ytv.py --revalidate-rt  # quarantine mismatched stored RT pages
.venv/bin/python sync_ytv.py --tmdb-backfill 600  # 600 TMDb attempts
.venv/bin/python sync_ytv.py --all      # catalog + 2000-title backfill
```

Copy `.env.example` to `.env` and add your TMDb API read-access token:

```bash
cp .env.example .env
```

```dotenv
TMDB_ACCESS_TOKEN=your_api_read_access_token
```

The project loads this file automatically. A `TMDB_ACCESS_TOKEN` already set
in the process environment takes precedence. `.env` is ignored by Git; do not
commit the token. Without it, catalog and Rotten Tomatoes synchronization
continue normally and the TMDb enrichment step is skipped.

Schedule `sync_ytv.py` to run nightly with either a systemd timer or cron.

**systemd timer** (pairs naturally with running the app itself as a systemd
service, below):

```bash
cp examples/popcorn-check-sync.service.example /tmp/popcorn-check-sync.service
cp examples/popcorn-check-sync.timer.example /tmp/popcorn-check-sync.timer
# Edit both for your user and installation path.
sudo cp /tmp/popcorn-check-sync.service /etc/systemd/system/popcorn-check-sync.service
sudo cp /tmp/popcorn-check-sync.timer /etc/systemd/system/popcorn-check-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now popcorn-check-sync.timer
```

`Persistent=true` means a run missed while the machine was off catches up at
the next boot instead of silently waiting for the next scheduled time. Check
on it with `systemctl list-timers popcorn-check-sync.timer` and
`journalctl -u popcorn-check-sync.service`.

**cron** (alternative): an example entry that runs `sync_ytv.py` nightly at
4:00 AM is provided in `examples/popcorn-check.cron.example`. Update its
installation path and log destination before adding it with `crontab -e`.

The catalog contains the general US inventories that JustWatch associates with
its YouTube TV, Netflix, and Amazon Prime Video packages; it is not personalized
for a subscriber's location, add-ons, recordings, or account entitlements. The
Amazon Prime Video snapshot is intentionally limited to its first 1,900 movies
by JustWatch popularity because JustWatch caps this unpartitioned result window.
Shared movies have one catalog/ratings row and one availability row per provider.
JustWatch supplies baseline metadata and critic scores. Rotten Tomatoes is
queried for audience scores and missing critic data.

Rotten Tomatoes matching requires an exact normalized title and permits a
one-year release-date difference in both search results and scorecards for
festival/theatrical conventions. If an exact-title/exact-year search result
links to a page whose title agrees but whose year is missing or inconsistent,
the search identity is retained as validation evidence. Eligible titles are
enriched in their best active-provider popularity order; misses retry after
seven days, and
successful ratings refresh after 30 days. `sync_ytv.py --revalidate-rt` audits
stored RT URLs against catalog identity. Mismatches are removed from display
but retained as JSON in the `rating_quarantine` table for recovery.

Genres use the first available nonempty source in this order: validated TMDb,
Rotten Tomatoes, then JustWatch. Genre lists are not merged across sources.

Genre filters use TMDb's official movie genre list (19 fixed genres). RT and
JustWatch genres are normalized into that vocabulary; source genres with no
TMDb equivalent (e.g. RT's Anime, Biography, Faith & Spirituality, Holiday,
LGBTQ+; JustWatch's european, reality, sport) are dropped. Movies without
genre data from any source appear under `No genre listed`.

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
