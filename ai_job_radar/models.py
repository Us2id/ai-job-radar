"""Data model shared by every source and pipeline stage."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class Job:
    """One job posting, normalised across sources.

    ``posted_at`` is the date the *source* claims. ``posted_trusted`` is True only when the
    date came from the employer's own applicant tracking system (ATS), where it cannot be
    refreshed by a repost on an aggregator.
    """

    title: str
    company: str
    url: str
    source: str  # e.g. "greenhouse:anthropic", "linkedin", "naukri", "remotive"

    location_raw: str = ""
    description: str = ""
    posted_at: datetime | None = None
    posted_trusted: bool = False

    # Parsed location
    city: str | None = None
    region: str | None = None
    country: str | None = None
    country_name: str | None = None
    work_mode: str = "unknown"  # onsite | hybrid | remote | remote-global | unknown

    # Job attributes
    employment_type: str | None = None
    salary: str | None = None
    seniority: str | None = None
    experience: str | None = None
    req_id: str | None = None
    ats: str | None = None  # greenhouse | lever | ashby | smartrecruiters | workable | recruitee | workday
    job_id: str | None = None
    apply_url: str | None = None  # direct apply link when different from ``url``
    emails: list[str] = field(default_factory=list)

    # Company attributes (filled by the source when it knows them, else by enrichment)
    company_website: str | None = None
    company_domain: str | None = None
    company_size: str | None = None
    company_size_source: str | None = None
    company_industry: str | None = None
    company_linkedin: str | None = None
    company_description: str | None = None
    poster_type: str = "unknown"  # direct | agency | unknown

    # Multi-source bookkeeping
    sources: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # Computed by the pipeline
    key: str = ""
    company_norm: str = ""
    relevance_score: int = 0
    relevance_terms: list[str] = field(default_factory=list)
    role_family: str = "other"
    domain_tags: list[str] = field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    times_seen: int = 1
    posted_claims: list[str] = field(default_factory=list)
    opened_at: datetime | None = None
    days_open: int | None = None
    in_window: bool = True
    reposted: bool = False
    legit_score: int = 50
    risk_level: str = "unscored"
    flags: list[str] = field(default_factory=list)
    on_company_board: str = "unknown"  # yes | no | unknown
    company_board_url: str | None = None
    verification_notes: str = ""
    domain_registered: str | None = None
    last_verified_live: datetime | None = None
    duplicate_of: str | None = None
    review_note: str = ""

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.company = (self.company or "").strip()
        self.url = (self.url or "").strip()
        if not self.sources:
            self.sources = [self.source]
        if self.url and self.url not in self.urls:
            self.urls.append(self.url)

    # ----------------------------------------------------------------------------------
    @property
    def source_kind(self) -> str:
        return self.source.split(":", 1)[0]

    @property
    def is_ats(self) -> bool:
        return self.source_kind in ATS_KINDS

    def best_url(self) -> str:
        return self.apply_url or self.url

    def stable_id(self) -> str:
        """Hash of the canonical URL; used as the DB key for a single source URL."""
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:16]


ATS_KINDS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "workday"}
AGGREGATOR_KINDS = {"linkedin", "indeed", "glassdoor", "naukri", "google_jobs", "adzuna", "jooble", "bayt"}
REMOTE_BOARD_KINDS = {"remotive", "remoteok"}


@dataclass
class SourceStats:
    name: str
    fetched: int = 0
    kept: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    note: str = ""


# ---------------------------------------------------------------------------- serialisation
import dataclasses as _dc
from datetime import datetime as _dt

_DT_FIELDS = {"posted_at", "first_seen", "last_seen", "opened_at", "last_verified_live"}


def job_to_dict(job: Job) -> dict:
    d = _dc.asdict(job)
    for k in _DT_FIELDS:
        v = d.get(k)
        if isinstance(v, _dt):
            d[k] = v.isoformat()
    return d


def job_from_dict(d: dict) -> Job:
    d = dict(d)
    for k in _DT_FIELDS:
        v = d.get(k)
        if isinstance(v, str) and v:
            try:
                d[k] = _dt.fromisoformat(v)
            except ValueError:
                d[k] = None
    known = {f.name for f in _dc.fields(Job)}
    return Job(**{k: v for k, v in d.items() if k in known})
