"""Text helpers: HTML to text, normalisation, date parsing."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone

from dateutil import parser as dtparser

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    if "<" in s and ">" in s:
        s = html.unescape(s)  # Greenhouse double-escapes
        s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", s)
        s = re.sub(r"(?i)<li[^>]*>", "- ", s)
        s = _TAG_RE.sub(" ", s)
    s = s.replace("\xa0", " ")
    s = _WS_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


COMPANY_SUFFIXES = re.compile(
    r"\b(pvt|private|ltd|limited|inc|incorporated|llc|llp|plc|corp|corporation|co|company|gmbh|ag|sa|bv|"
    r"technologies|technology|tech|solutions|labs|lab|software|systems|services|group|holdings|india|"
    r"global|international|intl|worldwide|the)\b\.?",
    re.I,
)


def normalize_company(name: str | None) -> str:
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9&+ ]+", " ", s)
    s = COMPANY_SUFFIXES.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or (name or "").lower().strip()


def normalize_title(title: str | None) -> str:
    s = (title or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(req|job|requisition)\s*(id|#)?\s*[:#-]?\s*[a-z0-9-]+\b", " ", s)
    s = re.sub(r"[^a-z0-9+#/ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_date(value) -> datetime | None:
    """Parse ISO strings, epoch seconds/millis, dates, or relative text ('3 days ago')."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # millis
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    rel = parse_relative_date(s)
    if rel is not None:
        return rel
    try:
        d = dtparser.parse(s)
    except (ValueError, OverflowError, TypeError):
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


_REL_RE = re.compile(
    r"(?i)(?:posted\s*)?(?:(\d+)\+?|a|an)\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago|\b(today|yesterday|just now|just posted)\b",
    re.IGNORECASE,
)


def parse_relative_date(s: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(tz=timezone.utc)
    m = _REL_RE.search(s or "")
    if not m:
        return None
    if m.group(3):
        word = m.group(3).lower()
        if word == "yesterday":
            return now - timedelta(days=1)
        return now
    n = int(m.group(1)) if m.group(1) else 1
    unit = m.group(2).lower()
    if unit.startswith("min"):
        return now - timedelta(minutes=n)
    if unit.startswith("h"):
        return now - timedelta(hours=n)
    if unit == "day":
        return now - timedelta(days=n)
    if unit == "week":
        return now - timedelta(weeks=n)
    if unit == "month":
        return now - timedelta(days=30 * n)
    if unit == "year":
        return now - timedelta(days=365 * n)
    return None


_EXP_RE = re.compile(
    r"(?i)\b(\d{1,2})(?:\s*(?:\+|-|–|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b(?:\s+of)?(?:\s+(?:relevant|hands[- ]on|industry|professional|work|total))?(?:\s+experience)?"
)


def extract_experience(text: str | None) -> str | None:
    if not text:
        return None
    m = _EXP_RE.search(text)
    if not m:
        return None
    lo, hi = m.group(1), m.group(2)
    if int(lo) > 30:
        return None
    return f"{lo}-{hi} yrs" if hi else f"{lo}+ yrs"


def snippet(text: str | None, n: int = 300) -> str:
    t = (text or "").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    s = url.strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
    s = re.sub(r"^www\d?\.", "", s)
    if "." not in s:
        return None
    return s


_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu", "or", "ne"}


def root_domain(domain: str | None) -> str | None:
    """'jobs.sub.example.co.in' -> 'example.co.in'; 'www.example.com' -> 'example.com'."""
    if not domain:
        return None
    parts = domain.lower().strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    if parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
