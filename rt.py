"""Rotten Tomatoes lookup, disambiguation, scorecard parsing and caching.

Cache layout (SQLite, cache.db, table `cache`):
  key TEXT, payload TEXT (JSON string), fetched_at REAL (unix epoch)

Keys:
  search:<query>      -> JSON list of candidate titles
  movie:v2:<slug>     -> full scorecard JSON (incl. synopsis)

A 7-day TTL applies to both; requests to Rotten Tomatoes are throttled so we
don't hammer the site while backfilling the YouTube TV catalog.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import sqlite3
import threading
import time
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CACHE_DB = "cache.db"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

BASE_URL = "https://www.rottentomatoes.com"
SEARCH_URL = "https://www.rottentomatoes.com/search"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Minimum interval between outbound RT requests (seconds).
_THROTTLE_SECONDS = 1.2
_last_request_at = 0.0
_throttle_lock = threading.Lock()


def _session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": USER_AGENT})
    return session


_HTTP = _session()


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _THROTTLE_SECONDS - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(CACHE_DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        "key TEXT PRIMARY KEY, payload TEXT, fetched_at REAL)"
    )
    return con


def _cache_get(key: str) -> Optional[Any]:
    con = _db()
    try:
        row = con.execute(
            "SELECT payload, fetched_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    payload, fetched_at = row
    if time.time() - fetched_at > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return None


def _cache_set(key: str, value: Any) -> None:
    con = _db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO cache (key, payload, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        con.commit()
    finally:
        con.close()


def _get(url: str, **kwargs: Any) -> requests.Response:
    _throttle()
    resp = _HTTP.get(url, timeout=(10, 20), **kwargs)
    resp.raise_for_status()
    return resp


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def search(query: str) -> list[dict[str, Any]]:
    """Search Rotten Tomatoes and return a disambiguated candidate list."""
    q = query.strip().lower()
    key = f"search:{q}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    resp = _get(SEARCH_URL, params={"search": query})
    soup = BeautifulSoup(resp.text, "html.parser")

    results: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for row in soup.find_all("search-page-media-row"):
        thumb = row.find("a", attrs={"data-qa": "thumbnail-link"}) or row.find("a", href=True)
        href = thumb.get("href") if thumb else ""
        slug = href.rstrip("/").split("/")[-1] if href else ""
        if not slug or slug in seen_slugs:
            continue

        title_link = row.find("a", attrs={"data-qa": "info-name"})
        title = title_link.get_text(strip=True) if title_link else None
        if not title:
            img = row.find("img")
            title = img.get("alt") if img else None
        if not title:
            continue

        year = _to_int(row.get("release-year"))
        score = _to_int(row.get("tomatometer-score"))
        certified = row.get("tomatometer-is-certified") == "true"
        sentiment = (row.get("tomatometer-sentiment") or "").upper()

        seen_slugs.add(slug)
        results.append(
            {
                "title": title,
                "year": year,
                "slug": slug,
                "tomatometer": score,
                "tomatometer_certified": certified,
                "tomatometer_sentiment": sentiment,
                "url": href or f"{BASE_URL}/m/{slug}",
            }
        )

    # Prefer movie slugs (/m/...) over TV (/tv/...) since this app is movie-focused.
    movies = [r for r in results if r["url"].count("/m/") > 0 or not r["url"].count("/tv/")]
    if movies:
        results = movies

    _cache_set(key, results)
    return results


def _script_json(text: str, script_id: str) -> Optional[dict[str, Any]]:
    m = re.search(
        rf'<script id="{re.escape(script_id)}"[^>]*>(.*?)</script>',
        text,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (TypeError, ValueError):
        return None


def _synopsis_from_html(soup: BeautifulSoup) -> Optional[str]:
    el = soup.find("rt-text", attrs={"data-qa": "synopsis-value"})
    if el is None:
        return None
    synopsis = el.get_text(" ", strip=True)
    synopsis = html_mod.unescape(synopsis).strip()
    return synopsis or None


def movie(slug: str) -> Optional[dict[str, Any]]:
    """Fetch a full movie scorecard (incl. synopsis) by slug."""
    slug = slug.strip().strip("/").split("/")[-1]
    key = f"movie:v2:{slug}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    resp = _get(f"{BASE_URL}/m/{slug}")
    text = resp.text
    soup = BeautifulSoup(text, "html.parser")

    # Core metadata lives in the media-hero JSON blob.
    hero = _script_json(text, "media-hero-json")
    content = (hero or {}).get("content", {}) or {}
    title = content.get("title")
    if not title:
        return None

    genres = content.get("metadataGenres") or []
    poster = content.get("posterSrc")

    # metadataProps is e.g. ["PG-13", "2024", "2h 46m"] — extract year + runtime.
    props = content.get("metadataProps") or []
    year: Optional[int] = None
    runtime: Optional[str] = None
    for p in props:
        s = str(p)
        if re.fullmatch(r"\d{4}", s):
            year = _to_int(s)
        elif re.search(r"\d+h\s*\d+m", s, re.IGNORECASE):
            runtime = s

    # Tomatometer + audience score rendered as score-icon + rt-text elements,
    # scoped to the main media-scorecard (the page also embeds many other
    # movies' score icons in "what to watch" / trailer sections).
    card = soup.find("media-scorecard") or soup
    critics_score_el = card.find("rt-text", attrs={"slot": "critics-score"})
    audience_score_el = card.find("rt-text", attrs={"slot": "audience-score"})
    critics_icon = card.find("score-icon-critics")
    audience_icon = card.find("score-icon-audience")

    def _pct(el: Optional[Any]) -> Optional[int]:
        if el is None:
            return None
        m = re.search(r"(\d+)", el.get_text(strip=True))
        return _to_int(m.group(1)) if m else None

    tomatometer = _pct(critics_score_el)
    popcornmeter = _pct(audience_score_el)

    certified = bool(critics_icon and critics_icon.get("certified") == "true")
    sentiment = (critics_icon.get("sentiment") if critics_icon else "") or ""
    audience_sentiment = (audience_icon.get("sentiment") if audience_icon else "") or ""

    # Critic/audience review counts + averages from JSON-LD structured data.
    critic_count: Optional[int] = None
    audience_count: Optional[int] = None
    critic_avg: Optional[str] = None
    audience_avg: Optional[str] = None
    for ld in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            blob = json.loads(ld.string or "{}")
        except (TypeError, ValueError):
            continue
        items = blob if isinstance(blob, list) else [blob]
        for it in items:
            if not isinstance(it, dict):
                continue
            ar = it.get("aggregateRating") or {}
            if not isinstance(ar, dict):
                continue
            name = ar.get("name", "").lower()
            val = _to_int(ar.get("ratingValue"))
            cnt = _to_int(ar.get("reviewCount") or ar.get("ratingCount"))
            if "tomatometer" in name or "critic" in name:
                if val is not None and tomatometer is None:
                    tomatometer = val
                critic_count = cnt
            elif "audience" in name or "popcorn" in name:
                if val is not None and popcornmeter is None:
                    popcornmeter = val
                audience_count = cnt

    # Where to watch text.
    where_to_watch: Optional[str] = None
    wtw = _script_json(text, "where-to-watch-json")
    if wtw:
        where_to_watch = wtw.get("whereToWatch") or wtw.get("text")

    result: dict[str, Any] = {
        "slug": slug,
        "title": title,
        "year": str(year) if year else "",
        "runtime": runtime,
        "genres": genres,
        "poster": poster,
        "tomatometer": tomatometer,
        "tomatometer_percent": f"{tomatometer}%" if tomatometer is not None else None,
        "tomatometer_sentiment": (sentiment or "").upper(),
        "tomatometer_certified": certified,
        "critic_average_rating": critic_avg,
        "critic_review_count": critic_count,
        "popcornmeter": popcornmeter,
        "popcornmeter_percent": f"{popcornmeter}%" if popcornmeter is not None else None,
        "audience_sentiment": (audience_sentiment or "").upper(),
        "audience_certified": False,
        "audience_score_type": None,
        "audience_average_rating": audience_avg,
        "audience_review_count": audience_count,
        "where_to_watch": where_to_watch,
        "url": f"{BASE_URL}/m/{slug}",
        "synopsis": _synopsis_from_html(soup),
    }
    _cache_set(key, result)
    return result


def lookup(title: str, year: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Search + disambiguate + return a single best-match scorecard."""
    candidates = search(title)
    if not candidates:
        return None

    def normalized(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", html_mod.unescape(value).casefold())

    needle = normalized(title)
    exact = [c for c in candidates if normalized(c.get("title") or "") == needle]
    if year is not None:
        exact_year = [c for c in exact if c.get("year") == year]
        if exact_year:
            exact = exact_year
        else:
            # An explicit conflicting year is unsafe. A yearless candidate is
            # acceptable only when it is the sole exact-title result.
            exact = [c for c in exact if c.get("year") is None]
    if len(exact) != 1:
        return None
    best = exact[0]

    slug = best.get("slug")
    if not slug:
        return None
    return movie(slug)


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "The Godfather"
    print(json.dumps(lookup(q), indent=2))
