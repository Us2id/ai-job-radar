from ai_job_radar.models import Job
from ai_job_radar.pipeline.relevance import DomainSpec, relevance, role_family, search_terms, seniority, tag_domains

ML_DESC = "You will train models in PyTorch, fine-tune LLMs, build RAG pipelines with vector databases and deploy with MLflow."
BOILER = "We are a fast-growing company. We use AI and machine learning across our products."


def test_titles():
    assert relevance("Machine Learning Engineer").score == 100
    assert relevance("Staff AI Engineer").score >= 90
    assert relevance("Head of AI").score >= 90
    assert relevance("AIR Quality Analyst").score == 0
    assert relevance("Backend Developer").score == 0


def test_excluded_titles():
    assert relevance("Sales Manager - AI Products").excluded_by
    assert relevance("Email Marketing Specialist", "LLM").excluded_by
    assert relevance("Growth Management - User Growth", ML_DESC).excluded_by


def test_description_only():
    assert relevance("Software Engineer", ML_DESC).score >= 60
    assert relevance("Software Engineer", BOILER).score < 50
    assert relevance("Security Engineer III", BOILER).score < 50


def test_role_and_seniority():
    assert role_family("Senior Data Scientist") == "data-science"
    assert role_family("Research Scientist, Climate") == "research"
    assert role_family("ML Platform Engineer") == "mlops-platform"
    assert seniority("Senior Data Scientist") == "senior"
    assert seniority("GenAI Intern") == "intern"
    assert seniority("Staff AI Engineer") == "staff+"
    assert seniority("Head of AI") == "director+"


def _job(desc: str, title: str = "ML Engineer") -> Job:
    return Job(title=title, company="X", url="u", source="linkedin", description=desc)


def test_domains_taxonomy_and_literal():
    j = _job("Build deep learning models for chest X-ray radiology reports used by hospitals and clinicians.")
    assert DomainSpec("tech").matches(j)[0]
    assert not DomainSpec("tech").active
    assert DomainSpec("healthcare").matches(j)[0]
    assert DomainSpec("health, climate").describe() == "healthcare, climate"
    assert not DomainSpec("mechanical").matches(j)[0]
    assert DomainSpec("radiology").matches(j)[0]  # literal fallback, one specific word
    assert not DomainSpec("environment").matches(_job("Fast-paced environment with great people."))[0]
    assert "healthcare" in tag_domains(j)


def test_search_terms():
    assert len(search_terms()) >= 10
    assert len(search_terms(deep=True)) > len(search_terms())
    assert "foo bar" in search_terms(extra=["foo bar"])
