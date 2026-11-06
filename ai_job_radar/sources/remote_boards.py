"""Remote-first boards with free APIs: Remotive and RemoteOK. Kept only when the posting is open
to candidates in the target country (worldwide / region / country named)."""
from __future__ import annotations

import time

from ..models import Job, SourceStats
from ..text import html_to_text, parse_date
from .base import RunContext, SourceResult

REMOTIVE_TERMS = ["machine learning", "artificial intelligence", "data scientist", "LLM", "deep learning", "NLP", "computer vision"]


def _eligible(ctx: RunContext, location_text: str, description: str) -> str | None:
    if not ctx.country:
        return "no country filter"
    return ctx.gaz.remote_mentions(description[:4000], ctx.country, location_text=location_text)


def fetch_remotive(ctx: RunContext) -> SourceResult:
    stats = SourceStats("remotive")
    t0 = time.time()
    jobs: dict[str, Job] = {}
    for term in REMOTIVE_TERMS:
        try:
            data = ctx.http.get_json("https://remotive.com/api/remote-jobs", params={"search": term, "limit": 200})
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"'{term}': {type(e).__name__}: {str(e)[:140]}")
            continue
        for r in data.get("jobs") or []:
            posted = parse_date(r.get("publication_date"))
            if posted and posted < ctx.prefilter_cutoff:
                continue
            url = r.get("url") or ""
            if not url or url in jobs:
                continue
            desc = html_to_text(r.get("description"))
            loc = r.get("candidate_required_location") or "Worldwide"
            why = _eligible(ctx, loc, desc)
            if not why:
                continue
            job = Job(
                title=r.get("title", ""), company=r.get("company_name", ""), url=url, source="remotive",
                location_raw=f"Remote ({loc})", description=desc, posted_at=posted, job_id=str(r.get("id")),
                employment_type=r.get("job_type"), salary=r.get("salary") or None,
            )
            job.extra["workplace_type"] = "remote"
            job.extra["remote_eligibility"] = why
            job.extra["tags"] = r.get("tags")
            jobs[url] = job
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    return SourceResult(list(jobs.values()), stats)


def fetch_remoteok(ctx: RunContext) -> SourceResult:
    stats = SourceStats("remoteok")
    t0 = time.time()
    jobs: list[Job] = []
    try:
        data = ctx.http.get_json("https://remoteok.com/api")
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"{type(e).__name__}: {str(e)[:140]}")
        return SourceResult([], stats)
    for r in data:
        if not isinstance(r, dict) or not r.get("position"):
            continue
        posted = parse_date(r.get("date")) or parse_date(r.get("epoch"))
        if posted and posted < ctx.prefilter_cutoff:
            continue
        desc = html_to_text(r.get("description"))
        loc = r.get("location") or "Worldwide"
        why = _eligible(ctx, loc, desc)
        if not why:
            continue
        job = Job(
            title=r.get("position", ""), company=r.get("company", ""), url=r.get("url") or r.get("apply_url") or "",
            source="remoteok", location_raw=f"Remote ({loc})", description=desc, posted_at=posted, job_id=str(r.get("id")),
            apply_url=r.get("apply_url"),
        )
        job.extra["workplace_type"] = "remote"
        job.extra["remote_eligibility"] = why
        job.extra["tags"] = r.get("tags")
        if r.get("salary_min") or r.get("salary_max"):
            job.salary = f"USD {r.get('salary_min') or ''}-{r.get('salary_max') or ''}"
        jobs.append(job)
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    return SourceResult(jobs, stats)
