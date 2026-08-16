"""YouTube TV catalog snapshot sync and Rotten Tomatoes enrichment."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import rt

YTTV_DB = "yttv.db"
GRAPHQL_URL = "https://apis.justwatch.com/graphql"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PACKAGE_YTT = "ytt"
PACKAGE_ID_YOUTUBETV = 2528
COUNTRY = "US"
LANGUAGE = "en"
PAGE_SIZE = 100
MAX_PAGES = 100  # Circuit breaker; completion comes from hasNextPage.
RT_RETRY_DAYS = 7

# JustWatch's US-English genre vocabulary plus the five RT-only categories we
# intentionally expose. Keys are stable API/filter values; labels are display
# text. Keep this ordered alphabetically by label.
CANONICAL_GENRES: tuple[tuple[str, str], ...] = (
    ("action", "Action & Adventure"),
    ("animation", "Animation"),
    ("anime", "Anime"),
    ("biography", "Biography"),
    ("comedy", "Comedy"),
    ("crime", "Crime"),
    ("documentation", "Documentary"),
    ("drama", "Drama"),
    ("faith_spirituality", "Faith & Spirituality"),
    ("fantasy", "Fantasy"),
    ("history", "History"),
    ("holiday", "Holiday"),
    ("horror", "Horror"),
    ("family", "Kids & Family"),
    ("lgbtq", "LGBTQ+"),
    ("european", "Made in Europe"),
    ("music", "Music & Musical"),
    ("thriller", "Mystery & Thriller"),
    ("reality", "Reality TV"),
    ("romance", "Romance"),
    ("scifi", "Science-Fiction"),
    ("sport", "Sport"),
    ("war", "War & Military"),
    ("western", "Western"),
)
GENRE_LABELS = dict(CANONICAL_GENRES)

RT_TO_CANONICAL_GENRE = {
    "Action": "action",
    "Adventure": "action",
    "Animation": "animation",
    "Anime": "anime",
    "Biography": "biography",
    "Comedy": "comedy",
    "Crime": "crime",
    "Documentary": "documentation",
    "Drama": "drama",
    "Faith & Spirituality": "faith_spirituality",
    "Fantasy": "fantasy",
    "Game Show": "reality",
    "History": "history",
    "Holiday": "holiday",
    "Horror": "horror",
    "Kids & Family": "family",
    "LGBTQ+": "lgbtq",
    "Music": "music",
    "Musical": "music",
    "Mystery & Thriller": "thriller",
    "Nature": "documentation",
    "Romance": "romance",
    "Sci-Fi": "scifi",
    "Sports": "sport",
    "Stand-Up": "comedy",
    "War": "war",
    "Western": "western",
}


def canonical_genres(
    jw_genres: Optional[list[str]], rt_genres: Optional[list[str]]
) -> tuple[list[str], list[str]]:
    """Merge raw source genres into ordered canonical keys and labels."""
    keys = {genre for genre in (jw_genres or []) if genre in GENRE_LABELS}
    keys.update(
        mapped
        for genre in (rt_genres or [])
        if (mapped := RT_TO_CANONICAL_GENRE.get(genre)) is not None
    )
    ordered_keys = [key for key, _ in CANONICAL_GENRES if key in keys]
    return ordered_keys, [GENRE_LABELS[key] for key in ordered_keys]

CATALOG_QUERY = """
query PopularTitles($first: Int!, $after: String) {
  popularTitles(
    country: US
    first: $first
    after: $after
    filter: {
      packages: ["ytt"]
      objectTypes: [MOVIE]
      includeTitlesWithoutUrl: true
    }
    sortBy: POPULAR
    sortRandomSeed: 0
  ) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        objectType
        content(country: US, language: en) {
          title
          originalReleaseYear
          shortDescription
          ... on MovieOrShowContent {
            fullPath
            posterUrl(profile: S718, format: JPG)
            externalIds { imdbId tmdbId }
            scoring { tomatoMeter certifiedFresh imdbScore tmdbScore jwRating }
            genres { technicalName }
          }
        }
      }
    }
  }
}
"""


def _session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=4, connect=4, read=4, status=4, backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}), respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    )
    return session


_HTTP = _session()


def _ensure_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(YTTV_DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS catalog ("
        "jw_id TEXT PRIMARY KEY, title TEXT, year INTEGER, popularity INTEGER, "
        "first_seen TEXT, last_seen TEXT)"
    )
    _ensure_columns(con, "catalog", {
        "active": "INTEGER NOT NULL DEFAULT 1",
        "jw_tomatometer": "INTEGER",
        "jw_certified_fresh": "INTEGER",
        "jw_synopsis": "TEXT",
        "jw_genres": "TEXT",
        "jw_poster": "TEXT",
        "jw_url": "TEXT",
        "imdb_id": "TEXT",
        "tmdb_id": "TEXT",
        "imdb_score": "REAL",
        "tmdb_score": "REAL",
        "jw_rating": "REAL",
        "jw_updated_at": "TEXT",
        "rt_status": "TEXT",
        "rt_attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "rt_last_attempt_at": "TEXT",
    })
    con.execute(
        """CREATE TABLE IF NOT EXISTS ratings (
            jw_id TEXT PRIMARY KEY, title TEXT, year INTEGER,
            tomatometer INTEGER, popcornmeter INTEGER,
            tomatometer_certified INTEGER, tomatometer_sentiment TEXT,
            audience_score_type TEXT, critic_avg TEXT, audience_avg TEXT,
            genres TEXT, poster TEXT, rt_url TEXT, updated_at TEXT,
            synopsis TEXT, synopsis_checked_at TEXT
        )"""
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _set_meta_in(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _set_meta(key: str, value: str) -> None:
    con = _db()
    try:
        _set_meta_in(con, key, value)
        con.commit()
    finally:
        con.close()


def _fetch_page(cursor: Optional[str]) -> dict[str, Any]:
    resp = _HTTP.post(
        GRAPHQL_URL,
        json={"query": CATALOG_QUERY, "variables": {"first": PAGE_SIZE, "after": cursor}},
        timeout=(10, 30),
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"JustWatch GraphQL error: {data['errors']}")
    page = data.get("data", {}).get("popularTitles")
    if not isinstance(page, dict):
        raise RuntimeError("JustWatch response omitted data.popularTitles")
    return page


def fetch_catalog(max_pages: int = MAX_PAGES) -> int:
    """Replace the active catalog snapshot after a complete successful fetch."""
    con = _db()
    cursor: Optional[str] = None
    seen_ids: set[str] = set()
    expected_total: Optional[int] = None
    completed = False
    now = _now()
    try:
        for _ in range(max_pages):
            page = _fetch_page(cursor)
            if expected_total is None:
                expected_total = page.get("totalCount")
            for edge in page.get("edges") or []:
                node = edge.get("node") or {}
                jw_id = node.get("id")
                content = node.get("content") or {}
                if not jw_id or not content.get("title"):
                    continue
                if jw_id in seen_ids:
                    raise RuntimeError(f"JustWatch returned duplicate title {jw_id}")
                seen_ids.add(jw_id)
                scoring = content.get("scoring") or {}
                external = content.get("externalIds") or {}
                genres = [g.get("technicalName") for g in content.get("genres") or []
                          if g.get("technicalName")]
                full_path = content.get("fullPath")
                con.execute(
                    """INSERT INTO catalog (
                        jw_id, title, year, popularity, first_seen, last_seen, active,
                        jw_tomatometer, jw_certified_fresh, jw_synopsis, jw_genres,
                        jw_poster, jw_url, imdb_id, tmdb_id, imdb_score, tmdb_score,
                        jw_rating, jw_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(jw_id) DO UPDATE SET
                        title=excluded.title, year=excluded.year,
                        popularity=excluded.popularity, last_seen=excluded.last_seen,
                        active=1, jw_tomatometer=excluded.jw_tomatometer,
                        jw_certified_fresh=excluded.jw_certified_fresh,
                        jw_synopsis=excluded.jw_synopsis, jw_genres=excluded.jw_genres,
                        jw_poster=excluded.jw_poster, jw_url=excluded.jw_url,
                        imdb_id=excluded.imdb_id, tmdb_id=excluded.tmdb_id,
                        imdb_score=excluded.imdb_score, tmdb_score=excluded.tmdb_score,
                        jw_rating=excluded.jw_rating, jw_updated_at=excluded.jw_updated_at""",
                    (jw_id, content["title"], content.get("originalReleaseYear"),
                     len(seen_ids) - 1, now, now, scoring.get("tomatoMeter"),
                     (1 if scoring.get("certifiedFresh") is True else
                      0 if scoring.get("certifiedFresh") is False else None),
                     content.get("shortDescription"), json.dumps(genres),
                     (f"https://images.justwatch.com{content['posterUrl']}"
                      if content.get("posterUrl") and not content["posterUrl"].startswith("http")
                      else content.get("posterUrl")),
                     f"https://www.justwatch.com{full_path}" if full_path else None,
                     external.get("imdbId"), external.get("tmdbId"),
                     scoring.get("imdbScore"), scoring.get("tmdbScore"),
                     scoring.get("jwRating"), now),
                )
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                completed = True
                break
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError("JustWatch pagination cursor did not advance")
            cursor = next_cursor

        if not completed:
            raise RuntimeError(f"JustWatch catalog exceeded safety limit of {max_pages} pages")
        if expected_total is not None and len(seen_ids) != int(expected_total):
            raise RuntimeError(
                f"Incomplete JustWatch snapshot: expected {expected_total}, got {len(seen_ids)}"
            )
        con.execute("UPDATE catalog SET active = 0")
        con.executemany("UPDATE catalog SET active = 1 WHERE jw_id = ?",
                        ((jw_id,) for jw_id in seen_ids))
        _set_meta_in(con, "catalog_total", str(len(seen_ids)))
        _set_meta_in(con, "catalog_synced_at", now)
        con.commit()
        return len(seen_ids)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def pending_rating_ids(limit: Optional[int] = None) -> list[tuple[str, str, int]]:
    """Return active titles lacking an RT audience score and eligible for retry."""
    con = _db()
    try:
        retry_before = (datetime.now(timezone.utc) - timedelta(days=RT_RETRY_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        query = """SELECT c.jw_id, c.title, c.year
            FROM catalog c LEFT JOIN ratings r ON r.jw_id = c.jw_id
            WHERE c.active = 1 AND r.popcornmeter IS NULL
              AND (c.rt_last_attempt_at IS NULL OR c.rt_last_attempt_at <= ?)
            ORDER BY c.popularity ASC"""
        params: list[Any] = [retry_before]
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [(row[0], row[1], row[2]) for row in con.execute(query, params)]
    finally:
        con.close()


def enrich(limit: int = 150) -> dict[str, int]:
    """Attempt RT enrichment without allowing persistent misses to starve the queue."""
    stats = {"attempted": 0, "matched": 0, "unmatched": 0, "errors": 0}
    con = _db()
    try:
        for idx, (jw_id, title, year) in enumerate(pending_rating_ids(limit=limit)):
            stats["attempted"] += 1
            attempted_at = _now()
            try:
                result = rt.lookup(title, year)
            except Exception:
                status, result = "error", None
                stats["errors"] += 1
            else:
                status = "matched" if result else "unmatched"
                stats["matched" if result else "unmatched"] += 1
            con.execute(
                "UPDATE catalog SET rt_status=?, rt_attempt_count=rt_attempt_count+1, "
                "rt_last_attempt_at=? WHERE jw_id=?", (status, attempted_at, jw_id)
            )
            if result:
                synopsis = result.get("synopsis")
                con.execute(
                    """INSERT INTO ratings (
                        jw_id, title, year, tomatometer, popcornmeter,
                        tomatometer_certified, tomatometer_sentiment,
                        audience_score_type, critic_avg, audience_avg,
                        genres, poster, rt_url, updated_at, synopsis, synopsis_checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(jw_id) DO UPDATE SET
                        title=excluded.title, year=excluded.year,
                        tomatometer=excluded.tomatometer, popcornmeter=excluded.popcornmeter,
                        tomatometer_certified=excluded.tomatometer_certified,
                        tomatometer_sentiment=excluded.tomatometer_sentiment,
                        audience_score_type=excluded.audience_score_type,
                        critic_avg=excluded.critic_avg, audience_avg=excluded.audience_avg,
                        genres=excluded.genres, poster=excluded.poster,
                        rt_url=excluded.rt_url, updated_at=excluded.updated_at,
                        synopsis=COALESCE(excluded.synopsis, ratings.synopsis),
                        synopsis_checked_at=excluded.synopsis_checked_at""",
                    (jw_id, title, result.get("year") or year,
                     result.get("tomatometer"), result.get("popcornmeter"),
                     1 if result.get("tomatometer_certified") else 0,
                     result.get("tomatometer_sentiment"), result.get("audience_score_type"),
                     result.get("critic_average_rating"), result.get("audience_average_rating"),
                     json.dumps(result.get("genres") or []), result.get("poster"),
                     result.get("url"), attempted_at, synopsis, attempted_at),
                )
            if (idx + 1) % 50 == 0:
                con.commit()
        con.commit()
    finally:
        con.close()
    _set_meta("ratings_synced_at", _now())
    return stats


def catalog_count(active_only: bool = False) -> int:
    con = _db()
    try:
        where = " WHERE active = 1" if active_only else ""
        return con.execute(f"SELECT COUNT(*) FROM catalog{where}").fetchone()[0]
    finally:
        con.close()


if __name__ == "__main__":
    print(f"active catalog rows: {catalog_count(active_only=True)}")
