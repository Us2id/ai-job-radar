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


# ----------------------------------------------------------------------------- company links and names
# Hosts that are job sites or ATS vendors: a "website" on one of these is not the employer's own site.
JOB_SITE_HOSTS = (
    "naukri.com", "glassdoor.", "indeed.com", "linkedin.com", "ziprecruiter.com", "bayt.com", "bdjobs.com",
    "hellowork.com", "google.com", "jooble.org", "adzuna.", "instahyre.com", "internshala.com", "foundit.", "monster.",
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workable.com", "myworkdayjobs.com",
    "bamboohr.com", "recruitee.com", "jobvite.com", "icims.com", "breezy.hr", "applytojob.com", "rippling.com",
    "freshteam.com", "zohorecruit.com", "wellfound.com", "angel.co", "remoteok.com", "remotive.com", "ycombinator.com",
)


def split_company_links(url: str | None) -> tuple[str | None, str | None]:
    """(website, linkedin_page) for a URL that was offered as a company website.

    LinkedIn company pages are kept as the LinkedIn link; job-site and ATS hosts are dropped.
    """
    u = (url or "").strip()
    if not u:
        return None, None
    if not re.match(r"(?i)https?://", u):
        u = "https://" + u
    host = (domain_of(u) or "").lower()
    if not host:
        return None, None
    if "linkedin.com" in host and "/company/" in u.lower():
        return None, u
    if any(h in host for h in JOB_SITE_HOSTS):
        return None, None
    return u, None


_MOJIBAKE_RE = re.compile(r"[ÃÂâ][\u0080-¿‘-„€™]")


def fix_mojibake(s: str | None) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 / cp1252 ('â€“' -> '–'); other text is returned unchanged."""
    if not s or not _MOJIBAKE_RE.search(s):
        return s or ""
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not _MOJIBAKE_RE.search(fixed) or fixed.count("�") == 0:
            return fixed
    return s


_REGION_WORDS = {
    "india", "usa", "us", "uk", "canada", "australia", "germany", "singapore", "ireland", "europe", "asia", "africa",
    "america", "americas", "uae", "dubai", "japan", "china", "france", "spain", "italy", "netherlands", "poland",
    "mexico", "brazil", "pakistan", "bangladesh", "philippines", "indonesia", "vietnam", "malaysia", "apac", "emea",
    "latam", "mena", "anz", "nordics", "benelux", "switzerland", "austria", "sweden", "denmark", "norway", "finland",
}
_NAME_PREFIX_RE = re.compile(r"(?i)^\s*(?:(?:careers?|jobs?|work|working|hiring|openings?|opportunities)\s+(?:at|with|@)\s+|join\s+(?:us\s+at\s+)?(?=\S))")
_NAME_SUFFIX_RE = re.compile(
    r"(?i)[\s:|–—-]*\b(careers?( site| page| portal)?|jobs?( board| portal| page)?|job board|career site|hiring|"
    r"recruitment|recruiting|talent( community| network)?|external( careers?)?|internal( careers?)?|global careers)\s*$"
)


def clean_company_name(name: str | None) -> str:
    """'Careers at Tide' -> 'Tide', 'Acme Jobs' -> 'Acme', 'ACME Careers Site' -> 'ACME'."""
    s = fix_mojibake(name).strip()
    if not s:
        return ""
    for _ in range(2):
        s2 = _NAME_PREFIX_RE.sub("", s)
        s2 = _NAME_SUFFIX_RE.sub("", s2).strip(" :|-–—")
        m = re.search(r"\s+in\s+(?:the\s+)?([A-Za-z]+)$", s2)
        if m and m.group(1).lower() in _REGION_WORDS:
            s2 = s2[: m.start()]
        if s2 == s or not s2:
            break
        s = s2
    return s or (name or "").strip()


# ----------------------------------------------------------------------------- website hints from text
_URL_OR_MAIL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'()\[\]]+|[\w.+-]+@([a-z0-9.-]+\.[a-z]{2,})")
_NOT_A_COMPANY_SITE = (
    "gmail.", "outlook.", "yahoo.", "hotmail.", "protonmail.", "icloud.", "google.", "youtube.", "twitter.", "x.com",
    "facebook.", "instagram.", "wikipedia.", "w3.org", "schema.org", "github.com", "medium.com", "bit.ly", "goo.gl",
    "apple.com", "microsoft.com", "amazon.", "amazonaws.", "cloudfront.", "glassdoor.", "crunchbase.",
)


def domain_hints(text: str | None, company_norm: str) -> list[str]:
    """Root domains mentioned in a posting that look like the employer's own site, best first.

    A domain whose label contains the company's name ('tide.co' for 'tide') beats one that is merely
    mentioned twice; job sites, mail providers and big platforms never count.
    """
    if not text:
        return []
    tokens = [t for t in (company_norm or "").split() if len(t) >= 3]
    counts: dict[str, int] = {}
    for m in _URL_OR_MAIL_RE.finditer(text):
        raw = m.group(1) or m.group(0)
        d = root_domain(domain_of(raw if "://" in raw or raw.startswith("www.") or "@" not in raw else raw))
        if not d or any(h in d for h in JOB_SITE_HOSTS) or any(h in d for h in _NOT_A_COMPANY_SITE):
            continue
        counts[d] = counts.get(d, 0) + 1
    ranked = []
    for d, n in counts.items():
        label = d.split(".")[0]
        named = any(t in label or label in t for t in tokens) if tokens else False
        if named or n >= 2:
            ranked.append((0 if named else 1, -n, len(d), d))
    return [d for _, _, _, d in sorted(ranked)]


# ----------------------------------------------------------------------------- URL identity
_TRACKING_PARAMS = {
    "ref", "refid", "trk", "trkinfo", "trackingid", "source", "src", "position", "pagenum", "originalsubdomain",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "campaign", "medium", "_ga", "_gl", "ss", "sid", "lipi",
    "licu", "li", "orig", "origin", "from", "referer", "referrer", "iis", "iisn", "ccid", "pos", "rank", "rid",
    "vjs", "vjk", "advn", "xkcb", "tk", "fromage", "sort", "page", "start", "ctx", "cmp", "t", "jvs",
}


def canonical_url(url: str | None) -> str:
    """The part of a URL that identifies the posting: scheme and host lowered, tracking parameters dropped,
    identifying parameters kept ('viewjob?jk=53b5d1' stays distinct from 'viewjob?jk=bd124a'), no fragment."""
    u = (url or "").strip()
    if not u:
        return ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        p = urlsplit(u)
    except ValueError:
        return u.lower()
    host = (p.netloc or "").lower()
    host = re.sub(r"^www\d?\.", "", host)
    path = re.sub(r"/{2,}", "/", p.path or "/").rstrip("/") or "/"
    keep = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_"))
    return urlunsplit((p.scheme.lower() or "https", host, path, urlencode(keep), ""))
