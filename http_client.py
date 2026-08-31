"""Shared outbound HTTP client for the Rotten Tomatoes, JustWatch and TMDb calls.

Each upstream gets its own `ThrottledClient`. They differ only in request rate,
HTTP method and default headers; they all want the same retry policy, the same
minimum gap between requests, and the same accounting of how often a 429 had to
be retried.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = (10, 30)


class ThrottledClient:
    """A requests session with a retry policy, a rate limit and 429 accounting."""

    def __init__(
        self,
        *,
        min_interval: float,
        methods: Iterable[str] = ("GET",),
        headers: Optional[dict[str, str]] = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        self._min_interval = min_interval
        self._timeout = timeout
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._rate_limit_retries = 0
        self._session = requests.Session()
        retries = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(methods),
            respect_retry_after_header=True,
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retries))
        if headers:
            self._session.headers.update(headers)

    @property
    def rate_limit_retries(self) -> int:
        """Return the number of HTTP 429 responses this client retried."""
        return self._rate_limit_retries

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.time()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        self._throttle()
        response = self._session.request(method, url, **kwargs)
        # Count before raising: a run that ends in an error should still report
        # how much of its time went to rate-limit backoff.
        retries = getattr(getattr(response, "raw", None), "retries", None)
        self._rate_limit_retries += sum(
            1 for retry in getattr(retries, "history", None) or ()
            if getattr(retry, "status", None) == 429
        )
        response.raise_for_status()
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)
