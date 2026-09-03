# Scholar Scraper — Palestinian Student Scholarship Aggregator

A Scrapy-based web scraping project that collects scholarship opportunities available to Palestinian students from two sources:

1. **for9a.com** — via the site's GraphQL API.
2. **Palestinian Ministry of Education and Higher Education website** — via HTML parsing and PDF processing powered by Google Gemini AI.

The scraper produces scholarship records using a **unified schema** and stores active records directly in **PostgreSQL**, with `pending` status so they can be reviewed by an administrator before being published on the `scholarAI` platform.

---

## Project Structure

```text
scholarships/
│
├── items.py
│   └── Unified scholarship data schema
│
├── pipelines.py
│   └── Ministry-specific filtering
│
├── postgres_pipeline.py
│   └── PostgreSQL storage and database error handling
│
├── utils_html.py
│   └── HTML sanitization using nh3
│
├── utils_ministry.py
│   └── Ministry-specific helper functions:
│       - Arabic text direction fixing
│       - Deadline extraction
│       - Extension-post detection
│
├── settings.py
│   └── Scrapy configuration and pipeline ordering
│
└── spiders/
    │
    ├── for9a_spider.py
    │   └── for9a.com GraphQL scraper
    │
    └── ministry_spider.py
        └── Ministry scraper with PDF + Gemini processing

.github/
└── workflows/
    └── daily-scrape.yml
        └── Automated daily scraping via GitHub Actions
```

---

# Requirements

* Python 3.11+
* PostgreSQL database
* Google Gemini API key
* Scrapy
* `psycopg2`
* `python-dotenv`
* `nh3`

All Python dependencies are listed in:

```text
requirements.txt
```

---

# Installation

Clone the repository and install the dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
GEMINI_API_KEY=your_gemini_api_key
DATABASE_URL=your_postgresql_connection_string
```

The `.env` file must **not** be committed to GitHub.

It should be included in `.gitignore`:

```text
.env
```

API keys and database credentials should always be stored securely.

---

# Environment Variables

The project uses the following environment variables:

| Variable         | Purpose                               |
| ---------------- | ------------------------------------- |
| `GEMINI_API_KEY` | Authentication for Google Gemini API  |
| `DATABASE_URL`   | PostgreSQL database connection string |

When running locally, these values are loaded from `.env`.

When running through GitHub Actions, they are provided through **GitHub Repository Secrets**.

---

# Unified Scholarship Schema

All spiders use the same `ScholarshipItem` schema defined in `items.py`.

The schema contains:

### Identification

* `source`
* `source_id`
* `title`
* `slug`
* `source_url`

### Summary

* `organization_name`
* `country`
* `deadline`
* `no_deadline`
* `image_url`

### Full Details

* `description_html`
* `benefits_html`
* `qualifications_html`

### Application Information

* `apply_link`
* `apply_email`
* `apply_phone`

### Ministry-specific fields

* `pdf_url`
* `attachments`
* `is_extension`

### Review Workflow

* `status`
* `scraped_at`
* `reviewed_at`
* `reviewed_by`

All sources therefore produce records with the same database structure.

---

# How the Scraping Pipeline Works

The project uses multiple Scrapy pipelines in a specific order.

```text
Spider
  │
  ▼
MinistryPipeline
  │
  │  Filter expired / extension records
  ▼
PostgreSQLPipeline
  │
  │  Sanitize HTML
  │  Insert / Update database
  ▼
PostgreSQL
```

The pipeline order is configured in `settings.py`:

```python
ITEM_PIPELINES = {
    "scholarships.pipelines.MinistryPipeline": 300,
    "scholarships.postgres_pipeline.PostgreSQLPipeline": 400,
}
```

The order is important because Ministry records are filtered **before** they reach PostgreSQL.

---

# MinistryPipeline

`MinistryPipeline` is responsible for Ministry-specific validation and filtering.

## 1. Source Filtering

The pipeline only processes records where:

```python
item.get("source") == "ministry"
```

Records from other sources are passed through unchanged.

---

## 2. Extension Notices

Ministry extension notices are detected using:

```python
is_extension
```

If a record is an extension notice, it is dropped:

```python
raise DropItem("Ministry extension notice")
```

Extension notices are therefore not stored as normal scholarship records.

---

## 3. Deadline Validation

The pipeline reads the `deadline` field and converts it from:

```text
YYYY-MM-DD
```

into a Python `date`.

For example:

```text
2026-09-15
```

becomes a `date` object.

If the deadline is invalid, the item is dropped.

---

## 4. Expired Scholarship Filtering

The project uses:

```python
is_deadline_still_valid()
```

to determine whether a scholarship is still active.

If the deadline has already passed, the scholarship is dropped before reaching PostgreSQL.

This means:

```text
Expired scholarship
        ↓
   DropItem
        ↓
Not stored in database
        ↓
Not available for publication
```

There is currently **no grace period**.

A scholarship whose deadline has passed is removed from the scraping pipeline immediately.

---

# PostgreSQL Storage

`PostgreSQLPipeline` is responsible for storing valid records in PostgreSQL.

The pipeline:

1. Opens a PostgreSQL connection when the spider starts.
2. Receives valid scraped items.
3. Sanitizes HTML fields.
4. Inserts or updates the scholarship.
5. Commits the transaction.
6. Tracks successful and failed database writes.
7. Closes the database connection when the spider finishes.

---

# HTML Sanitization

Scholarship descriptions, benefits, and qualifications are stored as HTML because the frontend needs to preserve formatting such as:

* paragraphs
* headings
* ordered lists
* unordered lists
* links
* bold text

However, raw HTML should not be stored and rendered without sanitization.

The project therefore uses **`nh3`** to sanitize HTML before inserting it into PostgreSQL.

The following fields are sanitized:

```python
description_html
benefits_html
qualifications_html
```

The sanitization logic is centralized in:

```text
utils_html.py
```

and called by the PostgreSQL pipeline:

```python
item["description_html"] = sanitize_html(
    item.get("description_html")
)

item["benefits_html"] = sanitize_html(
    item.get("benefits_html")
)

item["qualifications_html"] = sanitize_html(
    item.get("qualifications_html")
)
```

This provides a security layer against potentially dangerous HTML content while preserving the formatting required by the frontend.

---

# PostgreSQL Insert / Update Logic

The project uses PostgreSQL's:

```sql
ON CONFLICT (source, source_id)
DO UPDATE
```

to prevent duplicate records.

The unique identity of a scholarship is:

```text
(source, source_id)
```

rather than `source_id` alone.

This is necessary because different sources can use the same numerical IDs.

For example:

```text
for9a + 30171
ministry + 30171
```

are two different records.

The database therefore enforces uniqueness on the combination:

```text
(source, source_id)
```

---

# Updating Existing Scholarships

If the same scholarship is scraped again, PostgreSQL does not create another row.

Instead, the existing record is updated with the latest scraped information.

Fields such as:

* title
* source URL
* organization
* country
* deadline
* image
* description
* benefits
* qualifications
* application information
* attachments
* scraped timestamp

are updated.

This makes repeated scraping safe and prevents duplicate rows.

---

# Database Error Handling

The PostgreSQL pipeline handles database errors using transactions.

If an insert/update fails:

```python
self.connection.rollback()
```

is executed.

This is important because PostgreSQL transactions enter an aborted state after a failed SQL command.

Without a rollback, subsequent database commands would produce errors such as:

```text
current transaction is aborted,
commands ignored until end of transaction block
```

The pipeline therefore performs:

```text
SQL operation
     │
     ├── Success → COMMIT
     │
     └── Failure → ROLLBACK
```

---

# Scraping Statistics

The PostgreSQL pipeline tracks:

```python
success_count
error_count
```

At the end of the spider run, it logs:

```text
PostgreSQL connection closed.
Success: X, Errors: Y
```

This provides a simple summary of the database operations performed during the run.

If **all database writes fail**, the pipeline raises a `RuntimeError`.

This causes the Scrapy process to exit with a failure status, allowing GitHub Actions to recognize that the scheduled job failed.

---

# for9a Spider

The `for9a` spider communicates with the site's GraphQL API.

It retrieves scholarship opportunities filtered for Palestinian nationality.

The spider:

1. Queries the GraphQL endpoint.
2. Retrieves paginated scholarship records.
3. Follows each scholarship page.
4. Extracts detailed information from embedded `__NEXT_DATA__`.
5. Extracts:

   * description
   * benefits
   * qualifications
6. Queries the `applicationInfo` endpoint to retrieve the external application link.
7. Builds a unified `ScholarshipItem`.
8. Yields the item to the Scrapy pipeline.

The GraphQL endpoint used by the scraper is:

```text
https://lara.for9a.com/graphql
```

---

# Ministry Spider

The Ministry spider collects scholarship announcements from the Palestinian Ministry of Education and Higher Education website.

The spider processes Ministry posts into three categories.

## 1. Extension Notice

A post whose title indicates:

```text
تمديد التقديم
```

is treated as an extension notice.

The new deadline is extracted directly from the post text.

Extension notices are not automatically linked to their original scholarship.

They are currently treated separately and can be reviewed manually if needed.

---

## 2. Non-Scholarship Post

Posts such as:

* results announcements
* general news
* posts without scholarship PDFs

are skipped.

---

## 3. Scholarship Post

A scholarship post can contain one or more instruction PDFs.

Each instructions PDF can represent a separate scholarship program.

The spider groups:

```text
Instructions PDF
        +
Application link
        +
Additional attachments
```

into a scholarship record.

Additional PDFs such as eligible specialization lists are stored in:

```python
attachments
```

as raw URLs.

They are not sent to Gemini unnecessarily, helping conserve API quota.

---

# Gemini Processing

The Ministry spider uses Google Gemini to extract structured scholarship information from PDF documents.

Instead of sending every PDF through a separate Gemini request, all relevant instructions PDFs belonging to the same Ministry post are uploaded and processed together in a single request.

Gemini extracts structured information such as:

* title
* country
* organization
* deadline
* description
* benefits
* qualifications

This reduces the number of API requests required per Ministry post.

---

# Gemini Quota Management

The Ministry spider tracks the number of Gemini requests made during each run using:

```python
gemini_requests_made
```

The scraper currently limits itself to:

```text
18 Gemini requests per run
```

This leaves a safety margin below the configured daily free-tier limit.

When the limit is reached, the spider stops processing additional Gemini-dependent posts rather than continuing until the API returns rate-limit errors.

Unprocessed opportunities can then be picked up during the next scheduled run.

---

# Deadline Handling

Deadlines are normalized to:

```text
YYYY-MM-DD
```

before being stored in PostgreSQL.

Scholarships with expired deadlines are removed by `MinistryPipeline` before database insertion.

Scholarships without a known deadline can remain valid using:

```python
no_deadline = True
```

and:

```python
deadline = None
```

---

# Scholarship Status

Every scraped scholarship is initially stored with:

```text
status = pending
```

The scraper does not publish scholarships automatically.

The intended workflow is:

```text
Scraper
   ↓
PostgreSQL
   ↓
pending
   ↓
Admin Review
   ↓
published / rejected
```

Only the backend/admin workflow should change the status to:

```text
published
```

or:

```text
rejected
```

---

# Running the Spiders Locally

## for9a

Run:

```bash
scrapy crawl for9a
```

The scraped records are processed by the configured pipelines and stored in PostgreSQL.

---

## Ministry

Run:

```bash
scrapy crawl ministry
```

Valid Ministry scholarships are stored directly in PostgreSQL.

Expired Ministry scholarships and extension notices are dropped before database insertion.


---

# GitHub Actions — Automated Daily Scraping

The project includes:

```text
.github/workflows/daily-scrape.yml
```

GitHub Actions runs the scraping workflow automatically every 24 hours.

The workflow can also be started manually using:

```text
GitHub → Actions → Daily Scholarship Scrape → Run workflow
```

The scheduled workflow currently runs at:

```text
06:00 UTC
```

using:

```yaml
schedule:
  - cron: "0 6 * * *"
```

---

# GitHub Repository Secrets

The GitHub Actions workflow requires the following repository secrets:

## 1. GEMINI_API_KEY

This is the Google Gemini API key used by the Ministry spider.

Add it from:

```text
Repository
→ Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

Set:

```text
Name:
GEMINI_API_KEY

Secret:
your actual Gemini API key
```

---

## 2. DATABASE_URL

This is the PostgreSQL connection string used by `PostgreSQLPipeline`.

Add another repository secret:

```text
Name:
DATABASE_URL

Secret:
your PostgreSQL connection string
```

The actual database password should **never** be written directly inside:

```text
daily-scrape.yml
```

or committed to GitHub.

---

# GitHub Actions Workflow

The workflow provides the secrets to each spider through environment variables.

Example:

```yaml
- name: Run for9a spider
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
  run: scrapy crawl for9a
```

and:

```yaml
- name: Run ministry spider
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
  run: scrapy crawl ministry
```

This allows the same codebase to run locally and inside GitHub Actions without exposing credentials.

---

# Failure Detection

The automated workflow can detect failures from the Scrapy process.

For example, if the PostgreSQL pipeline cannot write **any** records, it raises a `RuntimeError`.

This causes the corresponding GitHub Actions step to fail.

The workflow therefore provides a basic monitoring mechanism:

```text
Daily GitHub Action
        ↓
Run scraper
        ↓
      ┌───────────────┐
      │ Scraping OK?  │
      └───────┬───────┘
              │
       ┌──────┴──────┐
       ↓             ↓
      Yes            No
       │             │
       ↓             ↓
 Database        GitHub Action
 updated             Failed
```

GitHub can then notify the repository owner about failed workflow runs according to the account's notification settings.

---

# Duplicate Prevention

The database already prevents duplicates using:

```text
UNIQUE (source, source_id)
```

The scraper also uses PostgreSQL's:

```sql
ON CONFLICT (source, source_id)
DO UPDATE
```

so rerunning a spider updates existing records instead of inserting duplicates.

A more advanced optimization is still planned:

The scraper should eventually check the backend before processing older records and stop when it reaches a record that is already known.

The planned backend endpoints are:

```text
GET /api/scholarships/exists?source={source}&source_id={source_id}
```

and:

```text
POST /api/scholarships
```

This integration depends on the final backend API specification, including:

* Base URL
* Authentication
* Request format
* Response format

---

# Current Limitations

## 1. Gemini API Quota

The Ministry spider currently limits Gemini usage to approximately:

```text
18 requests per run
```

to avoid exceeding the configured daily quota.

---

## 2. Strict Deadline Filtering

Expired scholarships are not stored in PostgreSQL.

There is currently no grace period.

---

## 3. Ministry Listing Coverage

The Ministry spider currently processes only the configured number of listing pages (`max_pages`).

The current configuration is based on the assumption that new active opportunities can be discovered within the scanned pages during a daily run.

This can be increased if required.

---

## 4. Manual Extension Matching

Extension notices are not automatically connected to their original scholarship.

The admin is responsible for reviewing and matching them.

---

## 5. Backend Duplicate Check

The advanced duplicate-check API integration is still pending final backend API details.

Database-level duplicate prevention is already implemented through:

```text
(source, source_id)
```

---

# Security Considerations

The project follows several basic security practices:

### Environment variables

Secrets are loaded using:

```python
os.getenv()
```

rather than hard-coded credentials.

### `.env`

The local `.env` file must not be committed.

### HTML sanitization

HTML obtained from external websites is sanitized using `nh3` before being stored in the database.

### Database transactions

Failed database operations are rolled back to prevent the PostgreSQL connection from remaining in an aborted transaction state.

### GitHub Secrets

Production credentials are stored in GitHub Repository Secrets instead of being written into the workflow file.

---

# Data Flow

The complete system currently follows this architecture:

```text
                   ┌──────────────────┐
                   │     for9a.com    │
                   └────────┬─────────┘
                            │
                         GraphQL
                            │
                            ▼
                    ┌───────────────┐
                    │  for9a Spider │
                    └───────┬───────┘
                            │
                            │
                            ▼
┌──────────────────┐   ┌───────────────┐
│ Ministry Website │──▶│ Ministry      │
│ HTML + PDFs      │   │ Spider        │
└──────────────────┘   └───────┬───────┘
                               │
                         Gemini AI
                               │
                               ▼
                       ScholarshipItem
                               │
                               ▼
                    ┌──────────────────┐
                    │ MinistryPipeline │
                    │                  │
                    │ • Extensions     │
                    │ • Invalid dates  │
                    │ • Expired items  │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ PostgreSQL       │
                    │ Pipeline         │
                    │                  │
                    │ • HTML sanitize  │
                    │ • INSERT/UPDATE  │
                    │ • Transactions   │
                    └────────┬─────────┘
                             │
                             ▼
                       PostgreSQL DB
                             │
                             ▼
                    ┌──────────────────┐
                    │   scholarAI      │
                    │     Backend      │
                    └────────┬─────────┘
                             │
                             ▼
                       Admin Review
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
                published          rejected
```

---

# Future Improvements

The following improvements are planned:

* Complete backend API integration.
* Implement efficient duplicate detection against the backend.
* Submit newly scraped records directly through the backend API if required by the final architecture.
* Add automated tests for:

  * deadline extraction
  * extension detection
  * PDF/program grouping
  * HTML sanitization
  * database insertion/update logic
* Expand Ministry listing-page coverage if necessary.
* Improve monitoring and email notifications for failed scheduled runs.
* Consider increasing Gemini processing capacity if the number of Ministry opportunities grows.

---

# Summary

Scholar Scraper is an automated data collection system designed to continuously collect scholarship opportunities from **for9a.com** and the **Palestinian Ministry of Education and Higher Education**.

The current system:

* Uses Scrapy for web scraping.
* Uses GraphQL for for9a.
* Uses PDF processing and Gemini AI for Ministry scholarships.
* Uses a unified `ScholarshipItem` schema.
* Filters expired Ministry scholarships before database insertion.
* Sanitizes externally sourced HTML using `nh3`.
* Stores valid records directly in PostgreSQL.
* Prevents duplicate records using `(source, source_id)`.
* Uses PostgreSQL transactions with rollback on failure.
* Tracks successful and failed database writes.
* Stores new records with `pending` status for admin review.
* Runs automatically every 24 hours through GitHub Actions.
* Keeps API keys and database credentials in environment variables/GitHub Secrets.
* Detects complete database-write failures so the scheduled GitHub workflow can report a failed run.
