"""Keyed job APIs: Google Jobs via SerpApi, Adzuna, Jooble. Each is skipped unless its key is set."""
from __future__ import annotations

import time
from datetime import timedelta

from ..geo import ADZUNA_COUNTRIES
from ..models import Job, SourceStats
from ..text import html_to_text, parse_date
from .base import RunContext, SourceResult


def fetch_google_jobs(ctx: RunContext) -> SourceResult:
    stats = SourceStats("google_jobs")
    key = ctx.key("SERPAPI_KEY") or ctx.key("SERPAPI_API_KEY")
    if not key:
        stats.note = "skipped (set SERPAPI_KEY to enable)"
        return SourceResult([], stats)
    t0 = time.time()
    chip = "today" if ctx.days <= 1 else "3days" if ctx.days <= 3 else "week" if ctx.days <= 7 else "month"
    pages_cap = int(ctx.opt("google_pages", 3))
    jobs: list[Job] = []
    seen: set[str] = set()
    for term in ctx.search_terms:
        token, page = None, 0
        while page < pages_cap:
            params = {"engine": "google_jobs", "q": term, "hl": "en", "api_key": key, "chips": f"date_posted:{chip}"}
            if ctx.country_name:
                params["location"] = ctx.country_name
            if ctx.country:
                params["gl"] = ctx.country.lower()
            if token:
                params["next_page_token"] = token
            try:
                data = ctx.http.get_json("https://serpapi.com/search.json", params=params)
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"'{term}' page {page + 1}: {type(e).__name__}: {str(e)[:140]}")
                break
            if data.get("error"):
                stats.errors.append(f"'{term}': {data['error'][:140]}")
                break
            for r in data.get("jobs_results") or []:
                ext = r.get("detected_extensions") or {}
                posted = parse_date(ext.get("posted_at"))
                if posted and posted < ctx.prefilter_cutoff:
                    continue
                apply_opts = r.get("apply_options") or []
                apply_url = apply_opts[0].get("link") if apply_opts else None
                url = r.get("share_link") or apply_url or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                job = Job(
                    title=r.get("title", ""), company=r.get("company_name", ""), url=url, source="google_jobs",
                    location_raw=r.get("location") or "", description=r.get("description") or "", posted_at=posted,
                    apply_url=apply_url, job_id=r.get("job_id"), employment_type=ext.get("schedule_type"),
                    salary=ext.get("salary"),
                )
                job.extra["via"] = r.get("via")
                job.extra["apply_options"] = [o.get("link") for o in apply_opts if o.get("link")][:6]
                if ext.get("work_from_home"):
                    job.extra["workplace_type"] = "remote"
                jobs.append(job)
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            page += 1
            if not token:
                break
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note = f"chip=date_posted:{chip}, ≤{pages_cap} pages/term"
    return SourceResult(jobs, stats)


def fetch_adzuna(ctx: RunContext) -> SourceResult:
    stats = SourceStats("adzuna")
    app_id, app_key = ctx.key("ADZUNA_APP_ID"), ctx.key("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        stats.note = "skipped (set ADZUNA_APP_ID and ADZUNA_APP_KEY to enable)"
        return SourceResult([], stats)
    cc = ADZUNA_COUNTRIES.get(ctx.country or "")
    if not cc:
        stats.note = f"skipped (Adzuna has no feed for {ctx.country_name or 'this country'})"
        return SourceResult([], stats)
    t0 = time.time()
    jobs: list[Job] = []
    seen: set[str] = set()
    pages_cap = int(ctx.opt("adzuna_pages", 4))
    for term in ctx.search_terms:
        for page in range(1, pages_cap + 1):
            try:
                data = ctx.http.get_json(
                    f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}",
                    params={"app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": term,
                            "max_days_old": ctx.days, "content-type": "application/json"},
                )
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"'{term}' p{page}: {type(e).__name__}: {str(e)[:140]}")
                break
            results = data.get("results") or []
            for r in results:
                if not r.get("redirect_url") or r["redirect_url"] in seen:
                    continue
                seen.add(r["redirect_url"])
                job = Job(
                    title=r.get("title", ""), company=(r.get("company") or {}).get("display_name", ""),
                    url=r.get("redirect_url", ""), source="adzuna", location_raw=(r.get("location") or {}).get("display_name", ""),
                    description=r.get("description") or "", posted_at=parse_date(r.get("created")), job_id=str(r.get("id")),
                    employment_type=r.get("contract_time") or r.get("contract_type"),
                )
                if r.get("salary_min") or r.get("salary_max"):
                    job.salary = f"{r.get('salary_min') or ''}-{r.get('salary_max') or ''} (Adzuna estimate)" if r.get("salary_is_predicted") == "1" else f"{r.get('salary_min') or ''}-{r.get('salary_max') or ''}"
                if (r.get("category") or {}).get("label"):
                    job.extra["category"] = r["category"]["label"]
                jobs.append(job)
            if len(results) < 50:
                break
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    return SourceResult(jobs, stats)


def fetch_jooble(ctx: RunContext) -> SourceResult:
    stats = SourceStats("jooble")
    key = ctx.key("JOOBLE_API_KEY")
    if not key:
        stats.note = "skipped (set JOOBLE_API_KEY to enable)"
        return SourceResult([], stats)
    t0 = time.time()
    jobs: list[Job] = []
    seen: set[str] = set()
    pages_cap = int(ctx.opt("jooble_pages", 3))
    for term in ctx.search_terms:
        for page in range(1, pages_cap + 1):
            try:
                data = ctx.http.post_json(f"https://jooble.org/api/{key}", {"keywords": term, "location": ctx.country_name or "", "page": page})
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"'{term}' p{page}: {type(e).__name__}: {str(e)[:140]}")
                break
            results = data.get("jobs") or []
            new = 0
            for r in results:
                link = r.get("link", "")
                if not link or link in seen:
                    continue
                seen.add(link)
                new += 1
                posted = parse_date(r.get("updated"))
                if posted and posted < ctx.prefilter_cutoff:
                    continue
                jobs.append(Job(
                    title=r.get("title", ""), company=r.get("company", ""), url=r.get("link", ""), source="jooble",
                    location_raw=r.get("location") or "", description=html_to_text(r.get("snippet")), posted_at=posted,
                    job_id=str(r.get("id")), employment_type=r.get("type"), salary=r.get("salary") or None,
                ))
            if not results or not new:
                break
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    return SourceResult(jobs, stats)
