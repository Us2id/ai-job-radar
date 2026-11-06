from datetime import datetime, timedelta, timezone

from ai_job_radar.models import Job, job_from_dict, job_to_dict
from ai_job_radar.pipeline.dedup import merge_jobs
from ai_job_radar.pipeline.store import Store
from ai_job_radar.pipeline.verify import score_all
from ai_job_radar.pipeline.window import apply_window

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def _j(title, company, source, url, city="Bengaluru", posted=None, trusted=False, **kw):
    j = Job(title=title, company=company, url=url, source=source, city=city, posted_at=posted, posted_trusted=trusted, **kw)
    j.seniority = "senior" if "senior" in title.lower() else "mid"
    return j


def test_merge_same_job_across_sites_prefers_ats():
    a = _j("Machine Learning Engineer", "Acme Pvt Ltd", "linkedin", "https://linkedin.com/jobs/1", posted=NOW - timedelta(days=1))
    b = _j("Machine Learning Engineer", "Acme", "naukri", "https://naukri.com/2", posted=NOW - timedelta(days=2))
    c = _j("Machine Learning Engineer", "Acme Technologies", "greenhouse:acme", "https://boards.greenhouse.io/acme/3", posted=NOW - timedelta(days=5), trusted=True)
    out = merge_jobs([a, b, c])
    assert len(out) == 1
    m = out[0]
    assert m.source == "greenhouse:acme" and m.posted_trusted
    assert set(m.sources) == {"linkedin", "naukri", "greenhouse:acme"}
    assert len(m.urls) == 3 and len(m.posted_claims) == 3


def test_merge_keeps_different_seniority_apart():
    a = _j("Machine Learning Engineer", "Acme", "linkedin", "https://l/1")
    b = _j("Senior Machine Learning Engineer", "Acme", "linkedin", "https://l/2")
    assert len(merge_jobs([a, b])) == 2


def test_window_first_run_uses_claimed_date():
    j = _j("ML Engineer", "Acme", "linkedin", "https://l/1", posted=NOW - timedelta(days=3))
    j.first_seen, j.times_seen, j.posted_claims = NOW, 1, [(NOW - timedelta(days=3)).date().isoformat()]
    apply_window(j, NOW, 7)
    assert j.in_window and j.days_open == 3 and not j.reposted
    j2 = _j("ML Engineer", "Acme", "linkedin", "https://l/1", posted=NOW - timedelta(days=12))
    j2.first_seen, j2.times_seen, j2.posted_claims = NOW, 1, [(NOW - timedelta(days=12)).date().isoformat()]
    apply_window(j2, NOW, 7)
    assert not j2.in_window


def test_window_detects_repost_from_first_seen():
    j = _j("ML Engineer", "Acme", "linkedin", "https://l/1", posted=NOW - timedelta(days=1))
    j.first_seen, j.times_seen = NOW - timedelta(days=20), 3
    j.posted_claims = [(NOW - timedelta(days=20)).date().isoformat(), (NOW - timedelta(days=1)).date().isoformat()]
    apply_window(j, NOW, 7)
    assert j.reposted and not j.in_window and j.days_open == 20


def test_store_remembers_first_seen(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    j = _j("ML Engineer", "Acme", "linkedin", "https://l/1")
    j.key = "acme|ml engineer|bengaluru"
    st.touch_job(j, NOW - timedelta(days=10))
    st.commit()
    j2 = _j("ML Engineer", "Acme", "linkedin", "https://l/1")
    j2.key = j.key
    st.touch_job(j2, NOW)
    assert j2.first_seen == NOW - timedelta(days=10) and j2.times_seen == 2
    st.upsert_company("acme", name="Acme", website="https://acme.com")
    assert st.get_company("acme")["website"] == "https://acme.com"
    st.close()


def test_scoring_scam_vs_ats():
    scam = _j("AI Trainer - Work From Home", "Confidential", "naukri", "https://n/1")
    scam.description = "Earn upto Rs 5000 per day. No interview. Pay a registration fee of Rs 999 via UPI. Contact us on WhatsApp 98765 43210."
    scam.first_seen, scam.days_open = NOW, 0
    good = _j("Machine Learning Engineer", "Acme", "greenhouse:acme", "https://boards.greenhouse.io/acme/1", trusted=True, req_id="R123")
    good.first_seen, good.days_open = NOW, 1
    score_all([scam, good], NOW)
    assert scam.legit_score <= 30 and scam.risk_level == "Suspicious"
    assert any("money" in f for f in scam.flags) and any("WhatsApp" in f for f in scam.flags)
    assert good.legit_score >= 75 and good.risk_level == "Likely genuine"


def test_scoring_ghost_signals():
    j = _j("Data Scientist", "Acme", "linkedin", "https://l/1")
    j.description = "We are always hiring! Join our talent pool for future opportunities."
    j.first_seen, j.days_open, j.reposted = NOW - timedelta(days=70), 70, True
    score_all([j], NOW)
    assert j.legit_score < 40
    assert any("evergreen" in f for f in j.flags) and any("open for" in f for f in j.flags) and any("reposted" in f for f in j.flags)


def test_job_roundtrip():
    j = _j("ML Engineer", "Acme", "linkedin", "https://l/1", posted=NOW)
    d = job_to_dict(j)
    back = job_from_dict(d)
    assert back.title == j.title and back.posted_at == NOW
