"""Command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=int(os.environ.get("AI_JOB_RADAR_DAYS", 7)), help="look-back window in days (default 7)")
    p.add_argument("--domain", default=os.environ.get("AI_JOB_RADAR_DOMAIN", "tech"), help="industry domain(s), e.g. healthcare, 'climate,energy', mechanical; 'tech' = no filter (default)")
    p.add_argument("--country", default=os.environ.get("AI_JOB_RADAR_COUNTRY", "India"), help="country name or ISO code; 'any' for no country filter (default India)")
    p.add_argument("--keywords", default="", help="extra search terms, comma-separated")
    p.add_argument("--deep", action="store_true", help="use the longer search-term list (more requests)")
    p.add_argument("--sources", default=",".join(["ats", "jobspy", "remote", "google", "adzuna", "jooble"]), help="lanes to run: ats,jobspy,remote,google,adzuna,jooble")
    p.add_argument("--out", help="output .xlsx path")
    p.add_argument("--db", help="SQLite path (default ~/.ai-job-radar/radar.sqlite)")
    p.add_argument("--results-per-term", type=int, default=200, help="results per search term per job site (default 200)")
    p.add_argument("--min-relevance", type=int, default=50, help="AI relevance threshold 0-100 (default 50)")
    p.add_argument("--all-boards", action="store_true", help="scan every known employer board, not only those hinting at the country")
    p.add_argument("--board-workers", type=int, default=12)
    p.add_argument("--delay", type=float, default=8.0, help="seconds between job-site search terms (default 8)")
    p.add_argument("--proxies", default=os.environ.get("AI_JOB_RADAR_PROXIES", ""), help="comma-separated proxies for job sites (user:pass@host:port)")
    p.add_argument("--limit-boards", type=int, help="(testing) only the first N boards")
    p.add_argument("--max-terms", type=int, help="(testing) only the first N search terms")
    p.add_argument("--jobspy-sites", default="", help="override job sites, e.g. linkedin,indeed,naukri,glassdoor")
    p.add_argument("--from-raw", help="skip fetching: re-run filtering, scoring and the workbook from a saved raw_runN.json.gz")
    p.add_argument("--enrich-budget", type=int, default=900, help="seconds of web lookups for company checks before falling back to cached data (default 900)")
    p.add_argument("--boards-file", action="append", default=[], help="extra YAML of boards (name, url, website, countries)")
    p.add_argument("--no-openjobs", action="store_true", help="skip the bundled OpenJobs board dataset")
    p.add_argument("--no-web-lookups", action="store_true", help="skip Clearbit / RDAP / Wikidata / board discovery")
    p.add_argument("--max-discover", type=int, default=80, help="max ATS board discoveries per run (cached 30 days)")
    p.add_argument("--linkedin-descriptions", action="store_true", help="fetch LinkedIn descriptions (one request per job; slow, more 429s)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="print a JSON summary at the end")


def cmd_run(a: argparse.Namespace) -> int:
    from .pipeline.run import RunConfig, run

    cfg = RunConfig(
        days=a.days, domain=a.domain, country=a.country, keywords=[k.strip() for k in a.keywords.split(",") if k.strip()],
        deep=a.deep, lanes=[s.strip() for s in a.sources.split(",") if s.strip()], out=a.out, db=a.db,
        results_per_term=a.results_per_term, min_relevance=a.min_relevance, all_boards=a.all_boards,
        board_workers=a.board_workers, delay=a.delay, proxies=[p.strip() for p in a.proxies.split(",") if p.strip()] or None,
        limit_boards=a.limit_boards, boards_files=a.boards_file, no_openjobs=a.no_openjobs, no_web_lookups=a.no_web_lookups,
        max_discover=a.max_discover, linkedin_fetch_description=a.linkedin_descriptions, quiet=a.quiet, max_terms=a.max_terms,
        jobspy_sites=[s.strip() for s in a.jobspy_sites.split(",") if s.strip()] or None, from_raw=a.from_raw, enrich_budget=a.enrich_budget,
    )
    if a.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 2
    s = run(cfg)
    if a.json:
        print(json.dumps({"output": str(s.output), "run_id": s.run_id, "counts": s.counts}, indent=2, default=str))
    else:
        print(f"\n{s.counts['in_window']} openings from the last {a.days} days written to {s.output}")
        print(f"risk levels: {s.counts['risk_levels']}")
        print(f"flagged for review: {s.counts['flagged']}  ·  companies: {s.counts['companies']}  ·  run id {s.run_id}")
    return 0


def cmd_discover(a: argparse.Namespace) -> int:
    from .http import Http
    from .pipeline.discover import discover_many
    from .seeds import save_discovered

    names: list[tuple[str, str | None]] = []
    for n in a.names:
        name, _, site = n.partition("|")
        names.append((name.strip(), site.strip() or None))
    if a.file:
        for line in Path(a.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                name, _, site = line.partition("|")
                names.append((name.strip(), site.strip() or None))
    if not names:
        print("give company names (optionally 'Name|website') or --file", file=sys.stderr)
        return 2
    res = discover_many(Http(timeout=15), names, workers=12)
    found = [b for b in res.values() if b]
    for n, b in sorted(res.items()):
        print(f"{n:40} {b.platform + ' ' + b.url if b else '-'}")
    if found and not a.dry_run:
        path = save_discovered(found)
        print(f"\n{len(found)} boards saved to {path} (used automatically in future runs)")
    return 0


def cmd_boards(a: argparse.Namespace) -> int:
    from .seeds import load_boards

    boards = load_boards()
    cc = a.country.upper() if a.country else None
    n = 0
    for b in boards:
        if cc and b.countries and cc not in [c.upper() for c in b.countries]:
            continue
        if a.platform and b.platform != a.platform:
            continue
        n += 1
        if not a.count:
            print(f"{b.platform:16} {b.name[:34]:34} {b.url}  {','.join(b.countries)}  [{b.origin}]")
    print(f"{n} boards")
    return 0


def cmd_runs(a: argparse.Namespace) -> int:
    from .pipeline.store import Store

    st = Store(a.db)
    for r in st.runs(a.limit):
        counts = json.loads(r.get("counts") or "{}")
        print(f"run {r['id']}  {r['started_at'][:16]}  in_window={counts.get('in_window')} flagged={counts.get('flagged')}  {r.get('output_path') or ''}")
    return 0


def cmd_review_export(a: argparse.Namespace) -> int:
    from .pipeline.review import export_review

    path = export_review(a.db, a.run_id, a.out, a.max_items)
    print(path)
    return 0


def cmd_review_import(a: argparse.Namespace) -> int:
    from .pipeline.review import import_review

    out = import_review(a.db, a.file, a.out)
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ai-job-radar", description="Recent LLM / AI / ML job openings, with fake and ghost posting flags, exported to Excel.")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("run", help="scan sources and write the workbook (default command)")
    _add_run_args(pr)
    pr.set_defaults(fn=cmd_run)
    pd = sub.add_parser("discover", help="find employer ATS boards from company names and remember them")
    pd.add_argument("names", nargs="*", help="company names, optionally 'Name|website'")
    pd.add_argument("--file", help="text file, one 'Name|website' per line")
    pd.add_argument("--dry-run", action="store_true")
    pd.set_defaults(fn=cmd_discover)
    pb = sub.add_parser("boards", help="list known employer boards")
    pb.add_argument("--country", help="ISO2 filter, e.g. IN")
    pb.add_argument("--platform")
    pb.add_argument("--count", action="store_true")
    pb.set_defaults(fn=cmd_boards)
    pu = sub.add_parser("runs", help="list previous runs")
    pu.add_argument("--db")
    pu.add_argument("--limit", type=int, default=20)
    pu.set_defaults(fn=cmd_runs)
    pe = sub.add_parser("review-export", help="export rows that deserve a human/LLM look as JSON")
    pe.add_argument("--db")
    pe.add_argument("--run-id", type=int)
    pe.add_argument("--out")
    pe.add_argument("--max-items", type=int, default=40)
    pe.set_defaults(fn=cmd_review_export)
    pi = sub.add_parser("review-import", help="apply reviewed JSON (company facts, notes) and rewrite the workbook")
    pi.add_argument("file")
    pi.add_argument("--db")
    pi.add_argument("--out")
    pi.set_defaults(fn=cmd_review_import)
    args = p.parse_args(argv)
    if not args.cmd:
        # Bare invocation: behave like `run` with defaults.
        args = p.parse_args(["run"] + (argv or sys.argv[1:]))
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
