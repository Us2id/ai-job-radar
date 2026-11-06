"""Is this an LLM / AI / ML job? Which role family, seniority and industry domains?"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import yaml

from ..models import Job

_FLAGS = re.IGNORECASE


def _load(name: str) -> dict:
    return yaml.safe_load((resources.files("ai_job_radar") / "data" / name).read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def keywords() -> dict:
    return _load("keywords.yaml")


@lru_cache(maxsize=1)
def domains_yaml() -> dict:
    return _load("domains.yaml")


@lru_cache(maxsize=1)
def _compiled() -> dict:
    kw = keywords()
    return {
        "title_include": [(re.compile(p["re"], _FLAGS), int(p["w"])) for p in kw["title_include"]],
        "title_exclude": [re.compile(p, _FLAGS) for p in kw["title_exclude"]],
        "desc_hard": [re.compile(p, _FLAGS) for p in kw["desc_hard"]],
        "desc_soft": [re.compile(p, _FLAGS) for p in kw["desc_soft"]],
        "role_families": [(name, re.compile(p, _FLAGS)) for name, p in kw["role_families"].items()],
        "seniority": [(name, re.compile(p, _FLAGS)) for name, p in kw["seniority"].items()],
    }


def search_terms(deep: bool = False, extra: list[str] | None = None) -> list[str]:
    kw = keywords()
    terms = list(kw.get("search_terms", []))
    if deep:
        terms += [t for t in kw.get("deep_search_terms", []) if t not in terms]
    for t in extra or []:
        if t and t not in terms:
            terms.append(t)
    return terms


@dataclass
class Relevance:
    score: int
    terms: list[str] = field(default_factory=list)
    excluded_by: str | None = None
    title_hit: bool = False


def relevance(title: str, description: str = "") -> Relevance:
    c = _compiled()
    title = title or ""
    for rx in c["title_exclude"]:
        if rx.search(title):
            # A sales title that is really an AI engineering title is rare; keep the rule simple.
            return Relevance(0, [], excluded_by=rx.pattern)
    title_score = 0
    terms: list[str] = []
    for rx, w in c["title_include"]:
        m = rx.search(title)
        if m:
            terms.append(m.group(0).strip())
            title_score = max(title_score, w)
    found: list[str] = []
    hard = soft = mentions = 0
    if description:
        for rx in c["desc_hard"]:
            hits = rx.findall(description)
            if hits:
                hard += 1
                mentions += min(3, len(hits))
                m = rx.search(description)
                found.append(m.group(0).strip() if m else rx.pattern)
        for rx in c["desc_soft"]:
            hits = rx.findall(description)
            if hits:
                soft += 1
                mentions += min(3, len(hits))
                m = rx.search(description)
                found.append(m.group(0).strip() if m else rx.pattern)
    if hard >= 2:
        desc_score = min(75, 56 + 5 * (hard - 2) + 2 * soft + min(6, mentions // 3))
    elif hard == 1:
        desc_score = min(60, 40 + 4 * soft + min(6, mentions // 2))
    elif soft:
        desc_score = min(45, 25 + 4 * soft)
    else:
        desc_score = 0
    score = max(title_score, desc_score)
    if title_score and (hard or soft):
        score = min(100, score + min(8, 2 * (hard + soft)))
    seen: set[str] = set()
    all_terms = []
    for t in terms + found:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            all_terms.append(t)
    return Relevance(int(score), all_terms[:12], title_hit=bool(title_score))


def role_family(title: str) -> str:
    for name, rx in _compiled()["role_families"]:
        if rx.search(title or ""):
            return name
    return "other"


def seniority(title: str, description: str = "") -> str:
    for name, rx in _compiled()["seniority"]:
        if rx.search(title or ""):
            return name
    d = (description or "")[:1500]
    m = re.search(r"(?i)\b(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)", d)
    if m:
        lo = int(m.group(1))
        if lo >= 8:
            return "senior"
        if lo <= 1:
            return "junior"
    return "mid"


# ------------------------------------------------------------------------------- domains
@dataclass
class DomainMatcher:
    key: str
    strong: list[re.Pattern]
    weak: list[re.Pattern] = field(default_factory=list)
    literal: bool = False

    def evidence(self, head: str, body: str) -> tuple[float, list[str]]:
        """Score and example matches. Head = title/industry/company blurb (weighs double)."""
        score = 0.0
        why: list[str] = []
        for rx in self.strong:
            h = rx.findall(head)
            b = rx.findall(body)
            if h:
                score += 2.0
                why.append(_first(rx, head))
            if b:
                score += min(3, len(b)) * 1.0
                if not h:
                    why.append(_first(rx, body))
        for rx in self.weak:
            h = rx.findall(head)
            b = rx.findall(body)
            if h:
                score += 1.0
                why.append(_first(rx, head))
            if b:
                score += min(2, len(b)) * 0.5
                if not h:
                    why.append(_first(rx, body))
        return score, why[:4]


def _first(rx: re.Pattern, text: str) -> str:
    m = rx.search(text)
    if not m:
        return rx.pattern
    g = m.group(0)
    return g if isinstance(g, str) else str(g)


@lru_cache(maxsize=1)
def taxonomy_matchers() -> dict[str, DomainMatcher]:
    out = {}
    for key, spec in (domains_yaml().get("domains") or {}).items():
        out[key] = DomainMatcher(key, [re.compile(p, _FLAGS) for p in spec.get("strong", [])],
                                 [re.compile(p, _FLAGS) for p in spec.get("weak", [])])
    return out


@lru_cache(maxsize=1)
def domain_aliases() -> dict[str, str]:
    out = {}
    for key, spec in (domains_yaml().get("domains") or {}).items():
        out[key.lower()] = key
        for a in spec.get("aliases", []):
            out[str(a).lower()] = key
    return out


GENERIC_WORDS = {
    "environment", "environments", "product", "products", "system", "systems", "platform", "service", "services",
    "solution", "solutions", "business", "team", "company", "technology", "engineering", "software", "data", "model",
    "models", "project", "application", "customer", "user", "work", "experience", "design", "development",
    "management", "operations", "analytics", "cloud", "mobile", "web", "network", "quality", "process", "research",
    "science", "market", "global", "digital", "enterprise", "industry", "field", "domain", "sector",
}


class DomainSpec:
    """Parses '--domain healthcare,climate' (or 'tech' for none) into matchers."""

    def __init__(self, spec: str | None):
        self.raw = (spec or "").strip()
        none_words = {str(w).lower() for w in domains_yaml().get("none", [])}
        self.keys: list[str] = []
        self.matchers: list[DomainMatcher] = []
        words = [w.strip().lower() for w in re.split(r"[,;/+]|\band\b", self.raw) if w.strip()]
        for w in words:
            if w in none_words:
                continue
            key = domain_aliases().get(w)
            if key:
                if key not in self.keys:
                    self.keys.append(key)
                    self.matchers.append(taxonomy_matchers()[key])
            else:
                stem = re.escape(w[:-1] if w.endswith("s") and len(w) > 4 else w)
                self.keys.append(w)
                self.matchers.append(DomainMatcher(w, [re.compile(r"\b" + stem + r"\w*", _FLAGS)], [], literal=True))

    @property
    def active(self) -> bool:
        return bool(self.matchers)

    def describe(self) -> str:
        return ", ".join(self.keys) if self.keys else "any (no domain filter)"

    def matches(self, job: Job) -> tuple[bool, list[str]]:
        """True when the job is in at least one requested domain; also returns matched words."""
        if not self.active:
            return True, []
        head = " ".join(x for x in [job.title, job.company, job.company_industry or "", job.company_description or ""] if x)
        body = job.description or ""
        why: list[str] = []
        ok = False
        for m in self.matchers:
            score, ev = m.evidence(head, body)
            need = 2.0
            if m.literal:
                need = 3.0 if m.key in GENERIC_WORDS else 1.0
            if score >= need:
                ok = True
                why += ev
        return ok, why


def tag_domains(job: Job) -> list[str]:
    """All taxonomy domains a job plausibly belongs to (for the 'Domain tags' column)."""
    head = " ".join(x for x in [job.title, job.company_industry or "", job.company_description or ""] if x)
    body = (job.description or "")[:8000]
    scored = []
    for key, m in taxonomy_matchers().items():
        score, _ = m.evidence(head, body)
        if score >= 2.0:
            scored.append((score, key))
    return [k for _s, k in sorted(scored, reverse=True)[:4]]
