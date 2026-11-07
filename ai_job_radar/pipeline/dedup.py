"""Merge the same opening seen on several sites into one row."""
from __future__ import annotations

import re
import unicodedata

from collections import defaultdict

from rapidfuzz import fuzz

from ..models import ATS_KINDS, Job
from ..text import canonical_url, normalize_company, normalize_title

SOURCE_RANK = {
    **{k: 0 for k in ATS_KINDS},
    "remotive": 1, "remoteok": 1,
    "google_jobs": 2,
    "adzuna": 3, "jooble": 3,
    "indeed": 4, "glassdoor": 5, "linkedin": 5, "naukri": 5, "bayt": 6,
}


def rank(job: Job) -> int:
    return SOURCE_RANK.get(job.source_kind, 7)


def job_key(job: Job) -> str:
    cn = job.company_norm or normalize_company(job.company)
    return f"{cn}|{normalize_title(job.title)}|{(job.city or '').lower()}"


def _city_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", " ", s.split(",")[0]).strip()


def _merge_into(primary: Job, other: Job) -> None:
    for s in other.sources:
        if s not in primary.sources:
            primary.sources.append(s)
    locs = primary.extra.setdefault("locations", [])
    if primary.location_raw and primary.location_raw not in locs:
        locs.insert(0, primary.location_raw)
    if other.location_raw:
        key = _city_key(other.city or other.location_raw)
        if key not in {_city_key(c) for c in primary.extra.get("cities", [])} and other.location_raw not in locs:
            locs.append(other.location_raw)
    cities = primary.extra.setdefault("cities", [])
    for c in (primary.city or primary.location_raw, other.city or other.location_raw):
        if c and _city_key(c) not in {_city_key(x) for x in cities}:
            cities.append(c)
    if len(locs) <= 1:
        primary.extra.pop("locations", None)
    for u in other.urls:
        if u not in primary.urls:
            primary.urls.append(u)
    # Prefer the employer's own date; otherwise keep every claim and the earliest as posted_at.
    if other.posted_at:
        primary.posted_claims.append(other.posted_at.date().isoformat())
        if not primary.posted_at or (other.posted_trusted and not primary.posted_trusted) or (
            other.posted_trusted == primary.posted_trusted and other.posted_at < primary.posted_at
        ):
            primary.posted_at = other.posted_at
            primary.posted_trusted = primary.posted_trusted or other.posted_trusted
    for attr in (
        "description", "location_raw", "city", "region", "country", "employment_type", "salary", "experience",
        "req_id", "ats", "apply_url", "company_website", "company_domain", "company_size", "company_size_source",
        "company_industry", "company_linkedin", "company_description", "job_id",
    ):
        if not getattr(primary, attr) and getattr(other, attr):
            setattr(primary, attr, getattr(other, attr))
    if len(other.description or "") > len(primary.description or "") * 1.5:
        primary.description = other.description
    if primary.work_mode == "unknown" and other.work_mode != "unknown":
        primary.work_mode = other.work_mode
    if primary.poster_type == "unknown" and other.poster_type != "unknown":
        primary.poster_type = other.poster_type
    for e in other.emails:
        if e not in primary.emails:
            primary.emails.append(e)
    for k, v in other.extra.items():
        primary.extra.setdefault(k, v)


def merge_jobs(jobs: list[Job]) -> list[Job]:
    # 1. identical URLs
    by_url: dict[str, Job] = {}
    rest: list[Job] = []
    for j in sorted(jobs, key=rank):
        j.company_norm = j.company_norm or normalize_company(j.company)
        if j.posted_at:
            d = j.posted_at.date().isoformat()
            if d not in j.posted_claims:
                j.posted_claims.append(d)
        u = canonical_url(j.url)
        if u and u in by_url:
            _merge_into(by_url[u], j)
            continue
        if u:
            by_url[u] = j
        rest.append(j)

    # 2. same company: identical normalised title + city, then fuzzy title
    groups: dict[str, list[Job]] = defaultdict(list)
    for j in rest:
        groups[j.company_norm].append(j)
    out: list[Job] = []
    for _company, members in groups.items():
        members.sort(key=rank)
        clusters: list[Job] = []
        for j in members:
            t = normalize_title(j.title)
            target = None
            for c in clusters:
                ct = normalize_title(c.title)
                same_city = (not j.city or not c.city or j.city.lower() == c.city.lower())
                if not same_city:
                    # A job-site posting repeated city by city (the same title, the same employer, neither from the
                    # employer's own board) is one opening advertised in several places: keep one row, list every city.
                    if t == ct and not j.is_ats and not c.is_ats:
                        target = c
                        break
                    continue
                if t == ct or (fuzz.token_sort_ratio(t, ct) >= 94 and (j.seniority or "") == (c.seniority or "")):
                    target = c
                    break
            if target is None:
                clusters.append(j)
            else:
                _merge_into(target, j)
        out.extend(clusters)
    for j in out:
        j.key = job_key(j)
        j.posted_claims = sorted(set(j.posted_claims))
    return out
