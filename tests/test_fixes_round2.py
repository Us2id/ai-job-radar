"""Tests for the defects the first full India run exposed."""
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from ai_job_radar.http import Http, RateLimited
from ai_job_radar.models import Job
from ai_job_radar.pipeline.relevance import relevance
from ai_job_radar.pipeline.run import summarize_errors
from ai_job_radar.pipeline.verify import score_all
from ai_job_radar.pipeline.window import apply_window
from ai_job_radar.sources.base import window_cutoff
from ai_job_radar.sources.boards import classify_board_error
from ai_job_radar.text import clean_company_name, fix_mojibake, split_company_links


def test_window_is_day_granular():
    now = datetime(2026, 11, 11, 21, 30, tzinfo=timezone.utc)
    assert window_cutoff(now, 7) == datetime(2026, 11, 4, tzinfo=timezone.utc)
    j = Job(title="ML Engineer", company="Acme", url="https://a.com/1", source="greenhouse:acme", posted_at=datetime(2026, 11, 4, tzinfo=timezone.utc))
    j.posted_claims = ["2026-11-04"]
    apply_window(j, now, 7)
    assert j.in_window, "a posting dated the first day of the window is inside it"
    j2 = Job(title="ML Engineer", company="Acme", url="https://a.com/2", source="greenhouse:acme", posted_at=datetime(2026, 11, 3, 23, tzinfo=timezone.utc))
    j2.posted_claims = ["2026-11-03"]
    apply_window(j2, now, 7)
    assert not j2.in_window


def test_fix_mojibake():
    assert fix_mojibake("ML Inference Engineer â\x80\x93 Terminal") == "ML Inference Engineer – Terminal"
    assert fix_mojibake("Weâ\x80\x99ve been") == "We’ve been"
    assert fix_mojibake("Zürich, naïve café") == "Zürich, naïve café"
    assert fix_mojibake("") == "" and fix_mojibake(None) == ""


@pytest.mark.parametrize("raw,clean", [
    ("Careers at Tide", "Tide"), ("Jobs at HPE", "HPE"), ("Acme Careers", "Acme"), ("NVIDIA External Career Site", "NVIDIA"),
    ("PwC Careers | Jobs", "PwC"), ("Join Stripe", "Stripe"), ("Tide", "Tide"), ("Careers", "Careers"), ("Join", "Join"),
])
def test_clean_company_name(raw, clean):
    assert clean_company_name(raw) == clean


def test_split_company_links():
    assert split_company_links("https://boards.greenhouse.io") == (None, None)
    assert split_company_links("https://www.linkedin.com/company/tide-banking/") == (None, "https://www.linkedin.com/company/tide-banking/")
    assert split_company_links("pwc.com") == ("https://pwc.com", None)
    assert split_company_links("https://www.naukri.com/acme-jobs-careers-123") == (None, None)


def test_anti_fraud_disclaimer_is_not_a_scam_signal():
    now = datetime.now(timezone.utc)
    disclaimer = ("We will never ask you to share bank details, passport, or payment app information while you are "
                  "interviewing. We'll also never ask you to pay a fee, send money, deposit or cash a check.")
    scam = "Selected candidates must pay a registration fee of Rs 500 via UPI before the interview. Share your bank details on WhatsApp."
    mixed = "Beware of fraud: we never ask for money. Note: a security deposit of Rs 2000 is required for the laptop kit."
    jobs = []
    for i, d in enumerate([disclaimer, scam, mixed]):
        j = Job(title="ML Engineer", company="Acme", url=f"https://acme.com/j/{i}", source="greenhouse:acme", description=d)
        j.company_norm = "acme"
        jobs.append(j)
    score_all(jobs, now)
    assert jobs[0].flags == [] and jobs[0].legit_score >= 75
    assert any("Asks for money" in f for f in jobs[1].flags) and jobs[1].legit_score <= 30
    assert any("Asks for money" in f for f in jobs[2].flags)


def test_non_ai_titles_are_capped_below_threshold():
    desc = "You will build LLM pipelines, fine-tune transformer models, deploy machine learning systems and run RAG evaluations."
    assert relevance("Project Manager", desc).score < 50
    assert relevance("Senior Vue Developer", desc).score < 50
    assert relevance("Vice President, Software Engineering", desc).score < 50
    assert relevance("Engineering Manager, Payment Services", desc).score < 50
    assert relevance("Software Engineer", desc).score >= 50, "generic engineering titles still qualify on a strong description"
    assert relevance("Product Manager, AI", desc).score >= 50
    assert relevance("Engineering Manager, Machine Learning", desc).score >= 50


def _fake_response(status, headers=None, body=b"{}"):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = body
    r.url = "https://example.test/x"
    return r


def test_http_long_retry_after_trips_the_circuit_breaker():
    h = Http()
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return _fake_response(429, {"Retry-After": "63523"})

    with mock.patch.object(h.session, "request", side_effect=fake_request):
        with pytest.raises(RateLimited):
            h.get("https://example.test/a")
        with pytest.raises(RateLimited):
            h.get("https://example.test/b")  # fails fast: no network call
    assert len(calls) == 1
    assert "example.test" in h.blocked_hosts()
    assert h.blocked_log and "429" in h.blocked_log[0]


def test_http_short_retry_after_is_waited_once():
    h = Http()
    responses = [_fake_response(429, {"Retry-After": "0"}), _fake_response(200, body=b'{"ok": true}')]
    with mock.patch.object(h.session, "request", side_effect=lambda *a, **k: responses.pop(0)):
        assert h.get_json("https://example.test/a") == {"ok": True}
    assert not h.blocked_hosts()


def test_decode_json_ignores_wrong_charset_header():
    r = _fake_response(200, {"Content-Type": "application/json; charset=ISO-8859-1"}, body="{\"t\": \"ML – Engineer\"}".encode("utf-8"))
    assert Http.decode_json(r) == {"t": "ML – Engineer"}


def test_board_error_classification_and_summary():
    assert classify_board_error("HTTPError: 404 Client Error: Not Found for url: x") == "missing"
    assert classify_board_error("bamboohr:acme: JSONDecodeError: Expecting value: line 1 column 1 (char 0)") == "missing"
    assert classify_board_error("RateLimited: apply.workable.com rate-limited us (retry after 63523s)") == "rate-limited"
    assert classify_board_error("ReadTimeout: HTTPSConnectionPool(...)") == "error"
    out = summarize_errors(["greenhouse:a: HTTPError: 404 Client Error: Not Found", "ashby:b: HTTPError: 404 Client Error", "lever:c: ReadTimeout: x"])
    assert len(out) == 2 and out[0].startswith("2 boards no longer exist") and "greenhouse:a" in out[0] and out[1].startswith("lever:c")


def test_pick_candidate_rules():
    from ai_job_radar.pipeline.enrich import WikiCandidate as WC, pick_candidate as pick
    capco = [WC("Q14428", 1, "Capcom", "Japanese video game company", "3,976 (2024)", ["https://capcom-germany.de/"], "Capcom"),
             WC("Q5034538", 4, "Capco", "Business consultancy company", None, ["http://www.capco.com/"], "Capco")]
    assert pick("Capco", None, capco).qid == "Q5034538"
    assert pick("Capco", "capco.com", capco).qid == "Q5034538"
    pwc = [WC("Q488048", 1, "PricewaterhouseCoopers", "multinational professional services brand", "295,371 (2023)", ["https://www.pwc.com/"], "PwC"),
           WC("Q50362983", 2, "PwC Norway", "company in Oslo, Norway", "1,926", ["http://www.pwc.com/"], None)]
    assert pick("PwC", "pwc.com", pwc).qid == "Q488048", "a domain tie is broken by having a Wikipedia article"
    assert pick("PwC", None, pwc) is None, "an alias-only match without a domain is left to the Wikipedia fallback"
    tide = [WC("Q23384", 1, "tide", "rise and fall of sea levels", None, [], "Tide"),
            WC("Q63076799", 3, "Tide", "British banking company", None, ["https://www.tide.co/"], "Tide (company)")]
    assert pick("Tide", "tide.co", tide).qid == "Q63076799"
    assert pick("Tide", None, tide) is None, "a name shared with other entities and no supporting context: decline"
    assert pick("Tide", None, tide, "business banking for SMEs").qid == "Q63076799"
    mercury = [WC("Q165745", 1, "Mercury Records", "American record label", None, ["http://www.mercuryrecords.com"], "Mercury Records")]
    assert pick("Mercury", "mercury.com", mercury) is None
    apple = [WC("Q312", 1, "Apple Inc.", "American multinational technology company", "164,000 (2022)", ["https://apple.com.cn/", "https://www.apple.com/"], "Apple Inc."),
             WC("Q89", 3, "apple", "edible fruit of the apple tree", None, [], "Apple")]
    assert pick("Apple", "apple.com", apple).qid == "Q312"
    assert pick("Apple", None, apple).qid == "Q312", "'Apple Inc.' normalises to 'apple'; the fruit is not company-like"


def test_domain_hints_prefer_the_employers_own_site():
    from ai_job_radar.text import domain_hints
    text = ("At Tide, we help SMEs. Apply at https://www.tide.co/careers or email talent@tide.co. "
            "See https://www.linkedin.com/company/tide-banking/ and http://schema.org/x. "
            "Benefits: https://benefits.example.com/a and https://benefits.example.com/b")
    assert domain_hints(text, "tide")[0] == "tide.co"
    assert domain_hints("Mail hr@gmail.com or see www.acme-corp.com now", "acme") == ["acme-corp.com"]
    assert domain_hints("nothing here", "acme") == []


def test_name_match_levels():
    from ai_job_radar.pipeline.enrich import name_match_level
    assert name_match_level("Capcom", "Capco") == 0
    assert name_match_level("Capco Technologies", "Capco") == 2, "suffixes are stripped before comparing"
    assert name_match_level("Capco Norway", "Capco") == 1
    assert name_match_level("Mastercard Inc", "Mastercard") == 2
    assert name_match_level("PricewaterhouseCoopers", "PwC") == 0
    assert name_match_level("London Stock Exchange Group", "London stock Exchange") == 2


def test_canonical_url_keeps_identifying_parameters():
    from ai_job_radar.text import canonical_url
    assert canonical_url("https://in.indeed.com/viewjob?jk=53b5d147754d168f&from=serp&utm_source=x") == "https://in.indeed.com/viewjob?jk=53b5d147754d168f"
    assert canonical_url("https://in.indeed.com/viewjob?jk=aaa") != canonical_url("https://in.indeed.com/viewjob?jk=bbb")
    assert canonical_url("https://www.glassdoor.co.in/job-listing/j?jl=1010319444203") == "https://glassdoor.co.in/job-listing/j?jl=1010319444203"
    assert canonical_url("https://www.linkedin.com/jobs/view/4488061858/?refId=abc&trackingId=xyz") == "https://linkedin.com/jobs/view/4488061858"
    assert canonical_url(None) == ""


def test_merge_keeps_distinct_indeed_postings():
    from ai_job_radar.pipeline.dedup import merge_jobs
    a = Job(title="ML Engineer", company="Acme", url="https://in.indeed.com/viewjob?jk=aaa", source="indeed", location_raw="Pune, India")
    b = Job(title="Data Scientist", company="Beta", url="https://in.indeed.com/viewjob?jk=bbb", source="indeed", location_raw="Pune, India")
    c = Job(title="Data Scientist", company="Beta", url="https://in.indeed.com/viewjob?jk=bbb&from=serp", source="indeed", location_raw="Pune, India")
    out = merge_jobs([a, b, c])
    assert len(out) == 2, "different jk ids stay apart; the same jk with tracking noise merges"


def test_site_preference():
    from ai_job_radar.pipeline.enrich import prefer_site
    assert prefer_site(["https://amazon.it/", "https://www.amazon.com/", "https://amazon.in"], "IN") == "https://www.amazon.com/"
    assert prefer_site(["https://epam.kz", "https://epam.in"], "IN") == "https://epam.in"
    assert prefer_site(["https://boards.greenhouse.io/x"], "IN") is None


def test_job_site_postings_repeated_across_cities_merge_into_one_row():
    from ai_job_radar.pipeline.dedup import merge_jobs
    from ai_job_radar.pipeline.excel import _location
    rows = []
    for i, city in enumerate(["Pune", "Bengaluru", "Chennai"]):
        j = Job(title="Machine Learning Engineer (Freelancer)", company="Soul Ai", url=f"https://www.naukri.com/job-{i}", source="naukri", location_raw=f"{city}, India")
        j.city = city
        rows.append(j)
    ats = Job(title="Machine Learning Engineer (Freelancer)", company="Soul Ai", url="https://boards.greenhouse.io/soul/1", source="greenhouse:soul", location_raw="Mumbai, India")
    ats.city = "Mumbai"
    out = merge_jobs(rows + [ats])
    assert len(out) == 2, "three city copies on a job site become one row; the employer-board posting stays its own row"
    merged = next(j for j in out if not j.is_ats)
    assert len(merged.extra["locations"]) == 3 and len(merged.urls) == 3
    assert "+2 more" in _location(merged)


def test_money_patterns_do_not_match_inside_longer_words():
    now = datetime.now(timezone.utc)
    j = Job(title="Senior LLMOps Engineer", company="Norstella", url="https://x.com/1", source="indeed",
            description="Translate prototype extraction logic into production-hardened, recurrent processing feeds.")
    j.company_norm = "norstella"
    score_all([j], now)
    assert not any("money" in f.lower() for f in j.flags)
    j2 = Job(title="Specialist", company="LTM", url="https://x.com/2", source="indeed",
             description="Please note that neither LTM nor any of its authorized recruitment agencies charge any candidate registration fee or any other fees.")
    j2.company_norm = "ltm"
    score_all([j2], now)
    assert not any("money" in f.lower() for f in j2.flags)


def test_same_name_companies_are_resolved_by_the_postings_words_or_declined():
    from ai_job_radar.pipeline.enrich import WikiCandidate as WC, pick_candidate as pick
    tide = [WC("Q1779936", 1, "Tide", "Norwegian public transport company", "57", ["http://www.tide.no"], "Tide (transportation company)"),
            WC("Q63076799", 3, "Tide", "British banking company", None, ["https://www.tide.co/"], "Tide (company)")]
    ctx = "At Tide we offer business banking accounts to SMEs in the UK and India. Fintech, payments, cards."
    assert pick("Tide", None, tide, ctx).qid == "Q63076799"
    assert pick("Tide", None, tide, "We make the best widgets for everyone.") is None, "no overlap with either description: decline rather than guess"
    assert pick("Tide", "tide.co", tide, "").qid == "Q63076799", "a known domain settles it regardless of context"


def test_guessed_website_does_not_trigger_the_new_domain_penalty():
    now = datetime.now(timezone.utc)
    base = dict(title="Data Scientist", company="Arrive", url="https://x.com/1", source="linkedin", description="A role.")
    guessed = Job(**base)
    guessed.company_norm = "arrive"
    guessed.domain_registered = (now - timedelta(days=16)).date().isoformat()
    guessed.extra["website_source"] = "clearbit name lookup"
    verified = Job(**dict(base, url="https://x.com/2"))
    verified.company_norm = "arrive"
    verified.domain_registered = guessed.domain_registered
    verified.extra["website_source"] = "posting"
    score_all([guessed, verified], now)
    assert not any("registered only" in f for f in guessed.flags) and "guessed" in guessed.verification_notes
    assert any("registered only" in f for f in verified.flags)


def test_repeat_run_skips_recent_nothing_found_markers(tmp_path):
    """A second run must not crash on the 'none:<date>' marker the first run cached (naive vs aware datetimes)."""
    from ai_job_radar.http import Http
    from ai_job_radar.pipeline.enrich import Enricher
    from ai_job_radar.pipeline.store import Store
    store = Store(tmp_path / "t.sqlite")
    store.upsert_company("acme", name="Acme", wikidata="none:2026-11-07")
    j = Job(title="ML Engineer", company="Acme", url="https://acme.com/1", source="greenhouse:acme")
    j.company_norm = "acme"
    e = Enricher(store, Http(), [], log=lambda *_: None, options={"no_web_lookups": True})
    e._resolve_sizes([[j]])  # no web lookups configured: must return quietly
    e.o["no_web_lookups"] = False
    calls = []
    e.http.get = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError("no network in tests"))
    e._resolve_sizes([[j]])
    assert calls == [], "a marker from today means: do not look this company up again"


def test_internshala_is_opt_in():
    from ai_job_radar.pipeline.run import LANES
    assert "internshala" not in LANES and "instahyre" in LANES and "hn" in LANES
    from ai_job_radar import cli
    import argparse
    p = argparse.ArgumentParser()
    # the run parser is built inside main(); check the flag exists by parsing --help text
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    assert "--internships" in buf.getvalue()


def test_only_real_404s_mark_a_board_missing():
    assert classify_board_error("workday:acme.wd5.myworkdayjobs.com/x: JSONDecodeError: Expecting value: line 1 column 1 (char 0)") != "missing"
    assert classify_board_error("workday:acme.wd5.myworkdayjobs.com/x: RuntimeError: Workday answered with an HTML page instead of JSON (service interruption or bot check)") == "rate-limited"
    assert classify_board_error("bamboohr:acme: JSONDecodeError: Expecting value: line 1 column 1 (char 0)") == "missing"
    assert classify_board_error("greenhouse:acme: HTTPError: 404 Client Error: Not Found for url: x") == "missing"
