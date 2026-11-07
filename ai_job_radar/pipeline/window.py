"""Was this opening opened within the last N days? Is it a repost?"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models import Job
from ..sources.base import window_cutoff


def _d(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if len(s) == 10 else datetime.fromisoformat(s)


def apply_window(job: Job, now: datetime, days: int) -> None:
    cutoff = window_cutoff(now, days)
    claims = sorted({c[:10] for c in job.posted_claims if c})
    first_seen = job.first_seen or now
    earliest_claim = _d(claims[0]) if claims else None
    latest_claim = _d(claims[-1]) if claims else None
    candidates = [c for c in (earliest_claim, first_seen) if c]
    opened = min(candidates) if candidates else now
    job.opened_at = opened
    job.days_open = max(0, (now - opened).days)
    reposted = False
    if latest_claim and job.times_seen > 1 and (latest_claim - first_seen).days >= 3:
        reposted = True
        job.extra["repost_reason"] = f"date refreshed since first seen on {first_seen.date().isoformat()}"
    elif latest_claim and earliest_claim and (latest_claim - earliest_claim).days >= 3:
        reposted = True
        job.extra["repost_reason"] = f"posted dates differ by {(latest_claim - earliest_claim).days} days across sites or cities"
    job.reposted = reposted
    job.in_window = opened >= cutoff
    if not job.posted_at and job.times_seen <= 1 and "date_unknown" not in job.flags:
        job.flags.append("date_unknown")
