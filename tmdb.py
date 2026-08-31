"""Small TMDb client for validated movie metadata enrichment."""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from http_client import ThrottledClient

load_dotenv(Path(__file__).with_name(".env"), override=False)

API_URL = "https://api.themoviedb.org/3"
REQUESTS_PER_SECOND = 30
THROTTLE_SECONDS = 1 / REQUESTS_PER_SECOND

# No session headers: the bearer token is read from the environment per request
# so a token added after import still takes effect.
_CLIENT = ThrottledClient(min_interval=THROTTLE_SECONDS, methods=("GET",))


class ConfigurationError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("TMDB_ACCESS_TOKEN"))


def _get(path: str, **params: Any) -> dict[str, Any]:
    token = os.environ.get("TMDB_ACCESS_TOKEN")
    if not token:
        raise ConfigurationError("TMDB_ACCESS_TOKEN is not configured")
    response = _CLIENT.get(
        f"{API_URL}{path}",
        params={"language": "en-US", **params},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("TMDb returned a non-object response")
    return data


def rate_limit_retry_count() -> int:
    """Return the number of HTTP 429 responses retried in this process."""
    return _CLIENT.rate_limit_retries


def movie(movie_id: str | int) -> dict[str, Any]:
    return _get(f"/movie/{movie_id}")


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", html.unescape(str(value or "")).casefold())


def _release_year(record: dict[str, Any]) -> Optional[int]:
    match = re.match(r"(\d{4})", str(record.get("release_date") or ""))
    return int(match.group(1)) if match else None


def _matches(record: dict[str, Any], title: str, year: Optional[int]) -> bool:
    needle = _normalized(title)
    titles = {_normalized(record.get("title")), _normalized(record.get("original_title"))}
    if needle not in titles:
        return False
    return year is None or _release_year(record) == year


def lookup(
    title: str,
    year: Optional[int],
    imdb_id: Optional[str] = None,
    tmdb_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return a title/year-validated TMDb movie with external-ID validation."""
    if tmdb_id:
        try:
            result = movie(tmdb_id)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
        else:
            if _matches(result, title, year) and (
                not imdb_id or result.get("imdb_id") == imdb_id
            ):
                return result

    if imdb_id:
        found = _get(f"/find/{imdb_id}", external_source="imdb_id")
        candidates = [
            item for item in found.get("movie_results") or []
            if isinstance(item, dict) and _matches(item, title, year)
        ]
        if len(candidates) != 1 or not candidates[0].get("id"):
            return None
        result = movie(candidates[0]["id"])
        if result.get("imdb_id") != imdb_id:
            return None
    else:
        return None

    if not _matches(result, title, year):
        return None
    return result
