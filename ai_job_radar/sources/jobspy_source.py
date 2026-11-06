"""LinkedIn, Indeed, Glassdoor, Naukri (and regional boards) through python-jobspy."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from ..geo import JOBSPY_COUNTRIES
from ..models import Job, SourceStats
from .base import RunContext, SourceResult

JOB_SITE_HOSTS = ("naukri.com", "glassdoor.", "indeed.com", "linkedin.com", "ziprecruiter.com", "bayt.com", "bdjobs.com", "hellowork.com", "google.com", "jooble.org", "adzuna.")

MENA = {"AE", "SA", "QA", "KW", "BH", "OM", "EG", "JO", "LB", "MA", "TN", "DZ", "IQ", "YE", "LY", "SD", "PK"}


def sites_for(cc: str | None) -> list[str]:
    """Job sites worth querying for a country. Indeed and Glassdoor need a country they support;
    with no country at all only LinkedIn searches worldwide."""
    sites = ["linkedin"]
    if cc in JOBSPY_COUNTRIES:
        sites += ["indeed", "glassdoor"]
    if cc == "IN":
        sites.append("naukri")
    if cc == "BD":
        sites.append("bdjobs")
    if cc == "FR":
        sites.append("hellowork")
    if cc in MENA:
        sites.append("bayt")
    return sites


def _s(v) -> str | None:
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except TypeError:
        pass
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "nat") else None


def _salary(row) -> str | None:
    lo, hi, cur, interval = _s(row.get("min_amount")), _s(row.get("max_amount")), _s(row.get("currency")), _s(row.get("interval"))
    if not lo and not hi:
        return None
    try:
        lo_f = f"{float(lo):,.0f}" if lo else ""
        hi_f = f"{float(hi):,.0f}" if hi else ""
    except ValueError:
        lo_f, hi_f = lo or "", hi or ""
    rng = f"{lo_f}-{hi_f}".strip("-")
    return " ".join(x for x in [cur or "", rng, f"/{interval}" if interval else ""] if x).strip()


def _row_to_job(row: dict, site: str, ctx: RunContext) -> Job | None:
    title, company, url = _s(row.get("title")), _s(row.get("company")), _s(row.get("job_url"))
    if not title or not url:
        return None
    posted = row.get("date_posted")
    posted_dt = None
    if posted is not None and _s(posted):
        try:
            posted_dt = datetime(posted.year, posted.month, posted.day, tzinfo=timezone.utc)
        except AttributeError:
            from ..text import parse_date
            posted_dt = parse_date(_s(posted))
    emails = row.get("emails")
    if isinstance(emails, str):
        emails = [e.strip() for e in emails.split(",") if e.strip()]
    elif not isinstance(emails, list):
        emails = []
    job = Job(
        title=title, company=company or "", url=url, source=site,
        location_raw=_s(row.get("location")) or "", description=_s(row.get("description")) or "",
        posted_at=posted_dt, posted_trusted=False, employment_type=_s(row.get("job_type")),
        salary=_salary(row), job_id=_s(row.get("id")), apply_url=_s(row.get("job_url_direct")),
        company_website=_s(row.get("company_url_direct")), company_size=_s(row.get("company_num_employees")),
        company_size_source=site if _s(row.get("company_num_employees")) else None,
        company_industry=_s(row.get("company_industry")), company_description=_s(row.get("company_description")),
        emails=[e for e in emails if "@" in e],
    )
    cu = _s(row.get("company_url"))
    if cu and "linkedin.com" in cu:
        job.company_linkedin = cu
    elif cu and any(h in cu for h in JOB_SITE_HOSTS):
        job.extra["job_site_company_page"] = cu
    elif cu and not job.company_website:
        job.company_website = cu
    if job.company_website and any(h in job.company_website for h in JOB_SITE_HOSTS):
        job.extra["job_site_company_page"] = job.company_website
        job.company_website = None
    if _s(row.get("is_remote")) in ("True", "true", "1"):
        job.extra["workplace_type"] = "remote"
    wfh = _s(row.get("work_from_home_type"))
    if wfh:
        job.extra["workplace_type"] = wfh.lower()
    for k in ("job_level", "experience_range", "skills", "vacancy_count", "company_rating", "listing_type", "company_addresses"):
        v = _s(row.get(k))
        if v:
            job.extra[k] = v
    if job.extra.get("experience_range"):
        job.experience = job.extra["experience_range"]
    return job


def fetch_jobspy(ctx: RunContext) -> SourceResult:
    stats = SourceStats("jobspy")
    t0 = time.time()
    try:
        from jobspy import scrape_jobs
    except Exception as e:  # pragma: no cover
        stats.errors.append(f"python-jobspy unavailable: {e}")
        return SourceResult([], stats)
    logging.getLogger("JobSpy").setLevel(logging.ERROR)
    sites = ctx.opt("jobspy_sites") or sites_for(ctx.country)
    hours = max(1, ctx.days * 24)
    location = ctx.country_name or ""
    country_indeed = JOBSPY_COUNTRIES.get(ctx.country or "", "usa")
    stats.note = f"sites={','.join(sites)} hours_old={hours} location='{location}'"
    jobs: list[Job] = []
    per_site: dict[str, int] = {}
    delay = float(ctx.opt("delay", 3.0))
    proxies = ctx.opt("proxies")
    terms = list(ctx.search_terms)
    ctx.log(f"[jobspy] {len(terms)} terms × {sites} (≤{ctx.results_per_term}/term/site) …")
    for i, term in enumerate(terms):
        try:
            df = scrape_jobs(
                site_name=sites, search_term=term, location=location or None, results_wanted=ctx.results_per_term,
                hours_old=hours, country_indeed=country_indeed, description_format="markdown", verbose=0,
                proxies=proxies, linkedin_fetch_description=bool(ctx.opt("linkedin_fetch_description", False)),
            )
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"'{term}': {type(e).__name__}: {str(e)[:160]}")
            continue
        errs = getattr(df, "attrs", {}).get("errors") or {}
        # LinkedIn throttles bursts with a 429 that clears within about a minute: wait once and retry that site.
        retry_sites = [s for s, msg in errs.items() if "429" in str(msg) and s in ("linkedin", "glassdoor")]
        if retry_sites and ctx.opt("retry_429", True):
            ctx.log(f"[jobspy] '{term}': {', '.join(retry_sites)} rate-limited, waiting 65s and retrying once …")
            time.sleep(65)
            try:
                df2 = scrape_jobs(
                    site_name=retry_sites, search_term=term, location=location or None, results_wanted=ctx.results_per_term,
                    hours_old=hours, country_indeed=country_indeed, description_format="markdown", verbose=0, proxies=proxies,
                    linkedin_fetch_description=bool(ctx.opt("linkedin_fetch_description", False)),
                )
                import pandas as pd
                df = pd.concat([df, df2], ignore_index=True) if len(df) else df2
                for s2 in retry_sites:
                    if not (getattr(df2, "attrs", {}).get("errors") or {}).get(s2):
                        errs.pop(s2, None)
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"'{term}' retry: {type(e).__name__}: {str(e)[:120]}")
        for site, msg in errs.items():
            stats.errors.append(f"'{term}' {site}: {str(msg)[:160]}")
        n = 0
        for row in df.to_dict("records"):
            site = _s(row.get("site")) or "jobspy"
            j = _row_to_job(row, site, ctx)
            if j:
                jobs.append(j)
                per_site[site] = per_site.get(site, 0) + 1
                n += 1
        ctx.log(f"[jobspy] '{term}': {n} rows" + (f" (errors: {', '.join(errs)})" if errs else ""))
        if i < len(terms) - 1 and delay:
            time.sleep(delay)
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note += "; rows per site: " + ", ".join(f"{k}={v}" for k, v in sorted(per_site.items()))
    return SourceResult(jobs, stats)
