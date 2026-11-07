"""Fetchers for public applicant-tracking-system (ATS) job boards.

Every function here takes a Board and a RunContext and returns Job objects whose posted_at is
trusted (it comes from the employer's own system). Postings older than the window are dropped
early so large boards stay cheap.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..models import Job
from ..pipeline.relevance import relevance
from ..text import clean_company_name, html_to_text, parse_date, split_company_links
from .base import Board, RunContext


def _mk(board: Board, ctx: RunContext, **kw) -> Job:
    kw.setdefault("source", board.id)
    kw.setdefault("ats", board.platform)
    kw.setdefault("company", board.name)
    kw["company"] = clean_company_name(kw.get("company")) or board.name
    website, linkedin = split_company_links(kw.get("company_website") or board.website)
    kw["company_website"] = website
    kw.setdefault("company_linkedin", linkedin or board.linkedin)
    kw.setdefault("posted_trusted", True)
    kw.setdefault("poster_type", "direct")
    job = Job(**kw)
    job.last_verified_live = ctx.now
    return job


def _fresh(ctx: RunContext, dt: datetime | None) -> bool:
    return dt is None or dt >= ctx.prefilter_cutoff


def _deadline(ctx: RunContext) -> float:
    """Monotonic time after which a board fetcher returns what it has (default 150 s per board)."""
    return time.monotonic() + float(ctx.opt("board_deadline", 150))


def _title_worth_detail(title: str) -> bool:
    """Whether to spend a request fetching the full posting for a title the list did not describe."""
    r = relevance(title)
    if r.excluded_by:
        return False
    if r.score >= 40:
        return True
    return bool(re.search(r"(?i)engineer|scientist|developer|research|architect|analyst|intern|lead|manager|specialist", title))


# ----------------------------------------------------------------------------- Greenhouse
def fetch_greenhouse(board: Board, ctx: RunContext) -> list[Job]:
    data = ctx.http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        posted = parse_date(j.get("first_published")) or parse_date(j.get("updated_at"))
        if not _fresh(ctx, posted):
            continue
        offices = [o.get("name") for o in j.get("offices") or [] if o.get("name")]
        loc = (j.get("location") or {}).get("name") or ", ".join(offices)
        job = _mk(
            board, ctx,
            title=j.get("title", ""), url=j.get("absolute_url", ""), location_raw=loc or "",
            description=html_to_text(j.get("content")), posted_at=posted, job_id=str(j.get("id")),
            req_id=j.get("requisition_id"), company=j.get("company_name") or board.name,
        )
        depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
        if depts:
            job.extra["department"] = ", ".join(depts)
        if offices:
            job.extra["offices"] = offices
        out.append(job)
    return out


# ----------------------------------------------------------------------------- Lever
def fetch_lever(board: Board, ctx: RunContext) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{board.slug}?mode=json"
    r = ctx.http.get(url)
    if r.status_code == 404:
        r = ctx.http.get(f"https://api.eu.lever.co/v0/postings/{board.slug}?mode=json")
    r.raise_for_status()
    out = []
    for j in r.json() or []:
        posted = parse_date(j.get("createdAt"))
        if not _fresh(ctx, posted):
            continue
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        wm = (j.get("workplaceType") or "").lower()
        job = _mk(
            board, ctx,
            title=j.get("text", ""), url=j.get("hostedUrl") or j.get("applyUrl") or "", apply_url=j.get("applyUrl"),
            location_raw=", ".join(locs), description="\n\n".join(p for p in parts if p).strip(), posted_at=posted,
            job_id=j.get("id"), employment_type=cats.get("commitment"),
        )
        if j.get("country"):
            job.extra["country_code"] = j["country"]
        if wm in ("remote", "hybrid", "onsite", "on-site"):
            job.extra["workplace_type"] = wm
        if cats.get("team") or cats.get("department"):
            job.extra["department"] = cats.get("team") or cats.get("department")
        sal = j.get("salaryRange") or {}
        if sal.get("min") and sal.get("max"):
            job.salary = f"{sal.get('currency', '')} {sal['min']}-{sal['max']} {sal.get('interval', '')}".strip()
        out.append(job)
    return out


# ----------------------------------------------------------------------------- Ashby
def fetch_ashby(board: Board, ctx: RunContext) -> list[Job]:
    data = ctx.http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        posted = parse_date(j.get("publishedAt"))
        if not _fresh(ctx, posted):
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        addr = (j.get("address") or {}).get("postalAddress") or {}
        loc_parts = [x for x in [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")] if x]
        loc = ", ".join(x for x in locs if x) or ", ".join(loc_parts)
        if loc_parts and ", ".join(loc_parts) not in loc:
            loc = f"{loc} ({', '.join(loc_parts)})" if loc else ", ".join(loc_parts)
        job = _mk(
            board, ctx,
            title=j.get("title", ""), url=j.get("jobUrl") or j.get("applyUrl") or "", apply_url=j.get("applyUrl"),
            location_raw=loc, description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            posted_at=posted, job_id=j.get("id"), employment_type=j.get("employmentType"),
        )
        if j.get("isRemote"):
            job.extra["workplace_type"] = "remote"
        elif j.get("workplaceType"):
            job.extra["workplace_type"] = str(j["workplaceType"]).lower()
        if j.get("department") or j.get("team"):
            job.extra["department"] = j.get("department") or j.get("team")
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        if comp:
            job.salary = comp
        out.append(job)
    return out


# ----------------------------------------------------------------------------- SmartRecruiters
def fetch_smartrecruiters(board: Board, ctx: RunContext) -> list[Job]:
    base = f"https://api.smartrecruiters.com/v1/companies/{board.slug}/postings"
    offset, fresh = 0, []
    while True:
        data = ctx.http.get_json(f"{base}?limit=100&offset={offset}")
        content = data.get("content") or []
        for j in content:
            posted = parse_date(j.get("releasedDate"))
            if _fresh(ctx, posted):
                fresh.append((j, posted))
        offset += len(content)
        if not content or offset >= int(data.get("totalFound") or 0) or offset >= 2000:
            break
    out = []
    cap = int(ctx.opt("max_detail_per_board", 80))
    deadline = _deadline(ctx)
    for j, posted in fresh[:cap]:
        if time.monotonic() > deadline:
            break
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        if loc.get("remote"):
            loc_s = f"Remote, {loc_s}" if loc_s else "Remote"
        desc, apply_url, url, industry, ref = "", None, f"https://jobs.smartrecruiters.com/{board.slug}/{j.get('id')}", None, j.get("ref")
        try:
            d = ctx.http.get_json(f"{base}/{j.get('id')}")
            secs = (d.get("jobAd") or {}).get("sections") or {}
            desc = "\n\n".join(html_to_text(secs.get(k, {}).get("text")) for k in ("jobDescription", "qualifications", "additionalInformation") if secs.get(k))
            apply_url = d.get("applyUrl")
            url = d.get("postingUrl") or url
            industry = (d.get("industry") or {}).get("label")
            ref = d.get("refNumber") or ref
        except Exception as e:  # keep the listing even if the detail call fails
            desc = f"(detail unavailable: {e})"
        job = _mk(
            board, ctx,
            title=j.get("name", ""), url=url, apply_url=apply_url, location_raw=loc_s, description=desc, posted_at=posted,
            job_id=str(j.get("id")), req_id=str(ref) if ref else None,
            employment_type=(j.get("typeOfEmployment") or {}).get("label"), company_industry=industry,
            company=(j.get("company") or {}).get("name") or board.name,
        )
        lvl = (j.get("experienceLevel") or {}).get("label")
        if lvl:
            job.extra["experience_level"] = lvl
        out.append(job)
    return out


# ----------------------------------------------------------------------------- Workable
def fetch_workable(board: Board, ctx: RunContext) -> list[Job]:
    """Workable's widget endpoint returns every published job with its description in one call.

    The newer apply.workable.com API needs one request per posting and rate-limits anonymous clients
    hard (HTTP 429 with a Retry-After of many hours), so it is only used when the widget is unavailable.
    """
    r = ctx.http.get(f"https://www.workable.com/api/accounts/{board.slug}", params={"details": "true"}, timeout=30)
    if r.status_code == 404:
        r.raise_for_status()
    if r.status_code == 200:
        data = ctx.http.decode_json(r)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            return _workable_widget_jobs(board, ctx, data)
    return _fetch_workable_apply_api(board, ctx)


def _workable_widget_jobs(board: Board, ctx: RunContext, data: dict) -> list[Job]:
    out = []
    company = data.get("name") or board.name
    for j in data["jobs"]:
        posted = parse_date(j.get("published_on")) or parse_date(j.get("created_at"))
        if not _fresh(ctx, posted):
            continue
        loc_s = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        locs = j.get("locations") if isinstance(j.get("locations"), list) else []
        if len(locs) > 1:
            loc_s = "; ".join(", ".join(x for x in [l.get("city"), l.get("region"), l.get("country")] if x) for l in locs if isinstance(l, dict)) or loc_s
        remote = str(j.get("telecommuting", "")).lower() == "true" or (j.get("workplace") == "remote")
        if remote:
            loc_s = f"Remote, {loc_s}" if loc_s else "Remote"
        job = _mk(
            board, ctx,
            title=j.get("title", ""), url=j.get("url") or f"https://apply.workable.com/{board.slug}/j/{j.get('shortcode')}/",
            apply_url=j.get("application_url"), location_raw=loc_s, description=html_to_text(j.get("description")),
            posted_at=posted, job_id=j.get("shortcode"), employment_type=j.get("employment_type"), company=company,
            company_industry=j.get("industry") or None,
        )
        if remote:
            job.extra["workplace_type"] = "remote"
        for k in ("department", "function", "experience", "education"):
            if j.get(k):
                job.extra[k] = j[k]
        out.append(job)
    return out


def _fetch_workable_apply_api(board: Board, ctx: RunContext) -> list[Job]:
    url = f"https://apply.workable.com/api/v3/accounts/{board.slug}/jobs"
    payload = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    fresh, token, pages = [], None, 0
    while pages < 20:
        body = dict(payload, token=token) if token else payload
        data = ctx.http.post_json(url, body)
        for j in data.get("results") or []:
            posted = parse_date(j.get("published"))
            if _fresh(ctx, posted):
                fresh.append((j, posted))
        token = data.get("nextPage")
        pages += 1
        if not token:
            break
    out = []
    cap = int(ctx.opt("max_detail_per_board", 80))
    deadline = _deadline(ctx)
    for j, posted in fresh[:cap]:
        if time.monotonic() > deadline:
            break
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        if j.get("remote") or (j.get("workplace") == "remote"):
            loc_s = f"Remote, {loc_s}" if loc_s else "Remote"
        desc = ""
        try:
            d = ctx.http.get_json(f"https://apply.workable.com/api/v2/accounts/{board.slug}/jobs/{j.get('shortcode')}")
            desc = "\n\n".join(html_to_text(d.get(k)) for k in ("description", "requirements", "benefits") if d.get(k))
        except Exception as e:
            desc = f"(detail unavailable: {e})"
        job = _mk(
            board, ctx,
            title=j.get("title", ""), url=f"https://apply.workable.com/{board.slug}/j/{j.get('shortcode')}/",
            location_raw=loc_s, description=desc, posted_at=posted, job_id=j.get("shortcode"),
            employment_type=j.get("type"),
        )
        if j.get("workplace"):
            job.extra["workplace_type"] = j["workplace"]
        if j.get("department"):
            job.extra["department"] = ", ".join(j["department"]) if isinstance(j["department"], list) else j["department"]
        out.append(job)
    return out


# ----------------------------------------------------------------------------- Recruitee
def fetch_recruitee(board: Board, ctx: RunContext) -> list[Job]:
    data = ctx.http.get_json(f"https://{board.slug}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers") or []:
        if j.get("status") not in (None, "published"):
            continue
        posted = parse_date(j.get("published_at")) or parse_date(j.get("created_at"))
        if not _fresh(ctx, posted):
            continue
        loc_s = ", ".join(x for x in [j.get("city"), j.get("state_name"), j.get("country")] if x)
        if j.get("remote"):
            loc_s = f"Remote, {loc_s}" if loc_s else "Remote"
        elif j.get("hybrid"):
            loc_s = f"Hybrid, {loc_s}" if loc_s else "Hybrid"
        desc = "\n\n".join(html_to_text(j.get(k)) for k in ("description", "requirements") if j.get(k))
        job = _mk(
            board, ctx,
            title=j.get("title", ""), url=j.get("careers_url") or "", apply_url=j.get("careers_apply_url"),
            location_raw=loc_s, description=desc, posted_at=posted, job_id=str(j.get("id")),
            employment_type=j.get("employment_type_code"), company=j.get("company_name") or board.name,
        )
        if j.get("experience_code"):
            job.extra["experience_level"] = j["experience_code"]
        if j.get("department"):
            job.extra["department"] = j["department"]
        if j.get("salary") and isinstance(j["salary"], dict) and j["salary"].get("min"):
            s = j["salary"]
            job.salary = f"{s.get('currency', '')} {s.get('min')}-{s.get('max')} {s.get('period', '')}".strip()
        out.append(job)
    return out


# ----------------------------------------------------------------------------- BambooHR
def fetch_bamboohr(board: Board, ctx: RunContext) -> list[Job]:
    data = ctx.http.get_json(f"https://{board.slug}.bamboohr.com/careers/list")
    out = []
    cap = int(ctx.opt("max_detail_per_board", 80))
    deadline = _deadline(ctx)
    for j in (data.get("result") or [])[:400]:
        if time.monotonic() > deadline:
            break
        title = j.get("jobOpeningName", "")
        if not _title_worth_detail(title):
            continue
        loc = j.get("location") or {}
        ats_loc = j.get("atsLocation") or {}
        loc_s = ", ".join(x for x in [loc.get("city"), loc.get("state"), ats_loc.get("country")] if x)
        desc, posted, url, loc_type = "", None, f"https://{board.slug}.bamboohr.com/careers/{j.get('id')}", None
        if len(out) < cap:
            try:
                d = (ctx.http.get_json(f"https://{board.slug}.bamboohr.com/careers/{j.get('id')}/detail").get("result") or {}).get("jobOpening") or {}
                desc = html_to_text(d.get("description"))
                posted = parse_date(d.get("datePosted"))
                url = d.get("jobOpeningShareUrl") or url
                loc_type = d.get("locationType")
            except Exception as e:
                desc = f"(detail unavailable: {e})"
        if not _fresh(ctx, posted):
            continue
        if loc_type and str(loc_type).lower() in ("remote", "1"):
            loc_s = f"Remote, {loc_s}" if loc_s else "Remote"
        job = _mk(
            board, ctx,
            title=title, url=url, location_raw=loc_s, description=desc, posted_at=posted, job_id=str(j.get("id")),
            employment_type=j.get("employmentStatusLabel"),
        )
        if j.get("departmentLabel"):
            job.extra["department"] = j["departmentLabel"]
        out.append(job)
    return out


# ----------------------------------------------------------------------------- Workday
_WD_POSTED = re.compile(r"(?i)posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)")


def workday_parts(url: str) -> tuple[str, str, str] | None:
    """'https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/...' -> (host, tenant, site)."""
    u = urlsplit(url if "://" in url else "https://" + url)
    host = u.netloc.lower()
    if "myworkdayjobs.com" not in host and "myworkdaysite.com" not in host:
        return None
    tenant = host.split(".")[0]
    segs = [s for s in u.path.split("/") if s]
    if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
        segs = segs[1:]
    if not segs:
        return None
    return host, tenant, segs[0]


def _wd_days(posted_on: str | None) -> int | None:
    m = _WD_POSTED.search(posted_on or "")
    if not m:
        return None
    if m.group(1).lower() == "today":
        return 0
    if m.group(1).lower() == "yesterday":
        return 1
    return int(m.group(2))


def fetch_workday(board: Board, ctx: RunContext) -> list[Job]:
    parts = workday_parts(board.url or board.slug)
    if not parts:
        raise ValueError(f"not a Workday URL: {board.url or board.slug}")
    host, tenant, site = parts
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    terms = ctx.opt("workday_terms") or ["machine learning", "artificial intelligence", "data scientist", "deep learning"]
    seen: dict[str, dict] = {}
    deadline = _deadline(ctx)
    for term in terms:
        offset = 0
        while offset < int(ctx.opt("workday_max_per_term", 100)) and time.monotonic() < deadline:
            try:
                data = ctx.http.post_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            except ValueError as e:  # an HTML page instead of JSON: Workday's "currently unavailable" interstitial or a bot check
                raise RuntimeError("Workday answered with an HTML page instead of JSON (service interruption or bot check); the board is kept for the next run") from e
            posts = data.get("jobPostings") or []
            for p in posts:
                age = _wd_days(p.get("postedOn"))
                if age is not None and age > ctx.days + 1:
                    continue
                path = p.get("externalPath")
                if path and path not in seen:
                    seen[path] = p
            offset += 20
            if len(posts) < 20 or offset >= int(data.get("total") or 0):
                break
    out = []
    cap = int(ctx.opt("max_detail_per_board", 80))
    strong = [(path, p) for path, p in seen.items() if relevance(p.get("title", "")).score >= 40]
    generic = [(path, p) for path, p in seen.items() if (path, p) not in strong and _title_worth_detail(p.get("title", ""))]
    for path, p in strong[:cap] + generic[: max(0, min(20, cap - len(strong)))]:
        if time.monotonic() > deadline:
            break
        title = p.get("title", "")
        detail = {}
        try:
            detail = ctx.http.get_json(f"https://{host}/wday/cxs/{tenant}/{site}{path}").get("jobPostingInfo") or {}
        except Exception:
            detail = {}
        posted = parse_date(detail.get("startDate"))
        if posted is None:
            age = _wd_days(p.get("postedOn"))
            posted = ctx.now - timedelta(days=age) if age is not None else None
        if not _fresh(ctx, posted):
            continue
        country = (detail.get("country") or {}).get("descriptor")
        loc = detail.get("location") or p.get("locationsText") or ""
        extra_locs = detail.get("additionalLocations") or []
        loc_s = ", ".join(x for x in [loc] + list(extra_locs) if x)
        if country and country.lower() not in loc_s.lower():
            loc_s = f"{loc_s}, {country}" if loc_s else country
        remote = str(detail.get("remoteType") or "").lower()
        if "remote" in remote:
            loc_s = f"Remote, {loc_s}"
        req = detail.get("jobReqId") or (p.get("bulletFields") or [None])[0]
        job = _mk(
            board, ctx,
            title=title, url=f"https://{host}/{site}{path}", location_raw=loc_s,
            description=html_to_text(detail.get("jobDescription")), posted_at=posted, posted_trusted=bool(detail.get("startDate")),
            job_id=req, req_id=req, employment_type=detail.get("timeType"),
        )
        out.append(job)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "bamboohr": fetch_bamboohr,
    "workday": fetch_workday,
}


# ----------------------------------------------------------------------------- board URL parsing
_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"([A-Za-z0-9_-]+)\.workable\.com")),
    ("recruitee", re.compile(r"([A-Za-z0-9_-]+)\.recruitee\.com")),
    ("bamboohr", re.compile(r"([A-Za-z0-9_-]+)\.bamboohr\.com")),
]


def board_from_url(url: str, name: str = "", website: str | None = None, countries: list[str] | None = None, origin: str = "user") -> Board | None:
    if not url:
        return None
    if "myworkdayjobs.com" in url or "myworkdaysite.com" in url:
        parts = workday_parts(url)
        if not parts:
            return None
        host, tenant, site = parts
        return Board(name or tenant, "workday", f"{host}/{site}", f"https://{host}/{site}", website, countries or [], origin)
    for platform, rx in _PATTERNS:
        m = rx.search(url)
        if m:
            slug = m.group(1)
            if platform == "workable" and slug in ("apply", "www", "jobs"):
                continue
            return Board(name or slug, platform, slug, url, website, countries or [], origin)
    return None
