# Job Alert Bot

A personal job-search assistant that runs on GitHub Actions. It:

1. Pulls new jobs from legitimate public job APIs.
2. Scores each job against your profile (skills, experience, location, salary and posting date), with a written explanation.
3. Stores jobs in SQLite so you never get the same alert twice, and marks jobs closed when they disappear from the employer's board.
4. Sends the best matches to Telegram (and optionally email).
5. Drafts a cover letter, recruiter message, resume tweaks and a checklist for a job you choose. You review them and apply yourself.

**It never applies for you.** It doesn't log into job portals, fill in forms, send messages to recruiters, or upload your documents.

---

## Contents
1. [How it works](#how-it-works)
2. [Job sources and their limits](#job-sources-and-their-limits)
3. [Local setup (macOS / Linux)](#local-setup-macos--linux)
4. [Your candidate profile](#your-candidate-profile)
5. [Telegram setup](#telegram-setup)
6. [GitHub Actions setup](#github-actions-setup)
7. [Schedule (UTC and IST)](#schedule-utc-and-ist)
8. [Database, persistence and duplicates](#database-persistence-and-duplicates)
9. [Optional AI and costs](#optional-ai-and-costs)
10. [Preparing an application](#preparing-an-application)
11. [Changing titles, locations and thresholds](#changing-titles-locations-and-thresholds)
12. [Adding a new job source](#adding-a-new-job-source)
13. [Privacy and security](#privacy-and-security)
14. [Troubleshooting](#troubleshooting)

---

## How it works

```
sources (Greenhouse, Lever, Ashby, SmartRecruiters, Remotive, RemoteOK, Adzuna…)
   │  normalised Job records
   ▼
matching.py  ── rule-based 0–100 score + reasons + concerns + unknowns
   ▼
database.py  ── SQLite: dedup by source ID → canonical URL → (company, title, location)
   ▼
ai_analysis.py (optional) ── summary, gaps, recommendation (kept separate from source data)
   ▼
notifications.py ── Telegram / email; a job is marked "notified" only after delivery is confirmed
```

| Path | Purpose |
|---|---|
| `jobs.py` | Entry point (`python jobs.py --help`) |
| `job_agent/sources/` | One adapter per job source |
| `job_agent/matching.py` | Scoring rules |
| `job_agent/database.py` | SQLite schema, migrations, dedup, statuses |
| `job_agent/notifications.py` | Telegram and email |
| `job_agent/ai_analysis.py` | Optional LLM analysis (OpenAI) |
| `job_agent/application_prep.py` | Drafts application material for review |
| `config/sources.json` | Which companies and search terms to query |
| `config/profile.example.json` | Public sample profile (no private data) |
| `tests/` | Offline tests with mocked APIs |

---

## Job sources and their limits

| Source | Access | Cost | Notes |
|---|---|---|---|
| **Greenhouse** company boards | Official public Job Board API | Free | Returns every open job for each listed company, so closed jobs are detected. India boards configured: Groww, Razorpay, InMobi, Druva, Rubrik, HackerRank, Observe.AI, plus global companies with India offices. |
| **Lever** company boards | Official public Postings API | Free | Meesho, Paytm, Zeta, Fam, CRED, Spotify. |
| **Ashby** company boards | Official public Job Posting API | Free | Sarvam, Atlan. |
| **SmartRecruiters** | Official public Posting API | Free | Keyword search within India for Bosch and Freshworks. The listing has no description, so these jobs score lower. |
| **Remotive** | Public API | Free | Remote jobs only, published 24 hours late. Their terms ask for at most about 4 calls a day and a link back to Remotive (alerts link to the original posting). |
| **RemoteOK** | Public API | Free | Remote jobs. Their terms ask you to credit Remote OK as the source. |
| **Adzuna (India)** | Official API with a free key | Free tier | The best broad India source available here. Skipped until you add `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. Estimated ("predicted") salaries are ignored. |
| **Arbeitnow** | Public API | Free | Mostly German and European jobs, so it's **off** by default. |

**Not automated: Naukri, LinkedIn, Indeed, Foundit, Wellfound, Cutshort, Instahyre, Hirist and iimjobs.** None of them offers a public API that permits this kind of use. Their terms forbid scraping, and most sit behind logins and anti-bot checks. The bot doesn't touch them. Instead:

- Run `python jobs.py links` to get ready-made search links for each portal.
- Turn on each portal's own email or app job alerts.

No tool finds every opening. The configured companies were checked on 2026-10-13 and returned live jobs, but boards change. A board that disappears is logged and skipped.

---

## Local setup (macOS / Linux)

```bash
git clone https://github.com/suryasrir/job-alert-bot.git
cd job-alert-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env                                  # fill in what you have; everything is optional
cp config/profile.example.json config/profile.json    # then edit it (git-ignored)

python -m pytest -q          # offline tests
python jobs.py run --dry-run # real fetch + scoring, no alerts sent
python jobs.py list          # best matches
python jobs.py show <id>     # one job with its score explanation
```

All commands:

| Command | What it does |
|---|---|
| `python jobs.py run [--dry-run] [--source greenhouse ...]` | Discover, score, store and notify |
| `python jobs.py list [--status shortlisted] [--min-score 60]` | List stored jobs |
| `python jobs.py show <id> [--full]` | Score breakdown, reasons and AI notes |
| `python jobs.py prepare <id> [--no-ai]` | Write an application pack for review |
| `python jobs.py mark <id> shortlisted\|dismissed\|applied_by_user --confirm` | Update status |
| `python jobs.py stats` | Count jobs by status |
| `python jobs.py links` | Manual search links for portals without an API |

You can shorten job IDs to their first few characters.

---

## Your candidate profile

The profile is loaded from the first of these that exists:

1. `CANDIDATE_PROFILE_JSON` environment variable (a **GitHub secret** in Actions)
2. `config/profile.json` (local, **git-ignored**)
3. `config/profile.example.json` (public sample: no salary, no contact details)

Main fields:

| Field | Meaning |
|---|---|
| `total_experience_years`, `ios_experience_years`, `react_native_experience_years` | Used to check experience requirements |
| `current_ctc_lpa`, `expected_ctc_lpa` | Salary check, only when a job lists an INR salary. **Keep these private.** |
| `notice_period_days` | Used in drafted messages |
| `preferred_locations`, `willing_to_relocate_to`, `open_to_other_india_locations`, `remote_ok` | Location scoring |
| `preferred_titles` | Exact title matches get full title points |
| `skills`, `skill_synonyms` | Skill matching (for example, "objc" counts as Objective-C) |
| `weights`, `thresholds` | Scoring settings (see below) |
| `summary`, `experience`, `education`, `certifications`, `contact` | Used **only** when drafting application material |

To use your full profile in GitHub Actions, paste the entire contents of `config/profile.json` into a secret named `CANDIDATE_PROFILE_JSON`.

---

## Telegram setup

1. In Telegram, open **@BotFather**, send `/newbot`, and follow the prompts. Copy the **bot token**.
2. Open your new bot and send it any message, for example `hi`.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789,...}`. That number is your **chat ID**.
4. Put both in `.env` for local runs, and in GitHub secrets for Actions.

Without these, the bot still finds and saves jobs. It notes in the run summary that alerts are off, and sends any unsent matches once Telegram is set up. Alerts are limited per run (`MAX_NOTIFICATIONS`, default 10). The rest arrive as one summary message.

Each alert shows the title, company, location and work arrangement, experience, salary if listed, score, up to 3 reasons, any concerns, the posting date, and a direct link to the original posting.

---

## GitHub Actions setup

1. Push this code to GitHub.
2. Go to **Settings → Secrets and variables → Actions → Secrets** and add:

   | Secret | Required? |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | For alerts |
   | `TELEGRAM_CHAT_ID` | For alerts |
   | `CANDIDATE_PROFILE_JSON` | Recommended. Without it, the public sample profile is used and salary matching is off. |
   | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Optional. Adds India-wide search. |
   | `OPENAI_API_KEY` | Optional. Paid. |

3. Optionally, under the **Variables** tab, add: `OPENAI_MODEL`, `MAX_NOTIFICATIONS`, `AI_MAX_JOBS_PER_RUN`.
4. Go to **Actions → Job Alert → Run workflow**. Tick **Dry run** for a first test.
5. Open the run to see its **Summary**: fetch counts, warnings and top matches.

**Secrets vs variables:** secrets are encrypted and masked in logs, so use them for anything private (tokens, keys, your profile with salary). Variables appear in plain text, so keep them to harmless settings.

The workflow runs the tests first, then discovery. A single failing source shows up as a warning, not a failure. The run only fails if **every** source fails or the tests fail.

---

## Schedule (UTC and IST)

GitHub cron uses **UTC**. The default `30 */6 * * *` runs at:

| UTC | IST (UTC+5:30) |
|---|---|
| 00:30 | 06:00 |
| 06:30 | 12:00 |
| 12:30 | 18:00 |
| 18:30 | 00:00 |

To change it, edit the `cron:` line in `.github/workflows/jobs.yml`. For example, `30 2,8,14 * * *` runs at 08:00, 14:00 and 20:00 IST. Scheduled runs often start 5–30 minutes late. GitHub also pauses schedules after 60 days with no activity in a public repository; re-enable them from the Actions tab.

**Free usage:** public repositories get free Actions minutes. A private repository on the Free plan gets 2,000 minutes a month. Each run takes about 1–3 minutes, so 4 runs a day comes to roughly 120–360 minutes a month.

---

## Database, persistence and duplicates

- Jobs are stored in `data/jobs.db` (SQLite, git-ignored). Schema changes are applied automatically as numbered migrations and never wipe existing data.
- **Persistence in Actions:** each run restores the latest copy of the database from the **Actions cache** and saves a new copy at the end. This is free and simple, with limits:
  - Unused cache entries are deleted after 7 days, and older ones go once the repo exceeds 10 GB. With 4 runs a day the cache stays fresh, but if the schedule stops for a week the database resets. The only effect is that some alerts may repeat.
  - For durable storage, keep the database somewhere external (for example, a private Turso or Supabase database) and change `Database`.
- **The database holds only public job data and match results.** It never stores your profile, salary expectations or contact details.
- **Overlapping runs:** the workflow's `concurrency` setting stops two runs at once, and local runs use a lock file.
- **Deduplication**, in order:
  1. Same source and same source job ID: update the existing record.
  2. Same canonical URL (tracking parameters such as `utm_*` removed, all other parameters kept): same job.
  3. Same company, title and location from a **different** source: stored and linked as a duplicate, not alerted again. Same-source jobs with identical titles are kept separate because they can be different vacancies.
- **Statuses:** `discovered`, `shortlisted`, `dismissed`, `application_prepared`, `applied_by_user`, `closed`, `expired`. Only you can set `applied_by_user`, with `--confirm`. A job is marked `closed` when the employer's board was fetched successfully and the job is gone, and `expired` when its listed deadline has passed.

---

## Optional AI and costs

| Feature | Cost |
|---|---|
| Discovery, scoring, dedup, Telegram, templated application packs | **Free** |
| Adzuna | Free key with a monthly quota |
| AI analysis (`OPENAI_API_KEY`) | **Paid, per use.** It analyses at most `AI_MAX_JOBS_PER_RUN` (default 5) new matches per run, each about 3–5k tokens. With a small model, expect well under US$1 a month at 4 runs a day. Check current OpenAI pricing. |

With AI turned on, the bot adds to each top match: a summary, required vs preferred skills, real experience gaps, and an apply, review or skip recommendation. AI output:
- is checked against a fixed format, and anything malformed is discarded;
- is stored separately (`ai_analysis`) and never changes the job's title, salary, URL or other source data;
- is labelled "AI note (unverified)" in alerts.

Job descriptions are treated as **untrusted input**. They are passed to the model as quoted data, with instructions to ignore any commands inside them. Your contact details and salary are never sent to the AI.

---

## Preparing an application

```bash
python jobs.py list --min-score 60
python jobs.py prepare 3f9a1c2b        # writes output/applications/<id>/application.md
```

The pack contains a tailored summary, the most relevant of your **existing** resume bullets, skills to highlight, a cover letter, a recruiter message, likely interview topics, concerns and missing information, and a checklist.

- Without AI it is filled in from templates, using only facts in your profile.
- With AI, the draft is rejected (and templates used instead) if it claims more years of experience than your profile states.
- Your original resume is never changed. The `output/` folder is git-ignored.
- **You submit the application yourself**, then record it with `python jobs.py mark <id> applied_by_user --confirm`.

`APPLICATION_MODE` must be `approval_required`. Any other value is refused. No submission feature exists.

---

## Changing titles, locations and thresholds

All of these live in your profile JSON:

```json
"preferred_titles": ["iOS Developer", "Swift Developer"],
"preferred_locations": ["Hyderabad", "Remote"],
"weights":    {"title": 25, "skills": 30, "experience": 15, "location": 15, "salary": 10, "recency": 5},
"thresholds": {"strong": 75, "good": 60, "possible": 45, "notify_min": 60, "store_min": 30}
```

- `notify_min`: lowest score that triggers an alert.
- `store_min`: jobs below this are dropped, unless the title is mobile-related.
- If a job lists no salary, the salary weight is spread across the other parts of the score, so a missing salary never lowers a score.
- Unrelated titles are capped at 25. Jobs outside India, or remote jobs restricted to other countries, are capped at 40. Jobs needing more than 2 extra years of experience are labelled **experience mismatch** and not alerted.

To change which companies are searched, edit `config/sources.json`. A company's board token is the slug in its jobs URL, for example `boards.greenhouse.io/<token>` or `jobs.lever.co/<company>`.

---

## Adding a new job source

1. Create a class in `job_agent/sources/` that subclasses `SourceAdapter`, sets `name`, and implements `fetch()`, returning a `FetchResult` of `Job` objects. Use `get_json()` from `job_agent/http.py`, which handles timeouts, retries and redaction.
2. Set `complete_listing = True` only if one fetch returns **every** open job on a board.
3. Register it in `REGISTRY` in `job_agent/sources/__init__.py`, and add a config block to `config/sources.json`.
4. Add a mocked test in `tests/test_sources.py`.

Use only documented or permitted APIs and feeds. Never bypass logins, CAPTCHAs or rate limits.

---

## Privacy and security

- **This repository is public.** Never commit `.env`, `config/profile.json`, resumes, `data/` or `output/`. All of them are in `.gitignore`.
- Secrets are only read from environment variables or GitHub secrets. Error messages strip secret values and URL query strings.
- Actions logs and run summaries in a public repo **can be read by anyone**. The bot prints only public job details and counts, never your profile.
- In a public repository, workflows triggered by fork pull requests can read Actions caches. This workflow has no `pull_request` trigger, and the cached database holds only public job data.
- The workflow token has read-only permissions (`contents: read`).
- Job text is converted to plain text, and HTML is escaped before going to Telegram. Nothing downloaded is ever executed.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No notification channel configured" | Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. |
| Telegram warning "did not confirm delivery" | Message your bot first, and check the chat ID. Unsent jobs are retried on the next run. |
| `greenhouse:<board>: HTTP 404` | The company moved or closed its board. Remove it from `config/sources.json`. |
| `adzuna: ... not set` | Add the free Adzuna keys, or set `"enabled": false`. |
| Repeated alerts after a long pause | The Actions cache expired (see persistence). |
| Few India iOS matches | Add Adzuna keys and more India company boards, and use `python jobs.py links` for the big portals. |
| Scheduled runs stopped | Re-enable the workflow in the Actions tab (GitHub pauses inactive repos). |
