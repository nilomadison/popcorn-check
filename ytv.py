"""Streaming-provider catalog snapshot sync and Rotten Tomatoes enrichment."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import rt
import tmdb as tmdb_module

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
JW_RESULT_WINDOW_LIMIT = 1900
MIN_RELEASE_YEAR = 0
MAX_RELEASE_YEAR = 9999
RT_RETRY_DAYS = 7
RT_REFRESH_DAYS = 30
RT_MATCHER_VERSION = "search-year-v2"
TMDB_RETRY_DAYS = 30
JW_THROTTLE_SECONDS = 0.75


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    jw_package: str
    snapshot_limit: Optional[int] = None


PROVIDERS: dict[str, Provider] = {
    "youtube_tv": Provider("youtube_tv", "YouTube TV", PACKAGE_YTT),
    "netflix": Provider("netflix", "Netflix", "nfx"),
    "amazon_prime": Provider(
        "amazon_prime", "Amazon Prime Video", "amp", JW_RESULT_WINDOW_LIMIT
    ),
}

_last_jw_request_at = 0.0
_jw_throttle_lock = threading.Lock()

# TMDb's official movie genre list (the fixed set returned by TMDb's
# /genre/movie/list endpoint). Keys are stable API/filter values; labels
# are TMDb's own display text. Keep this ordered alphabetically by label.
CANONICAL_GENRES: tuple[tuple[str, str], ...] = (
    ("action", "Action"),
    ("adventure", "Adventure"),
    ("animation", "Animation"),
    ("comedy", "Comedy"),
    ("crime", "Crime"),
    ("documentary", "Documentary"),
    ("drama", "Drama"),
    ("family", "Family"),
    ("fantasy", "Fantasy"),
    ("history", "History"),
    ("horror", "Horror"),
    ("music", "Music"),
    ("mystery", "Mystery"),
    ("romance", "Romance"),
    ("science_fiction", "Science Fiction"),
    ("thriller", "Thriller"),
    ("tv_movie", "TV Movie"),
    ("war", "War"),
    ("western", "Western"),
)
GENRE_LABELS = dict(CANONICAL_GENRES)

# Normalizes genre strings from any source (TMDb, RT, JustWatch) onto the
# TMDb vocabulary above. TMDb's own names are listed for completeness since
# every source is funneled through this single table. RT and JustWatch
# genres with no TMDb equivalent (RT: Anime, Biography, Faith &
# Spirituality, Game Show, Holiday, LGBTQ+, Sports; JustWatch: european,
# reality, sport) are intentionally left unmapped and dropped.
GENRE_ALIASES = {
    "Action": "action",
    "Adventure": "adventure",
    "Animation": "animation",
    "Comedy": "comedy",
    "Crime": "crime",
    "Documentary": "documentary",
    "Drama": "drama",
    "Family": "family",
    "Fantasy": "fantasy",
    "History": "history",
    "Horror": "horror",
    "Music": "music",
    "Mystery": "mystery",
    "Romance": "romance",
    "Science Fiction": "science_fiction",
    "Thriller": "thriller",
    "TV Movie": "tv_movie",
    "War": "war",
    "Western": "western",
    "Kids & Family": "family",
    "Musical": "music",
    "Mystery & Thriller": "thriller",
    "Nature": "documentary",
    "Sci-Fi": "science_fiction",
    "Stand-Up": "comedy",
    "action": "action",
    "animation": "animation",
    "comedy": "comedy",
    "crime": "crime",
    "documentation": "documentary",
    "drama": "drama",
    "family": "family",
    "fantasy": "fantasy",
    "history": "history",
    "horror": "horror",
    "music": "music",
    "romance": "romance",
    "scifi": "science_fiction",
    "thriller": "thriller",
    "war": "war",
    "western": "western",
}


def canonical_genres(
    jw_genres: Optional[list[str]],
    rt_genres: Optional[list[str]],
    tmdb_genres: Optional[list[str]] = None,
) -> tuple[list[str], list[str]]:
    """Choose TMDb, then RT, then JW genres and normalize the selected list."""
    keys, labels, _ = preferred_genres(jw_genres, rt_genres, tmdb_genres)
    return keys, labels


def preferred_genres(
    jw_genres: Optional[list[str]],
    rt_genres: Optional[list[str]],
    tmdb_genres: Optional[list[str]] = None,
) -> tuple[list[str], list[str], Optional[str]]:
    """Return normalized genres from only the first nonempty source."""
    if tmdb_genres:
        source, raw = "tmdb", tmdb_genres
    elif rt_genres:
        source, raw = "rt", rt_genres
    elif jw_genres:
        source, raw = "justwatch", jw_genres
    else:
        return [], [], None
    keys = {
        mapped for genre in raw
        if (mapped := GENRE_ALIASES.get(genre)) is not None
    }
    ordered_keys = [key for key, _ in CANONICAL_GENRES if key in keys]
    return ordered_keys, [GENRE_LABELS[key] for key in ordered_keys], source

CATALOG_QUERY = """
query PopularTitles(
  $first: Int!, $after: String, $package: String!,
  $minYear: Int, $maxYear: Int, $sortBy: PopularTitlesSorting!
) {
  popularTitles(
    country: US
    first: $first
    after: $after
    filter: {
      packages: [$package]
      objectTypes: [MOVIE]
      releaseYear: { min: $minYear, max: $maxYear }
      includeTitlesWithoutUrl: true
    }
    sortBy: $sortBy
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


def _throttle_jw() -> None:
    global _last_jw_request_at
    with _jw_throttle_lock:
        wait = JW_THROTTLE_SECONDS - (time.time() - _last_jw_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_jw_request_at = time.time()


def _ensure_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _db() -> sqlite3.Connection:
    """Open a writable connection and apply schema/data migrations."""
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
        "tmdb_validated_id": "TEXT",
        "tmdb_genres": "TEXT",
        "tmdb_status": "TEXT",
        "tmdb_attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "tmdb_last_attempt_at": "TEXT",
        "tmdb_updated_at": "TEXT",
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
    _ensure_columns(con, "ratings", {
        "audience_sentiment": "TEXT",
        "audience_certified": "INTEGER",
        "critic_review_count": "INTEGER",
        "audience_review_count": "INTEGER",
        "rt_search_title": "TEXT",
        "rt_search_year": "INTEGER",
        "rt_identity_source": "TEXT",
    })
    con.execute(
        """CREATE TABLE IF NOT EXISTS rating_quarantine (
            jw_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            reason TEXT NOT NULL,
            invalidated_at TEXT NOT NULL
        )"""
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS providers (
            provider_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            jw_package TEXT NOT NULL UNIQUE
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS catalog_providers (
            jw_id TEXT NOT NULL,
            provider_key TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            popularity INTEGER,
            first_seen TEXT,
            last_seen TEXT,
            PRIMARY KEY (jw_id, provider_key),
            FOREIGN KEY (jw_id) REFERENCES catalog(jw_id),
            FOREIGN KEY (provider_key) REFERENCES providers(provider_key)
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_providers_active "
        "ON catalog_providers(provider_key, active, popularity)"
    )
    con.executemany(
        "INSERT INTO providers (provider_key, name, jw_package) VALUES (?, ?, ?) "
        "ON CONFLICT(provider_key) DO UPDATE SET "
        "name=excluded.name, jw_package=excluded.jw_package",
        ((p.key, p.name, p.jw_package) for p in PROVIDERS.values()),
    )

    # One-time migration: all pre-provider catalog rows came from YouTube TV.
    migrated = con.execute(
        "SELECT 1 FROM meta WHERE key = 'provider_schema_migrated'"
    ).fetchone()
    if not migrated:
        con.execute(
            """INSERT OR IGNORE INTO catalog_providers (
                jw_id, provider_key, active, popularity, first_seen, last_seen
            )
            SELECT jw_id, 'youtube_tv', active, popularity, first_seen, last_seen
            FROM catalog"""
        )
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('provider_schema_migrated', ?)",
            (_now(),),
        )

    # Ratings created before attempt tracking was introduced are successful
    # historical attempts, not untouched queue entries. Backfill their state
    # once so new titles receive their first pass before refresh work begins.
    rt_state_migrated = con.execute(
        "SELECT 1 FROM meta WHERE key = 'rt_attempt_state_migrated'"
    ).fetchone()
    if not rt_state_migrated:
        con.execute(
            """UPDATE catalog SET
                   rt_status = COALESCE(rt_status, 'matched'),
                   rt_last_attempt_at = COALESCE(
                       rt_last_attempt_at,
                       (SELECT r.updated_at FROM ratings r
                        WHERE r.jw_id = catalog.jw_id)
                   )
               WHERE EXISTS (SELECT 1 FROM ratings r
                             WHERE r.jw_id = catalog.jw_id)"""
        )
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('rt_attempt_state_migrated', ?)",
            (_now(),),
        )

    # Matching-rule changes should reconsider recent misses immediately, but
    # after untouched titles. An old timestamp places them in the retry tier.
    matcher_version = con.execute(
        "SELECT value FROM meta WHERE key = 'rt_matcher_version'"
    ).fetchone()
    if not matcher_version or matcher_version[0] != RT_MATCHER_VERSION:
        con.execute(
            "UPDATE catalog SET rt_last_attempt_at='2000-01-01 00:00:00' "
            "WHERE rt_status='unmatched'"
        )
        _set_meta_in(con, "rt_matcher_version", RT_MATCHER_VERSION)
    con.commit()
    return con


def _read_db() -> sqlite3.Connection:
    """Open the existing catalog without acquiring a SQLite writer lock."""
    uri = f"{Path(YTTV_DB).resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
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


def _provider(provider: str | Provider) -> Provider:
    if isinstance(provider, Provider):
        return provider
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise ValueError(f"Unknown provider: {provider}") from exc


def _fetch_page(
    provider: str | Provider,
    cursor: Optional[str],
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    sort_by: str = "POPULAR",
) -> dict[str, Any]:
    config = _provider(provider)
    _throttle_jw()
    resp = _HTTP.post(
        GRAPHQL_URL,
        json={
            "query": CATALOG_QUERY,
            "variables": {
                "first": PAGE_SIZE,
                "after": cursor,
                "package": config.jw_package,
                "minYear": min_year,
                "maxYear": max_year,
                "sortBy": sort_by,
            },
        },
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


def fetch_catalog(
    provider: str | Provider = "youtube_tv", max_pages: int = MAX_PAGES
) -> int:
    """Replace one provider's active snapshot after a complete successful fetch."""
    config = _provider(provider)
    con = _db()
    seen_ids: set[str] = set()
    expected_total: Optional[int] = None
    now = _now()

    def save_edges(
        edges: list[dict[str, Any]], local_ids: set[str], strict_duplicates: bool,
        result_limit: Optional[int] = None,
    ) -> None:
        for edge in edges:
            if result_limit is not None and len(local_ids) >= result_limit:
                break
            node = edge.get("node") or {}
            jw_id = node.get("id")
            content = node.get("content") or {}
            if not jw_id or not content.get("title"):
                continue
            if jw_id in local_ids:
                if strict_duplicates:
                    raise RuntimeError(f"JustWatch returned duplicate title {jw_id}")
                continue
            local_ids.add(jw_id)
            if jw_id in seen_ids:
                continue
            seen_ids.add(jw_id)
            scoring = content.get("scoring") or {}
            external = content.get("externalIds") or {}
            genres = [
                g.get("technicalName") for g in content.get("genres") or []
                if g.get("technicalName")
            ]
            full_path = content.get("fullPath")
            con.execute(
                """INSERT INTO catalog (
                    jw_id, title, year, active,
                    jw_tomatometer, jw_certified_fresh, jw_synopsis, jw_genres,
                    jw_poster, jw_url, imdb_id, tmdb_id, imdb_score, tmdb_score,
                    jw_rating, jw_updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(jw_id) DO UPDATE SET
                    title=excluded.title, year=excluded.year,
                    jw_tomatometer=excluded.jw_tomatometer,
                    jw_certified_fresh=excluded.jw_certified_fresh,
                    jw_synopsis=excluded.jw_synopsis, jw_genres=excluded.jw_genres,
                    jw_poster=excluded.jw_poster, jw_url=excluded.jw_url,
                    imdb_id=excluded.imdb_id, tmdb_id=excluded.tmdb_id,
                    imdb_score=excluded.imdb_score, tmdb_score=excluded.tmdb_score,
                    jw_rating=excluded.jw_rating, jw_updated_at=excluded.jw_updated_at""",
                (jw_id, content["title"], content.get("originalReleaseYear"),
                 scoring.get("tomatoMeter"),
                 (1 if scoring.get("certifiedFresh") is True else
                  0 if scoring.get("certifiedFresh") is False else None),
                 content.get("shortDescription"), json.dumps(genres),
                 (f"https://images.justwatch.com{content['posterUrl']}"
                  if content.get("posterUrl") and
                  not content["posterUrl"].startswith("http")
                  else content.get("posterUrl")),
                 f"https://www.justwatch.com{full_path}" if full_path else None,
                 external.get("imdbId"), external.get("tmdbId"),
                 scoring.get("imdbScore"), scoring.get("tmdbScore"),
                 scoring.get("jwRating"), now),
            )
            con.execute(
                """INSERT INTO catalog_providers (
                    jw_id, provider_key, active, popularity, first_seen, last_seen
                ) VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(jw_id, provider_key) DO UPDATE SET
                    active=1, popularity=excluded.popularity,
                    last_seen=excluded.last_seen""",
                (jw_id, config.key, len(seen_ids) - 1, now, now),
            )

    def fetch_window(
        min_year: Optional[int], max_year: Optional[int], allow_truncated: bool,
        sort_by: str = "POPULAR", strict_duplicates: bool = True,
        result_limit: Optional[int] = None,
    ) -> tuple[int, int]:
        cursor: Optional[str] = None
        local_ids: set[str] = set()
        window_total: Optional[int] = None
        completed = False
        for _ in range(max_pages):
            page = _fetch_page(config, cursor, min_year, max_year, sort_by)
            if window_total is None:
                window_total = int(page.get("totalCount") or 0)
                if (not allow_truncated and window_total > JW_RESULT_WINDOW_LIMIT
                        and min_year is not None and max_year is not None
                        and min_year < max_year):
                    midpoint = (min_year + max_year) // 2
                    left_total, left_count = fetch_window(min_year, midpoint, False)
                    right_total, right_count = fetch_window(midpoint + 1, max_year, False)
                    if left_total + right_total != window_total:
                        raise RuntimeError(
                            "JustWatch release-year partitions changed during retrieval"
                        )
                    return window_total, left_count + right_count
            save_edges(
                page.get("edges") or [], local_ids, strict_duplicates, result_limit
            )
            if result_limit is not None and window_total is not None \
                    and len(local_ids) == min(window_total, result_limit):
                completed = True
                break
            if allow_truncated and expected_total is not None \
                    and len(seen_ids) == expected_total:
                completed = True
                break
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                completed = True
                break
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError("JustWatch pagination cursor did not advance")
            cursor = next_cursor

        if not completed:
            raise RuntimeError(
                f"JustWatch catalog exceeded safety limit of {max_pages} pages"
            )
        if not allow_truncated and len(local_ids) != window_total:
            raise RuntimeError(
                f"Incomplete JustWatch snapshot: expected {window_total}, "
                f"got {len(local_ids)}"
            )
        return window_total or 0, len(local_ids)

    try:
        if config.snapshot_limit is not None:
            expected_total, _ = fetch_window(
                None, None, True, result_limit=config.snapshot_limit
            )
            snapshot_total = min(expected_total, config.snapshot_limit)
            if len(seen_ids) != snapshot_total:
                raise RuntimeError(
                    f"Incomplete JustWatch snapshot: expected {snapshot_total}, "
                    f"got {len(seen_ids)}"
                )
            inaccessible_total = expected_total - len(seen_ids)
        else:
            expected_total, _ = fetch_window(None, None, True)
            retrievable_total = expected_total
            if len(seen_ids) != expected_total:
                retrievable_total, _ = fetch_window(
                    MIN_RELEASE_YEAR, MAX_RELEASE_YEAR, False
                )
            # Release-year partitions cannot select titles whose year is null. The
            # capped unfiltered window may expose some of them; record any remainder
            # explicitly instead of weakening validation for year-addressable rows.
            inaccessible_total = expected_total - len(seen_ids)
            if len(seen_ids) < retrievable_total or inaccessible_total < 0:
                raise RuntimeError(
                    f"Incomplete JustWatch snapshot: expected {expected_total}, "
                    f"got {len(seen_ids)}"
                )
        con.execute(
            "UPDATE catalog_providers SET active = 0 WHERE provider_key = ?",
            (config.key,),
        )
        con.executemany(
            "UPDATE catalog_providers SET active = 1 "
            "WHERE provider_key = ? AND jw_id = ?",
            ((config.key, jw_id) for jw_id in seen_ids),
        )
        _set_meta_in(con, f"catalog_total:{config.key}", str(len(seen_ids)))
        _set_meta_in(con, f"catalog_reported_total:{config.key}", str(expected_total))
        _set_meta_in(
            con, f"catalog_inaccessible_total:{config.key}", str(inaccessible_total)
        )
        if config.snapshot_limit is not None:
            _set_meta_in(
                con, f"catalog_snapshot_limit:{config.key}", str(config.snapshot_limit)
            )
        _set_meta_in(con, f"catalog_synced_at:{config.key}", now)
        con.commit()
        return len(seen_ids)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def pending_rating_ids(limit: Optional[int] = None) -> list[tuple[str, str, int]]:
    """Return eligible RT work ordered by its best active-provider popularity."""
    con = _db()
    try:
        retry_before = (datetime.now(timezone.utc) - timedelta(days=RT_RETRY_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        refresh_before = (
            datetime.now(timezone.utc) - timedelta(days=RT_REFRESH_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        query = """SELECT c.jw_id, c.title, c.year
            FROM catalog c LEFT JOIN ratings r ON r.jw_id = c.jw_id
            WHERE EXISTS (SELECT 1 FROM catalog_providers cp
                          WHERE cp.jw_id = c.jw_id AND cp.active = 1)
              AND (
                    c.rt_last_attempt_at IS NULL
                    OR (r.popcornmeter IS NULL AND c.rt_last_attempt_at <= ?)
                    OR (r.popcornmeter IS NOT NULL AND c.rt_last_attempt_at <= ?)
                  )
            ORDER BY COALESCE(
                       (SELECT MIN(cp.popularity)
                        FROM catalog_providers cp
                        WHERE cp.jw_id = c.jw_id AND cp.active = 1),
                       2147483647
                     ) ASC,
                     CASE
                       WHEN c.rt_last_attempt_at IS NULL THEN 0
                       WHEN r.popcornmeter IS NULL THEN 1
                       ELSE 2
                     END,
                     c.title COLLATE NOCASE ASC"""
        params: list[Any] = [retry_before, refresh_before]
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [(row[0], row[1], row[2]) for row in con.execute(query, params)]
    finally:
        con.close()


def _upsert_rating(
    con: sqlite3.Connection,
    jw_id: str,
    catalog_title: str,
    catalog_year: Optional[int],
    result: dict[str, Any],
    attempted_at: str,
) -> None:
    synopsis = result.get("synopsis")
    con.execute(
        """INSERT INTO ratings (
            jw_id, title, year, tomatometer, popcornmeter,
            tomatometer_certified, tomatometer_sentiment,
            audience_score_type, critic_avg, audience_avg,
            genres, poster, rt_url, updated_at, synopsis, synopsis_checked_at,
            audience_sentiment, audience_certified,
            critic_review_count, audience_review_count,
            rt_search_title, rt_search_year, rt_identity_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            synopsis_checked_at=excluded.synopsis_checked_at,
            audience_sentiment=excluded.audience_sentiment,
            audience_certified=excluded.audience_certified,
            critic_review_count=excluded.critic_review_count,
            audience_review_count=excluded.audience_review_count,
            rt_search_title=excluded.rt_search_title,
            rt_search_year=excluded.rt_search_year,
            rt_identity_source=excluded.rt_identity_source""",
        (
            jw_id, result.get("title") or catalog_title,
            result.get("year") or catalog_year,
            result.get("tomatometer"), result.get("popcornmeter"),
            1 if result.get("tomatometer_certified") else 0,
            result.get("tomatometer_sentiment"),
            result.get("audience_score_type"),
            result.get("critic_average_rating"),
            result.get("audience_average_rating"),
            json.dumps(result.get("genres") or []), result.get("poster"),
            result.get("url"), attempted_at, synopsis, attempted_at,
            result.get("audience_sentiment"),
            1 if result.get("audience_certified") else 0,
            result.get("critic_review_count"),
            result.get("audience_review_count"),
            result.get("rt_search_title"), result.get("rt_search_year"),
            result.get("rt_identity_source"),
        ),
    )


def _validated_rt_result(
    result: dict[str, Any],
    catalog_title: str,
    catalog_year: Optional[int],
    search_title: Any = None,
    search_year: Any = None,
    identity_source: Any = None,
) -> Optional[dict[str, Any]]:
    """Reapply the identity evidence used when an RT match was first stored."""
    if rt.scorecard_matches(result, catalog_title, catalog_year):
        validated = dict(result)
        validated["rt_search_title"] = search_title
        validated["rt_search_year"] = search_year
        validated["rt_identity_source"] = "scorecard"
        return validated
    if identity_source == "search" and rt.search_identity_matches(
        result, catalog_title, catalog_year, search_title, search_year
    ):
        validated = dict(result)
        validated["rt_search_title"] = search_title
        validated["rt_search_year"] = search_year
        validated["rt_identity_source"] = "search"
        return validated
    return None


def enrich(limit: int = 150) -> dict[str, int]:
    """Attempt RT enrichment without allowing persistent misses to starve the queue."""
    stats = {"attempted": 0, "matched": 0, "unmatched": 0, "errors": 0}
    work = pending_rating_ids(limit=limit)
    total = len(work)
    started_at = time.monotonic()
    initial_rate_limit_retries = rt.rate_limit_retry_count()
    print(f"RT enrichment queued: {total} titles", flush=True)
    con = _db()
    try:
        for idx, (jw_id, title, year) in enumerate(work):
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
                _upsert_rating(con, jw_id, title, year, result, attempted_at)
            if (idx + 1) % 50 == 0:
                con.commit()
                _print_rt_progress(
                    stats, total, started_at, initial_rate_limit_retries
                )
        con.commit()
        if total and total % 50:
            _print_rt_progress(stats, total, started_at, initial_rate_limit_retries)
    finally:
        con.close()
    _set_meta("ratings_synced_at", _now())
    return stats


def _print_rt_progress(
    stats: dict[str, int],
    total: int,
    started_at: float,
    initial_rate_limit_retries: int,
) -> None:
    """Print a durable progress checkpoint after committed RT work."""
    attempted = stats["attempted"]
    elapsed = max(time.monotonic() - started_at, 0.001)
    rate = attempted / elapsed * 60
    percent = attempted / total * 100 if total else 100.0
    retries = max(0, rt.rate_limit_retry_count() - initial_rate_limit_retries)
    eta_seconds = (total - attempted) / rate * 60 if rate else 0
    print(
        f"RT progress: {attempted}/{total} ({percent:.1f}%) | "
        f"matched={stats['matched']} unmatched={stats['unmatched']} "
        f"errors={stats['errors']} | 429 retries={retries} | "
        f"{rate:.1f} titles/min | elapsed={timedelta(seconds=int(elapsed))} "
        f"eta={timedelta(seconds=int(eta_seconds))}",
        flush=True,
    )


def revalidate_ratings(limit: Optional[int] = None) -> dict[str, int]:
    """Recheck stored RT URLs and quarantine scorecards with wrong identities."""
    stats = {
        "checked": 0, "validated": 0, "invalid": 0, "restored": 0, "errors": 0
    }
    con = _db()
    try:
        con.row_factory = sqlite3.Row
        query = """SELECT r.*, c.title AS catalog_title, c.year AS catalog_year
            FROM ratings r JOIN catalog c ON c.jw_id = r.jw_id
            WHERE EXISTS (SELECT 1 FROM catalog_providers cp
                          WHERE cp.jw_id = c.jw_id AND cp.active = 1)
            ORDER BY (SELECT MIN(cp.popularity) FROM catalog_providers cp
                      WHERE cp.jw_id = c.jw_id AND cp.active = 1) ASC"""
        params: list[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = con.execute(query, params).fetchall()

        for idx, row in enumerate(rows):
            stats["checked"] += 1
            checked_at = _now()
            path = urlparse(row["rt_url"] or "").path
            slug = path.rstrip("/").split("/")[-1]
            if not slug:
                stats["errors"] += 1
                continue
            try:
                result = rt.cached_movie(slug) or rt.movie(slug)
            except Exception:
                stats["errors"] += 1
                continue
            if not result:
                stats["errors"] += 1
                continue

            validated_result = _validated_rt_result(
                result, row["catalog_title"], row["catalog_year"],
                row["rt_search_title"], row["rt_search_year"],
                row["rt_identity_source"],
            )
            if validated_result:
                _upsert_rating(
                    con, row["jw_id"], row["catalog_title"],
                    row["catalog_year"], validated_result, checked_at
                )
                con.execute(
                    "UPDATE catalog SET rt_status='matched', rt_last_attempt_at=? "
                    "WHERE jw_id=?",
                    (checked_at, row["jw_id"]),
                )
                con.execute(
                    "DELETE FROM rating_quarantine WHERE jw_id=?", (row["jw_id"],)
                )
                stats["validated"] += 1
            else:
                payload = {key: row[key] for key in row.keys()}
                reason = (
                    f"catalog identity {row['catalog_title']!r} "
                    f"({row['catalog_year']}) does not match RT scorecard "
                    f"{result.get('title')!r} ({result.get('year')})"
                )
                con.execute(
                    """INSERT INTO rating_quarantine (
                           jw_id, payload, reason, invalidated_at
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(jw_id) DO UPDATE SET
                           payload=excluded.payload, reason=excluded.reason,
                           invalidated_at=excluded.invalidated_at""",
                    (row["jw_id"], json.dumps(payload), reason, checked_at),
                )
                con.execute("DELETE FROM ratings WHERE jw_id=?", (row["jw_id"],))
                con.execute(
                    """UPDATE catalog SET rt_status='invalid',
                       rt_last_attempt_at=NULL WHERE jw_id=?""",
                    (row["jw_id"],),
                )
                stats["invalid"] += 1

            if (idx + 1) % 50 == 0:
                con.commit()

        # Reconsider quarantined rows as validation rules improve. This makes
        # quarantine genuinely recoverable without requiring a DB restore.
        quarantined = con.execute(
            """SELECT q.jw_id, q.payload
               FROM rating_quarantine q JOIN catalog c ON c.jw_id = q.jw_id
               WHERE EXISTS (SELECT 1 FROM catalog_providers cp
                             WHERE cp.jw_id = c.jw_id AND cp.active = 1)"""
        ).fetchall()
        for row in quarantined:
            try:
                payload = json.loads(row["payload"])
                path = urlparse(payload.get("rt_url") or "").path
                slug = path.rstrip("/").split("/")[-1]
                result = rt.cached_movie(slug) if slug else None
            except (TypeError, ValueError):
                continue
            catalog_title = payload.get("catalog_title") or payload.get("title")
            catalog_year = payload.get("catalog_year") or payload.get("year")
            validated_result = (
                _validated_rt_result(
                    result, catalog_title, catalog_year,
                    payload.get("rt_search_title"),
                    payload.get("rt_search_year"),
                    payload.get("rt_identity_source"),
                )
                if result else None
            )
            if not validated_result:
                continue
            restored_at = _now()
            _upsert_rating(
                con, row["jw_id"], catalog_title, catalog_year,
                validated_result, restored_at,
            )
            con.execute(
                "UPDATE catalog SET rt_status='matched', rt_last_attempt_at=? "
                "WHERE jw_id=?",
                (restored_at, row["jw_id"]),
            )
            con.execute(
                "DELETE FROM rating_quarantine WHERE jw_id=?", (row["jw_id"],)
            )
            stats["restored"] += 1
        con.commit()
    finally:
        con.close()
    _set_meta("ratings_revalidated_at", _now())
    return stats


def pending_tmdb_ids(
    limit: Optional[int] = None,
) -> list[tuple[str, str, int, Optional[str], Optional[str]]]:
    """Return available titles that still need validated TMDb metadata."""
    con = _db()
    try:
        retry_before = (
            datetime.now(timezone.utc) - timedelta(days=TMDB_RETRY_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        query = """SELECT c.jw_id, c.title, c.year, c.imdb_id, c.tmdb_id
            FROM catalog c
            WHERE EXISTS (SELECT 1 FROM catalog_providers cp
                          WHERE cp.jw_id = c.jw_id AND cp.active = 1)
              AND c.tmdb_genres IS NULL
              AND (c.tmdb_last_attempt_at IS NULL OR c.tmdb_last_attempt_at <= ?)
            ORDER BY (SELECT MIN(cp.popularity) FROM catalog_providers cp
                      WHERE cp.jw_id = c.jw_id AND cp.active = 1) ASC"""
        params: list[Any] = [retry_before]
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [tuple(row) for row in con.execute(query, params)]
    finally:
        con.close()


def enrich_tmdb(limit: int = 250) -> dict[str, int]:
    """Validate TMDb identity and store its genre list for available movies."""
    stats = {"attempted": 0, "matched": 0, "unmatched": 0, "errors": 0}
    if not tmdb_module.is_configured():
        return stats

    con = _db()
    try:
        for idx, (jw_id, title, year, imdb_id, raw_tmdb_id) in enumerate(
            pending_tmdb_ids(limit=limit)
        ):
            stats["attempted"] += 1
            attempted_at = _now()
            try:
                result = tmdb_module.lookup(title, year, imdb_id, raw_tmdb_id)
            except Exception:
                status, result = "error", None
                stats["errors"] += 1
            else:
                status = "matched" if result else "unmatched"
                stats["matched" if result else "unmatched"] += 1

            con.execute(
                "UPDATE catalog SET tmdb_status=?, "
                "tmdb_attempt_count=tmdb_attempt_count+1, tmdb_last_attempt_at=? "
                "WHERE jw_id=?",
                (status, attempted_at, jw_id),
            )
            if result:
                genres = [
                    genre.get("name") for genre in result.get("genres") or []
                    if isinstance(genre, dict) and genre.get("name")
                ]
                con.execute(
                    """UPDATE catalog SET tmdb_validated_id=?, tmdb_genres=?,
                       tmdb_updated_at=? WHERE jw_id=?""",
                    (str(result["id"]), json.dumps(genres), attempted_at, jw_id),
                )
            if (idx + 1) % 100 == 0:
                con.commit()
        con.commit()
    finally:
        con.close()
    _set_meta("tmdb_synced_at", _now())
    return stats


def catalog_count(active_only: bool = False) -> int:
    con = _db()
    try:
        if active_only:
            return con.execute(
                "SELECT COUNT(DISTINCT jw_id) FROM catalog_providers WHERE active = 1"
            ).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
    finally:
        con.close()


if __name__ == "__main__":
    print(f"active catalog rows: {catalog_count(active_only=True)}")
