"""Legitimacy score: a 0-100 number with reasons, never a verdict."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources

import yaml

from ..models import Job
from ..text import domain_of, normalize_title, root_domain
from .textmodel import fraud_probability

FREE_MAIL = re.compile(r"(?i)@(gmail|yahoo|hotmail|outlook|live|rediffmail|ymail|aol|protonmail|proton|icloud|mail)\.")


@lru_cache(maxsize=1)
def cfg() -> dict:
    return yaml.safe_load((resources.files("ai_job_radar") / "data" / "scam_patterns.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def compiled() -> dict:
    c = cfg()
    return {
        "scam": [(s, [re.compile(p, re.I) for p in s.get("patterns", [])]) for s in c["scam"]],
        "evergreen": [re.compile(p, re.I) for p in c["ghost"]["evergreen_patterns"]],
        "generic": [re.compile(p, re.I) for p in c["company"]["generic_patterns"]],
        "agency": [re.compile(p, re.I) for p in c["company"]["agency_patterns"]],
        "brands": [b.lower() for b in c["company"]["big_brands"]],
    }


def poster_type(company: str) -> str:
    name = company or ""
    for rx in compiled()["agency"]:
        if rx.search(name):
            return "agency"
    return "unknown"


def _years_since(date_s: str | None, now: datetime) -> float | None:
    if not date_s or date_s == "unknown":
        return None
    try:
        d = datetime.fromisoformat(date_s[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - d).days / 365.25


_NEGATION_RE = re.compile(
    r"(?i)\b(never|not|no|none|neither|nor|won'?t|wouldn'?t|don'?t|doesn'?t|didn'?t|cannot|can'?t|without|free of|"
    r"beware|fraud\w*|scam\w*|legitimate|caution|alert|red flag|warning|bogus|fake|impersonat\w*|phishing|suspicious|"
    r"is free|are free|no cost|at no|any kind of fee|any fee)\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]")


def _negated(text: str, start: int) -> bool:
    """True when the sentence leading up to a match says the opposite: 'we will never ask you to pay a fee'."""
    before = text[max(0, start - 220):start]
    sentence = _SENTENCE_SPLIT_RE.split(before)[-1]
    return bool(_NEGATION_RE.search(sentence))


def _scam_hits(rxs: list, text: str) -> tuple[list[str], bool]:
    """Patterns that really fired, and whether any match was inside an anti-fraud disclaimer instead."""
    hits: list[str] = []
    negated = False
    for rx in rxs:
        real = False
        for m in rx.finditer(text):
            if _negated(text, m.start()):
                negated = True
                continue
            real = True
            break
        if real:
            hits.append(rx.pattern)
    return hits, negated


def score_all(jobs: list[Job], now: datetime) -> None:
    """Scores every job in place. Needs the whole list to spot one title posted in many cities."""
    c, k = cfg(), compiled()
    spread: dict[tuple[str, str], set[str]] = defaultdict(set)
    for j in jobs:
        spread[(j.company_norm, normalize_title(j.title))].add((j.city or j.location_raw or "").lower())
    for j in jobs:
        score = 58
        flags: list[str] = []
        notes: list[str] = []
        hard_cap = 100
        text = f"{j.title}\n{j.description or ''}"
        pos = c["positive"]

        # ---- positives
        if j.is_ats or j.on_company_board == "yes":
            score += pos["on_ats_or_board"]
            notes.append("listed on the employer's own job board")
        if j.req_id:
            score += pos["has_req_id"]
        if j.posted_trusted:
            score += pos["trusted_date"]
        if j.company_board_url or (j.extra.get("known_company")):
            score += pos["known_company"]
        yrs = _years_since(j.domain_registered, now)
        if yrs is not None:
            days = yrs * 365.25
            applied = False
            guessed = str(j.extra.get("website_source", "")).startswith("clearbit")
            for lim, w in pos["domain_too_new_days"]:
                if days < lim:
                    if guessed:
                        notes.append(f"website guessed from the company name; that domain is only {int(days)} days old")
                    else:
                        score += w
                        flags.append(f"company domain registered only {int(days)} days ago")
                    applied = True
                    break
            if not applied:
                for lim, w in pos["domain_age_years"]:
                    if yrs >= lim:
                        score += w
                        notes.append(f"domain registered {int(yrs)}+ years ago")
                        break
        apply_dom = root_domain(domain_of(j.apply_url or j.url))
        if j.company_domain and apply_dom and j.company_domain == apply_dom:
            score += pos["company_website_matches_apply_domain"]
        if j.company_linkedin:
            score += pos["linkedin_page"]
        if j.company_size:
            score += pos["size_known"]

        # ---- scam signals
        anti_fraud_notice = False
        for s, rxs in k["scam"]:
            hits, negated = _scam_hits(rxs, text)
            anti_fraud_notice = anti_fraud_notice or negated
            if s["id"] == "free_email_apply" and any(FREE_MAIL.search(e) for e in j.emails):
                hits.append("emails field")
            if hits:
                score += int(s["weight"])
                flags.append(s["reason"])
                if s.get("hard_cap") is not None:
                    hard_cap = min(hard_cap, int(s["hard_cap"]))

        if anti_fraud_notice:
            score += int(pos.get("anti_fraud_notice", 0))
            notes.append("carries an anti-fraud notice (says it never asks for fees or personal data)")

        # ---- learned text signal
        tm = c.get("text_model") or {}
        if tm and (j.description or "") and len(j.description) > 120:
            p = fraud_probability(f"{j.title}\n{j.description[:6000]}")
            if p is not None:
                j.extra["scam_text_prob"] = round(p, 3)
                for thr, w in sorted(tm.get("thresholds", []), key=lambda x: -x[0]):
                    if p >= thr:
                        score += int(w)
                        flags.append(f"{tm.get('reason', 'text resembles known scam postings')} (model {p:.2f})")
                        break

        # ---- company name signals
        name = j.company or ""
        if not name.strip():
            score += c["company"]["generic_weight"]
            flags.append("no company name")
        elif any(rx.search(name) for rx in k["generic"]):
            score += c["company"]["generic_weight"]
            flags.append("company not named (confidential / 'leading MNC')")
        if j.poster_type == "unknown":
            j.poster_type = "direct" if j.is_ats else poster_type(name)
        if j.poster_type == "agency":
            score += c["company"]["agency_weight"]
            notes.append("posted by a staffing / consulting firm")
        if any(b in name.lower() for b in k["brands"]) and any(f for f in flags if f not in ("no company name",)) and not j.is_ats:
            score += c["company"]["brand_impersonation_weight"]
            flags.append("big-brand name combined with scam signals")

        # ---- ghost signals
        g = c["ghost"]
        if any(rx.search(text) for rx in k["evergreen"]):
            score += int(g["evergreen_weight"])
            flags.append("evergreen / talent-pool wording")
        if j.days_open is not None:
            for lim, w in sorted(g["days_open_thresholds"], key=lambda x: -x[0]):
                if j.days_open >= lim:
                    score += int(w)
                    flags.append(f"open for {j.days_open} days")
                    break
        if j.reposted:
            score += int(g["reposted_weight"])
            flags.append("reposted (" + str(j.extra.get("repost_reason") or "date refreshed since first seen") + ")")
        n_loc = max(len(spread[(j.company_norm, normalize_title(j.title))]), len(j.extra.get("locations") or []))
        if n_loc >= int(g["many_locations_threshold"]):
            score += int(g["many_locations_weight"])
            flags.append(f"same title posted in {n_loc} locations")
        if j.on_company_board == "no":
            score += int(g["not_on_company_board_weight"])
            flags.append("not found on the employer's own job board")
        if j.times_seen > 1 and not j.reposted:
            notes.append(f"seen in {j.times_seen} runs")

        score = max(0, min(hard_cap, score))
        j.legit_score = int(score)
        for lim, label in c["levels"]:
            if score >= lim:
                j.risk_level = label
                break
        for f in flags:
            if f not in j.flags:
                j.flags.append(f)
        j.verification_notes = "; ".join(notes)
