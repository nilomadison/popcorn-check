"""YouTube TV catalog sync via the JustWatch GraphQL API.

Fetches the YouTube TV (package `ytt`) movie catalog, stores it in `yttv.db`
(table `catalog`), and enriches titles with Rotten Tomatoes scores/synopses
via `rt.py` (table `ratings`).

DB schema (yttv.db):
  catalog(jw_id, title, year, popularity, first_seen, last_seen)
  ratings(jw_id, title, year, tomatometer, popcornmeter, tomatometer_certified,
          tomatometer_sentiment, audience_score_type, critic_avg, audience_avg,
          genres, poster, rt_url, updated_at, synopsis, synopsis_checked_at)
  meta(key, value)
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

import rt

YTTV_DB = "yttv.db"
GRAPHQL_URL = "https://apis.justwatch.com/graphql"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# YouTube TV package identifier used by JustWatch.
PACKAGE_YTT = "ytt"
PACKAGE_ID_YOUTUBETV = 2528
COUNTRY = "US"
LANGUAGE = "en"

PAGE_SIZE = 100
MAX_PAGES = 19  # offsets 0..1800 in steps of 100 (about 1900 popular titles)


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(YTTV_DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog (
            jw_id TEXT PRIMARY KEY,
            title TEXT,
            year INTEGER,
            popularity INTEGER,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS ratings (
            jw_id TEXT PRIMARY KEY,
            title TEXT,
            year INTEGER,
            tomatometer INTEGER,
            popcornmeter INTEGER,
            tomatometer_certified INTEGER,
            tomatometer_sentiment TEXT,
            audience_score_type TEXT,
            critic_avg TEXT,
            audience_avg TEXT,
            genres TEXT,
            poster TEXT,
            rt_url TEXT,
            updated_at TEXT,
            synopsis TEXT,
            synopsis_checked_at TEXT
        )
        """
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _set_meta(key: str, value: str) -> None:
    con = _db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        con.commit()
    finally:
        con.close()


def _catalog_query(page: int) -> dict[str, Any]:
    """Build a JustWatch GraphQL query for one page of the YTTV movie catalog."""
    return {
        "query": (
            "query PopularTitles($first: Int!, $after: String) {"
            "  popularTitles("
            "    country: \"US\""
            "    first: $first"
            "    filter: { packages: [\"ytt\"], objectTypes: [\"MOVIE\"] }"
            "    sortBy: POPULAR"
            "    sortRandomSeed: 0"
            "    after: $after"
            "  ) {"
            "    totalCount"
            "    pageInfo { hasNextPage endCursor }"
            "    edges {"
            "      node {"
            "        id"
            "        objectType"
            "        title"
            "        originalReleaseYear"
            "      }"
            "    }"
            "  }"
            "}"
        ),
        "variables": {"first": PAGE_SIZE, "after": None},
    }


def _fetch_page(cursor: Optional[str]) -> dict[str, Any]:
    payload = {
        "query": (
            "query PopularTitles($first: Int!, $after: String) {"
            "  popularTitles("
            "    country: \"US\""
            "    first: $first"
            "    filter: { packages: [\"ytt\"], objectTypes: [\"MOVIE\"] }"
            "    sortBy: POPULAR"
            "    sortRandomSeed: 0"
            "    after: $after"
            "  ) {"
            "    totalCount"
            "    pageInfo { hasNextPage endCursor }"
            "    edges { node { id objectType title originalReleaseYear } }"
            "  }"
            "}"
        ),
        "variables": {"first": PAGE_SIZE, "after": cursor},
    }
    resp = requests.post(
        GRAPHQL_URL,
        json=payload,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"JustWatch GraphQL error: {data['errors']}")
    return data.get("data", {}).get("popularTitles", {})


def fetch_catalog(max_pages: int = MAX_PAGES) -> int:
    """Refresh the catalog from JustWatch, upserting rows. Returns title count."""
    con = _db()
    upserted = 0
    cursor: Optional[str] = None
    seen_total: Optional[int] = None
    try:
        now = _now()
        for _ in range(max_pages):
            page = _fetch_page(cursor)
            if seen_total is None:
                seen_total = page.get("totalCount")
                _set_meta("catalog_total", str(seen_total or 0))
            edges = page.get("edges") or []
            if not edges:
                break
            for edge in edges:
                node = edge.get("node") or {}
                jw_id = node.get("id")
                if not jw_id:
                    continue
                title = node.get("title")
                year = node.get("originalReleaseYear")
                popularity = node.get("popularity")
                if popularity is None:
                    # popularity isn't in the minimal query; derive from order.
                    popularity = upserted
                # first_seen: preserve existing value on re-sync.
                existing = con.execute(
                    "SELECT first_seen FROM catalog WHERE jw_id = ?", (jw_id,)
                ).fetchone()
                first_seen = existing[0] if existing else now
                con.execute(
                    "INSERT INTO catalog "
                    "(jw_id, title, year, popularity, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(jw_id) DO UPDATE SET "
                    "title=excluded.title, year=excluded.year, "
                    "popularity=excluded.popularity, last_seen=excluded.last_seen",
                    (jw_id, title, year, popularity, first_seen, now),
                )
                upserted += 1
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        con.commit()
    finally:
        con.close()

    _set_meta("catalog_synced_at", _now())
    return upserted


def pending_rating_ids(limit: Optional[int] = None) -> list[tuple[str, str, int]]:
    """Catalog rows needing RT enrichment (no rating, or missing synopsis).

    Prioritizes ratings with a missing synopsis first, then lower popularity
    rank (i.e. higher popularity value = later). Returns (jw_id, title, year).
    """
    con = _db()
    try:
        query = """
            SELECT c.jw_id, c.title, c.year
            FROM catalog c
            LEFT JOIN ratings r ON r.jw_id = c.jw_id
            WHERE r.jw_id IS NULL OR r.synopsis_checked_at IS NULL
            ORDER BY
                CASE WHEN r.jw_id IS NULL THEN 1 ELSE 0 END DESC,
                c.popularity ASC
        """
        if limit:
            query += f" LIMIT {int(limit)}"
        return [(r[0], r[1], r[2]) for r in con.execute(query)]
    finally:
        con.close()


def enrich(limit: int = 150) -> dict[str, int]:
    """Run up to `limit` RT enrichment attempts. Returns attempt stats."""
    stats = {"attempted": 0, "matched": 0, "synopsis_refreshed": 0}
    con = _db()
    try:
        pending = pending_rating_ids(limit=limit)
        now = _now()
        for idx, (jw_id, title, year) in enumerate(pending):
            stats["attempted"] += 1
            try:
                result = rt.lookup(title, year)
            except Exception:
                continue
            if not result:
                continue

            # Determine whether this is a new rating or a synopsis refresh.
            existing = con.execute(
                "SELECT synopsis_checked_at FROM ratings WHERE jw_id = ?", (jw_id,)
            ).fetchone()
            was_unrated = existing is None

            synopsis = result.get("synopsis")
            synopsis_checked = now if synopsis else None
            con.execute(
                """
                INSERT INTO ratings (
                    jw_id, title, year, tomatometer, popcornmeter,
                    tomatometer_certified, tomatometer_sentiment,
                    audience_score_type, critic_avg, audience_avg,
                    genres, poster, rt_url, updated_at, synopsis,
                    synopsis_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(jw_id) DO UPDATE SET
                    tomatometer=excluded.tomatometer,
                    popcornmeter=excluded.popcornmeter,
                    tomatometer_certified=excluded.tomatometer_certified,
                    tomatometer_sentiment=excluded.tomatometer_sentiment,
                    audience_score_type=excluded.audience_score_type,
                    critic_avg=excluded.critic_avg,
                    audience_avg=excluded.audience_avg,
                    genres=excluded.genres,
                    poster=excluded.poster,
                    rt_url=excluded.rt_url,
                    updated_at=excluded.updated_at,
                    synopsis=COALESCE(excluded.synopsis, ratings.synopsis),
                    synopsis_checked_at=COALESCE(
                        excluded.synopsis_checked_at, ratings.synopsis_checked_at
                    )
                """,
                (
                    jw_id,
                    title,
                    result.get("year") or year,
                    result.get("tomatometer"),
                    result.get("popcornmeter"),
                    1 if result.get("tomatometer_certified") else 0,
                    result.get("tomatometer_sentiment"),
                    result.get("audience_score_type"),
                    result.get("critic_average_rating"),
                    result.get("audience_average_rating"),
                    json.dumps(result.get("genres") or []),
                    result.get("poster"),
                    result.get("url"),
                    now,
                    synopsis,
                    synopsis_checked,
                ),
            )
            stats["matched"] += 1
            if not was_unrated and synopsis_checked:
                stats["synopsis_refreshed"] += 1

            if (idx + 1) % 50 == 0:
                con.commit()
        con.commit()
    finally:
        con.close()
    _set_meta("ratings_synced_at", _now())
    return stats


def catalog_count() -> int:
    con = _db()
    try:
        return con.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
    finally:
        con.close()


if __name__ == "__main__":
    print(f"catalog rows: {catalog_count()}")
