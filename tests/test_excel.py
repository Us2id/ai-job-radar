from datetime import datetime, timezone

from openpyxl import load_workbook

from ai_job_radar.models import Job
from ai_job_radar.pipeline.excel import ABOUT, write_workbook


def test_workbook(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    j = Job(title="ML Engineer", company="Acme", url="https://boards.greenhouse.io/acme/1", source="greenhouse:acme", city="Pune", country="IN", country_name="India", posted_at=now, first_seen=now, legit_score=88, risk_level="Likely genuine", description="x" * 500)
    j.key = "k"
    out = write_workbook(tmp_path / "o.xlsx", [j], [j], [{"name": "Acme", "jobs": 1, "best_score": 88}], {"Generated": "now", "Errors": ["none"]}, ABOUT)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Jobs", "Flagged", "Companies", "Run log", "About"]
    ws = wb["Jobs"]
    hdr = [c.value for c in ws[1]]
    row = dict(zip(hdr, [c.value for c in ws[2]]))
    assert row["Company"] == "Acme" and row["Location"] == "Pune, India" and row["Legitimacy score"] == 88
    assert ws.cell(row=2, column=hdr.index("Apply link") + 1).hyperlink is not None
