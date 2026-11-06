"""Writes the Excel workbook: Jobs, Flagged, Companies, Run log, About."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..models import Job
from ..text import snippet

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="0563C1", underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")


def _d(dt: datetime | None):
    return dt.replace(tzinfo=None) if isinstance(dt, datetime) else None


def _src_label(j: Job) -> str:
    return ", ".join(sorted({s.split(":", 1)[0] for s in j.sources}))


def _work_mode(j: Job) -> str:
    return {"onsite": "Onsite", "hybrid": "Hybrid", "remote": "Remote", "remote-global": "Remote (worldwide)", "unknown": "Unknown"}.get(j.work_mode, j.work_mode)


def _location(j: Job) -> str:
    parts = [p for p in [j.city, j.region] if p]
    if j.country_name or j.country:
        parts.append(j.country_name or j.country)
    s = ", ".join(parts)
    return s if s else (j.location_raw or "")


# (header, width, getter, kind) — kind: text | link | date | int | wrap
COLUMNS = [
    ("Company", 26, lambda j: j.company, "text"),
    ("Company website", 28, lambda j: j.company_website, "link"),
    ("Company size", 18, lambda j: j.company_size, "text"),
    ("Job title", 42, lambda j: j.title, "text"),
    ("Apply link", 46, lambda j: j.best_url(), "link"),
    ("Location", 26, _location, "text"),
    ("Work mode", 16, _work_mode, "text"),
    ("Posted (claimed)", 15, lambda j: _d(j.posted_at), "date"),
    ("First seen", 15, lambda j: _d(j.first_seen), "date"),
    ("Days open", 10, lambda j: j.days_open, "int"),
    ("Legitimacy score", 11, lambda j: j.legit_score, "int"),
    ("Risk level", 20, lambda j: j.risk_level, "text"),
    ("Flags / reasons", 48, lambda j: "; ".join(j.flags), "wrap"),
    ("On company careers page?", 14, lambda j: {"yes": "Yes", "no": "No", "unknown": "Unknown"}.get(j.on_company_board, j.on_company_board), "text"),
    ("Company careers / ATS", 36, lambda j: j.company_board_url, "link"),
    ("Sources", 22, _src_label, "text"),
    ("Platform / ATS", 15, lambda j: j.ats or j.source_kind, "text"),
    ("Requisition ID", 16, lambda j: j.req_id, "text"),
    ("Role family", 16, lambda j: j.role_family, "text"),
    ("Seniority", 12, lambda j: j.seniority, "text"),
    ("Experience", 12, lambda j: j.experience, "text"),
    ("Employment type", 14, lambda j: j.employment_type, "text"),
    ("Salary (as listed)", 20, lambda j: j.salary, "text"),
    ("AI relevance", 10, lambda j: j.relevance_score, "int"),
    ("Matched AI terms", 30, lambda j: ", ".join(j.relevance_terms), "wrap"),
    ("Domain tags", 22, lambda j: ", ".join(j.domain_tags), "text"),
    ("Company industry", 22, lambda j: j.company_industry, "text"),
    ("Company LinkedIn", 30, lambda j: j.company_linkedin, "link"),
    ("Poster type", 12, lambda j: j.poster_type, "text"),
    ("Size source", 12, lambda j: j.company_size_source, "text"),
    ("Domain registered", 14, lambda j: j.domain_registered if j.domain_registered != "unknown" else None, "text"),
    ("Verification notes", 36, lambda j: j.verification_notes, "wrap"),
    ("Last verified live", 15, lambda j: _d(j.last_verified_live), "date"),
    ("Times seen", 9, lambda j: j.times_seen, "int"),
    ("Also listed at", 40, lambda j: "\n".join(u for u in j.urls if u != j.url)[:2000], "wrap"),
    ("Description (start)", 60, lambda j: snippet(j.description, 400), "wrap"),
    ("Review note", 30, lambda j: j.review_note, "wrap"),
    ("Job ID", 18, lambda j: j.job_id, "text"),
]


def _write_rows(ws: Worksheet, columns, rows, score_col: int | None = None, wrap_rows: bool = True) -> None:
    ws.append([c[0] for c in columns])
    for cell in ws[1]:
        cell.fill, cell.font, cell.alignment = HEADER_FILL, HEADER_FONT, Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 32
    for r in rows:
        vals = []
        for _h, _w, get, kind in columns:
            v = get(r)
            if kind == "date" and isinstance(v, datetime):
                v = v.replace(tzinfo=None)
            vals.append(v if v not in ("", []) else None)
        ws.append(vals)
        rn = ws.max_row
        for ci, (_h, _w, _g, kind) in enumerate(columns, 1):
            cell = ws.cell(row=rn, column=ci)
            if kind == "link" and cell.value:
                cell.hyperlink = str(cell.value)
                cell.font = LINK_FONT
            if kind == "date" and cell.value:
                cell.number_format = "yyyy-mm-dd"
            cell.alignment = WRAP if (kind == "wrap" and wrap_rows) else TOP
    for ci, (_h, w, _g, _k) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = "E2" if len(columns) > 5 else "A2"
    ws.auto_filter.ref = ws.dimensions
    if score_col and ws.max_row > 1:
        col = get_column_letter(score_col)
        ws.conditional_formatting.add(f"{col}2:{col}{ws.max_row}", ColorScaleRule(start_type="num", start_value=0, start_color="F8696B", mid_type="num", mid_value=55, mid_color="FFEB84", end_type="num", end_value=100, end_color="63BE7B"))


def _sort_key(j: Job):
    return (-j.legit_score, -(j.posted_at.timestamp() if j.posted_at else 0), j.company.lower())


def write_workbook(path: str | Path, jobs: list[Job], flagged: list[Job], companies: list[dict], run_log: dict, about: list[tuple[str, str]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    _write_rows(ws, COLUMNS, sorted(jobs, key=_sort_key), score_col=11)

    ws2 = wb.create_sheet("Flagged")
    flag_cols = [("Why flagged", 50, lambda j: j.extra.get("_flag_reason") or "; ".join(j.flags), "wrap")] + COLUMNS
    _write_rows(ws2, flag_cols, sorted(flagged, key=lambda j: (j.legit_score, j.company.lower())), score_col=12)

    ws3 = wb.create_sheet("Companies")
    comp_cols = [
        ("Company", 28, lambda c: c.get("name"), "text"),
        ("Website", 30, lambda c: c.get("website"), "link"),
        ("Size", 18, lambda c: c.get("size"), "text"),
        ("Size source", 12, lambda c: c.get("size_source"), "text"),
        ("Industry", 22, lambda c: c.get("industry"), "text"),
        ("Jobs in this run", 10, lambda c: c.get("jobs"), "int"),
        ("Best legitimacy score", 12, lambda c: c.get("best_score"), "int"),
        ("Careers / ATS board", 36, lambda c: c.get("board_url"), "link"),
        ("ATS platform", 14, lambda c: c.get("board_platform"), "text"),
        ("Domain registered", 14, lambda c: c.get("domain_registered"), "text"),
        ("LinkedIn", 30, lambda c: c.get("linkedin"), "link"),
        ("Wikidata", 12, lambda c: c.get("wikidata"), "text"),
        ("Poster type", 12, lambda c: c.get("poster_type"), "text"),
        ("Flags seen", 48, lambda c: c.get("flags"), "wrap"),
        ("Notes", 36, lambda c: c.get("notes"), "wrap"),
    ]
    _write_rows(ws3, comp_cols, sorted(companies, key=lambda c: (-(c.get("jobs") or 0), (c.get("name") or "").lower())), score_col=7)

    ws4 = wb.create_sheet("Run log")
    ws4.column_dimensions["A"].width = 30
    ws4.column_dimensions["B"].width = 110
    for k, v in run_log.items():
        ws4.append([k, v if not isinstance(v, (list, dict)) else "\n".join(map(str, v)) if isinstance(v, list) else "; ".join(f"{a}: {b}" for a, b in v.items())])
        ws4.cell(row=ws4.max_row, column=2).alignment = WRAP
        ws4.cell(row=ws4.max_row, column=1).font = Font(bold=True)

    ws5 = wb.create_sheet("About")
    ws5.column_dimensions["A"].width = 30
    ws5.column_dimensions["B"].width = 110
    for k, v in about:
        ws5.append([k, v])
        ws5.cell(row=ws5.max_row, column=2).alignment = WRAP
        ws5.cell(row=ws5.max_row, column=1).font = Font(bold=True)
    wb.save(path)
    return path


ABOUT = [
    ("What this is", "Recent LLM / AI / ML job openings gathered from employer job boards and job sites, de-duplicated, scored for legitimacy, and filtered to the country, domain and time window you asked for."),
    ("Jobs sheet", "Every opening that opened inside the window. Sorted by legitimacy score, then date. Use the filters in row 1."),
    ("Flagged sheet", "Openings worth a second look: scam signals, ghost-job signals, reposts of older postings, and postings whose date could not be confirmed. They also appear on the Jobs sheet when they are inside the window."),
    ("Companies sheet", "One row per employer with what could be verified: website, size, careers board, domain age, LinkedIn page."),
    ("Posted (claimed) vs First seen", "'Posted' is what the source says. Job sites refresh that date on every repost. 'First seen' is when this tool first saw the opening; from the second run on it is the more honest date."),
    ("Legitimacy score", "0-100. Starts at 50. Up for: listed on the employer's own board, requisition ID, trusted date, old domain, website matches the apply link, LinkedIn page, known size. Down for: asks for money (capped at 30), WhatsApp/Telegram contact, free-mail applications, earn-per-day pitches, ID/bank details up front, pressure tactics, unnamed company, staffing agency, new domain, evergreen wording, long open, reposted, same title in many cities, missing from the employer's board. It is a signal, not a verdict."),
    ("Risk level", "Likely genuine (75+), Looks fine (55-74), Check before applying (35-54), Suspicious (below 35)."),
    ("On company careers page?", "Yes = the opening is on the employer's own ATS board (or was fetched from it). No = the employer has a public board and this title is not on it. Unknown = no public board was found."),
    ("Company size", "Taken from the job site (Indeed), the employer's board data, or Wikidata. Treat as an estimate; the 'Size source' column says where it came from."),
    ("Work mode", "Parsed from the location text. 'Remote (worldwide)' means the posting says anywhere / worldwide; plain 'Remote' is tied to the listed country."),
    ("Sources", "ATS = employer boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Workday). Job sites via python-jobspy (LinkedIn, Indeed, Glassdoor, Naukri and regional boards). Remote boards (Remotive, RemoteOK). Optional keyed lanes: Google Jobs via SerpApi, Adzuna, Jooble."),
]
