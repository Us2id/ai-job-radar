"""A small HTTP helper: retries, per-host throttling, JSON helpers, a browser-like User-Agent."""
from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class Http:
    def __init__(self, timeout: float = 25.0, min_interval: float = 0.0, user_agent: str | None = None):
        self.timeout = timeout
        self.min_interval = min_interval
        self.session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.8,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST", "HEAD"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=64)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": user_agent or DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"})
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}

    def set_host_interval(self, host: str, seconds: float) -> None:
        self._host_interval[host] = seconds

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        interval = self._host_interval.get(host, self.min_interval)
        if interval <= 0:
            return
        with self._lock:
            last = self._last_call.get(host, 0.0)
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    def get(self, url: str, **kw: Any) -> requests.Response:
        self._throttle(url)
        kw.setdefault("timeout", self.timeout)
        return self.session.get(url, **kw)

    def post(self, url: str, **kw: Any) -> requests.Response:
        self._throttle(url)
        kw.setdefault("timeout", self.timeout)
        return self.session.post(url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, payload: Any, **kw: Any) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(kw.pop("headers", {}) or {})
        r = self.post(url, json=payload, headers=headers, **kw)
        r.raise_for_status()
        return r.json()


_shared: Http | None = None


def shared() -> Http:
    global _shared
    if _shared is None:
        _shared = Http()
    return _shared
