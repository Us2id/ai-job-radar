"""Internshala (India): AI / ML internships from the listing pages, with dates from each posting's JSON-LD."""
from __future__ import annotations

import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from ..models import Job, SourceStats
from ..pipeline.relevance import relevance
from ..text import html_to_text, parse_date, parse_relative_date
from .base import RunContext, SourceResult

SLUGS = ["machine-learning", "artificial-intelligence-ai", "data-science", "deep-learning", "natural-language-processing-nlp", "computer-vision"]
_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _cards(page_html: str) -> list[dict]:
    out = []
    for blk in re.split(r'<div class="container-fluid individual_internship ', page_html)[1:]:
        link = re.search(r"data-href='([^']+)'|data-href=\"([^\"]+)\"", blk)
        title = re.search(r'class="job-title-href"[^>]*>(.*?)</a>', blk, re.S)
        if not (link and title):
            continue
        company = re.search(r'class="company-name">\s*(.*?)\s*</p>', blk, re.S)
        loc = re.search(r'class="row-1-item locations">.*?<a[^>]*>(.*?)</a>', blk, re.S)
        stipend = re.search(r"class='stipend'>(.*?)</span>", blk)
        duration = re.search(r'ic-16-calendar"></i>\s*<span>(.*?)</span>', blk, re.S)
        posted = re.search(r'class="status-[a-z]+"[^>]*>.*?<span>(.*?)</span>', blk, re.S)
        clean = lambda m: html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""
        out.append({"path": link.group(1) or link.group(2), "title": clean(title), "company": clean(company), "location": clean(loc),
                    "stipend": clean(stipend), "duration": clean(duration), "posted": clean(posted)})
    return out


def fetch_internshala(ctx: RunContext) -> SourceResult:
    stats = SourceStats("internshala")
    if ctx.country not in (None, "IN"):
        stats.note = "skipped (Internshala lists internships in India)"
        return SourceResult([], stats)
    t0 = time.time()
    ctx.http.set_host_interval("internshala.com", 1.0)
    cards: dict[str, dict] = {}
    for slug in ctx.opt("internshala_slugs") or SLUGS:
        for page in range(1, int(ctx.opt("internshala_pages", 3)) + 1):
            url = f"https://internshala.com/internships/{slug}-internship/" + (f"page-{page}/" if page > 1 else "")
            try:
                r = ctx.http.get(url, headers={"Accept": "text/html"})
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"{slug} p{page}: {type(e).__name__}: {str(e)[:120]}")
                break
            if r.status_code != 200:
                stats.errors.append(f"{slug} p{page}: HTTP {r.status_code}")
                break
            got = _cards(r.text)
            if not got:
                break
            for c in got:
                cards.setdefault(c["path"], c)
    cands = []
    for c in cards.values():
        rough = parse_relative_date(c["posted"] or "", ctx.now)
        if rough and rough < ctx.prefilter_cutoff:
            continue
        if relevance(c["title"] + " internship").score >= 45:
            cands.append(c)
    cap = int(ctx.opt("internshala_max_details", 200))
    jobs: list[Job] = []

    def build(c: dict) -> Job | None:
        url = "https://internshala.com" + c["path"]
        ld: dict = {}
        try:
            page = ctx.http.get(url, headers={"Accept": "text/html"}).text
            for block in _LD_RE.findall(page):
                if '"JobPosting"' in block:
                    ld = json.loads(block)
                    break
        except Exception:  # noqa: BLE001
            ld = {}
        posted = parse_date(ld.get("datePosted")) or parse_relative_date(c["posted"] or "", ctx.now)
        if posted and posted < ctx.prefilter_cutoff:
            return None
        title = c["title"] if re.search(r"(?i)intern", c["title"]) else f"{c['title']} Internship"
        job = Job(
            title=title, company=c["company"] or (ld.get("hiringOrganization") or {}).get("name", ""), url=url, source="internshala",
            location_raw=c["location"], description=html_to_text(ld.get("description")), posted_at=posted, employment_type="Internship",
            salary=c["stipend"] or None, job_id=c["path"].rsplit("/", 1)[-1],
        )
        if c["duration"]:
            job.extra["duration"] = c["duration"]
        if ld.get("validThrough"):
            job.extra["valid_through"] = str(ld["validThrough"])[:10]
        if "work from home" in c["location"].lower():
            job.extra["workplace_type"] = "remote"
        return job

    with ThreadPoolExecutor(max_workers=2) as ex:
        for job in ex.map(build, cands[:cap]):
            if job:
                jobs.append(job)
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note = f"{len(cards)} listed, {len(cands)} AI-relevant and recent, {min(len(cands), cap)} detail pages read"
    return SourceResult(jobs, stats)
