"""A small HTTP helper: retries, per-host throttling and concurrency limits, rate-limit circuit breaker, JSON helpers."""
from __future__ import annotations

import email.utils
import json
import re
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

# A 429 whose Retry-After is longer than this is not worth waiting for inside a run:
# the host is marked as rate-limited and every further call to it fails fast.
SHORT_RETRY_AFTER = 8.0
MAX_BLOCK_SECONDS = 6 * 3600


class RateLimited(requests.RequestException):
    """The host asked us to back off for longer than a run can afford."""

    def __init__(self, host: str, retry_after: float, url: str = ""):
        self.host, self.retry_after = host, retry_after
        super().__init__(f"{host} rate-limited us (retry after {int(retry_after)}s)")


def _retry_after_seconds(r: requests.Response) -> float | None:
    ra = r.headers.get("Retry-After")
    if ra is None:
        return None
    ra = ra.strip()
    if re.fullmatch(r"\d+", ra):
        return float(ra)
    try:
        t = email.utils.parsedate_to_datetime(ra)
        return max(0.0, t.timestamp() - time.time())
    except Exception:  # noqa: BLE001
        return None


def _make_retry() -> Retry:
    kw = dict(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],  # 429 is handled by Http itself (see RateLimited)
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        respect_retry_after_header=False,  # urllib3 would otherwise retry 429s itself and sleep on Retry-After
        raise_on_status=False,
    )
    try:
        return Retry(retry_after_max=15, **kw)
    except TypeError:  # older urllib3
        return Retry(**kw)


class Http:
    def __init__(self, timeout: float = 25.0, min_interval: float = 0.0, user_agent: str | None = None):
        self.timeout = timeout
        self.min_interval = min_interval
        self.session = requests.Session()
        adapter = HTTPAdapter(max_retries=_make_retry(), pool_connections=32, pool_maxsize=64)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # No Accept-Language by default: Wikimedia's edge rate-limiter treats a browser-style Accept-Language
        # from a non-browser User-Agent as a scraper signature and answers HTTP 429 to the second request.
        self.session.headers.update({"User-Agent": user_agent or DEFAULT_UA})
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._host_sem: dict[str, threading.Semaphore] = {}
        self._blocked_until: dict[str, float] = {}
        self.blocked_log: list[str] = []  # human-readable notes for the run log

    # ------------------------------------------------------------------ host policies
    def set_host_interval(self, host: str, seconds: float) -> None:
        self._host_interval[host] = seconds

    def set_host_concurrency(self, host: str, n: int) -> None:
        self._host_sem[host] = threading.Semaphore(max(1, n))

    def blocked_hosts(self) -> dict[str, float]:
        now = time.monotonic()
        return {h: round(t - now) for h, t in self._blocked_until.items() if t > now}

    def unblock(self, host: str) -> None:
        """A lane that chose to wait out the Retry-After lifts the block itself."""
        with self._lock:
            self._blocked_until.pop(host, None)

    def is_blocked(self, host: str) -> bool:
        return self._blocked_until.get(host, 0.0) > time.monotonic()

    def _block(self, host: str, seconds: float, url: str, detail: str = "") -> None:
        seconds = min(max(seconds, 30.0), MAX_BLOCK_SECONDS)
        with self._lock:
            self._blocked_until[host] = time.monotonic() + seconds
            note = f"{host}: rate-limited (HTTP 429, retry after {int(seconds)}s); remaining calls to it skipped" + (f" [{detail}]" if detail else "")
            if note not in self.blocked_log:
                self.blocked_log.append(note)

    def _throttle(self, host: str) -> None:
        interval = self._host_interval.get(host, self.min_interval)
        if interval <= 0:
            return
        with self._lock:
            last = self._last_call.get(host, 0.0)
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    # ------------------------------------------------------------------ requests
    def _request(self, method: str, url: str, **kw: Any) -> requests.Response:
        host = urlsplit(url).netloc
        blocked = self._blocked_until.get(host, 0.0) - time.monotonic()
        if blocked > 0:
            raise RateLimited(host, blocked, url)
        kw.setdefault("timeout", self.timeout)
        sem = self._host_sem.get(host)
        if sem:
            sem.acquire()
        try:
            for attempt in (1, 2):
                self._throttle(host)
                r = self.session.request(method, url, **kw)
                if r.status_code != 429:
                    return r
                ra = _retry_after_seconds(r)
                if ra is None:
                    ra = 2.0 if attempt == 1 else 120.0
                if attempt == 1 and ra <= SHORT_RETRY_AFTER:
                    time.sleep(ra)
                    continue
                detail = " ".join(f"{k}={v}" for k, v in r.headers.items() if k.lower() in ("server", "x-cache", "via", "x-ratelimit-limit", "x-ratelimit-remaining", "cf-ray"))
                body = re.sub(r"\s+", " ", r.text[:160]) if r.text else ""
                self._block(host, ra, url, (detail + " " + body).strip())
                raise RateLimited(host, ra, url)
            return r  # pragma: no cover - loop always returns or raises
        finally:
            if sem:
                sem.release()

    def get(self, url: str, **kw: Any) -> requests.Response:
        return self._request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> requests.Response:
        return self._request("POST", url, **kw)

    @staticmethod
    def decode_json(r: requests.Response) -> Any:
        """JSON is UTF-8 by specification; decoding the bytes directly avoids mojibake from wrong charset headers."""
        try:
            return json.loads(r.content)
        except (UnicodeDecodeError, ValueError):
            return r.json()

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        r.raise_for_status()
        return self.decode_json(r)

    def post_json(self, url: str, payload: Any, **kw: Any) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(kw.pop("headers", {}) or {})
        r = self.post(url, json=payload, headers=headers, **kw)
        r.raise_for_status()
        return self.decode_json(r)


_shared: Http | None = None


def shared() -> Http:
    global _shared
    if _shared is None:
        _shared = Http()
    return _shared
