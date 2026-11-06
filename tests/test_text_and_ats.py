from datetime import timezone

from ai_job_radar.sources.ats import _wd_days, board_from_url, workday_parts
from ai_job_radar.text import domain_of, extract_experience, html_to_text, normalize_company, normalize_title, parse_date, root_domain


def test_dates():
    assert parse_date("2026-11-01T10:00:00Z").tzinfo is not None
    assert parse_date(1786469891368).year == 2026
    assert parse_date("garbage") is None
    assert parse_date("3 days ago") is not None and parse_date("Posted Today") is not None
    assert parse_date("2026-11-04 08:46:16 UTC").tzinfo == timezone.utc


def test_text_helpers():
    assert html_to_text("&lt;p&gt;Hello &amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;one&lt;/li&gt;&lt;/ul&gt;") == "Hello & welcome\n- one"
    assert normalize_company("Qure.ai Technologies Pvt. Ltd.") == "qure ai"
    assert normalize_company("Acme") == normalize_company("ACME Inc")
    assert normalize_title("Senior ML Engineer (Req #1234)") == "senior ml engineer"
    assert extract_experience("We need 3-5 years of experience") == "3-5 yrs"
    assert root_domain("jobs.sub.example.co.in") == "example.co.in"
    assert domain_of("https://www.razorpay.com/careers") == "razorpay.com"


def test_board_urls():
    assert board_from_url("https://boards.greenhouse.io/anthropic").platform == "greenhouse"
    assert board_from_url("https://job-boards.greenhouse.io/anthropic/jobs/1").slug == "anthropic"
    assert board_from_url("https://jobs.lever.co/meesho").slug == "meesho"
    assert board_from_url("https://jobs.ashbyhq.com/openai").platform == "ashby"
    assert board_from_url("https://apply.workable.com/huggingface/").slug == "huggingface"
    assert board_from_url("https://illuvium.recruitee.com/").platform == "recruitee"
    assert board_from_url("https://zepto.bamboohr.com/careers").platform == "bamboohr"
    wd = board_from_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x")
    assert wd.platform == "workday" and wd.slug == "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    assert board_from_url("https://example.com/careers") is None
    assert workday_parts("https://amd.wd1.myworkdayjobs.com/External") == ("amd.wd1.myworkdayjobs.com", "amd", "External")


def test_workday_posted_on():
    assert _wd_days("Posted Today") == 0
    assert _wd_days("Posted Yesterday") == 1
    assert _wd_days("Posted 3 Days Ago") == 3
    assert _wd_days("Posted 30+ Days Ago") == 30
    assert _wd_days(None) is None
