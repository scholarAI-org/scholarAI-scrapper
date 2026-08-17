# Scholar Scraper — Palestinian Student Scholarship Aggregator

A Scrapy-based web scraping project that collects scholarship opportunities available to Palestinian students from two sources:

1. **for9a.com** — via the site's GraphQL API
2. **Palestinian Ministry of Education and Higher Education website** — via HTML parsing and PDF processing powered by Google Gemini AI

The final output is a set of scholarship records in a unified schema, ready to be sent to the **scholarAI** backend with `pending` status, awaiting admin review before publishing.

## Project Structure
scholarships/
├── items.py # Unified scholarship data schema
├── pipelines.py # Post-crawl processing: filtering expired items, saving results
├── settings.py # Scrapy settings (User-Agent, download delay, etc.)
├── utils_ministry.py # Helper functions: Arabic text direction fixing, deadline extraction, extension-post detection
└── spiders/
├── for9a_spider.py # Spider for for9a.com (GraphQL)
└── ministry_spider.py # Spider for the Ministry website (PDF + Gemini)

.github/workflows/
└── daily-scrape.yml # Scheduled daily run via GitHub Actions

## Requirements

- Python 3.11+
- A Google Gemini API key (free tier available at [aistudio.google.com](https://aistudio.google.com/apikey))

## Installation

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root directory and add:
GEMINI_API_KEY=your_key_here

**Note:** The `.env` file is excluded from version control via `.gitignore`. Never commit API keys directly into the code. When running via GitHub Actions, the key is instead read from a repository secret (see [Scheduled Runs](#scheduled-runs-github-actions) below).

## Usage

### for9a spider:
```bash
scrapy crawl for9a -o for9a_output.json
```

### Ministry spider:
```bash
scrapy crawl ministry
```
(Results are automatically saved to `ministry_output.json` via the pipeline.)

## How It Works

### for9a Spider
Queries the site's GraphQL endpoint (`lara.for9a.com/graphql`) to retrieve a paginated list of scholarships filtered for Palestinian nationality, then follows each scholarship's page to extract full details (description, benefits, qualifications) from embedded `__NEXT_DATA__`, and finally queries the `applicationInfo` endpoint to retrieve the direct external application link. Expired opportunities are filtered out before being yielded.

### Ministry Spider
Crawls the Ministry's scholarship blog listing pages (limited to `max_pages`, currently the first page, based on the observation that active scholarships are concentrated there) and follows each post, classifying it into one of three categories:

- **Extension notice** — a deadline update for an existing scholarship, identified by a title starting with "تمديد التقديم". The new deadline is extracted directly from the post's text (not a PDF). Extension records are **not automatically linked** to their original scholarship — they're saved as independent `pending` records for the admin to review and match manually.
- **Non-scholarship post** — results announcements or general news (no PDF attachments), skipped.
- **Scholarship post** — contains one or more "instructions" PDFs, each representing a distinct scholarship program (e.g., a single post may cover multiple universities/organizations). Each program's instructions PDF is grouped with its related links: an "apply" link (`apply_link`) and any additional attachment PDFs (`attachments`, e.g. eligible specializations lists) — these attachments are stored as raw links, not processed by Gemini, to conserve API quota.

All instructions PDFs belonging to the same post are uploaded together and sent to Gemini in a **single combined request**, which returns structured data (title, country, organization, deadline, description, benefits, requirements) for each program at once — minimizing the number of Gemini API calls per post.

### Gemini Quota Management
The Gemini free tier is limited to 20 requests/day per project. To avoid crashing mid-run with rate-limit errors, the Ministry spider tracks its own request count (`gemini_requests_made`) and stops itself gracefully once it reaches `max_gemini_requests_per_run` (currently 18, leaving a small safety margin). Any posts not processed in a run will be picked up automatically on the next scheduled run.

## Scheduled Runs (GitHub Actions)

The spiders can run automatically once every 24 hours via `.github/workflows/daily-scrape.yml`, using GitHub's built-in scheduler (cron) — no dedicated server required. The workflow can also be triggered manually from the GitHub Actions tab.

**Setup:**
1. Add your Gemini API key as a repository secret: **Settings → Secrets and variables → Actions → New repository secret**, named `GEMINI_API_KEY`.
2. Adjust the `cron` schedule in the workflow file if a different run time is needed.

## Duplicate Prevention (Work in Progress)

The goal is for each spider run to stop as soon as it encounters a post already present in the backend database (since listings are ordered newest-first, everything after that point is guaranteed to already exist). This requires backend API endpoints:

- `GET /api/scholarships/exists?source={source}&source_id={source_id}` — check if a record already exists
- `POST /api/scholarships` — submit a new record

This integration is pending final API details (base URL, authentication method) from the backend team. Once available, `_already_in_database()` in `ministry_spider.py` will be connected to the real endpoint, replacing the current placeholder.

## Database Schema (Reference)

Provided to the backend team for table creation. See `docs/schema.sql` (or the schema shared separately) for the full `CREATE TABLE` statement. Key points:

- Uniqueness is enforced on `(source, source_id)` together, **not** `source_id` alone — since `for9a` and `ministry` use independent ID sequences that can coincidentally collide.
- `deadline` is stored as `DATE`, always provided as `YYYY-MM-DD` or `NULL` by the scrapers.
- `attachments` is a text array, since Ministry posts may include multiple supplementary PDFs per program.
- `status` defaults to `'pending'` for every scraped record; only an admin transitions it to `'published'` or `'rejected'`.

## Known Limitations

1. **Gemini API free tier is capped at 20 requests/day**, per Google Cloud project. The spider self-limits to 18 requests/run as a safety margin. Since enabling billing removes the free tier entirely (rather than extending it) on the same project, there is currently no free way to raise this cap — see the spider's inline comments for details. Options going forward: enable billing on a dedicated project and accept the cost, or use a separate Google account/project to get an independent daily quota.
2. **Deadline filtering is strict (no grace period).** A scholarship whose deadline has passed — even by one day — is filtered out entirely and won't appear in the output. This was a deliberate decision to keep the filtering logic simple and predictable.
3. **The ministry spider is limited to the first page** (`max_pages = 1`) of the listing, based on the assumption that no more than one page's worth of new scholarships will appear within a 24-hour scheduling window. This can be increased if needed.
4. **Extension-post matching to the original scholarship is manual.** The spider extracts extension notices as independent records but does not attempt to automatically link them to the scholarship they extend — this is left to admin review.
5. **Duplicate-check against the backend database is not yet wired up** (see [Duplicate Prevention](#duplicate-prevention-work-in-progress) above).

## Suggested Next Steps

- Complete the backend API integration (`_already_in_database`, submitting new records) once endpoint details are confirmed
- Consider raising the Gemini request cap (paid tier or a secondary Google account/project) if scan coverage needs to expand beyond `max_pages = 1`
- Add automated tests for the deadline-extraction and program-grouping logic, given how many edge cases were discovered manually during development