"""Company enrichment and verification: website, domain age, size, LinkedIn page, ATS board, cross-check."""
from __future__ import annotations

from dataclasses import dataclass

import json
import re
import threading
import time
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from rapidfuzz import fuzz

from ..http import Http, RateLimited
from ..models import Job
from ..sources.base import Board
from ..text import JOB_SITE_HOSTS, domain_hints, domain_of, normalize_company, normalize_title, root_domain, split_company_links
from .discover import discover_one
from .store import Store

WIKI_UA = {"User-Agent": "ai-job-radar/0.1 (+https://github.com/Us2id/ai-job-radar)"}


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
def _note_value(notes: str | None, key: str) -> str | None:
    m = re.search(re.escape(key) + r":([^\s]+(?: [a-z]+)*)", notes or "")
    return m.group(1).split(" wikipedia:")[0] if m else None


def name_match_level(candidate: str, target: str) -> int:
    """How well a looked-up name denotes the company we asked about: 2 = the same name after normalisation,
    1 = the asked-for words all present with at most two extra ('London Stock Exchange Group' for 'London
    Stock Exchange', but also 'PwC Norway' for 'PwC', so callers treat 1 with care), 0 = no.
    Never a near-miss on spelling: 'Capcom' is not 'Capco'."""
    c, t = normalize_company(candidate), normalize_company(target)
    if not c or not t:
        return 0
    if c == t:
        return 2
    ct, tt = c.split(), t.split()
    return 1 if set(tt) <= set(ct) and len(ct) - len(tt) <= 2 else 0


def name_matches(candidate: str, target: str) -> bool:
    return name_match_level(candidate, target) > 0


_GLOBAL_TLDS = {"com", "io", "ai", "co", "org", "net", "dev", "app", "tech", "cloud", "software", "info", "biz", "global"}


def site_rank(domain: str, country: str | None) -> tuple:
    """Sort key for choosing among a company's domains: a global TLD first, then the country's own, then the rest; shorter wins ties."""
    tld = domain.rsplit(".", 1)[-1].lower()
    cc = (country or "").lower()
    return (0 if tld in _GLOBAL_TLDS else 1 if cc and tld == cc else 2, len(domain), domain)


def prefer_site(urls: list[str], country: str | None) -> str | None:
    """The best employer website among several (Wikidata lists apple.com.cn next to apple.com)."""
    cands = []
    for u in urls:
        site, _ = split_company_links(u)
        d = root_domain(domain_of(site)) if site else None
        if site and d:
            cands.append((site_rank(d, country), site))
    return min(cands)[1] if cands else None


def clearbit_domain(http: Http, name: str, country: str | None = None) -> str | None:
    """The employer's domain from Clearbit's free autocomplete; among name matches the shortest domain wins
    (mastercard.com over mastercard.com.au)."""
    try:
        r = http.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}, timeout=10)
        if r.status_code != 200:
            return None
        target = normalize_company(name)
        matches = []
        for c in r.json() or []:
            cn = normalize_company(c.get("name", ""))
            dom = (c.get("domain") or "").lower()
            if not cn or not dom or any(h in dom for h in JOB_SITE_HOSTS):
                continue
            lvl = name_match_level(cn, target)
            if lvl:
                matches.append((2 - lvl, site_rank(dom, country), dom))
        if matches:
            return sorted(matches)[0][2]
    except Exception:  # noqa: BLE001
        return None
    return None


_rdap_bootstrap: dict[str, str] | None = None
_rdap_lock = threading.Lock()


def rdap_base(http: Http, domain: str) -> str:
    """The registry's own RDAP server for this domain's TLD (IANA bootstrap, fetched once per run);
    rdap.org, a redirector behind Cloudflare that throttles bursts, only for TLDs without one."""
    global _rdap_bootstrap
    with _rdap_lock:
        if _rdap_bootstrap is None:
            table: dict[str, str] = {}
            try:
                for tlds, urls in http.get("https://data.iana.org/rdap/dns.json", timeout=15).json().get("services", []):
                    for t in tlds:
                        table[t.lower()] = urls[0].rstrip("/") + "/"
            except Exception:  # noqa: BLE001
                pass
            _rdap_bootstrap = table
    return _rdap_bootstrap.get(domain.rsplit(".", 1)[-1].lower(), "https://rdap.org/")


def rdap_registration(http: Http, domain: str) -> str | None:
    """ISO date the domain was registered; 'unknown' when the registry has no record or no date;
    None when the lookup could not be made (rate limit, timeout), so it is retried on the next run."""
    try:
        r = http.get(f"{rdap_base(http, domain)}domain/{domain}", timeout=8, allow_redirects=True, headers={"Accept": "application/rdap+json"})
        if r.status_code == 404:
            return "unknown"
        if r.status_code != 200:
            return None
        for e in r.json().get("events") or []:
            if e.get("eventAction") == "registration" and e.get("eventDate"):
                return str(e["eventDate"])[:10]
    except Exception:  # noqa: BLE001
        return None
    return "unknown"


# Words that make a one-line Wikidata/Wikipedia description read like a company. Deliberately without 'group',
# 'agency', 'studio', 'lab' or 'organization': a rock band is a group and a performing-arts collective is an
# organisation, and both have been mistaken for employers of the same name.
_COMPANY_WORDS = re.compile(
    r"(?i)\b(company|companies|corporation|startup|start-up|business|enterprise|firm|manufacturer|bank|"
    r"provider|conglomerate|technology|software|platform|retailer|airline|publisher|subsidiary|multinational|"
    r"consultancy|consulting|fintech|unicorn|marketplace|e-commerce|ecommerce|operator|supplier|developer|"
    r"brand|chain|insurer|automaker|carmaker|telecom|utility|lender|exchange|outsourcing|services? (company|firm|provider))\b"
)


@dataclass
class WikiCompany:
    qid: str | None = None
    employees: str | None = None
    website: str | None = None
    wiki_title: str | None = None
    industry: str | None = None
    employees_source: str | None = None


def _claim_values(claims: dict, prop: str) -> list:
    return [cl["mainsnak"]["datavalue"]["value"] for cl in claims.get(prop, []) if cl.get("mainsnak", {}).get("datavalue")]


def wikidata_company(http: Http, name: str, domain: str | None) -> WikiCompany:
    """Employee count, official website and English Wikipedia title for a company, verified by website domain
    when one is known and otherwise by an exact name/alias match plus a company-like description.

    Raises RateLimited when Wikidata asks us to back off; every other failure returns an empty result.
    """
    try:
        s = http.get("https://www.wikidata.org/w/api.php", params={"action": "wbsearchentities", "search": name, "language": "en", "type": "item", "limit": 7, "format": "json"}, headers=WIKI_UA, timeout=12).json()
    except RateLimited:
        raise
    except Exception:  # noqa: BLE001
        return WikiCompany()
    target = normalize_company(name)
    plausible = []
    for c in s.get("search") or []:
        desc = c.get("description") or ""
        label = c.get("label") or ""
        matched = (c.get("match") or {}).get("text") or label
        name_ok = name_matches(label, target) or name_matches(matched, target)
        if name_ok and (_COMPANY_WORDS.search(desc) or domain):
            plausible.append((0 if _COMPANY_WORDS.search(desc) else 1, c))
    plausible.sort(key=lambda x: x[0])
    for _, c in plausible[:2]:
        # Special:EntityData is served from Wikimedia's CDN cache and is the recommended way to read one entity;
        # the action API's wbgetentities with claims is what earns anonymous clients an HTTP 429.
        try:
            ent = http.get(f"https://www.wikidata.org/wiki/Special:EntityData/{c['id']}.json", headers=WIKI_UA, timeout=20).json().get("entities", {}).get(c["id"]) or {}
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001
            continue
        claims = ent.get("claims") or {}
        sites = [u for u in _claim_values(claims, "P856") if isinstance(u, str)]
        site_domains = {root_domain(domain_of(u)) for u in sites}
        if domain and site_domains and domain not in site_domains:
            continue  # a different company with the same name
        best = None
        for cl in claims.get("P1128", []):
            dv = cl.get("mainsnak", {}).get("datavalue", {}).get("value", {})
            amount = dv.get("amount")
            when = ""
            for q in (cl.get("qualifiers") or {}).get("P585", []):
                when = (q.get("datavalue") or {}).get("value", {}).get("time", "")
            if amount and (best is None or when > best[1]):
                best = (amount, when)
        emp = None
        if best:
            try:
                emp = f"{int(float(best[0].lstrip('+'))):,}" + (f" ({best[1][1:5]})" if best[1] else "")
            except ValueError:
                emp = best[0]
        title = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
        return WikiCompany(c["id"], emp, next((u for u in sites if u), None), title)
    return WikiCompany()


def _wiki_summary(http: Http, title: str) -> dict | None:
    """English Wikipedia REST summary (CDN-cached, follows redirects); None when there is no such page."""
    try:
        r = http.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_")), headers=WIKI_UA, timeout=15)
    except RateLimited:
        raise
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _entity_facts(http: Http, qid: str) -> tuple[str | None, list[str]]:
    """(latest employee count P1128, official websites P856) of one Wikidata entity via the CDN-cached EntityData endpoint."""
    try:
        ent = http.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json", headers=WIKI_UA, timeout=20).json().get("entities", {}).get(qid) or {}
    except RateLimited:
        raise
    except Exception:  # noqa: BLE001
        return None, []
    claims = ent.get("claims") or {}
    sites = [u for u in _claim_values(claims, "P856") if isinstance(u, str)]
    best = None
    for cl in claims.get("P1128", []):
        dv = cl.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        amount = dv.get("amount")
        when = ""
        for q in (cl.get("qualifiers") or {}).get("P585", []):
            when = (q.get("datavalue") or {}).get("value", {}).get("time", "")
        if amount and (best is None or when > best[1]):
            best = (amount, when)
    if not best:
        return None, sites
    try:
        return f"{int(float(best[0].lstrip('+'))):,}" + (f" ({best[1][1:5]})" if best[1] else ""), sites
    except ValueError:
        return best[0], sites


def wikipedia_company(http: Http, name: str, domain: str | None, country: str | None = None) -> WikiCompany:
    """Resolve a company through English Wikipedia: the article named after the company (or 'Name (company)'),
    accepted only when its one-line description reads like a company and, when a website domain is known,
    the recorded website agrees. Employees come from Wikidata's P1128, then from the infobox.

    Wikimedia's edge limiter is strict with anonymous clients, so this spends at most one light REST call on
    en.wikipedia.org and one cached EntityData call on wikidata.org per company; the heavier infobox parse
    only runs when Wikidata has no employee count.
    """
    target = normalize_company(name)
    for title in (name, f"{name} (company)"):
        d = _wiki_summary(http, title)
        if not d or d.get("type") != "standard":
            continue
        desc = f"{d.get('description') or ''}. {(d.get('extract') or '')[:300]}"
        if normalize_company(re.sub(r"\s*\(.*\)$", "", d.get("title") or "")) != target or not _COMPANY_WORDS.search(desc):
            continue
        qid = d.get("wikibase_item")
        emp, sites = _entity_facts(http, qid) if qid else (None, [])
        site_domains = {root_domain(domain_of(u)) for u in sites}
        if domain and site_domains and domain not in site_domains:
            continue  # same name, different company
        stale = _employees_year(emp) < datetime.now(tz=timezone.utc).year - 2
        box = wikipedia_infobox(http, d.get("title") or title) if (not emp or stale) else {}
        site_ok = prefer_site(sites, country) or split_company_links(box.get("website"))[0]
        if domain and not site_domains and site_ok and root_domain(domain_of(site_ok)) != domain:
            continue
        employees, source = emp, "wikidata"
        if box.get("employees") and _employees_year(box["employees"]) >= _employees_year(emp):
            employees, source = box["employees"], "wikipedia"
        return WikiCompany(qid, employees, site_ok, d.get("title"), box.get("industry"), source if employees else None)
    return WikiCompany()


_WIKI_MARKUP_RE = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->|\{\{(?:increase|decrease|steady|nowrap|small|efn|cn|citation needed)[^}]*\}\}", re.I | re.S)


def _infobox_field(wikitext: str, *names: str) -> str | None:
    for n in names:
        m = re.search(r"(?im)^\s*\|\s*" + n + r"\s*=\s*(.+?)\s*$", wikitext)
        if m and m.group(1).strip():
            v = _WIKI_MARKUP_RE.sub("", m.group(1))
            v = re.sub(r"\{\{(?:url|official url|official website)\|?([^}|]*)[^}]*\}\}", r"\1", v, flags=re.I)
            v = re.sub(r"\{\{[^}]*\}\}", " ", v)
            v = re.sub(r"\[\[(?:[^]|]*\|)?([^]]*)\]\]", r"\1", v)
            v = re.sub(r"'{2,}|&nbsp;|\s+", " ", v).strip(" ,;")
            if v:
                return v
    return None


def wikipedia_infobox(http: Http, title: str) -> dict:
    """Employees / website / industry from the lead-section infobox of an English Wikipedia article."""
    try:
        r = http.get("https://en.wikipedia.org/w/api.php", params={"action": "parse", "page": title, "prop": "wikitext", "section": 0, "redirects": 1, "format": "json", "formatversion": 2}, headers=WIKI_UA, timeout=15)
        if r.status_code != 200:
            return {}
        wt = (r.json().get("parse") or {}).get("wikitext") or ""
    except RateLimited:
        raise
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    emp = _infobox_field(wt, "num_employees", "employees", "number_of_employees")
    if emp:
        m = re.search(r"(?:c\.|circa|~|≈|approx\.?)?\s*(\d[\d,.]*)\s*(thousand|million|k|m)?(?:\s*\+)?(?:.{0,25}?\(?(?:fy|as of)?\s*((?:19|20)\d\d)\)?)?", emp, re.I)
        if m:
            try:
                n = float(m.group(1).replace(",", ""))
                unit = (m.group(2) or "").lower()
                n = n * (1000 if unit in ("thousand", "k") else 1_000_000 if unit in ("million", "m") else 1)
                out["employees"] = f"{int(n):,}" + (f" ({m.group(3)})" if m.group(3) else "")
            except ValueError:
                out["employees"] = emp[:40]
    site = _infobox_field(wt, "website", "homepage", "url")
    if site and "." in site and " " not in site.strip():
        out["website"] = site.strip() if site.startswith("http") else "https://" + site.strip()
    ind = _infobox_field(wt, "industry", "genre")
    if ind:
        out["industry"] = ind[:80]
    return out


# ----------------------------------------------------------------------------- company size: batched Wikidata search
@dataclass
class WikiCandidate:
    qid: str
    rank: int
    label: str
    description: str
    employees: str | None
    sites: list[str]
    article: str | None


def _fmt_employees(stmts: list[tuple[str, str]]) -> str | None:
    """stmts: (point-in-time, amount). The most recent statement, formatted '39,800 (2025)'."""
    if not stmts:
        return None
    when, amount = max(stmts)
    try:
        return f"{int(float(str(amount).lstrip('+'))):,}" + (f" ({when[:4]})" if when[:4].isdigit() else "")
    except ValueError:
        return str(amount)


def sparql_company_search(http: Http, names: list[str], per_name: int = 7) -> dict[str, list[WikiCandidate]]:
    """One request to the Wikidata query service resolves a batch of company names: it runs the entity search
    for every name server-side and returns each candidate's description, employee count (P1128), official
    websites (P856) and English Wikipedia article. One call replaces ~2 calls per company against the action
    API, whose edge rate limit is far stricter for anonymous clients.

    Raises RateLimited when the query service asks us to back off.
    """
    if not names:
        return {}
    values = " ".join(json.dumps(n) for n in names)
    query = f"""
SELECT ?search ?item ?num ?label ?desc ?emp ?empDate ?site ?article WHERE {{
  VALUES ?search {{ {values} }}
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "EntitySearch"; mwapi:search ?search; mwapi:language "en"; mwapi:limit "{int(per_name)}".
    ?item wikibase:apiOutputItem mwapi:item. ?num wikibase:apiOrdinal true.
  }}
  OPTIONAL {{ ?item rdfs:label ?label FILTER(LANG(?label) = "en") }}
  OPTIONAL {{ ?item schema:description ?desc FILTER(LANG(?desc) = "en") }}
  OPTIONAL {{ ?item p:P1128 ?st . ?st ps:P1128 ?emp . OPTIONAL {{ ?st pq:P585 ?empDate }} }}
  OPTIONAL {{ ?item wdt:P856 ?site }}
  OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }}
}}"""
    r = http.get("https://query.wikidata.org/sparql", params={"query": query, "format": "json"},
                 headers={**WIKI_UA, "Accept": "application/sparql-results+json"}, timeout=90)
    if r.status_code != 200:
        return {}
    rows = (r.json().get("results") or {}).get("bindings") or []
    acc: dict[str, dict[str, dict]] = {}
    for b in rows:
        search = b["search"]["value"]
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        d = acc.setdefault(search, {}).setdefault(qid, {
            "rank": int(b.get("num", {}).get("value", 99)), "label": b.get("label", {}).get("value", ""),
            "desc": b.get("desc", {}).get("value", ""), "emp": [], "sites": set(),
            "article": b.get("article", {}).get("value"),
        })
        if "emp" in b:
            d["emp"].append((b.get("empDate", {}).get("value", "")[:10], b["emp"]["value"]))
        if "site" in b:
            d["sites"].add(b["site"]["value"])
    out: dict[str, list[WikiCandidate]] = {}
    for search, cands in acc.items():
        lst = []
        for qid, d in cands.items():
            art = d["article"]
            title = unquote(art.rsplit("/", 1)[-1]).replace("_", " ") if art else None
            lst.append(WikiCandidate(qid, d["rank"], d["label"], d["desc"], _fmt_employees(d["emp"]), sorted(d["sites"]), title))
        out[search] = sorted(lst, key=lambda c: c.rank)
    return out


def _employees_year(employees: str | None) -> int:
    m = re.search(r"\((\d{4})\)", employees or "")
    return int(m.group(1)) if m else 0


_CTX_STOP = set("""the a an and or of for in to with on at by from as is are be we our your you this that it its company companies corporation
inc ltd limited services solutions group global india indian international technologies technology software platform team teams
role work working experience years skills strong ability across within about into over more than will also other their they have has""".split())


def _ctx_words(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower()) if w not in _CTX_STOP}


def pick_candidate(name: str, domain: str | None, cands: list[WikiCandidate], context: str = "") -> WikiCandidate | None:
    """The candidate that is this company: website domain match when a domain is known, otherwise the same
    name with a company-like description (a longer name such as 'PwC Norway' only when it has its own
    Wikipedia article). Never a near-miss ('Capcom' for 'Capco')."""
    target = normalize_company(name)
    scored = []
    for c in cands:
        site_domains = {root_domain(domain_of(u)) for u in c.sites}
        domain_ok = bool(domain) and domain in site_domains
        lvl = name_match_level(c.label, target)
        company_like = bool(_COMPANY_WORDS.search(c.description))
        if domain and site_domains and not domain_ok:
            continue  # a company with a website, but not this one
        ok = domain_ok or (lvl == 2 and (company_like or c.employees)) or (lvl == 1 and company_like and c.article)
        if not ok:
            continue
        scored.append(((0 if domain_ok else 1), (0 if domain_ok else 2 - lvl), (0 if c.article else 1), (0 if c.employees else 1), c.rank, c))
    if not scored:
        return None
    scored.sort(key=lambda t: t[:5])
    if domain and scored[0][0] == 0:
        return scored[0][5]  # the website settles it
    name_tokens = set(normalize_company(name).split())
    ctx = _ctx_words(context) - name_tokens

    def support(c: WikiCandidate) -> int:
        return len((_ctx_words(c.description + " " + (c.article or "")) - name_tokens) & ctx)

    exact = [t[5] for t in scored if t[1] == 0]
    if len(exact) > 1:
        # Several companies share this name. The postings decide: the candidate whose Wikidata description shares
        # words with the job text wins; with no overlap, or a tie, decline rather than guess.
        ranked = sorted(exact, key=lambda c: (-support(c), c.rank))
        if support(ranked[0]) == 0 or (len(ranked) > 1 and support(ranked[0]) == support(ranked[1])):
            return None
        return ranked[0]
    best = scored[0][5]
    if best.article and "(" in best.article and support(best) == 0:
        return None  # 'Tide (transportation company)' for postings that never mention transport: the name is shared
    return best


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
        self.counters = {"text_hint": 0, "clearbit": 0, "rdap": 0, "sparql": 0, "wikipedia": 0, "sizes": 0, "discover": 0, "crosscheck": 0, "budget_skipped": 0}
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
        website = split_company_links(rec.get("website") or next((j.company_website for j in jobs if j.company_website), None))[0]
        website_source = _note_value(rec.get("notes"), "website-source") or ("posting" if website else None)
        size = rec.get("size") or next((j.company_size for j in jobs if j.company_size), None)
        size_source = rec.get("size_source") or next((j.company_size_source for j in jobs if j.company_size), None)
        industry = rec.get("industry") or next((j.company_industry for j in jobs if j.company_industry), None)
        linkedin = rec.get("linkedin") or next((j.company_linkedin for j in jobs if j.company_linkedin), None)
        board = self.boards_by_norm.get(cn)
        if board and not website:
            website = board.website
            website_source = "board" if website else website_source
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
        if not domain and not rec.get("website"):
            text = "\n".join((j.description or "")[:6000] for j in jobs[:3])
            for hint in domain_hints(text, cn)[:1]:
                domain, website, website_source = hint, f"https://{hint}", "posting text"
                self.counters["text_hint"] += 1
        if not domain and web_ok and not rec.get("website") and self.counters["clearbit"] < int(self.o.get("max_clearbit", 400)):
            self.counters["clearbit"] += 1
            d = clearbit_domain(self.http, name, self.o.get("country"))
            if d:
                domain, website, website_source = root_domain(d), f"https://{d}", "clearbit name lookup"
        if not board and domain:
            board = self.boards_by_domain.get(domain)
        # Domain age (cached 90 days).
        registered = rec.get("domain_registered")
        checked = rec.get("domain_checked_at")
        stale = not checked or datetime.fromisoformat(checked) < now - timedelta(days=90)
        if domain and (not registered or stale) and web_ok and self.counters["rdap"] < int(self.o.get("max_rdap", 400)):
            self.counters["rdap"] += 1
            got = rdap_registration(self.http, domain)
            if got is not None:
                registered, checked = got, now.isoformat()
        wikidata = rec.get("wikidata")  # company sizes are resolved in a separate, paced phase (see _resolve_sizes)
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
        notes = rec.get("notes") or ""
        if website and website_source and "website-source:" not in notes:
            notes = (notes + f" website-source:{website_source}").strip()
        self.store.upsert_company(
            cn, name=name, website=website, domain=domain, size=size, size_source=size_source, industry=industry,
            linkedin=linkedin, board_platform=board_platform, board_url=board_url, board_checked_at=bchecked,
            domain_registered=registered, domain_checked_at=checked, wikidata=wikidata, notes=notes or None,
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
            ws = _note_value(rec2.get("notes"), "website-source")
            if ws:
                j.extra["website_source"] = ws
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
        self._resolve_sizes(ordered)
        self.store.commit()
        c = self.counters
        self.log(f"[enrich] lookups: website-from-text={c['text_hint']} clearbit={c['clearbit']} rdap={c['rdap']} wikidata-batches={c['sparql']} wikipedia={c['wikipedia']} sizes-found={c['sizes']} board-discovery={c['discover']} cross-checks={c['crosscheck']}, companies past budget={c['budget_skipped']}, errors={errors}, {int(time.monotonic() - self._t0)}s")

    # ------------------------------------------------------------------ company size
    def _push_company(self, cn: str, jobs: list[Job]) -> None:
        rec = self.store.get_company(cn) or {}
        for j in jobs:
            j.company_website = rec.get("website") or j.company_website
            j.company_domain = rec.get("domain") or j.company_domain
            j.company_size = rec.get("size") or j.company_size
            j.company_size_source = rec.get("size_source") or j.company_size_source
            j.company_industry = rec.get("industry") or j.company_industry

    def _resolve_sizes(self, ordered: list[list[Job]]) -> None:
        """Company size (and a website / industry when still unknown) for every company that has none.

        Phase 1 sends company names in batches of 20 to the Wikidata query service (one request per batch).
        Phase 2 tries English Wikipedia one company at a time, slowly, for the names the batch did not settle.
        Results, including 'nothing found', are cached in the store for 90 days.
        """
        if self.o.get("no_web_lookups"):
            return
        todo: list[tuple[str, str, str | None, list[Job]]] = []
        now = datetime.now(tz=timezone.utc)
        for g in ordered:
            cn = g[0].company_norm
            rec = self.store.get_company(cn) or {}
            if rec.get("size") or rec.get("override"):
                continue
            marker = rec.get("wikidata") or ""
            if marker.startswith("none:"):
                try:  # 'none:2026-11-07' = looked up on that date and found nothing; try again after 90 days
                    if date.fromisoformat(marker[5:15]) > (now - timedelta(days=90)).date():
                        continue
                except (ValueError, TypeError):
                    pass
            elif marker and marker != "none":
                continue  # resolved to an entity before; it simply has no employee count
            name = rec.get("name") or g[0].company
            if len(name) < 3 or g[0].poster_type == "agency":
                continue
            todo.append((cn, name, rec.get("domain"), g))
        if not todo:
            return
        self.log(f"[enrich] company size: {len(todo)} companies to resolve via Wikidata/Wikipedia …")
        unresolved: list[tuple[str, str, str | None, list[Job]]] = []
        for i in range(0, len(todo), 20):
            batch = todo[i:i + 20]
            if not self._in_budget() or self.http.is_blocked("query.wikidata.org"):
                unresolved += batch
                continue
            self.counters["sparql"] += 1
            try:
                found = sparql_company_search(self.http, [t[1] for t in batch])
            except RateLimited as e:
                self.log(f"[enrich] {e}")
                found = {}
            except Exception as e:  # noqa: BLE001
                self.log(f"[enrich] wikidata batch failed: {type(e).__name__}: {str(e)[:100]}")
                found = {}
            for cn, name, domain, g in batch:
                context = " ".join((j.description or "")[:3000] for j in g[:2]) + " " + (g[0].company_industry or "")
                c = pick_candidate(name, domain, found.get(name) or [], context)
                if c is None:
                    unresolved.append((cn, name, domain, g))
                    continue
                self._record_size(cn, c.qid, c.employees, "wikidata", prefer_site(c.sites, self.o.get("country")), None, c.article)
                if c.article and (not c.employees or _employees_year(c.employees) < now.year - 2):
                    unresolved.append((cn, name, domain, g))  # the infobox may know, or know something newer
                self._push_company(cn, g)
        cap = int(self.o.get("max_wikipedia", 40))
        done = 0
        for cn, name, domain, g in unresolved:
            if done >= cap or not self._in_budget() or self.http.is_blocked("en.wikipedia.org"):
                break
            done += 1
            self.counters["wikipedia"] += 1
            try:
                w = wikipedia_company(self.http, name, domain, self.o.get("country"))
            except RateLimited as e:
                self.log(f"[enrich] {e}; remaining company-size lookups wait for the next run")
                break
            except Exception:  # noqa: BLE001
                w = WikiCompany()
            self._record_size(cn, w.qid, w.employees, w.employees_source or "wikipedia", w.website, w.industry, w.wiki_title)
            self._push_company(cn, g)
        self.store.commit()

    def _record_size(self, cn: str, qid: str | None, employees: str | None, source: str, website: str | None, industry: str | None, article: str | None) -> None:
        rec = self.store.get_company(cn) or {}
        fields: dict = {"wikidata": qid or f"none:{datetime.now(tz=timezone.utc).date().isoformat()}"}
        if employees and _employees_year(employees) >= _employees_year(rec.get("size")):
            if not rec.get("size"):
                self.counters["sizes"] += 1
            fields["size"], fields["size_source"] = employees, source
        site_ok, _ = split_company_links(website)
        if site_ok and not rec.get("website"):
            fields["website"] = site_ok
            fields["domain"] = root_domain(domain_of(site_ok))
            if "website-source:" not in (rec.get("notes") or ""):
                fields["notes"] = ((rec.get("notes") or "") + " website-source:wikipedia").strip()
        if industry and not rec.get("industry"):
            fields["industry"] = industry
        if article:
            fields["notes"] = ((rec.get("notes") or "") + f" wikipedia:{article}").strip()
        self.store.upsert_company(cn, **fields)
