"""Employer boards to scan: a curated list, the OpenJobs dataset, boards you discovered, boards you add."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import yaml

from .pipeline.store import default_home
from .sources.ats import board_from_url
from .sources.base import Board
from .text import split_company_links


def _pkg_data(name: str) -> Path:
    return resources.files("ai_job_radar") / "data" / "seeds" / name  # type: ignore[return-value]


def user_seed_paths() -> list[Path]:
    home = default_home()
    return [home / "boards.yaml", Path("boards.yaml")]


def load_boards(include_openjobs: bool = True, extra_files: list[str | Path] | None = None) -> list[Board]:
    boards: dict[str, Board] = {}

    def add(b: Board | None) -> None:
        if b:
            b.website, li = split_company_links(b.website)
            b.linkedin = b.linkedin or li
        if b and b.id not in boards:
            boards[b.id] = b
        elif b:
            cur = boards[b.id]
            cur.website = cur.website or b.website
            cur.linkedin = cur.linkedin or b.linkedin
            for c in b.countries:
                if c not in cur.countries:
                    cur.countries.append(c)
            if not cur.name or cur.name == cur.slug:
                cur.name = b.name

    for entry in yaml.safe_load(_pkg_data("curated.yaml").read_text(encoding="utf-8")) or []:
        add(board_from_url(entry["url"], entry.get("name", ""), entry.get("website"), entry.get("countries") or [], "curated"))
    if include_openjobs:
        for e in json.loads(_pkg_data("openjobs_boards.json").read_text(encoding="utf-8")):
            add(Board(e["name"], e["platform"], e["slug"], e.get("url", ""), e.get("website"), e.get("countries") or [], "openjobs"))
    for p in list(user_seed_paths()) + [Path(x) for x in (extra_files or [])]:
        if p.exists():
            for entry in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
                if isinstance(entry, str):
                    add(board_from_url(entry, origin="user"))
                else:
                    add(board_from_url(entry["url"], entry.get("name", ""), entry.get("website"), entry.get("countries") or [], "user"))
    disc = default_home() / "discovered.yaml"
    if disc.exists():
        for entry in yaml.safe_load(disc.read_text(encoding="utf-8")) or []:
            add(board_from_url(entry["url"], entry.get("name", ""), entry.get("website"), entry.get("countries") or [], "discovered"))
    return list(boards.values())


def save_discovered(boards: list[Board]) -> Path:
    path = default_home() / "discovered.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    cur = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
    seen = {e.get("url") for e in cur or []}
    for b in boards:
        if b.url not in seen:
            cur.append({"name": b.name, "url": b.url, "website": b.website, "countries": b.countries})
    path.write_text(yaml.safe_dump(cur, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
