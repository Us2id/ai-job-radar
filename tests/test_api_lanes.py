"""The keyed lanes are parsed from canned responses so the code paths are exercised without keys."""
from datetime import datetime, timezone

from ai_job_radar.geo import gazetteer
from ai_job_radar.sources.apis import fetch_adzuna, fetch_google_jobs, fetch_jooble
from ai_job_radar.sources.base import RunContext
from ai_job_radar.sources.remote_boards import fetch_remoteok, fetch_remotive

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, **kw):
        self.calls.append((url, kw.get("params")))
        for key, payload in self.routes.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected url {url}")

    def post_json(self, url, payload, **kw):
        self.calls.append((url, payload))
        for key, resp in self.routes.items():
            if key in url:
                return resp
        raise AssertionError(f"unexpected url {url}")


def _ctx(http, keys=None, country="IN"):
    return RunContext(now=NOW, days=7, country=country, country_name="India" if country else None, search_terms=["machine learning"],
                      http=http, gaz=gazetteer(), log=lambda m: None, options={"keys": keys or {}, "google_pages": 1})


def test_google_jobs_lane_skips_without_key_and_parses_with_key():
    assert fetch_google_jobs(_ctx(FakeHttp({}))).stats.note.startswith("skipped")
    http = FakeHttp({"serpapi.com": {"jobs_results": [{"title": "ML Engineer", "company_name": "Acme", "location": "Bengaluru, India",
                                                        "description": "PyTorch", "job_id": "x", "share_link": "https://g/x",
                                                        "detected_extensions": {"posted_at": "2 days ago", "schedule_type": "Full-time"},
                                                        "apply_options": [{"title": "Acme", "link": "https://acme.com/jobs/1"}]}],
                                       "serpapi_pagination": {}}})
    res = fetch_google_jobs(_ctx(http, {"SERPAPI_KEY": "k"}))
    assert res.stats.fetched == 1 and res.jobs[0].apply_url == "https://acme.com/jobs/1" and res.jobs[0].posted_at
    assert http.calls[0][1]["chips"] == "date_posted:week" and http.calls[0][1]["gl"] == "in"


def test_adzuna_lane():
    http = FakeHttp({"api.adzuna.com": {"results": [{"title": "Data Scientist", "company": {"display_name": "Acme"}, "redirect_url": "https://a/1",
                                                     "location": {"display_name": "Pune, Maharashtra"}, "description": "ML", "created": "2026-11-05T10:00:00Z",
                                                     "id": "1", "salary_min": 1000000, "salary_max": 2000000, "category": {"label": "IT Jobs"}}]}})
    res = fetch_adzuna(_ctx(http, {"ADZUNA_APP_ID": "a", "ADZUNA_APP_KEY": "b"}))
    assert res.stats.fetched == 1 and res.jobs[0].salary and "/jobs/in/search/1" in http.calls[0][0]
    assert fetch_adzuna(_ctx(http, {"ADZUNA_APP_ID": "a", "ADZUNA_APP_KEY": "b"}, country="NP")).stats.note.startswith("skipped")


def test_jooble_lane_filters_old():
    http = FakeHttp({"jooble.org": {"jobs": [
        {"title": "AI Engineer", "company": "Acme", "link": "https://j/1", "location": "Delhi", "snippet": "<b>LLM</b>", "updated": "2026-11-06T00:00:00", "id": 1},
        {"title": "Old AI Engineer", "company": "Acme", "link": "https://j/2", "location": "Delhi", "snippet": "x", "updated": "2026-09-01T00:00:00", "id": 2},
    ]}})
    res = fetch_jooble(_ctx(http, {"JOOBLE_API_KEY": "k"}))
    assert [j.title for j in res.jobs] == ["AI Engineer"] and res.jobs[0].description == "LLM"


def test_remote_boards_eligibility():
    http = FakeHttp({"remotive.com": {"jobs": [
        {"title": "ML Engineer", "company_name": "A", "url": "https://r/1", "publication_date": "2026-11-05T00:00:00", "candidate_required_location": "Worldwide", "description": "x", "id": 1},
        {"title": "ML Engineer", "company_name": "B", "url": "https://r/2", "publication_date": "2026-11-05T00:00:00", "candidate_required_location": "USA", "description": "US only", "id": 2},
    ]}, "remoteok.com": [{"legal": "..."}, {"position": "AI Engineer", "company": "C", "url": "https://ok/1", "date": "2026-11-06T00:00:00+00:00", "location": "India", "description": "x", "id": "1", "tags": ["ai"]}]})
    r1 = fetch_remotive(_ctx(http))
    assert [j.company for j in r1.jobs] == ["A"] and r1.jobs[0].extra["remote_eligibility"]
    r2 = fetch_remoteok(_ctx(http))
    assert len(r2.jobs) == 1 and r2.jobs[0].company == "C"
