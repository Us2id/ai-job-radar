# ai-job-radar

Find **LLM / AI / ML job openings posted in the last N days**, in any country and any industry domain,
flag **fake, scam and ghost postings**, and get everything in one **Excel workbook**.

- Scans employer job boards directly (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee,
  BambooHR, Workday) plus job sites (LinkedIn, Indeed, Glassdoor, Naukri and regional boards), remote boards
  (Remotive, RemoteOK), Instahyre (India, with the employer's own headcount and website) and the monthly
  Hacker News "Who is hiring" thread. Internshala (India internships) with `--internships`. Optional keyed
  lanes: Google Jobs (SerpApi), Adzuna, Jooble.
- Remembers when each opening was first seen, so reposts of old jobs are not mistaken for new ones.
- Scores every posting 0-100 for legitimacy with reasons: asks for money, WhatsApp-only contact,
  free-mail applications, unnamed company, brand-new domain, evergreen wording, long open, reposted,
  missing from the employer's own board, and more.
- Defaults: last **7 days**, **India**, **tech** (no domain filter). Every default is a flag.
- Any country: a world gazetteer reads city, state and country text, and AI/ML titles are recognised in
  English, German, French, Spanish, Portuguese, Italian, Dutch, Japanese, Chinese and Korean.
- Works as a plain command line tool and as a **Claude Code skill** (`/ai-job-radar`).

## Quick start

```bash
git clone https://github.com/Us2id/ai-job-radar.git
cd ai-job-radar
./install.sh                      # creates .venv, links `ai-job-radar` into ~/.local/bin, links the skill

ai-job-radar run                                   # last 7 days, India, all AI/ML roles
ai-job-radar run --days 10 --country Germany       # any country name or ISO code
ai-job-radar run --domain healthcare               # healthcare AI jobs
ai-job-radar run --domain "climate, energy" --country any
ai-job-radar run --domain mechanical --days 14     # unknown words are matched literally
ai-job-radar run --deep --keywords "speech, robotics"
```

Output: `output/ai-jobs_<country>_<domain>_<days>d_<date>.xlsx` with six sheets:

| Sheet | Content |
|---|---|
| Jobs | Every opening that opened inside the window, sorted by legitimacy score then date |
| New this run | Only the openings this database had never seen before: the sheet to read daily |
| Flagged | Postings worth a second look: scam signals, ghost signals, reposts, unknown dates |
| Companies | One row per employer: website, size (+source), careers board, domain age, LinkedIn page |
| Run log | What was scanned, counts per source, errors, parameters |
| About | How to read every column and how the score is built |

Columns on the Jobs sheet: Company, Company website, Company size, Job title, Apply link, Location,
Work mode, Posted (claimed), First seen, Days open, Legitimacy score, Risk level, Flags / reasons,
On company careers page?, Company careers / ATS, Sources, Platform / ATS, Requisition ID, Role family,
Seniority, Experience, Employment type, Salary (as listed), Salary / year (parsed), Salary / year INR approx,
AI relevance, Matched AI terms, Domain tags,
Company industry, Company LinkedIn, Poster type, Size source, Domain registered, Verification notes,
Last verified live, Times seen, Also listed at, Description (start), Review note, Job ID.

## How it works

1. **Fetch.** All lanes run in parallel. Employer boards return exact publish dates; postings older than
   the window are dropped at the source. Job sites are queried per search term with their own
   "posted in the last N hours" filters, with pauses and one automatic retry on rate limits.
2. **Filter.** A posting is kept when its title or description is clearly about AI/ML work (a 0-100
   relevance score; threshold `--min-relevance`, default 50), when its location is in the requested
   country (a world gazetteer parses city/state/country text; remote postings count when they say
   worldwide, the country, or its region), and when it matches the requested domain(s).
3. **Merge.** The same opening seen on several sites becomes one row with all sources and links.
   Employer-board versions win over job-site copies. A job-site posting repeated city by city becomes
   one row that lists every city (and "same title in N locations" still counts as a ghost-job signal).
4. **Remember.** A SQLite database (`~/.ai-job-radar/radar.sqlite`) records when each opening was first
   seen. The window is applied to the *earliest* of the claimed posting date and the first-seen date, so
   a job reposted every week is reported once and flagged as reposted. The window is counted in whole
   days: `--days 7` on 11 November means everything opened on or after 4 November.
5. **Enrich.** Company website (from the posting, the board, or Clearbit autocomplete), domain registration
   date (RDAP), size (job-site data, then Wikidata in one batched query per 20 companies, then the English
   Wikipedia infobox), LinkedIn page, and the company's public ATS board (probed from its name). Job-site
   postings are cross-checked against the employer's own board. Lookups are cached in the database, so
   coverage grows run by run.
6. **Score.** Legitimacy starts at 58 and moves with the signals above; "asks for money" caps it at 30.
   Levels: Likely genuine (75+), Looks fine (55-74), Check before applying (35-54), Suspicious (<35).
   It is a signal with reasons, never a verdict.
7. **Write** the workbook.

## Claude Code skill

`install.sh` links `skills/ai-job-radar` into `~/.claude/skills`. In any Claude Code session:

```
/ai-job-radar healthcare jobs in India from the last 10 days
```

The skill runs the tool, then (optionally) researches the doubtful companies on the web through
`review-export` / `review-import`, which fills in websites, sizes and verdicts and rewrites the workbook.

Run it every morning: `ai-job-radar schedule install --at 08:00 -- --days 1 --country India` installs a daily
launchd job on macOS (and prints the crontab line on Linux); `schedule status` and `schedule uninstall` manage it.

## Options worth knowing

```
--days N                 window in days (default 7)
--country NAME|ISO|any   default India
--domain WORDS           default tech (no filter); comma-separate several; any word works
--keywords a,b           extra search terms for the job-site lanes
--deep                   longer search-term list (more requests)
--sources ats,jobspy,remote,instahyre,hn,google,adzuna,jooble
--internships            also scan Internshala (India internships)
--all-boards             scan every known employer board, not only the ones hinting at the country
--results-per-term N     per search term per job site (default 200)
--min-relevance N        AI relevance threshold (default 50)
--delay S                pause between job-site search terms (default 8)
--proxies a,b            proxies for job sites (or AI_JOB_RADAR_PROXIES)
--no-web-lookups         skip Clearbit / RDAP / Wikidata / board discovery (faster, offline-friendly)
--linkedin-descriptions  fetch LinkedIn descriptions (one request per job; slow)
--out PATH  --db PATH    output workbook / database paths
```

Optional keys (environment variables): `SERPAPI_KEY` (Google Jobs, the closest thing to a web-wide
crawl since Google indexes any page with JobPosting markup), `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`,
`JOOBLE_API_KEY`. Lanes without keys are skipped and say so in the Run log.

## Employer boards

About 1,800 boards are bundled (dead boards were pruned, and any that disappear later are skipped automatically): a curated list of AI-hiring employers (`ai_job_radar/data/seeds/curated.yaml`)
and the OpenJobs company dataset with hiring-country hints. By default a run scans boards that hint at
the requested country plus boards with no hint; `--all-boards` scans everything.

Add your own:

```bash
ai-job-radar discover "Qure.ai|https://qure.ai" "Sarvam AI|https://sarvam.ai"   # probes the public ATS endpoints
ai-job-radar boards --country IN --count
```

or list URLs in `~/.ai-job-radar/boards.yaml` (`- {name: X, url: https://boards.greenhouse.io/x, website: https://x.com}`).

## Honest limits

- No tool can crawl "the whole internet". Coverage comes from employer boards, the big job sites and,
  with a key, Google's job index. Employers that only post on their own custom portal and nowhere else
  are missed unless their board is on a supported ATS.
- Job sites refresh "posted" dates on every repost. From the second run on, *First seen* is the honest date.
- Hosts that answer HTTP 429 are left alone for the rest of the run (the Run log sheet says so) instead of
  being hammered; Wikimedia in particular throttles anonymous clients hard, so company sizes fill in over
  a few runs rather than all at once.
- Boards that no longer exist (HTTP 404) are skipped for 30 days and listed once in the Run log.
- Workable's board API has an hourly quota: on back-to-back runs for several countries, some Workable boards
  answer 429 and are skipped for that run (the Run log lists them); they come back on the next run.
- Job-site scraping (LinkedIn, Indeed, Glassdoor, Naukri) is best effort: these sites rate-limit and
  block. Employer boards, remote boards and the keyed APIs are the stable part.
- Company size is an estimate from whichever source had it; the *Size source* column says which.
- Fake-job detection is heuristic. Treat the score as a prompt to check, not a conclusion.

## Development

```bash
./install.sh
.venv/bin/pytest -q
```

Taxonomies live in `ai_job_radar/data/*.yaml` (search terms, relevance rules, domain synonyms, scam
patterns, place aliases) and can be edited without touching code.

## License

MIT. Bundled third-party data is listed in `THIRD_PARTY_NOTICES.md`.
