"""Rotten Tomatoes lookup, disambiguation, scorecard parsing and caching.

Cache layout (SQLite, cache.db, table `cache`):
  key TEXT, payload TEXT (JSON string), fetched_at REAL (unix epoch)

Keys:
  search:<query>      -> JSON list of candidate titles
  movie:v2:<slug>     -> full scorecard JSON (incl. synopsis)

A 7-day TTL applies to both; requests to Rotten Tomatoes are throttled so we
don't hammer the site while backfilling provider catalogs.
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
_THROTTLE_SECONDS = 0.1
_last_request_at = 0.0
_throttle_lock = threading.Lock()
_rate_limit_retries = 0


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


def _cache_get(key: str, allow_expired: bool = False) -> Optional[Any]:
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
    if not allow_expired and time.time() - fetched_at > CACHE_TTL_SECONDS:
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
    global _rate_limit_retries
    _throttle()
    resp = _HTTP.get(url, timeout=(10, 20), **kwargs)
    _rate_limit_retries += sum(
        1 for retry in getattr(resp.raw.retries, "history", ())
        if retry.status == 429
    )
    resp.raise_for_status()
    return resp


def rate_limit_retry_count() -> int:
    """Return the number of HTTP 429 responses retried in this process."""
    return _rate_limit_retries


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalized(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+", "", html_mod.unescape(str(value or "")).casefold()
    )


def scorecard_matches(
    scorecard: Optional[dict[str, Any]], title: str, year: Optional[int]
) -> bool:
    """Match exact titles, allowing RT/JW release conventions to differ by one year."""
    if not scorecard or _normalized(scorecard.get("title")) != _normalized(title):
        return False
    if year is None:
        return True
    scorecard_year = _to_int(scorecard.get("year"))
    return scorecard_year is not None and abs(scorecard_year - year) <= 1


def search_identity_matches(
    scorecard: Optional[dict[str, Any]],
    title: str,
    year: Optional[int],
    search_title: Any,
    search_year: Any,
) -> bool:
    """Trust an exact RT search identity when its page omits/misstates year."""
    return (
        scorecard is not None
        and year is not None
        and _normalized(scorecard.get("title")) == _normalized(title)
        and _normalized(search_title) == _normalized(title)
        and _to_int(search_year) == year
    )


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

    # The scorecard JSON is the most complete source for scores, averages,
    # counts, certification and audience type. Visible elements are retained as
    # fallbacks because RT occasionally rolls out partial markup changes.
    scorecard = _script_json(text, "media-scorecard-json") or {}
    critics_data = scorecard.get("criticsScore") or {}
    audience_data = scorecard.get("audienceScore") or {}
    if not isinstance(critics_data, dict):
        critics_data = {}
    if not isinstance(audience_data, dict):
        audience_data = {}

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

    tomatometer = _to_int(critics_data.get("score"))
    if tomatometer is None:
        tomatometer = _pct(critics_score_el)
    popcornmeter = _to_int(audience_data.get("score"))
    if popcornmeter is None:
        popcornmeter = _pct(audience_score_el)

    certified = critics_data.get("certified") is True or bool(
        critics_icon and critics_icon.get("certified") == "true"
    )
    sentiment = (
        critics_data.get("sentiment")
        or (critics_icon.get("sentiment") if critics_icon else "")
        or ""
    )
    audience_sentiment = (
        audience_data.get("sentiment")
        or (audience_icon.get("sentiment") if audience_icon else "")
        or ""
    )
    audience_certified = audience_data.get("certified") is True or bool(
        audience_icon and audience_icon.get("certified") == "true"
    )
    critic_count = _to_int(
        critics_data.get("reviewCount") or critics_data.get("ratingCount")
    )
    audience_count = _to_int(
        audience_data.get("reviewCount") or audience_data.get("ratingCount")
    )
    critic_avg = critics_data.get("averageRating")
    audience_avg = audience_data.get("averageRating")
    critic_avg = str(critic_avg) if critic_avg not in (None, "") else None
    audience_avg = str(audience_avg) if audience_avg not in (None, "") else None
    audience_score_type = audience_data.get("scoreType") or None

    # JSON-LD remains a fallback for counts and percentage scores.
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
                if critic_count is None:
                    critic_count = cnt
            elif "audience" in name or "popcorn" in name:
                if val is not None and popcornmeter is None:
                    popcornmeter = val
                if audience_count is None:
                    audience_count = cnt

    # Where to watch text.
    where_to_watch: Optional[str] = None
    wtw = _script_json(text, "where-to-watch-json")
    if wtw:
        where_to_watch = (
            wtw.get("affiliatesText") or wtw.get("whereToWatch") or wtw.get("text")
        )

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
        "audience_certified": audience_certified,
        "audience_score_type": audience_score_type,
        "audience_average_rating": audience_avg,
        "audience_review_count": audience_count,
        "where_to_watch": where_to_watch,
        "url": f"{BASE_URL}/m/{slug}",
        "synopsis": _synopsis_from_html(soup),
    }
    _cache_set(key, result)
    return result


def cached_movie(slug: str) -> Optional[dict[str, Any]]:
    """Return a cached scorecard even if expired, for local identity audits."""
    slug = slug.strip().strip("/").split("/")[-1]
    cached = _cache_get(f"movie:v2:{slug}", allow_expired=True)
    return cached if isinstance(cached, dict) else None


def lookup(title: str, year: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Search + disambiguate + return a single best-match scorecard."""
    needle = _normalized(title)
    def exact_matches(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        exact = [
            c for c in candidates if _normalized(c.get("title")) == needle
        ]
        if year is None:
            return exact
        # RT commonly labels festival and theatrical releases one year apart.
        # Keep candidate selection consistent with final page validation.
        exact_year = [
            c for c in exact
            if _to_int(c.get("year")) is not None
            and abs(_to_int(c.get("year")) - year) <= 1
        ]
        if exact_year:
            return exact_year
        # An explicit conflicting year is unsafe. A yearless candidate is
        # acceptable only when it is the sole exact-title result.
        return [c for c in exact if c.get("year") is None]

    exact = exact_matches(search(title))
    if len(exact) != 1 and year is not None:
        exact = exact_matches(search(f"{title} {year}"))
    if len(exact) != 1:
        return None
    best = exact[0]

    slug = best.get("slug")
    if not slug:
        return None
    result = movie(slug)
    if not result:
        return None

    # Search-result metadata and the linked scorecard can disagree. Never let
    # a misleading RT search row attach a different movie's enrichment data.
    scorecard_identity = scorecard_matches(result, title, year)
    search_identity = search_identity_matches(
        result, title, year, best.get("title"), best.get("year")
    )
    if not scorecard_identity and not search_identity:
        return None
    matched = dict(result)
    matched["rt_search_title"] = best.get("title")
    matched["rt_search_year"] = best.get("year")
    matched["rt_identity_source"] = (
        "scorecard" if scorecard_identity else "search"
    )
    return matched


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "The Godfather"
    print(json.dumps(lookup(q), indent=2))
