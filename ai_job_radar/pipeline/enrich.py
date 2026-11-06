"""Company enrichment and verification: website, domain age, size, LinkedIn page, ATS board, cross-check."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from ..http import Http
from ..models import Job
from ..sources.base import Board
from ..text import domain_of, normalize_company, normalize_title, root_domain
from .discover import discover_one
from .store import Store

WIKI_UA = {"User-Agent": "ai-job-radar/0.1 (job search tool; contact via GitHub)"}


# ----------------------------------------------------------------------------- board title listings
def list_board_titles(board: Board, http: Http, query: str | None = None) -> list[tuple[str, str]]:
    """Current openings on a board as (title, url). For Workday a query is required (search)."""
    p, s = board.platform, board.slug
    if p == "greenhouse":
        d = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
        return [(j.get("title", ""), j.get("absolute_url", "")) for j in d.get("jobs", [])]
    if p == "lever":
        r = http.get(f"https://api.lever.co/v0/postings/{s}?mode=json")
        if r.status_code == 404:
            r = http.get(f"https://api.eu.lever.co/v0/postings/{s}?mode=json")
        r.raise_for_status()
        return [(j.get("text", ""), j.get("hostedUrl", "")) for j in r.json() or []]
    if p == "ashby":
        d = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        return [(j.get("title", ""), j.get("jobUrl", "")) for j in d.get("jobs", []) if j.get("isListed", True)]
    if p == "smartrecruiters":
        out, offset = [], 0
        while offset < 1000:
            d = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100&offset={offset}")
            c = d.get("content") or []
            out += [(j.get("name", ""), f"https://jobs.smartrecruiters.com/{s}/{j.get('id')}") for j in c]
            offset += len(c)
            if not c or offset >= int(d.get("totalFound") or 0):
                break
        return out
    if p == "workable":
        d = http.post_json(f"https://apply.workable.com/api/v3/accounts/{s}/jobs", {"query": query or "", "location": [], "department": [], "worktype": [], "remote": []})
        return [(j.get("title", ""), f"https://apply.workable.com/{s}/j/{j.get('shortcode')}/") for j in d.get("results") or []]
    if p == "recruitee":
        d = http.get_json(f"https://{s}.recruitee.com/api/offers/")
        return [(j.get("title", ""), j.get("careers_url", "")) for j in d.get("offers") or []]
    if p == "bamboohr":
        d = http.get_json(f"https://{s}.bamboohr.com/careers/list")
        return [(j.get("jobOpeningName", ""), f"https://{s}.bamboohr.com/careers/{j.get('id')}") for j in d.get("result") or []]
    if p == "workday":
        from ..sources.ats import workday_parts
        parts = workday_parts(board.url or s)
        if not parts:
            return []
        host, tenant, site = parts
        d = http.post_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query or ""})
        return [(j.get("title", ""), f"https://{host}/{site}{j.get('externalPath', '')}") for j in d.get("jobPostings") or []]
    return []


# ----------------------------------------------------------------------------- lookups
def clearbit_domain(http: Http, name: str) -> str | None:
    try:
        r = http.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}, timeout=10)
        if r.status_code != 200:
            return None
        for c in r.json() or []:
            cn = normalize_company(c.get("name", ""))
            if cn and (cn == normalize_company(name) or fuzz.token_set_ratio(cn, normalize_company(name)) >= 90):
                return c.get("domain")
    except Exception:  # noqa: BLE001
        return None
    return None


def rdap_registration(http: Http, domain: str) -> str | None:
    """ISO date the domain was registered, or None when the registry does not say."""
    try:
        r = http.get(f"https://rdap.org/domain/{domain}", timeout=8, allow_redirects=True)
        if r.status_code != 200:
            return "unknown"
        for e in r.json().get("events") or []:
            if e.get("eventAction") == "registration" and e.get("eventDate"):
                return str(e["eventDate"])[:10]
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


_COMPANY_WORDS = re.compile(r"(?i)\b(company|corporation|startup|start-up|business|enterprise|firm|manufacturer|bank|developer|provider|conglomerate|organization|organisation|group|laborator|institute|technology|software|platform|brand|chain|retailer|airline|publisher|studio|agency|lab)\b")


def wikidata_company(http: Http, name: str, domain: str | None) -> tuple[str | None, str | None, str | None]:
    """(qid, employee_count, official_website) for a company name, verified by website domain when possible."""
    try:
        s = http.get("https://www.wikidata.org/w/api.php", params={"action": "wbsearchentities", "search": name, "language": "en", "type": "item", "limit": 6, "format": "json"}, headers=WIKI_UA, timeout=12).json()
        cands = s.get("search") or []
        if not cands:
            return None, None, None
        ids = "|".join(c["id"] for c in cands)
        ents = http.get("https://www.wikidata.org/w/api.php", params={"action": "wbgetentities", "ids": ids, "props": "claims|descriptions|labels", "languages": "en", "format": "json"}, headers=WIKI_UA, timeout=15).json().get("entities") or {}
        target = normalize_company(name)
        for c in cands:
            ent = ents.get(c["id"]) or {}
            claims = ent.get("claims") or {}
            sites = [cl["mainsnak"]["datavalue"]["value"] for cl in claims.get("P856", []) if cl.get("mainsnak", {}).get("datavalue")]
            site_domains = {root_domain(domain_of(u)) for u in sites}
            desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value", "")
            label = ((ent.get("labels") or {}).get("en") or {}).get("value", "")
            domain_ok = bool(domain) and domain in site_domains
            name_ok = normalize_company(label) == target and bool(_COMPANY_WORDS.search(desc))
            if not (domain_ok or name_ok):
                continue
            emp = None
            best = None
            for cl in claims.get("P1128", []):
                dv = cl.get("mainsnak", {}).get("datavalue", {}).get("value", {})
                amount = dv.get("amount")
                when = ""
                for q in (cl.get("qualifiers") or {}).get("P585", []):
                    when = (q.get("datavalue") or {}).get("value", {}).get("time", "")
                if amount and (best is None or when > best[1]):
                    best = (amount, when)
            if best:
                try:
                    emp = f"{int(float(best[0].lstrip('+'))):,}" + (f" ({best[1][1:5]})" if best[1] else "")
                except ValueError:
                    emp = best[0]
            site = next((u for u in sites if u), None)
            return c["id"], emp, site
    except Exception:  # noqa: BLE001
        return None, None, None
    return None, None, None


# ----------------------------------------------------------------------------- the enricher
class Enricher:
    def __init__(self, store: Store, http: Http, boards: list[Board], log=print, options: dict | None = None):
        self.store, self.http, self.log = store, http, log
        self.o = options or {}
        self.boards_by_norm: dict[str, Board] = {}
        self.boards_by_domain: dict[str, Board] = {}
        for b in boards:
            self.boards_by_norm.setdefault(normalize_company(b.name), b)
            if b.website:
                d = root_domain(domain_of(b.website))
                if d:
                    self.boards_by_domain.setdefault(d, b)
        self._titles_cache: dict[str, list[tuple[str, str]] | None] = {}
        self._lock = threading.Lock()
        self.counters = {"clearbit": 0, "rdap": 0, "wikidata": 0, "discover": 0, "crosscheck": 0, "budget_skipped": 0}
        self._t0 = time.monotonic()
        self.budget = float(self.o.get("enrich_budget", 900))

    def _in_budget(self) -> bool:
        return (time.monotonic() - self._t0) < self.budget

    # ------------------------------------------------------------------ helpers
    def _board_for(self, job: Job, rec: dict) -> Board | None:
        if job.is_ats:
            return None
        b = self.boards_by_norm.get(job.company_norm)
        if not b and job.company_domain:
            b = self.boards_by_domain.get(job.company_domain)
        if b:
            return b
        if rec.get("board_platform") and rec.get("board_platform") != "none" and rec.get("board_url"):
            from ..sources.ats import board_from_url
            return board_from_url(rec["board_url"], job.company, job.company_website)
        return None

    def _titles(self, board: Board, query: str | None = None) -> list[tuple[str, str]] | None:
        key = board.id if board.platform not in ("workday", "workable") else f"{board.id}|{query}"
        with self._lock:
            if key in self._titles_cache:
                return self._titles_cache[key]
        try:
            titles = list_board_titles(board, self.http, query)
        except Exception:  # noqa: BLE001
            titles = None
        with self._lock:
            self._titles_cache[key] = titles
        return titles

    # ------------------------------------------------------------------ per company
    def enrich_company(self, jobs: list[Job]) -> None:
        job0 = jobs[0]
        cn = job0.company_norm
        rec = self.store.get_company(cn) or {}
        now = datetime.now(tz=timezone.utc)
        name = job0.company
        # Pull what any sighting already knows.
        website = rec.get("website") or next((j.company_website for j in jobs if j.company_website), None)
        size = rec.get("size") or next((j.company_size for j in jobs if j.company_size), None)
        size_source = rec.get("size_source") or next((j.company_size_source for j in jobs if j.company_size), None)
        industry = rec.get("industry") or next((j.company_industry for j in jobs if j.company_industry), None)
        linkedin = rec.get("linkedin") or next((j.company_linkedin for j in jobs if j.company_linkedin), None)
        board = self.boards_by_norm.get(cn)
        if board and not website:
            website = board.website
        if not website:
            for j in jobs:
                if j.is_ats and j.url:
                    break
        if website and website.startswith("http") is False:
            website = "https://" + website
        domain = root_domain(domain_of(website)) if website else None
        web_ok = not self.o.get("no_web_lookups") and self._in_budget()
        if not web_ok and not self.o.get("no_web_lookups"):
            self.counters["budget_skipped"] += 1
        if not domain and web_ok and not rec.get("website") and self.counters["clearbit"] < int(self.o.get("max_clearbit", 400)):
            self.counters["clearbit"] += 1
            d = clearbit_domain(self.http, name)
            if d:
                domain, website = root_domain(d), f"https://{d}"
        if not board and domain:
            board = self.boards_by_domain.get(domain)
        # Domain age (cached 90 days).
        registered = rec.get("domain_registered")
        checked = rec.get("domain_checked_at")
        stale = not checked or datetime.fromisoformat(checked) < now - timedelta(days=90)
        if domain and (not registered or stale) and web_ok and self.counters["rdap"] < int(self.o.get("max_rdap", 400)):
            self.counters["rdap"] += 1
            registered = rdap_registration(self.http, domain)
            checked = now.isoformat()
        # Size via Wikidata (only when nothing else knows it).
        wikidata = rec.get("wikidata")
        if not size and not wikidata and web_ok and self.counters["wikidata"] < int(self.o.get("max_wikidata", 100)):
            self.counters["wikidata"] += 1
            qid, emp, site = wikidata_company(self.http, name, domain)
            wikidata = qid or "none"
            if emp:
                size, size_source = emp, "wikidata"
            if site and not website:
                website = site
                domain = root_domain(domain_of(site))
        # ATS board discovery for companies we only saw on aggregators (cached 30 days).
        board_platform, board_url = rec.get("board_platform"), rec.get("board_url")
        bchecked = rec.get("board_checked_at")
        bstale = not bchecked or datetime.fromisoformat(bchecked) < now - timedelta(days=30)
        if board:
            board_platform, board_url = board.platform, board.url
        elif (not board_platform or bstale) and not any(j.is_ats for j in jobs) and web_ok \
                and job0.poster_type != "agency" and len(name) >= 3 and self.counters["discover"] < int(self.o.get("max_discover", 80)):
            self.counters["discover"] += 1
            found = discover_one(self.http, name, website, max_slugs=3)
            board_platform, board_url = (found.platform, found.url) if found else ("none", None)
            bchecked = now.isoformat()
            if found:
                board = found
        self.store.upsert_company(
            cn, name=name, website=website, domain=domain, size=size, size_source=size_source, industry=industry,
            linkedin=linkedin, board_platform=board_platform, board_url=board_url, board_checked_at=bchecked,
            domain_registered=registered, domain_checked_at=checked, wikidata=wikidata,
        )
        rec2 = self.store.get_company(cn) or {}
        for j in jobs:
            j.company_website = rec2.get("website") or j.company_website
            j.company_domain = rec2.get("domain") or j.company_domain
            j.company_size = rec2.get("size") or j.company_size
            j.company_size_source = rec2.get("size_source") or j.company_size_source
            j.company_industry = rec2.get("industry") or j.company_industry
            j.company_linkedin = rec2.get("linkedin") or j.company_linkedin
            j.domain_registered = rec2.get("domain_registered")
            if rec2.get("board_url") and rec2.get("board_platform") != "none":
                j.company_board_url = rec2["board_url"]
            if j.is_ats:
                j.on_company_board = "yes"
                j.company_board_url = j.company_board_url or j.url.rsplit("/", 1)[0]
        # Cross-check aggregator postings against the employer's board (per-title boards are capped).
        if board and web_ok:
            per_title = board.platform in ("workday", "workable")
            checked = 0
            for j in sorted(jobs, key=lambda x: -x.relevance_score):
                if j.is_ats:
                    continue
                if per_title and checked >= int(self.o.get("max_crosscheck_per_company", 10)):
                    break
                checked += 1
                self.counters["crosscheck"] += 1
                titles = self._titles(board, j.title if board.platform in ("workday", "workable") else None)
                if titles is None:
                    continue
                nt = normalize_title(j.title)
                best, best_url = 0, None
                for t, u in titles:
                    sc = fuzz.token_set_ratio(nt, normalize_title(t))
                    if sc > best:
                        best, best_url = sc, u
                if best >= 88:
                    j.on_company_board = "yes"
                    j.company_board_url = best_url or board.url
                    if best_url and not j.apply_url:
                        j.apply_url = best_url
                else:
                    j.on_company_board = "no"
                    j.company_board_url = board.url

    def run(self, jobs: list[Job]) -> None:
        groups: dict[str, list[Job]] = {}
        for j in jobs:
            groups.setdefault(j.company_norm, []).append(j)
        # Companies with the most openings first, so the discovery budget goes where it matters.
        ordered = sorted(groups.values(), key=lambda g: (-len(g), g[0].company.lower()))
        self.log(f"[enrich] {len(groups)} companies (budget {int(self.budget)}s for web lookups) …")
        workers = int(self.o.get("enrich_workers", 16))
        errors = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(self.enrich_company, g) for g in ordered]
            for i, f in enumerate(futs, 1):
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    if errors <= 5:
                        self.log(f"[enrich] company error: {type(e).__name__}: {str(e)[:120]}")
                if i % 25 == 0:
                    self.log(f"[enrich] {i}/{len(groups)} companies, {int(time.monotonic() - self._t0)}s")
        self.store.commit()
        c = self.counters
        self.log(f"[enrich] lookups: clearbit={c['clearbit']} rdap={c['rdap']} wikidata={c['wikidata']} board-discovery={c['discover']} cross-checks={c['crosscheck']}, companies past budget={c['budget_skipped']}, errors={errors}, {int(time.monotonic() - self._t0)}s")
