"""Hand-off for a reviewer (a person or an LLM agent): export doubtful rows, import decisions, rewrite the workbook."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..models import Job
from .excel import ABOUT, write_workbook
from .store import Store
from .verify import score_all


def export_review(db: str | None, run_id: int | None, out: str | None, max_items: int = 40) -> Path:
    st = Store(db)
    run = st.get_run(run_id) if run_id else st.latest_run()
    if not run:
        raise SystemExit("no runs yet")
    jobs = st.load_run_jobs(run["id"])
    doubtful = [j for j in jobs if j.in_window and (35 <= j.legit_score < 70 or not j.company_size or not j.company_website or j.on_company_board == "no")]
    doubtful.sort(key=lambda j: (j.legit_score, j.company.lower()))
    seen_companies: set[str] = set()
    items = []
    for j in doubtful:
        if j.company_norm in seen_companies:
            continue
        seen_companies.add(j.company_norm)
        items.append({
            "key": j.key, "company": j.company, "company_norm": j.company_norm, "title": j.title, "url": j.best_url(),
            "location": j.location_raw, "legit_score": j.legit_score, "risk_level": j.risk_level, "flags": j.flags,
            "company_website": j.company_website, "company_size": j.company_size, "on_company_board": j.on_company_board,
            "description_start": (j.description or "")[:600],
            "fill_in": {"company_website": None, "company_size": None, "size_source": None, "verdict": None, "note": None},
        })
        if len(items) >= max_items:
            break
    payload = {"run_id": run["id"], "exported_at": datetime.now(tz=timezone.utc).isoformat(), "instructions": (
        "For each item, research the company (official website, approximate employee count, whether the posting exists on "
        "the company's own careers page, any scam reports) and fill `fill_in`. verdict is one of: genuine, suspicious, fake, "
        "unknown. Leave fields null when unknown. Then run: ai-job-radar review-import <this file>"), "items": items}
    path = Path(out) if out else Path(st.path).parent / f"review_run{run['id']}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    st.close()
    return path


def import_review(db: str | None, file: str, out: str | None) -> Path:
    st = Store(db)
    payload = json.loads(Path(file).read_text(encoding="utf-8"))
    run = st.get_run(int(payload["run_id"]))
    if not run:
        raise SystemExit(f"run {payload['run_id']} not found")
    jobs = st.load_run_jobs(run["id"])
    by_key = {j.key: j for j in jobs}
    by_company: dict[str, list[Job]] = {}
    for j in jobs:
        by_company.setdefault(j.company_norm, []).append(j)
    applied = 0
    for it in payload.get("items", []):
        fi = it.get("fill_in") or {}
        if not any(v for v in fi.values()):
            continue
        applied += 1
        cn = it.get("company_norm")
        if fi.get("company_website") or fi.get("company_size"):
            st.upsert_company(cn, name=it.get("company"), website=fi.get("company_website"), size=fi.get("company_size"),
                              size_source=fi.get("size_source") or ("review" if fi.get("company_size") else None), notes=fi.get("note"), override=1)
        for j in by_company.get(cn, []):
            if fi.get("company_website"):
                j.company_website = fi["company_website"]
            if fi.get("company_size"):
                j.company_size, j.company_size_source = fi["company_size"], fi.get("size_source") or "review"
            if fi.get("note"):
                j.review_note = fi["note"]
            v = (fi.get("verdict") or "").lower()
            if v == "fake":
                j.flags.append("reviewer verdict: fake")
                j.extra["review_cap"] = 15
            elif v == "suspicious":
                j.flags.append("reviewer verdict: suspicious")
                j.extra["review_cap"] = 45
            elif v == "genuine":
                j.flags.append("reviewer verdict: genuine")
                j.extra["review_floor"] = 75
    now = datetime.now(tz=timezone.utc)
    score_all(jobs, now)
    for j in jobs:
        if "review_cap" in j.extra:
            j.legit_score = min(j.legit_score, int(j.extra["review_cap"]))
        if "review_floor" in j.extra:
            j.legit_score = max(j.legit_score, int(j.extra["review_floor"]))
        if "review_cap" in j.extra or "review_floor" in j.extra:
            from .verify import cfg
            for lim, label in cfg()["levels"]:
                if j.legit_score >= lim:
                    j.risk_level = label
                    break
    in_window = [j for j in jobs if j.in_window]
    flagged = [j for j in jobs if j.extra.get("_flag_reason") or j.risk_level in ("Suspicious", "Check before applying") or not j.in_window]
    companies: dict[str, dict] = {}
    for j in jobs:
        rec = st.get_company(j.company_norm) or {}
        c = companies.setdefault(j.company_norm, {"name": j.company, "website": rec.get("website") or j.company_website, "size": rec.get("size") or j.company_size,
                                                  "size_source": rec.get("size_source") or j.company_size_source, "industry": rec.get("industry") or j.company_industry,
                                                  "jobs": 0, "best_score": 0, "board_url": rec.get("board_url"), "board_platform": rec.get("board_platform"),
                                                  "domain_registered": rec.get("domain_registered"), "linkedin": rec.get("linkedin"), "wikidata": rec.get("wikidata"),
                                                  "poster_type": j.poster_type, "flags": "; ".join(j.flags), "notes": rec.get("notes") or j.review_note})
        c["jobs"] += 1
        c["best_score"] = max(c["best_score"], j.legit_score)
    out_path = Path(out) if out else Path(run.get("output_path") or "output/reviewed.xlsx")
    counts = json.loads(run.get("counts") or "{}")
    run_log = {"Generated": now.strftime("%Y-%m-%d %H:%M UTC"), "Reviewed": f"{applied} items applied from {file}", "Original run": run["id"], "Counts": counts}
    write_workbook(out_path, in_window, flagged, list(companies.values()), run_log, ABOUT)
    st.save_run_jobs(run["id"], jobs)
    st.close()
    return out_path
