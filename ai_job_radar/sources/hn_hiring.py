"""Hacker News 'Ask HN: Who is hiring?' monthly thread via the public Algolia API."""
from __future__ import annotations

import re
import time
from datetime import datetime

from ..models import Job, SourceStats
from ..text import html_to_text
from .base import RunContext, SourceResult


def fetch_hn_hiring(ctx: RunContext) -> SourceResult:
    stats = SourceStats("hn_hiring")
    t0 = time.time()
    try:
        hits = ctx.http.get_json("https://hn.algolia.com/api/v1/search_by_date", params={"query": "\"who is hiring\"", "tags": "story,author_whoishiring", "hitsPerPage": 4})["hits"]
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"search: {type(e).__name__}: {str(e)[:120]}")
        return SourceResult([], stats)
    threads = [h for h in hits if "who is hiring" in h.get("title", "").lower()][:2]
    jobs: list[Job] = []
    for th in threads:
        try:
            d = ctx.http.get_json(f"https://hn.algolia.com/api/v1/items/{th['objectID']}")
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"thread {th['objectID']}: {type(e).__name__}: {str(e)[:120]}")
            continue
        for k in d.get("children") or []:
            if not k.get("text"):
                continue
            created = datetime.fromisoformat(k["created_at"].replace("Z", "+00:00"))
            if created < ctx.prefilter_cutoff:
                continue
            text = html_to_text(k["text"])
            first = text.split("\n", 1)[0]
            parts = [p.strip() for p in re.split(r"\s\|\s", first) if p.strip()]
            company = parts[0] if parts else ""
            title = parts[1] if len(parts) > 1 else first[:90]
            rest = parts[2:] if len(parts) > 2 else []
            loc_bits = [p for p in rest if re.search(r"(?i)remote|onsite|on-site|hybrid|[A-Z][a-z]+,|\bUS\b|\bUK\b|\bEU\b|India|Europe|Asia|worldwide|anywhere", p)]
            job = Job(
                title=title[:120], company=company[:100], url=f"https://news.ycombinator.com/item?id={k['id']}", source="hn_hiring",
                location_raw=" | ".join(loc_bits)[:200], description=text, posted_at=created, poster_type="direct", job_id=str(k["id"]),
            )
            job.extra["thread"] = th.get("title")
            if re.search(r"(?i)\bremote\b", first):
                job.extra["workplace_type"] = "remote"
            jobs.append(job)
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note = f"threads: {', '.join(t.get('title', '') for t in threads)}"
    return SourceResult(jobs, stats)
