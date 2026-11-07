"""Instahyre (India): public job-search API plus each posting's JobPosting JSON-LD for date, description and employer website."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from ..models import Job, SourceStats
from ..pipeline.relevance import relevance
from ..text import html_to_text, parse_date
from .base import RunContext, SourceResult

API = "https://www.instahyre.com/api/v1/job_search"
DEFAULT_SKILLS = ["machine learning", "deep learning", "NLP", "LLM", "generative AI", "computer vision", "data science", "artificial intelligence"]
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _detail(ctx: RunContext, url: str) -> dict | None:
    try:
        page = ctx.http.get(url, headers={"Accept": "text/html"}).text
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
    ctx.http.set_host_interval("www.instahyre.com", 0.4)
    skills = ctx.opt("instahyre_skills") or DEFAULT_SKILLS
    pages = int(ctx.opt("instahyre_pages", 8))
    seen: dict[int, dict] = {}
    for skill in skills:
        for page in range(pages):
            try:
                d = ctx.http.get_json(API, params={"limit": 35, "offset": page * 35, "skills": skill}, headers={"Accept": "application/json"})
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"'{skill}' p{page}: {type(e).__name__}: {str(e)[:120]}")
                break
            for o in d.get("objects") or []:
                seen.setdefault(o["id"], o)
            if not (d.get("meta") or {}).get("next"):
                break
    cands = []
    for o in seen.values():
        kw = " ".join(o.get("keywords") or [])
        if relevance(o.get("title", ""), kw).score >= 45:
            cands.append(o)
    cap = int(ctx.opt("instahyre_max_details", 1200))
    jobs: list[Job] = []

    def build(o: dict) -> Job | None:
        url = o.get("public_url") or ""
        if not url:
            return None
        ld = _detail(ctx, url)
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

    with ThreadPoolExecutor(max_workers=4) as ex:
        for job in ex.map(build, cands[:cap]):
            if job:
                jobs.append(job)
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note = f"{len(seen)} listed, {len(cands)} AI-relevant, {min(len(cands), cap)} detail pages read"
    return SourceResult(jobs, stats)
