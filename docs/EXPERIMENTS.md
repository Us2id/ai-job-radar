# Experiments branch: candidate additions and what the tests showed

Each item below was built on this branch and run against live sources on 2026-11-07. Numbers are
from those runs; they will drift. "Adopt" is a recommendation, not a decision.

| # | Addition | What it adds | Test result | Cost / risk | Recommendation |
|---|---|---|---|---|---|
| 1 | Instahyre lane (India) | Direct-employer postings with company size and website from the employer, posting date and description from the page's JobPosting markup | see below | ~0.4 s per request, ~5 min per run; one site's public API | adopt for India |
| 2 | Internshala lane (India) | AI/ML internships with stipend, duration, posting date | see below | HTML parsing (may break on redesign); many unpaid WFH postings from tiny orgs, where the scam scoring earns its keep | adopt, off by default unless internships wanted |
| 3 | HN "Who is hiring" lane | Monthly thread, mostly remote/global startups, direct employers | 206 comments in the Nov 2026 thread, 55 AI-relevant, 11 open to India/worldwide, all within 7 days of thread start | one API, free, tiny | adopt |
| 4 | Scam-text classifier | Learned signal from the EMSCAD dataset (17,880 postings, 866 fraudulent), exported to plain JSON, no new dependency | 5-fold CV AUC 0.988 on its own data, but on the full India run (1,737 openings, 821 with a long description) the highest probability was 0.56 and no posting crossed the 0.85 flag line; the 70 heuristically flagged rows all scored below 0.5 | trained on 2012-2014 Western postings; 0.8 MB of data; used only as a weak −12/−25 signal | optional: no false flags, but no evidence it finds anything the rules miss on real Indian AI postings |
| 5 | Salary normalisation | "8-12 LPA", "₹ 12,000 /month", "$120K-$180K" → annual range + approximate INR for sorting | 12/12 hand cases pass; parsed a salary for 416 of 1,737 openings in the full India run (24%) | fixed exchange rates (approximate) | adopt |
| 6 | "New this run" sheet | Only openings never seen before, the daily-reading sheet | trivial; verified on repeat runs | none | adopt |
| 7 | `schedule install --at 08:00` | Daily run via macOS launchd (cron line on Linux) | install / status / uninstall verified against a throw-away home: plist written, loaded, listed, removed; two bugs found and fixed in testing (a wrong relative import; the `--at` value leaking into the run arguments) | needs the laptop awake at that time | adopt |
| 8 | Live-link check for job-site postings | One GET per link, detects "no longer accepting applications" | LinkedIn 20/20 and Naukri 20/20 fetchable, Glassdoor blocked (403); 0 closed among week-old postings | ~2 s per link; low value inside a 7-day window | optional flag, capped |
| 9 | Foundit, Shine, Hirist, Cutshort, iimjobs, TimesJobs, hiring.cafe | More Indian boards / a career-page aggregator | Foundit refuses non-browser clients; Shine, Hirist, Cutshort, iimjobs render with JavaScript and expose no public JSON; TimesJobs TLS fails; hiring.cafe sits behind Cloudflare | would need a headless browser and would violate their terms | do not add |

## Details

### 1-3. New lanes, live run (India, 7 days, 2026-11-07 03:00 IST, `--sources instahyre,internshala,hn`)

| Lane | Listed | AI-relevant | Pages read | Kept (relevant, India, dated) | Unique in window | Time |
|---|---|---|---|---|---|---|
| Instahyre | 1,464 | 1,085 | 500 (cap, since raised to 1,200) | 41 | 35 | 388 s |
| Internshala | 251 | 86 | 86 | 42 | 38 | 102 s |
| HN Who is hiring (Oct + Nov threads) | 206 comments | 55 | 2 | 5 | 1 | 6 s |

- Instahyre rows come with the employer's own headcount (35/35) and website (35/35); nothing else in the
  tool provides size that reliably for Indian companies.
- The HN lane produced the first real repost detection: a company that posted the same role in the
  October and November threads was flagged "reposted".
- Internshala is dominated by work-from-home internships from very small organisations; 9 of 74 rows
  were flagged (evergreen wording, reposts, dates before the window). Internship stipends parse into
  the salary columns ("₹ 12,000 - 20,000 /month" → INR 144,000-240,000 per year).

### 4. Scam-text classifier

- Data: EMSCAD mirror (17,880 postings, 866 fraudulent). Model: TF-IDF unigrams+bigrams (40k features,
  sublinear tf, l2) + logistic regression (balanced classes). 5-fold cross-validation AUC 0.988.
- Thresholds: ≥0.95 → −25 (precision 0.99, recall 0.55 in CV); ≥0.85 → −12 (precision 0.97, recall 0.71).
- Exported to `ai_job_radar/data/scam_textmodel.json.gz` (0.8 MB) and scored in pure Python
  (`pipeline/textmodel.py`); output matches scikit-learn to three decimals on test strings.
- On the 72 scored rows of the lanes run above, no row crossed 0.85 (the rows were internships and
  employer-posted roles, not scams). On synthetic Indian scam text ("earn Rs 5000 per day, registration
  fee, WhatsApp") it returns 0.99; on a normal ML job description 0.16.

### 8. Live-link check

- 20 LinkedIn and 20 Naukri links from a week-old run: all fetchable with one plain request, none closed.
  Glassdoor answers 403. At ~2 s per link a 1,000-row run would take over half an hour, for little gain
  inside a 7-day window. Kept as an idea for an opt-in, capped flag; not wired in.

### 9. Boards that cannot be added cleanly

- Foundit: its search endpoint refuses non-browser clients ("content negotiation failed").
- Shine, Hirist, Cutshort, iimjobs: JavaScript-rendered pages with no public JSON in the HTML.
- TimesJobs: TLS handshake fails from this machine.
- hiring.cafe: Cloudflare challenge page for any automated client.

### 7. Scheduler

- `schedule install --at 07:30 --days 3 --country Germany` wrote `~/Library/LaunchAgents/io.github.us2id.ai-job-radar.plist`
  with `StartCalendarInterval 07:30` and the run arguments `--days 3 --country Germany`, loaded it with launchctl;
  `schedule status` printed it; `schedule uninstall` unloaded and removed it, leaving no launchd job behind.
- Tested with `HOME` pointed at a scratch directory so nothing was scheduled on the real account.
- On Linux the command prints the crontab line instead of installing it.

### 1. Instahyre: rate limits and what was tried against them

- A second run a few hours after the first was answered with HTTP 429 (Retry-After ~40 s) from the first page
  of most searches: the site's per-IP quota remembers the earlier run's ~1,000 detail-page requests.
- The lane now sits out up to four short Retry-After pauses per run, fetches detail pages two at a time with
  0.6 s between requests, and (when the listing ever carries a `reviewed_at`) skips postings reviewed before the
  window. The listing's `reviewed_at` is empty in practice, so one detail request per AI-relevant listing is
  still the cost of a posting date; the sitemap (which would carry dates for free) sits behind a Cloudflare
  challenge, so it was not used.

### 4. Scam-text classifier on the full India run

- Replayed the full India run (15,866 raw postings, 1,737 openings in the window) through this branch with the
  classifier on. 821 openings had a description long enough to score. Probabilities: median 0.04, 95th percentile
  0.13, maximum 0.56 (a "Chemical Engineer - Data Science" posting). Nothing reached the 0.85 threshold, so the
  model changed no score. None of the 70 openings carrying a heuristic flag (reposts, same title in many cities,
  missing from the employer's board, unnamed company) scored above 0.5: those are ghost-job signals, not the
  "pay a registration fee" scams the model was trained on.
- Conclusion: harmless, cheap to keep, but on a week of real Indian AI postings it found nothing the rules missed.

### Overlap of the new lanes with the default lanes (same day, same window)

- Instahyre: 58 openings in the window, 53 of them absent from the 1,171-row default run (employer-posted roles
  at Meesho, InMobi, Cognizant, Myntra, PepsiCo, Moveworks and others that never reached LinkedIn/Naukri).
- Internshala + HN: 38 openings, all absent from the default run (37 internships, 1 remote startup role).
