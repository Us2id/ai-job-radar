---
name: ai-job-radar
description: Find LLM / AI / ML job openings posted in the last N days (default 7) for any country (default India) and any industry domain (default tech = all), flag fake and ghost postings, and write an Excel workbook. Use when the user asks for recent AI/ML jobs, a job sheet, new openings, or to run the job radar.
---

# ai-job-radar

You run the `ai-job-radar` command line tool, then review the doubtful rows with web research, then
summarise. The tool does the heavy lifting (crawling, de-duplication, scoring, Excel). You add judgment.

## 1. Work out the parameters from the request

| Parameter | Default | Examples of how users say it |
|---|---|---|
| `--days N` | 7 | "last 10 days", "this month" (30), "today" (1) |
| `--country` | India | "in Germany", "US jobs", "Singapore", "anywhere" (`any`) |
| `--domain` | tech (no filter) | "healthcare", "climate", "mechanical", "fintech, insurance"; any word works |
| `--keywords` | none | extra search terms the user names, e.g. "speech", "robotics" |
| `--deep` | off | "search thoroughly", "as many as possible" |

Country accepts any name or ISO code. Domain accepts anything: known domains expand to synonyms,
unknown words are matched literally. Several domains can be comma-separated.

## 2. Run it

```bash
ai-job-radar run --days 7 --country India --domain tech
```

- If the command is missing, run `./install.sh` from the repository root (or `bash <repo>/install.sh`).
- Optional lanes switch on automatically when keys exist in the environment: `SERPAPI_KEY` (Google Jobs),
  `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`, `JOOBLE_API_KEY`.
- A full run takes roughly 10-25 minutes (employer boards are scanned in parallel; job sites are
  scanned slowly on purpose to avoid rate limits). Run it in the background and keep the user informed.
- The output path is printed at the end: `output/ai-jobs_<country>_<domain>_<days>d_<date>.xlsx`.
- The run is incremental: a local SQLite database remembers when each opening was first seen, so
  repeat runs tell reposts from genuinely new openings.

## 3. Review the doubtful rows (bounded, optional but valuable)

```bash
ai-job-radar review-export --max-items 25
```

This writes a JSON file of companies that need a human look (unknown website or size, middling legitimacy
score, not found on the employer's board). For each item, use web search to find:

- the company's official website and approximate size (employee count or range, with the source);
- whether the posting exists on the company's own careers page;
- scam reports (search the company name with "scam", "fake job", "registration fee").

Fill `fill_in` for each item (`company_website`, `company_size`, `size_source`, `verdict` one of
genuine / suspicious / fake / unknown, `note`), save the file, then:

```bash
ai-job-radar review-import <path-to-json>
```

The workbook is rewritten with your findings. Skip this step if the user wants speed, and never
invent facts: leave fields null when the web does not say.

## 4. Report

Tell the user, in plain language:

- how many openings were found for the window, country and domain;
- the path of the workbook and what each sheet holds (Jobs, Flagged, Companies, Run log, About);
- the risk-level breakdown and the two or three most interesting flags;
- any source that failed (the Run log sheet lists errors), and that job-site lanes can be rate-limited.

Do not paste the whole table into the chat. Never state a posting is fake; say what was flagged and why.

## Other commands

- `ai-job-radar discover "Company|website" ...` finds a company's public ATS board and remembers it.
- `ai-job-radar boards --country IN --count` shows how many boards are known for a country.
- `ai-job-radar runs` lists previous runs.
- `ai-job-radar run --help` lists every option (sources, proxies, thresholds, limits).
