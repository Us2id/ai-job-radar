"""Salary strings -> approximate annual figure for sorting. Static FX rates (approximate, documented)."""
from __future__ import annotations

import re

# Approximate rates to INR, kept static on purpose; the column is for sorting, not payroll.
TO_INR = {"INR": 1.0, "USD": 84.0, "EUR": 91.0, "GBP": 107.0, "SGD": 63.0, "AED": 23.0, "CAD": 61.0, "AUD": 55.0, "JPY": 0.56, "CHF": 95.0}
_CUR = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD", "usd": "USD", "us$": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
        "sgd": "SGD", "s$": "SGD", "aed": "AED", "cad": "CAD", "c$": "CAD", "aud": "AUD", "a$": "AUD", "chf": "CHF", "jpy": "JPY", "¥": "JPY"}
_NUM = r"(\d[\d,]*(?:\.\d+)?)\s*(k|l|lacs?|lakhs?|lpa|cr|crores?|m|mn|million)?"
_RANGE_RE = re.compile(r"(?i)(₹|rs\.?|inr|us\$|usd|\$|€|eur|£|gbp|sgd|s\$|aed|cad|c\$|aud|a\$|chf|jpy|¥)?\s*" + _NUM + r"(?:\s*(?:-|–|to)\s*(?:₹|rs\.?|inr|\$|€|£)?\s*" + _NUM + r")?\s*(lpa|lacs? p\.?a\.?|lakhs? p\.?a\.?|per annum|p\.?a\.?|/\s*year|per year|yearly|annual(?:ly)?|/\s*month|per month|monthly|/\s*hour|per hour|hourly|/\s*day|per day|daily)?")


def _mult(unit: str | None) -> float:
    u = (unit or "").lower()
    if u in ("k",):
        return 1e3
    if u in ("l", "lac", "lacs", "lakh", "lakhs", "lpa"):
        return 1e5
    if u in ("cr", "crore", "crores"):
        return 1e7
    if u in ("m", "mn", "million"):
        return 1e6
    return 1.0


def parse_salary(s: str | None) -> dict | None:
    """Returns {currency, low, high, period, annual_low, annual_high, annual_inr_mid} or None."""
    if not s or re.search(r"(?i)not disclosed|undisclosed|competitive|negotiable|unpaid", s):
        return None
    m = _RANGE_RE.search(s)
    if not m or not m.group(2):
        return None
    cur_tok, lo, lo_u, hi, hi_u, per = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    cur = _CUR.get((cur_tok or "").lower(), None)
    text = s.lower()
    if not cur:
        if re.search(r"lpa|lakh|lacs?|crore|\bcr\b|₹|inr|rs\b", text):
            cur = "INR"
        elif re.search(r"usd|\$", text):
            cur = "USD"
        else:
            cur = "INR" if re.search(r"p\.?a\.?|per annum", text) else None
    if not cur:
        return None
    unit_lo = lo_u or ("lpa" if (per or "").lower() == "lpa" else None)
    unit_hi = hi_u or lo_u or unit_lo
    try:
        low = float(lo.replace(",", "")) * _mult(unit_lo if unit_lo else (per if (per or "").lower() == "lpa" else None))
        high = float(hi.replace(",", "")) * _mult(unit_hi) if hi else low
    except ValueError:
        return None
    if lo_u is None and hi_u and hi:  # "8-12 LPA": the unit after the second number applies to both
        low = float(lo.replace(",", "")) * _mult(hi_u)
    p = (per or "").lower()
    period = "year"
    if "month" in p:
        period = "month"
    elif "hour" in p:
        period = "hour"
    elif "day" in p:
        period = "day"
    elif not p and cur == "INR" and high < 200000 and low < 200000 and not (lo_u or hi_u):
        period = "month"  # bare small INR figures are almost always monthly
    factor = {"year": 1, "month": 12, "hour": 2000, "day": 250}[period]
    annual_low, annual_high = low * factor, high * factor
    mid_inr = (annual_low + annual_high) / 2 * TO_INR.get(cur, 1.0)
    return {"currency": cur, "low": low, "high": high, "period": period, "annual_low": annual_low, "annual_high": annual_high, "annual_inr_mid": round(mid_inr)}
