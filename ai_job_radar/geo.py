"""World gazetteer. Resolves any country from free text and parses job location strings.

Data: GeoNames (CC BY 4.0) for cities and countries, pycountry for subdivisions (states,
provinces), plus a small alias file. Nothing here is specific to one country.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from importlib import resources

import pycountry
import yaml

REMOTE_RE = re.compile(r"(?i)\b(remote|work[- ]from[- ]home|wfh|telecommut\w*|distributed|home[- ]based|anywhere|worldwide|global)\b")
HYBRID_RE = re.compile(r"(?i)\bhybrid\b")
ONSITE_RE = re.compile(r"(?i)\b(on-?site|in-?office|office-based)\b")
GLOBAL_REMOTE_RE = re.compile(
    r"remote[^.\n]{0,30}\b(worldwide|anywhere|globally|global|any country|any location|all countries)\b|"
    r"\b(worldwide|anywhere|globally|global)\b[^.\n]{0,20}\bremote\b|work from anywhere|anywhere in the world|"
    r"open to (candidates|applicants|anyone) (worldwide|globally|anywhere|from any country|in any country)|"
    r"timezone[- ]agnostic|time[- ]zone[- ]agnostic|no location restriction|fully remote \(global\)|location[- ]agnostic"
)
SPLIT_RE = re.compile(r"\s*(?:,|;|\||/|•|·|\(|\)|\[|\]|\bor\b|\band\b|&| - | – | — |:)\s*")
STRIP_RE = re.compile(
    r"(?i)^(greater |metro |metropolitan |city of |remote[ -]+|hybrid[ -]+)|"
    r"( area| metropolitan (region|area)| metro area| region| urban| rural| district| city| metro| office| hq| headquarters)$"
)
NOISE = {
    "remote", "hybrid", "onsite", "on-site", "on site", "in office", "in-office", "work from home", "wfh", "anywhere",
    "worldwide", "global", "flexible", "multiple locations", "various", "other", "n/a", "na", "tbd", "home", "home office",
    "any", "all", "nationwide", "telecommute", "distributed", "office", "hq", "headquarters", "locations", "location",
}


def ascii_fold(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


@dataclass
class Loc:
    raw: str = ""
    city: str | None = None
    region: str | None = None
    country: str | None = None  # ISO2
    country_name: str | None = None
    work_mode: str = "unknown"  # onsite | hybrid | remote | remote-global | unknown
    remote: bool = False
    matched: list[str] = field(default_factory=list)


class Gazetteer:
    def __init__(self) -> None:
        pkg = resources.files("ai_job_radar") / "data"
        with gzip.open(pkg / "geo.json.gz", "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        extras = yaml.safe_load((pkg / "geo_aliases.yaml").read_text(encoding="utf-8")) or {}
        self.countries: dict[str, dict] = data["countries"]
        self.alias: dict[str, str] = {}
        for cc, c in self.countries.items():
            self.alias[c["name"].lower()] = cc
            self.alias[cc.lower()] = cc
            self.alias[c["iso3"].lower()] = cc
        for c in pycountry.countries:
            for attr in ("name", "official_name", "common_name"):
                v = getattr(c, attr, None)
                if v and c.alpha_2 in self.countries:
                    self.alias.setdefault(v.lower(), c.alpha_2)
        for k, v in (extras.get("countries") or {}).items():
            self.alias[str(k).lower()] = v
        # cities: name -> [(cc, pop, admin1, canonical)]
        self.cities: dict[str, list[tuple[str, int, str, str]]] = {}
        for name, cc, pop, admin1, canonical in data["cities"]:
            self.cities.setdefault(name, []).append((cc, pop, admin1, canonical))
        for k, (canonical, cc) in (extras.get("cities") or {}).items():
            self.cities[str(k).lower()] = [(cc, 10**9, "", canonical)]
        # subdivisions: name -> (cc, code); code -> [cc]
        self.subdiv: dict[str, list[tuple[str, str]]] = {}
        self.subdiv_code: dict[str, list[tuple[str, str]]] = {}
        for s in pycountry.subdivisions:
            if s.country_code not in self.countries:
                continue
            code = s.code.split("-", 1)[-1]
            plain = ascii_fold(s.name)
            for key in {s.name.lower(), plain.lower()}:
                lst = self.subdiv.setdefault(key, [])
                if (s.country_code, plain) not in lst:
                    lst.append((s.country_code, plain))
            self.subdiv_code.setdefault(code.upper(), []).append((s.country_code, plain))
        for k, v in (extras.get("regions") or {}).items():
            self.subdiv[str(k).lower()] = [(v, str(k).title())]
        self.global_words = [w.lower() for w in extras.get("global_words", [])]
        self.demonyms = {k: [w.lower() for w in v] for k, v in (extras.get("demonyms") or {}).items()}
        self.continent_words = {k: [w.lower() for w in v] for k, v in (extras.get("continents") or {}).items()}
        self.region_words = {w for ws in self.continent_words.values() for w in ws}

    # ------------------------------------------------------------------ countries
    def resolve_country(self, text: str | None) -> tuple[str, str] | None:
        """'india' / 'IN' / 'IND' / 'U.S.' -> ('IN', 'India'). None when unknown."""
        if not text:
            return None
        t = text.strip().lower().strip(".")
        cc = self.alias.get(t) or self.alias.get(t + ".")
        if cc:
            return cc, self.countries[cc]["name"]
        try:
            hits = pycountry.countries.search_fuzzy(text.strip())
        except LookupError:
            return None
        if hits and hits[0].alpha_2 in self.countries:
            cc = hits[0].alpha_2
            return cc, self.countries[cc]["name"]
        return None

    def country_name(self, cc: str | None) -> str | None:
        return self.countries.get(cc or "", {}).get("name")

    def continent(self, cc: str | None) -> str | None:
        return self.countries.get(cc or "", {}).get("continent")

    def country_words(self, cc: str) -> list[str]:
        words = set(self.demonyms.get(cc, []))
        name = self.country_name(cc)
        if name:
            words.add(name.lower())
        words.add(cc.lower())
        for alias, c in self.alias.items():
            if c == cc and len(alias) > 3:
                words.add(alias)
        return sorted(words, key=len, reverse=True)

    # ------------------------------------------------------------------ locations
    def parse_location(self, raw: str | None) -> Loc:
        loc = Loc(raw=(raw or "").strip())
        s = loc.raw
        if not s:
            return loc
        low = s.lower()
        if HYBRID_RE.search(low):
            loc.work_mode = "hybrid"
        elif ONSITE_RE.search(low):
            loc.work_mode = "onsite"
        if REMOTE_RE.search(low):
            loc.remote = True
            loc.work_mode = "remote-global" if any(w in low for w in self.global_words) else "remote"

        tokens = [ascii_fold(t.strip(" .")) for t in SPLIT_RE.split(s) if t and t.strip(" .")]
        recs: list[dict] = []
        for tok in tokens:
            tl = tok.lower()
            tl2 = STRIP_RE.sub("", tl).strip(" .")
            if tl in NOISE or tl2 in NOISE or not tl2:
                continue
            if tl in self.region_words or tl2 in self.region_words:
                loc.matched.append(f"regionword:{tok}")
                continue
            rec = {"tok": tok, "cc": None, "sd": None, "cities": [], "code": None}
            if tok.isupper() and 2 <= len(tok) <= 3:
                rec["code"] = tok
            else:
                rec["cc"] = self.alias.get(tl) or self.alias.get(tl2)
            rec["sd"] = self.subdiv.get(tl) or self.subdiv.get(tl2)
            hits = self.cities.get(tl) or self.cities.get(tl2) or []
            if not hits and not rec["cc"] and not rec["sd"] and " " in tl2:
                # "bengaluru karnataka" written without separators
                parts = tl2.split()
                for i in range(len(parts) - 1, 0, -1):
                    hits = self.cities.get(" ".join(parts[:i])) or []
                    if hits:
                        rest = " ".join(parts[i:])
                        rec["cc"] = rec["cc"] or self.alias.get(rest)
                        rec["sd"] = rec["sd"] or self.subdiv.get(rest)
                        break
            rec["cities"] = list(hits)
            if rec["cc"] or rec["sd"] or rec["cities"] or rec["code"]:
                recs.append(rec)
                loc.matched.append(tok)

        all_cities = [h for r in recs for h in r["cities"]]
        codes = [r["code"] for r in recs if r["code"]]

        # Every token votes for countries; a token that is both a city and a state of the
        # same country counts once (max, not sum). Explicit country names weigh most.
        votes: dict[str, float] = defaultdict(float)
        order: list[str] = []
        for r in recs:
            tv: dict[str, float] = {}

            def vote(cc: str, w: float) -> None:
                if cc not in tv:
                    order.append(cc)
                tv[cc] = max(tv.get(cc, 0.0), w)

            if r["cc"]:
                vote(r["cc"], 2.0 if r["sd"] else 3.0)
            for cc, _name in r["sd"] or []:
                vote(cc, 1.0)
            for h in r["cities"]:
                vote(h[0], 0.5 + min(1.5, math.log10(max(h[1], 10)) / 6.0))
            if r["code"]:
                code = r["code"]
                for h in all_cities:
                    if h[2].upper() == code:
                        vote(h[0], 1.5)
                cc = self.alias.get(code.lower())
                if cc:
                    vote(cc, 0.9)
                for cc, _name in self.subdiv_code.get(code, []):
                    vote(cc, 0.8)
            for cc, w in tv.items():
                votes[cc] += w
        country = None
        if votes:
            best = max(votes.values())
            country = next(cc for cc in order if votes[cc] == best)

        if all_cities:
            inside = [h for h in all_cities if country is None or h[0] == country]
            if inside:
                loc.city = max(inside, key=lambda h: h[1])[3]
        for r in recs:
            for cc, name in r["sd"] or []:
                if country is None or cc == country:
                    loc.region = name
                    break
            if loc.region:
                break
        if loc.region is None and country:
            for code in codes:
                for cc, name in self.subdiv_code.get(code, []):
                    if cc == country:
                        loc.region = name
                        break
                if loc.region:
                    break
        if country:
            loc.country = country
            loc.country_name = self.country_name(country)
        if loc.work_mode == "unknown" and (loc.city or loc.country):
            loc.work_mode = "onsite"
        return loc

    # ------------------------------------------------------------------ remote eligibility
    def remote_mentions(self, text: str | None, cc: str, location_text: str | None = None) -> str | None:
        """Why a remote posting looks open to candidates in ``cc`` (or None).

        Bare words like 'worldwide' count only in the short location text; in a description they must
        appear in a remote context ('remote, worldwide', 'work from anywhere'), since 'global company'
        and 'customers worldwide' are everywhere.
        """
        if not text and not location_text:
            return None
        loc = (location_text or "").lower()
        low = (text or "").lower()
        for w in self.global_words:
            if loc and re.search(r"\b" + re.escape(w) + r"\b", loc):
                return f"location says '{w}'"
        m = GLOBAL_REMOTE_RE.search(low)
        if m:
            return f"says '{m.group(0)[:40]}'"
        both = f"{loc}\n{low}"
        for w in self.country_words(cc):
            if len(w) <= 2:
                continue
            if re.search(r"\b" + re.escape(w) + r"\b", both):
                return f"mentions '{w}'"
        cont = self.continent(cc)
        for w in self.continent_words.get(cont or "", []):
            if re.search(r"\b" + re.escape(w) + r"\b", both):
                return f"mentions region '{w}'"
        return None


_GAZ: Gazetteer | None = None


def gazetteer() -> Gazetteer:
    global _GAZ
    if _GAZ is None:
        _GAZ = Gazetteer()
    return _GAZ


# JobSpy (Indeed / Glassdoor) country names, keyed by ISO2. Others are unsupported there.
JOBSPY_COUNTRIES = {
    "AR": "argentina", "AU": "australia", "AT": "austria", "BH": "bahrain", "BE": "belgium", "BR": "brazil",
    "CA": "canada", "CL": "chile", "CN": "china", "CO": "colombia", "CR": "costa rica", "CZ": "czech republic",
    "DK": "denmark", "EC": "ecuador", "EG": "egypt", "FI": "finland", "FR": "france", "DE": "germany",
    "GR": "greece", "HK": "hong kong", "HU": "hungary", "IN": "india", "ID": "indonesia", "IE": "ireland",
    "IL": "israel", "IT": "italy", "JP": "japan", "KW": "kuwait", "LU": "luxembourg", "MY": "malaysia",
    "MX": "mexico", "MA": "morocco", "NL": "netherlands", "NZ": "new zealand", "NG": "nigeria", "NO": "norway",
    "OM": "oman", "PK": "pakistan", "PA": "panama", "PE": "peru", "PH": "philippines", "PL": "poland",
    "PT": "portugal", "QA": "qatar", "RO": "romania", "SA": "saudi arabia", "SG": "singapore",
    "ZA": "south africa", "KR": "south korea", "ES": "spain", "SE": "sweden", "CH": "switzerland",
    "TW": "taiwan", "TH": "thailand", "TR": "türkiye", "UA": "ukraine", "AE": "united arab emirates",
    "GB": "uk", "US": "usa", "UY": "uruguay", "VE": "venezuela", "VN": "vietnam",
}

# Adzuna country codes (their API path segment).
ADZUNA_COUNTRIES = {"GB": "gb", "US": "us", "AT": "at", "AU": "au", "BE": "be", "BR": "br", "CA": "ca", "CH": "ch",
                    "DE": "de", "ES": "es", "FR": "fr", "IN": "in", "IT": "it", "MX": "mx", "NL": "nl", "NZ": "nz",
                    "PL": "pl", "SG": "sg", "ZA": "za"}
