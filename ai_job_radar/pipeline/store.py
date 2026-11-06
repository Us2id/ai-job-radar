"""SQLite store: when did we first see each job, what do we know about each company, run history."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..models import Job, job_from_dict, job_to_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  key TEXT PRIMARY KEY, title TEXT, company TEXT, company_norm TEXT, url TEXT, source TEXT, country TEXT,
  first_seen TEXT, last_seen TEXT, times_seen INTEGER DEFAULT 1, posted_claims TEXT, last_posted_at TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS companies (
  company_norm TEXT PRIMARY KEY, name TEXT, website TEXT, domain TEXT, size TEXT, size_source TEXT, industry TEXT,
  linkedin TEXT, board_platform TEXT, board_slug TEXT, board_url TEXT, board_checked_at TEXT,
  domain_registered TEXT, domain_checked_at TEXT, wikidata TEXT, notes TEXT, override INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, params TEXT, counts TEXT, errors TEXT, output_path TEXT
);
CREATE TABLE IF NOT EXISTS run_jobs (run_id INTEGER, key TEXT, data TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def default_home() -> Path:
    return Path(os.environ.get("AI_JOB_RADAR_HOME", Path.home() / ".ai-job-radar"))


def _locked(fn):
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_home() / "radar.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ jobs
    @_locked
    def touch_job(self, job: Job, now: datetime) -> None:
        """Record this sighting; fills first_seen / last_seen / times_seen / posted_claims on the job."""
        row = self.db.execute("SELECT * FROM jobs WHERE key=?", (job.key,)).fetchone()
        claims = set(job.posted_claims)
        if row:
            try:
                claims |= set(json.loads(row["posted_claims"] or "[]"))
            except json.JSONDecodeError:
                pass
            first_seen = datetime.fromisoformat(row["first_seen"])
            times = int(row["times_seen"] or 1) + 1
        else:
            first_seen = now
            times = 1
        job.first_seen = first_seen
        job.last_seen = now
        job.times_seen = times
        job.posted_claims = sorted(claims)
        self.db.execute(
            """INSERT INTO jobs (key,title,company,company_norm,url,source,country,first_seen,last_seen,times_seen,posted_claims,last_posted_at,data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET title=excluded.title, url=excluded.url, source=excluded.source,
                 last_seen=excluded.last_seen, times_seen=excluded.times_seen, posted_claims=excluded.posted_claims,
                 last_posted_at=excluded.last_posted_at, data=excluded.data""",
            (
                job.key, job.title, job.company, job.company_norm, job.url, job.source, job.country,
                first_seen.isoformat(), now.isoformat(), times, json.dumps(sorted(claims)),
                job.posted_at.isoformat() if job.posted_at else None, json.dumps(job_to_dict(job))[:200000],
            ),
        )

    @_locked
    def known_job_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    # ------------------------------------------------------------------ companies
    @_locked
    def get_company(self, company_norm: str) -> dict | None:
        row = self.db.execute("SELECT * FROM companies WHERE company_norm=?", (company_norm,)).fetchone()
        return dict(row) if row else None

    @_locked
    def upsert_company(self, company_norm: str, **fields) -> None:
        if not company_norm:
            return
        cur = self.get_company(company_norm) or {"company_norm": company_norm}
        if cur.get("override"):
            # Manual overrides (from review-import) win over automatic enrichment for website / size.
            for k in ("website", "domain", "size", "size_source"):
                if cur.get(k):
                    fields.pop(k, None)
        cur.update({k: v for k, v in fields.items() if v is not None})
        cur["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        cols = ["company_norm", "name", "website", "domain", "size", "size_source", "industry", "linkedin",
                "board_platform", "board_slug", "board_url", "board_checked_at", "domain_registered",
                "domain_checked_at", "wikidata", "notes", "override", "updated_at"]
        vals = [cur.get(c) for c in cols]
        self.db.execute(
            f"INSERT OR REPLACE INTO companies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )

    # ------------------------------------------------------------------ runs
    @_locked
    def start_run(self, params: dict) -> int:
        cur = self.db.execute("INSERT INTO runs (started_at, params) VALUES (?,?)",
                              (datetime.now(tz=timezone.utc).isoformat(), json.dumps(params, default=str)))
        self.db.commit()
        return int(cur.lastrowid)

    @_locked
    def finish_run(self, run_id: int, counts: dict, errors: list, output_path: str | None) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, counts=?, errors=?, output_path=? WHERE id=?",
                        (datetime.now(tz=timezone.utc).isoformat(), json.dumps(counts, default=str),
                         json.dumps(errors, default=str), output_path, run_id))
        self.db.commit()

    @_locked
    def save_run_jobs(self, run_id: int, jobs: list[Job]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO run_jobs (run_id, key, data) VALUES (?,?,?)",
                            [(run_id, j.key, json.dumps(job_to_dict(j))) for j in jobs])
        self.db.commit()

    @_locked
    def latest_run(self) -> dict | None:
        row = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    @_locked
    def get_run(self, run_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def load_run_jobs(self, run_id: int) -> list[Job]:
        rows = self.db.execute("SELECT data FROM run_jobs WHERE run_id=?", (run_id,)).fetchall()
        return [job_from_dict(json.loads(r["data"])) for r in rows]

    @_locked
    def runs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    @_locked
    def commit(self) -> None:
        self.db.commit()

    @_locked
    def close(self) -> None:
        self.db.commit()
        self.db.close()
