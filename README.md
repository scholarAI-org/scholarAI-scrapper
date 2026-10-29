# ScholarAI Scraper

Data extraction pipeline for **ScholarAI**. It collects scholarship announcements from public sources, cleans them, fills the missing structured fields with an LLM, and writes records that match the ScholarAI data contract.

Every record is stored as `status = pending` and reviewed by an Admin before publication. Anything the pipeline is unsure about is marked with a **review flag** instead of being silently guessed.

## Sources

| Source | Spider | Audience | What it is |
|---|---|---|---|
| [for9a](https://www.for9a.com) | `for9a` | `PALESTINIAN` | Structured scholarship listings (Arabic + English), filtered to Palestinian applicants. |
| Telegram channel `@eduscope2` | `telegram` | `GAZA` | Free-text posts for students from Gaza. Each post is checked ("is this a scholarship?") and the official page it links to is read for more detail. |

Without `DATABASE_URL` the spiders run in **dry-run** mode and only write JSON files to `output/`.

## How it works

```
source -> spider -> parser -> (browser fallback) -> LLM enrichment -> filter -> database
```

1. **Spider** downloads the listing and detail pages (for9a) or the latest channel posts (Telegram).
2. **Parser** turns raw HTML into a contract-shaped record without an LLM: titles, countries, levels, funding label, deadline, description, benefits.
3. **Browser fallback** (Telegram, optional): re-reads official pages that plain HTTP cannot read. It obeys `robots.txt`, uses no stealth tricks, and gives up on captchas.
4. **LLM enrichment** extracts the rest: fields of study, required documents, financial coverage, funding duration and, for Telegram, the full record. Each value comes with a verbatim quote from the source; unverifiable values are flagged.
5. **Filter** drops Telegram posts that are not scholarships and scholarships whose deadline has passed.
6. **Database** upserts on `(source, source_id)`. Rows an Admin already reviewed (`status != pending`) are never overwritten.

**Re-runs are cheap.** Records whose source version did not change are skipped before the LLM step, the Telegram spider reads only posts newer than the newest stored one, and LLM answers are cached.

### LLM providers

Groq and Gemini (free tiers) are supported, each with several API keys. Calls alternate between keys; a rate-limited key is skipped and a failing provider is paused so the other takes over.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env        # then add your keys
```

Tests (database tests run only if `TEST_DATABASE_URL` points to an empty PostgreSQL database):

```bash
pip install -r requirements-dev.txt
pytest -q
```

Optional, for the browser fallback: `playwright install chromium` (or have Google Chrome installed).

### Environment variables

Read from `.env` (never commit it).

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string. Empty = dry run |
| `DB_DRY_RUN` | `1` = read and validate everything, write nothing |
| `GROQ_API_KEY`, `GROQ_API_KEY_2`, ... | Groq keys (or `GROQ_API_KEYS=a,b,c`) |
| `GEMINI_API_KEY`, `GEMINI_API_KEY_2`, ... | Gemini keys (or `GEMINI_API_KEYS=a,b,c`) |
| `LLM_PROVIDERS` | Providers and their order (default `groq,gemini`) |
| `GROQ_MODEL`, `GEMINI_MODEL` | Defaults `openai/gpt-oss-120b`, `gemini-3.5-flash` |
| `LLM_TIMEOUT`, `LLM_COOLDOWN` | Seconds per call (45) / pause for a failing provider (300) |
| `GROQ_MIN_INTERVAL`, `GEMINI_MIN_INTERVAL` | Minimum seconds between calls on one key |
| `BROWSER_CHANNEL` | `chrome` or `msedge` to use an installed browser |

## Usage

```bash
scrapy crawl for9a -a limit=10          # first 10 scholarships
scrapy crawl telegram -a limit=10       # latest 10 channel posts
scrapy crawl telegram -a since_id=190   # only posts newer than id 190
scrapy crawl telegram -a browser=1      # re-read blocked pages with a real browser
scrapy crawl for9a -a llm=0             # skip the LLM step

DB_DRY_RUN=1 scrapy crawl for9a         # full run, nothing written
```

Each run writes `output/<spider>.json` and a database summary `output/db_report_<spider>.json`.

## Database

The scraper writes to the `scholarships` table (PostgreSQL). The table must have a unique constraint on `(source, source_id)`, because records are upserted on it, and the columns listed under **Data contract** below.

The backend owns the real schema. The tests rebuild the table (`tests/legacy_schema.sql` plus `tests/schema_migration.sql`) and check inserts, updates and the Admin-review protection.

## Data contract

Each record contains: `title_ar/en`, `cover_image_url`, `provider_name_ar/en`, `host_countries[]`, `study_levels[]` (`BACHELOR`, `MASTER`, `PHD`, `DIPLOMA`, `POSTDOC`), `fields_of_study_scope` (`ALL` / `SPECIFIC`), `fields_of_study[]`, `funding_type` (`FULL` / `PARTIAL`, or null when unknown), `financial_coverage_ar/en`, `funding_duration {min_months, max_months}`, `deadline` (`YYYY-MM-DD`), `updated_at` (UTC), `description_ar/en` (plain text), `benefits[]`, `required_documents[]`, `application_url`, `apply_email`, `apply_phone`, `audience`, `palestinians_eligible` and `review_flags[]`.

`palestinians_eligible` is `true` / `false` / `null`. For for9a it comes from the source's nationality criterion (open to all or names Palestinians -> `true`; a list without them -> `false`; no criterion -> `null`). For Telegram it comes from the LLM reading the post.

`no_deadline` is `true` only when the source says there is no deadline; an unknown deadline is `null` with the `deadline_missing` flag.

## Review flags

Flags tell the Admin what to double-check.

| Flag | Meaning |
|---|---|
| `funding_title_mismatch` | The title says "fully funded" but the source label says partial. |
| `fields_scope_assumed_all` | No field restriction was found, so "all fields" was assumed. |
| `llm_evidence_unverified:<field>` | The quote backing an LLM value was not found in the source text. |
| `benefits_count_mismatch` | Arabic and English benefit lists differ in length. |
| `official_page_unreachable` | The official page could not be read; data comes from the post only. |
| `official_page_shortener` | The official link was a link shortener whose page could not be read. |
| `deadline_passed` / `deadline_missing` | The deadline is in the past / not stated. |
| `application_closed` | The application form is closed. |
| `palestinians_not_eligible` | The nationality criterion lists nationalities without Palestinians. |
| `nationality_unknown` | No nationality criterion was found (for9a). |
| `description_llm_written` | The description was written by the LLM from the source text. |
| `llm_failed` / `llm_skipped` | The LLM step did not run for this record. |

## Project layout

```
scholarships/
  spiders/               for9a_spider.py, telegram_spider.py
  for9a_parser.py        for9a data -> contract record
  telegram_parser.py     channel page + official page parsing
  llm_enrich.py          LLM client (providers, keys, validation, evidence)
  llm_pipeline.py        LLM enrichment + cache (for9a)
  telegram_enrich.py     Telegram prompt, schema and merge
  telegram_pipeline.py   browser fallback + Telegram LLM pipeline
  browser_fetch.py       real-browser page reader (Playwright)
  filter_pipeline.py     drops non-scholarships and expired ones
  db.py, db_pipeline.py  record -> table row, validation, upsert, skip-unchanged
tests/                   unit tests (+ PostgreSQL tests with TEST_DATABASE_URL)
tools/check_output.py    CI check: empty output, LLM failures, database errors
.github/workflows/       daily run
output/                  generated files (git-ignored)
```

## Automation

`.github/workflows/daily-scrape.yml` runs every day at 05:00 UTC, and on demand from the Actions tab (with optional `limit` and `dry_run`). It runs the tests first, then one job per source; each job runs its spider and `tools/check_output.py`, which fails the run if the output is empty, most LLM calls failed, or database writes failed. Output and database reports are kept as workflow artifacts for 14 days.

The two sources run as separate jobs (`scrape-for9a`, `scrape-telegram`), so one failing source does not hide the other.

Repository secrets (Settings -> Secrets and variables -> Actions): `DATABASE_URL`, `GROQ_API_KEY`, `GROQ_API_KEY_2`, `GEMINI_API_KEY`, `GEMINI_API_KEY_2`. Unset ones are ignored. GitHub pauses scheduled workflows in a public repository after 60 days without activity; re-enable them from the Actions tab.

## Known limits

- Telegram posts edited after they were first scraped are not re-extracted.
- The same scholarship published by both sources is stored twice (no cross-source de-duplication).
- for9a descriptions can split a sentence across paragraphs (a quirk of the source HTML).

## Ethics

- Only public pages and public channel previews are read.
- `robots.txt` is respected; blocked pages are reported, not circumvented.
- Requests are throttled, and the LLM free tiers are rate-limited by design.
- Content is attributed to its source (`source_url`) and reviewed by an Admin before publication.