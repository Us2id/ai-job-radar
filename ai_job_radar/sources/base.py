from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from ..geo import Gazetteer
from ..http import Http
from ..models import Job, SourceStats


@dataclass
class Board:
    """One employer career board on a public ATS."""

    name: str
    platform: str  # greenhouse | lever | ashby | smartrecruiters | workable | recruitee | workday | bamboohr
    slug: str
    url: str = ""
    website: str | None = None
    countries: list[str] = field(default_factory=list)  # ISO2 hints, may be empty
    linkedin: str | None = None
    origin: str = "curated"  # curated | openjobs | discovered | user

    @property
    def id(self) -> str:
        return f"{self.platform}:{self.slug}"


def window_cutoff(now: datetime, days: int) -> datetime:
    """Start of the day N days before `now` (UTC): a 7-day window on 11 Nov covers 4-11 Nov inclusive,
    and sources that only give a date never fall out of the window because of the time of day."""
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class RunContext:
    now: datetime
    days: int
    country: str | None  # ISO2 or None for "anywhere"
    country_name: str | None
    search_terms: list[str]
    http: Http
    gaz: Gazetteer
    log: Callable[[str], None] = print
    results_per_term: int = 200
    boards: list[Board] = field(default_factory=list)
    options: dict = field(default_factory=dict)

    @property
    def cutoff(self) -> datetime:
        return window_cutoff(self.now, self.days)

    @property
    def prefilter_cutoff(self) -> datetime:
        """ATS lanes drop postings older than this before the pipeline sees them (one day of slack)."""
        return self.cutoff - timedelta(days=1)

    def key(self, name: str) -> str | None:
        return (self.options.get("keys") or {}).get(name) or os.environ.get(name)

    def opt(self, name: str, default=None):
        return self.options.get(name, default)


@dataclass
class SourceResult:
    jobs: list[Job]
    stats: SourceStats
