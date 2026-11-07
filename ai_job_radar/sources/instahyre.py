"""Instahyre (India): public job-search API plus each posting's JobPosting JSON-LD for date, description and employer website."""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from ..http import RateLimited
from ..models import Job, SourceStats
from ..pipeline.relevance import relevance
from ..text import html_to_text, parse_date
from .base import RunContext, SourceResult

API = "https://www.instahyre.com/api/v1/job_search"
DEFAULT_SKILLS = ["machine learning", "deep learning", "NLP", "LLM", "generative AI", "computer vision", "data science", "artificial intelligence"]
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


MAX_WAITS = 4  # how many short rate-limit pauses one run will sit through for this site


def _get(ctx: RunContext, url: str, waits: list[int], **kw):
    """GET that sits out a short Retry-After (Instahyre answers bursts with 429 + ~40 s) a bounded number of times."""
    while True:
        try:
            return ctx.http.get(url, **kw)
        except RateLimited as e:
            if waits[0] >= MAX_WAITS or e.retry_after > 120:
                raise
            waits[0] += 1
            ctx.log(f"[instahyre] rate-limited, waiting {int(e.retry_after) + 1}s ({waits[0]}/{MAX_WAITS}) …")
            time.sleep(e.retry_after + 1)
            ctx.http.unblock(e.host)


def _detail(ctx: RunContext, url: str, waits: list[int]) -> dict | None:
    try:
        page = _get(ctx, url, waits, headers={"Accept": "text/html"}).text
    except Exception:  # noqa: BLE001
        return None
    for block in _LD_RE.findall(page):
        if '"JobPosting"' not in block:
            continue
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    return {}


def fetch_instahyre(ctx: RunContext) -> SourceResult:
    stats = SourceStats("instahyre")
    if ctx.country not in (None, "IN"):
        stats.note = "skipped (Instahyre lists jobs in India)"
        return SourceResult([], stats)
    t0 = time.time()
    ctx.http.set_host_interval("www.instahyre.com", 0.6)
    ctx.http.set_host_concurrency("www.instahyre.com", 2)
    skills = ctx.opt("instahyre_skills") or DEFAULT_SKILLS
    pages = int(ctx.opt("instahyre_pages", 8))
    seen: dict[int, dict] = {}
    waits = [0]
    for skill in skills:
        for page in range(pages):
            try:
                r = _get(ctx, API, waits, params={"limit": 35, "offset": page * 35, "skills": skill}, headers={"Accept": "application/json"})
                r.raise_for_status()
                d = ctx.http.decode_json(r)
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"'{skill}' p{page}: {type(e).__name__}: {str(e)[:120]}")
                break
            for o in d.get("objects") or []:
                seen.setdefault(o["id"], o)
            if not (d.get("meta") or {}).get("next"):
                break
    cands, stale = [], 0
    slack = ctx.prefilter_cutoff - timedelta(days=2)
    for o in seen.values():
        kw = " ".join(o.get("keywords") or [])
        if relevance(o.get("title", ""), kw).score < 45:
            continue
        # The listing carries the date the posting was last reviewed; a review older than the window means the
        # posting is older too, so its detail page (one request each) is not worth fetching.
        reviewed = parse_date(o.get("reviewed_at") or o.get("modified") or o.get("created"))
        if reviewed and reviewed < slack:
            stale += 1
            continue
        cands.append(o)
    cap = int(ctx.opt("instahyre_max_details", 1200))
    jobs: list[Job] = []

    def build(o: dict) -> Job | None:
        url = o.get("public_url") or ""
        if not url:
            return None
        ld = _detail(ctx, url, waits)
        if ld is None:
            return None
        posted = parse_date(ld.get("datePosted")) if ld else None
        if posted and posted < ctx.prefilter_cutoff:
            return None
        emp = o.get("employer") or {}
        org = ld.get("hiringOrganization") or {} if ld else {}
        loc = o.get("locations") or ""
        job = Job(
            title=o.get("title", ""), company=emp.get("company_name") or org.get("name") or "", url=url, source="instahyre",
            location_raw=loc, description=html_to_text(ld.get("description")) if ld else "", posted_at=posted,
            job_id=str(o.get("id")), company_website=org.get("sameAs") or None, poster_type="direct",
        )
        if emp.get("employee_count"):
            job.company_size = f"~{emp['employee_count']} employees"
            job.company_size_source = "instahyre"
        if emp.get("company_founded"):
            job.extra["founded"] = emp["company_founded"]
        if o.get("keywords"):
            job.extra["skills"] = ", ".join(o["keywords"][:12])
        if ld and ld.get("validThrough"):
            job.extra["valid_through"] = str(ld["validThrough"])[:10]
        if "work from home" in loc.lower():
            job.extra["workplace_type"] = "remote"
        return job

    with ThreadPoolExecutor(max_workers=2) as ex:
        for job in ex.map(build, cands[:cap]):
            if job:
                jobs.append(job)
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note = f"{len(seen)} listed, {len(cands) + stale} AI-relevant, {stale} older than the window by their review date, {min(len(cands), cap)} detail pages read, {waits[0]} rate-limit pauses"
    return SourceResult(jobs, stats)
