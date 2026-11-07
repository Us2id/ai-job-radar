"""The end-to-end run: fetch, normalise, filter, merge, remember, enrich, score, write."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..geo import gazetteer
from ..http import Http
from ..models import Job, SourceStats, job_from_dict, job_to_dict
from ..seeds import load_boards
from ..sources.apis import fetch_adzuna, fetch_google_jobs, fetch_jooble
from ..sources.base import RunContext
from ..sources.boards import classify_board_error, fetch_boards
from ..sources.jobspy_source import fetch_jobspy
from ..sources.remote_boards import fetch_remoteok, fetch_remotive
from ..sources.hn_hiring import fetch_hn_hiring
from ..sources.instahyre import fetch_instahyre
from ..sources.internshala import fetch_internshala
from ..text import clean_company_name, extract_experience, fix_mojibake, normalize_company
from .salary import parse_salary
from .dedup import merge_jobs
from .enrich import Enricher
from .excel import ABOUT, write_workbook
from .relevance import DomainSpec, domain_aliases, relevance, role_family, search_terms, seniority, tag_domains
from .store import Store, default_home
from .verify import poster_type, score_all
from .window import apply_window

LANES = ["ats", "jobspy", "remote", "instahyre", "hn", "google", "adzuna", "jooble"]  # internshala is opt-in (--internships)


@dataclass
class RunConfig:
    days: int = 7
    domain: str = "tech"
    country: str = "India"
    keywords: list[str] = field(default_factory=list)
    deep: bool = False
    lanes: list[str] = field(default_factory=lambda: list(LANES))
    out: str | None = None
    db: str | None = None
    results_per_term: int = 200
    min_relevance: int = 50
    all_boards: bool = False
    board_workers: int = 12
    delay: float = 8.0
    proxies: list[str] | None = None
    limit_boards: int | None = None
    boards_files: list[str] = field(default_factory=list)
    no_openjobs: bool = False
    no_web_lookups: bool = False
    max_discover: int = 80
    linkedin_fetch_description: bool = False
    keys: dict[str, str] = field(default_factory=dict)
    quiet: bool = False
    include_unknown_date: bool = True
    label: str | None = None
    max_terms: int | None = None
    jobspy_sites: list[str] | None = None
    from_raw: str | None = None
    enrich_budget: int = 900


@dataclass
class RunSummary:
    output: Path | None
    run_id: int
    counts: dict
    stats: list[SourceStats]
    jobs: list[Job]
    flagged: list[Job]


def _log_factory(quiet: bool):
    def log(msg: str) -> None:
        if not quiet:
            print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)
    return log


def _apply_workplace(job: Job, gaz) -> None:
    loc = gaz.parse_location(job.location_raw)
    job.city, job.region, job.country, job.country_name = loc.city, loc.region, loc.country, loc.country_name
    job.work_mode = loc.work_mode
    wt = (job.extra.get("workplace_type") or "").lower()
    if wt.startswith("remote") and job.work_mode not in ("remote", "remote-global"):
        job.work_mode = "remote"
    elif wt.startswith("hybrid") and job.work_mode == "onsite":
        job.work_mode = "hybrid"
    if job.country is None and job.extra.get("country_code"):
        cc = str(job.extra["country_code"]).upper()
        if cc in gaz.countries:
            job.country, job.country_name = cc, gaz.country_name(cc)


def _country_ok(job: Job, cc: str | None, gaz) -> tuple[bool, str | None]:
    if cc is None:
        return True, None
    if job.country == cc:
        return True, None
    words = gaz.country_words(cc)
    raw = (job.location_raw or "").lower()
    if job.country and job.country != cc:
        if any(re.search(r"\b" + re.escape(w) + r"\b", raw) for w in words if len(w) > 2):
            return True, "multi-location posting that includes the target country"
        return False, None
    # No country parsed.
    if job.work_mode in ("remote", "remote-global") or not raw:
        why = job.extra.get("remote_eligibility") or gaz.remote_mentions(job.description, cc, location_text=raw)
        if job.work_mode == "remote-global" and not why:
            why = "remote, worldwide"
        if why:
            return True, f"remote: {why}"
        return False, None
    if any(re.search(r"\b" + re.escape(w) + r"\b", raw) for w in words if len(w) > 2):
        return True, None
    return False, None


def summarize_errors(errors: list[str]) -> list[str]:
    """Boards that no longer exist are listed once, compactly; everything else is kept verbatim (up to 150 lines)."""
    missing, rate, other = [], [], []
    for e in errors:
        e = e.removeprefix("(original) ")
        kind = classify_board_error(e)
        (missing if kind == "missing" else rate if kind == "rate-limited" else other).append(e)
    out: list[str] = []
    if missing:
        ids = [e.split(": ", 1)[0] for e in missing]
        out.append(f"{len(missing)} boards no longer exist (HTTP 404 / empty); they are skipped for 30 days: " + ", ".join(ids[:80]) + (" …" if len(ids) > 80 else ""))
    if rate:
        ids = [e.split(": ", 1)[0] for e in rate]
        out.append(f"{len(rate)} boards were rate-limited and skipped this run: " + ", ".join(ids[:40]) + (" …" if len(ids) > 40 else ""))
    out += other[:150]
    return out


def run(cfg: RunConfig) -> RunSummary:
    log = _log_factory(cfg.quiet)
    t_start = time.time()
    now = datetime.now(tz=timezone.utc)
    gaz = gazetteer()
    cc = cname = None
    if cfg.country and cfg.country.strip().lower() not in ("any", "all", "world", "global", "worldwide", "anywhere", "*", ""):
        res = gaz.resolve_country(cfg.country)
        if not res:
            raise SystemExit(f"Unknown country: {cfg.country!r}. Use a country name or ISO code, or 'any'.")
        cc, cname = res
    domain = DomainSpec(cfg.domain)
    terms = search_terms(cfg.deep, cfg.keywords)
    if cfg.max_terms:
        terms = terms[: cfg.max_terms]
    if domain.active:
        extra_words = []
        for key in domain.keys:
            extra_words.append(key)
            for alias, k in domain_aliases().items():
                if k == key and alias != key and len(extra_words) < 2 * len(domain.keys):
                    extra_words.append(alias)
                    break
        for w in extra_words[:4]:
            for ai in ("machine learning", "AI"):
                t = f"{ai} {w}"
                if t not in terms:
                    terms.append(t)
    http = Http(timeout=25)
    # Wikimedia asks clients to go easy; parallel bursts earn an HTTP 429 that costs the whole lookup lane.
    http.set_host_interval("www.wikidata.org", 1.0)
    http.set_host_concurrency("www.wikidata.org", 1)
    http.set_host_interval("en.wikipedia.org", 1.2)
    http.set_host_concurrency("en.wikipedia.org", 1)
    http.set_host_interval("autocomplete.clearbit.com", 0.2)
    http.set_host_concurrency("rdap.org", 1)
    http.set_host_interval("rdap.org", 1.0)  # the fallback redirector sits behind Cloudflare and answers bursts with 429
    http.set_host_concurrency("rdap.verisign.com", 4)
    http.set_host_interval("www.workable.com", 0.3)
    boards = load_boards(include_openjobs=not cfg.no_openjobs, extra_files=cfg.boards_files)
    if cfg.limit_boards:
        boards = boards[: cfg.limit_boards]
    options = {
        "all_boards": cfg.all_boards, "board_workers": cfg.board_workers, "delay": cfg.delay, "proxies": cfg.proxies,
        "keys": cfg.keys, "linkedin_fetch_description": cfg.linkedin_fetch_description, "jobspy_sites": cfg.jobspy_sites,
    }
    ctx = RunContext(now=now, days=cfg.days, country=cc, country_name=cname, search_terms=terms, http=http, gaz=gaz,
                     log=log, results_per_term=cfg.results_per_term, boards=boards, options=options)
    log(f"ai-job-radar v{__version__}: last {cfg.days} days · country={cname or 'any'} · domain={domain.describe()} · {len(terms)} search terms · {len(boards)} boards known")

    store = Store(cfg.db)
    params = {"days": cfg.days, "country": cfg.country, "country_code": cc, "domain": cfg.domain, "lanes": cfg.lanes,
              "terms": terms, "deep": cfg.deep, "min_relevance": cfg.min_relevance, "version": __version__}
    run_id = store.start_run(params)
    skip_boards = store.boards_with_status("missing", 30)
    if skip_boards:
        options["skip_boards"] = skip_boards

    # ------------------------------------------------------------------ fetch all lanes concurrently
    lane_fns = {
        "ats": fetch_boards, "jobspy": fetch_jobspy, "google": fetch_google_jobs, "adzuna": fetch_adzuna,
        "jooble": fetch_jooble, "instahyre": fetch_instahyre, "internshala": fetch_internshala, "hn": fetch_hn_hiring,
    }
    results = []
    if cfg.from_raw:
        import gzip
        with gzip.open(cfg.from_raw, "rt", encoding="utf-8") as fh:
            saved = json.load(fh)
        from ..sources.base import SourceResult
        st = SourceStats("replay")
        st.note = f"replayed {cfg.from_raw}"
        for s in saved.get("stats", []):
            st.errors += [f"(original) {e}" for e in s.get("errors", [])]
        results.append(SourceResult([job_from_dict(d) for d in saved["jobs"]], st))
        log(f"[pipeline] replaying {len(results[0].jobs)} raw postings from {cfg.from_raw}")
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {}
        for lane in ([] if cfg.from_raw else cfg.lanes):
            if lane == "remote":
                futs[ex.submit(fetch_remotive, ctx)] = "remotive"
                futs[ex.submit(fetch_remoteok, ctx)] = "remoteok"
            elif lane in lane_fns:
                futs[ex.submit(lane_fns[lane], ctx)] = lane
        for f, lane in futs.items():
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                st = SourceStats(lane)
                st.errors.append(f"lane crashed: {type(e).__name__}: {str(e)[:200]}")
                results.append(__import__("ai_job_radar.sources.base", fromlist=["SourceResult"]).SourceResult([], st))
    raw: list[Job] = [j for r in results for j in r.jobs]
    stats = [r.stats for r in results]
    for j in raw:  # repair mojibake and board-style company names from any lane
        j.title = fix_mojibake(j.title)
        j.description = fix_mojibake(j.description)
        j.company = clean_company_name(j.company)
    ats_stats = next((s for s in stats if s.name == "ats-boards"), None)
    if ats_stats and not cfg.from_raw:
        marks = []
        for e in ats_stats.errors:
            bid, _, msg = e.partition(": ")
            if ":" in bid and classify_board_error(msg) == "missing":
                marks.append((bid, "missing", msg))
        if marks:
            store.mark_boards(marks, now)
    log(f"[pipeline] {len(raw)} raw postings from {len(results)} lanes")
    raw_dir = default_home() / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"raw_{Path(store.path).stem}_run{run_id}_{now.strftime('%Y%m%d-%H%M%S')}.json.gz"
    if not cfg.from_raw:
        import gzip
        with gzip.open(raw_path, "wt", encoding="utf-8") as fh:
            json.dump({"params": params, "stats": [s.__dict__ for s in stats], "jobs": [job_to_dict(j) for j in raw]}, fh, default=str)
        log(f"[pipeline] raw postings saved to {raw_path} (re-run the pipeline on them with --from-raw)")

    # ------------------------------------------------------------------ normalise + filter
    kept: list[Job] = []
    dropped = {"not_ai": 0, "excluded_title": 0, "other_country": 0, "other_domain": 0, "no_title": 0}
    for j in raw:
        if not j.title:
            dropped["no_title"] += 1
            continue
        j.company_norm = normalize_company(j.company)
        _apply_workplace(j, gaz)
        rel = relevance(j.title, j.description)
        j.relevance_score, j.relevance_terms = rel.score, rel.terms
        if rel.excluded_by:
            dropped["excluded_title"] += 1
            continue
        if rel.score < cfg.min_relevance:
            dropped["not_ai"] += 1
            continue
        ok, why = _country_ok(j, cc, gaz)
        if not ok:
            dropped["other_country"] += 1
            continue
        if why:
            j.extra["country_match"] = why
        j.role_family = role_family(j.title)
        j.seniority = seniority(j.title, j.description)
        if j.poster_type == "unknown":
            j.poster_type = "direct" if j.is_ats else poster_type(j.company)
        j.experience = j.experience or extract_experience(j.description)
        j.domain_tags = tag_domains(j)
        ok, why_d = domain.matches(j)
        if not ok:
            dropped["other_domain"] += 1
            continue
        if why_d:
            j.extra["domain_match"] = why_d
        kept.append(j)
    kept_by_source: dict[str, int] = {}
    for j in kept:
        kept_by_source[j.source_kind] = kept_by_source.get(j.source_kind, 0) + 1
    log(f"[pipeline] relevant to AI/ML and {cname or 'any country'}: {len(kept)} (dropped: {dropped}; kept by source: {kept_by_source})")

    # ------------------------------------------------------------------ merge duplicates, remember, window
    merged = merge_jobs(kept)
    for j in merged:
        sal = parse_salary(j.salary)
        if sal:
            j.extra["salary_annual_inr"] = sal["annual_inr_mid"]
            j.extra["salary_annual"] = f"{sal['currency']} {sal['annual_low']:,.0f}-{sal['annual_high']:,.0f}" if sal["annual_low"] != sal["annual_high"] else f"{sal['currency']} {sal['annual_low']:,.0f}"
        store.touch_job(j, now)
        apply_window(j, now, cfg.days)
    store.commit()
    in_window = [j for j in merged if j.in_window]
    reposts_out = [j for j in merged if not j.in_window]
    log(f"[pipeline] {len(merged)} unique openings; {len(in_window)} opened within {cfg.days} days; {len(reposts_out)} older postings set aside")

    # ------------------------------------------------------------------ enrich + score
    enricher = Enricher(store, http, boards, log=log, options={"no_web_lookups": cfg.no_web_lookups, "max_discover": cfg.max_discover, "enrich_budget": cfg.enrich_budget, "country": cc})
    enricher.run(merged)
    score_all(merged, now)

    # ------------------------------------------------------------------ outputs
    flagged = []
    for j in merged:
        reasons = []
        if not j.in_window:
            if j.reposted or j.times_seen > 1:
                reasons.append(f"repost: first seen {j.first_seen.date().isoformat() if j.first_seen else '?'}, before the window")
            else:
                reasons.append(f"source says posted {j.opened_at.date().isoformat() if j.opened_at else '?'}, before the window")
        if j.risk_level in ("Suspicious", "Check before applying"):
            reasons.append(f"{j.risk_level}: " + "; ".join(j.flags))
        elif j.reposted:
            reasons.append("reposted: " + "; ".join(j.flags))
        elif "date_unknown" in j.flags:
            reasons.append("posting date unknown")
        if reasons:
            j.extra["_flag_reason"] = " | ".join(reasons)
            flagged.append(j)
    companies: dict[str, dict] = {}
    for j in merged:
        rec = store.get_company(j.company_norm) or {}
        c = companies.setdefault(j.company_norm, {
            "name": j.company, "website": rec.get("website") or j.company_website, "size": rec.get("size") or j.company_size,
            "size_source": rec.get("size_source") or j.company_size_source, "industry": rec.get("industry") or j.company_industry,
            "jobs": 0, "best_score": 0, "board_url": rec.get("board_url") if rec.get("board_platform") != "none" else None,
            "board_platform": rec.get("board_platform") if rec.get("board_platform") != "none" else None,
            "domain_registered": rec.get("domain_registered") if rec.get("domain_registered") != "unknown" else None,
            "linkedin": rec.get("linkedin") or j.company_linkedin, "wikidata": rec.get("wikidata") if rec.get("wikidata") != "none" else None,
            "poster_type": j.poster_type, "flags": "", "notes": rec.get("notes") or "",
        })
        c["jobs"] += 1
        c["best_score"] = max(c["best_score"], j.legit_score)
        fl = "; ".join(f for f in j.flags if f != "date_unknown")
        if fl and fl not in c["flags"]:
            c["flags"] = (c["flags"] + " | " + fl).strip(" |")
    counts = {
        "raw": len(raw), "relevant": len(kept), "unique": len(merged), "in_window": len(in_window), "flagged": len(flagged),
        "reposts_outside_window": len(reposts_out), "companies": len(companies), "dropped": dropped,
        "by_source": {s.name: s.fetched for s in stats}, "kept_by_source": dict(sorted(kept_by_source.items())),
        "known_jobs_in_db": store.known_job_count(),
        "risk_levels": {lvl: sum(1 for j in in_window if j.risk_level == lvl) for lvl in ("Likely genuine", "Looks fine", "Check before applying", "Suspicious")},
        "seconds": int(time.time() - t_start),
    }
    errors = [e for s in stats for e in s.errors]
    error_summary = summarize_errors(errors)
    rate_notes = list(http.blocked_log)
    out = Path(cfg.out) if cfg.out else Path("output") / f"ai-jobs_{(cc or 'any').lower()}_{re.sub(r'[^a-z0-9]+', '-', (cfg.domain or 'any').lower()).strip('-') or 'any'}_{cfg.days}d_{now.strftime('%Y-%m-%d')}.xlsx"
    run_log = {
        "Generated": now.strftime("%Y-%m-%d %H:%M UTC"), "Tool": f"ai-job-radar v{__version__}", "Window": f"last {cfg.days} days (opened on or after {ctx.cutoff.date().isoformat()})",
        "Country": cname or "any", "Domain": domain.describe(), "Search terms": ", ".join(terms),
        "Lanes": ", ".join(cfg.lanes), "Boards scanned": next((s.note for s in stats if s.name == "ats-boards"), ""),
        "Counts": {k: v for k, v in counts.items() if k not in ("dropped", "by_source", "risk_levels")},
        "Dropped before merge": dropped, "Fetched per source": counts["by_source"], "Kept per source": counts["kept_by_source"], "Risk levels (in window)": counts["risk_levels"],
        "Source notes": [f"{s.name}: fetched={s.fetched} time={s.seconds:.0f}s {s.note}".strip() for s in stats] + rate_notes,
        "Errors": error_summary or ["none"], "Database": str(store.path),
    }
    new_this_run = [j for j in in_window if j.times_seen == 1]
    counts["new_this_run"] = len(new_this_run)
    write_workbook(out, in_window, flagged, list(companies.values()), run_log, ABOUT, new_this_run=new_this_run)
    store.save_run_jobs(run_id, merged)
    store.finish_run(run_id, counts, errors, str(out))
    store.close()
    n_missing = sum(1 for e in errors if classify_board_error(e) == "missing")
    log(f"[done] {len(in_window)} openings → {out}  ({counts['seconds']}s, {len(errors) - n_missing} source errors, {n_missing} boards no longer exist)")
    return RunSummary(out, run_id, counts, stats, in_window, flagged)
