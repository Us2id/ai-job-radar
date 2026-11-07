"""Runs every ATS board in parallel and collects per-board errors instead of failing the run."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..models import Job, SourceStats
from .ats import FETCHERS
from .base import Board, RunContext, SourceResult


def _select_boards(boards: list[Board], ctx: RunContext) -> list[Board]:
    skip = set(ctx.opt("skip_boards") or ())
    boards = [b for b in boards if b.id not in skip]
    if ctx.opt("all_boards") or not ctx.country:
        return boards
    out = []
    for b in boards:
        hints = [c.upper() for c in b.countries]
        if not hints or ctx.country.upper() in hints:
            out.append(b)
    return out


MISSING_MARKERS = ("404 Client Error", "TooManyRedirects", "410 Client Error")


def classify_board_error(err: str) -> str:
    """'missing' for boards that no longer exist, 'rate-limited' for a host that is pushing back, or 'error'.

    A non-JSON answer counts as missing only for BambooHR, whose unknown subdomains return an HTML page;
    elsewhere (Workday in particular) an HTML page with status 200 is a bot check, not a vanished board.
    """
    if any(m in err for m in MISSING_MARKERS) or (err.startswith("bamboohr:") and "JSONDecodeError" in err):
        return "missing"
    if "rate-limited" in err or "429" in err or "bot check" in err or "HTML page" in err:
        return "rate-limited"
    return "error"


def fetch_boards(ctx: RunContext) -> SourceResult:
    stats = SourceStats("ats-boards")
    t0 = time.time()
    boards = _select_boards(ctx.boards, ctx)
    skipped = len(set(ctx.opt("skip_boards") or ()) & {b.id for b in ctx.boards})
    stats.note = f"{len(boards)} of {len(ctx.boards)} boards selected for {ctx.country or 'any country'}"
    if skipped:
        stats.note += f" ({skipped} boards that were missing on a recent run are skipped for 30 days)"
    ctx.log(f"[ats] fetching {len(boards)} boards ({len(FETCHERS)} platforms) …")
    jobs: list[Job] = []
    per_platform: dict[str, int] = {}
    workers = int(ctx.opt("board_workers", 12))

    slow: list[tuple[float, str]] = []

    def run(b: Board) -> tuple[Board, list[Job] | None, str | None]:
        fn = FETCHERS.get(b.platform)
        if fn is None:
            return b, None, f"unsupported platform {b.platform}"
        t1 = time.time()
        try:
            got = fn(b, ctx)
            return b, got, None
        except Exception as e:  # noqa: BLE001 - one bad board must not kill the run
            return b, None, f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            dt = time.time() - t1
            if dt > 20:
                slow.append((dt, b.id))

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run, b) for b in boards]
        for f in as_completed(futs):
            b, got, err = f.result()
            done += 1
            if err:
                stats.errors.append(f"{b.id}: {err}")
            else:
                jobs.extend(got or [])
                per_platform[b.platform] = per_platform.get(b.platform, 0) + len(got or [])
            if done % 50 == 0:
                ctx.log(f"[ats] {done}/{len(boards)} boards, {len(jobs)} fresh postings so far")
    stats.fetched = len(jobs)
    stats.seconds = time.time() - t0
    stats.note += "; fresh postings per platform: " + ", ".join(f"{k}={v}" for k, v in sorted(per_platform.items()))
    if slow:
        stats.note += "; slowest boards: " + ", ".join(f"{bid} {dt:.0f}s" for dt, bid in sorted(slow, reverse=True)[:8])
    ctx.log(f"[ats] done: {len(jobs)} postings within the window from {len(boards)} boards, {len(stats.errors)} board errors, {stats.seconds:.0f}s")
    return SourceResult(jobs, stats)
