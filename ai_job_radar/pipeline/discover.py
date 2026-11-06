"""Find a company's public ATS board from its name (and website) by probing well-known URL shapes."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from rapidfuzz import fuzz

from ..http import Http
from ..sources.base import Board
from ..text import domain_of, normalize_company, root_domain

PLATFORMS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "bamboohr")


def slug_candidates(name: str, website: str | None = None) -> list[tuple[str, bool]]:
    """(slug, strong) pairs. 'strong' slugs are specific enough to trust without a name check."""
    raw = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    raw = re.sub(r"\s+", " ", raw).strip()
    base = normalize_company(name)
    out: list[tuple[str, bool]] = []

    def add(s: str, strong: bool) -> None:
        s = s.strip("-")
        if s and len(s) >= 3 and s not in [x for x, _ in out]:
            out.append((s, strong))

    if website:
        d = root_domain(domain_of(website))
        if d:
            add(d.split(".")[0], True)
    add(raw.replace(" ", ""), True)
    add(raw.replace(" ", "-"), True)
    add(base.replace(" ", ""), len(base) >= 5)
    add(base.replace(" ", "-"), len(base) >= 5)
    words = base.split()
    if len(words) > 1 and len(words[0]) >= 5:
        add(words[0], False)
    return out[:7]


def _names_match(a: str, b: str) -> bool:
    na, nb = normalize_company(a), normalize_company(b)
    if not na or not nb:
        return False
    return na == nb or fuzz.token_set_ratio(na, nb) >= 80 or fuzz.partial_ratio(na, nb) >= 90


def _looks_like_test(titles: list[str]) -> bool:
    t = " ".join(titles[:3]).lower()
    return bool(re.search(r"\btest\b|\buat\b|\bdummy\b|\bsample\b", t))


def probe(http: Http, platform: str, slug: str, company: str, strong: bool) -> Board | None:
    """A board is accepted only when it has at least one real posting and (where the API tells us)
    the account name matches the company; otherwise the slug must be specific to the company."""
    try:
        if platform == "greenhouse":
            r = http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=8)
            if r.status_code == 200 and _names_match(r.json().get("name", ""), company):
                return Board(company, platform, slug, f"https://boards.greenhouse.io/{slug}")
        elif platform == "lever":
            r = http.get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=5", timeout=8)
            if r.status_code == 200 and isinstance(r.json(), list) and r.json() and strong:
                if not _looks_like_test([j.get("text", "") for j in r.json()]):
                    return Board(company, platform, slug, f"https://jobs.lever.co/{slug}")
        elif platform == "ashby":
            r = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=8)
            if r.status_code == 200 and r.json().get("jobs") and strong:
                if not _looks_like_test([j.get("title", "") for j in r.json()["jobs"]]):
                    return Board(company, platform, slug, f"https://jobs.ashbyhq.com/{slug}")
        elif platform == "smartrecruiters":
            r = http.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=5", timeout=8)
            if r.status_code == 200 and int(r.json().get("totalFound") or 0) > 0:
                content = r.json().get("content") or []
                c = (content[0].get("company") if content else None) or {}
                if _names_match(c.get("name", ""), company) and not _looks_like_test([j.get("name", "") for j in content]):
                    return Board(company, platform, slug, f"https://careers.smartrecruiters.com/{slug}")
        elif platform == "workable":
            r = http.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=8)
            if r.status_code == 200 and _names_match(r.json().get("name", ""), company) and r.json().get("jobs"):
                return Board(company, platform, slug, f"https://apply.workable.com/{slug}/")
        elif platform == "recruitee":
            r = http.get(f"https://{slug}.recruitee.com/api/offers/", timeout=8)
            if r.status_code == 200 and r.json().get("offers"):
                offers = r.json()["offers"]
                cname = offers[0].get("company_name", "")
                if (cname and _names_match(cname, company)) or strong:
                    return Board(company, platform, slug, f"https://{slug}.recruitee.com/")
        elif platform == "bamboohr":
            r = http.get(f"https://{slug}.bamboohr.com/careers/list", timeout=8)
            if r.status_code == 200 and r.json().get("result") and strong:
                if not _looks_like_test([j.get("jobOpeningName", "") for j in r.json()["result"]]):
                    return Board(company, platform, slug, f"https://{slug}.bamboohr.com/careers")
    except Exception:  # noqa: BLE001 - network / JSON errors mean "not found"
        return None
    return None


def discover_one(http: Http, name: str, website: str | None = None, platforms=PLATFORMS, max_slugs: int = 4) -> Board | None:
    """Try the most specific slugs first; the platforms for one slug are probed concurrently."""
    for slug, strong in slug_candidates(name, website)[:max_slugs]:
        with ThreadPoolExecutor(max_workers=len(platforms)) as ex:
            results = list(ex.map(lambda p: probe(http, p, slug, name, strong), platforms))
        for b in results:
            if b:
                b.website = website
                b.origin = "discovered"
                return b
    return None


def discover_many(http: Http, companies: list[tuple[str, str | None]], workers: int = 12, log=print) -> dict[str, Board | None]:
    out: dict[str, Board | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(discover_one, http, n, w): n for n, w in companies}
        done = 0
        for f in as_completed(futs):
            n = futs[f]
            out[n] = f.result()
            done += 1
            if done % 25 == 0:
                log(f"[discover] {done}/{len(companies)} companies probed, {sum(1 for v in out.values() if v)} boards found")
    return out
