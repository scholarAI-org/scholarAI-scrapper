"""PostgreSQL helpers. The connection string comes from DATABASE_URL only."""
import json
import os
import re
from datetime import date, datetime, timezone

from psycopg2.extras import Json

LEVELS = {"BACHELOR", "MASTER", "PHD", "DIPLOMA", "POSTDOC"}
FUNDING = {"FULL", "PARTIAL", "NONE"}
SOURCES = {"for9a", "telegram"}
AUDIENCES = {"PALESTINIAN", "GAZA"}

# item key -> column. Everything else in the table is owned by the backend/Admin.
COLUMNS = [
    "source", "source_id", "source_url", "title", "slug", "title_ar", "title_en", "image_url",
    "provider_name_ar", "provider_name_en", "host_countries", "study_levels",
    "fields_of_study_scope", "fields_of_study", "funding_type",
    "financial_coverage_ar", "financial_coverage_en",
    "funding_min_months", "funding_max_months", "deadline", "no_deadline",
    "source_updated_at", "description_ar", "description_en", "benefits",
    "required_documents", "apply_link", "apply_email", "apply_phone",
    "palestinians_eligible", "audience", "review_flags", "evidence",
    "ingestion_type", "scraped_at", "status",
]
JSONB = {"host_countries", "fields_of_study", "benefits", "required_documents",
         "review_flags", "evidence"}
# never overwritten on conflict. The slug is part of public URLs, so it is fixed on first insert.
NOT_UPDATED = {"source", "source_id", "status", "slug"}

# limits of the existing varchar columns
MAX_LEN = {"source_id": 50, "slug": 100, "apply_email": 255, "apply_phone": 50}

UPSERT_SQL = (
    "INSERT INTO scholarships ({cols}) VALUES ({vals}) "
    "ON CONFLICT (source, source_id) DO UPDATE SET {sets} "
    # rows an Admin already reviewed (published / rejected) are never overwritten
    "WHERE scholarships.status = 'pending' "
    "RETURNING (xmax = 0) AS inserted"
).format(
    cols=", ".join(COLUMNS),
    vals=", ".join(f"%({c})s" for c in COLUMNS),
    sets=", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in NOT_UPDATED),
)


def connect(url=None):
    import psycopg2
    return psycopg2.connect(url or os.environ["DATABASE_URL"], connect_timeout=15)


def db_url():
    return os.getenv("DATABASE_URL", "").strip() or None


def dry_run():
    return db_url() is None or os.getenv("DB_DRY_RUN", "").strip() in ("1", "true", "yes")


def slugify(title, source, source_id):
    """readable slug (Arabic letters kept) + a source/id suffix so two scholarships never collide"""
    base = re.sub(r"[\s_]+", "-", re.sub(r"[^\w\s-]", "", (title or "").strip().lower())).strip("-")[:80]
    ident = re.sub(r"\W", "", str(source_id).rsplit("/", 1)[-1])  # "eduscope2/194" -> "194"
    return f"{base}-{source[:1]}{ident}"[:100].lstrip("-")


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _parse_ts(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_row(item):
    """Scraped record (frontend contract) -> table row dict. Pure, testable."""
    dur = item.get("funding_duration") or {}
    flags = list(item.get("review_flags") or [])
    scope = item.get("fields_of_study_scope")
    if scope is None:  # the table requires ALL/SPECIFIC; say it was an assumption
        scope = "ALL"
        if "fields_scope_assumed_all" not in flags:
            flags.append("fields_scope_assumed_all")
    for col, flag in (("apply_email", "apply_email_too_long"), ("apply_phone", "apply_phone_too_long")):
        if len(item.get(col) or "") > MAX_LEN[col]:  # drop the value, do not lose the scholarship
            item = {**item, col: None}
            flags.append(flag)
    source_id = str(item.get("source_id") or "")
    title_ar = item.get("title_ar")
    llm = item.get("llm") or {}
    evidence = ({"provider": llm.get("provider"), "model": llm.get("model"),
                 "quotes": llm.get("evidence") or {}} if llm else {})
    deadline = item.get("deadline") or None
    return {
        "source": item.get("source"),
        "source_id": source_id,
        "source_url": item.get("source_url"),
        "title": title_ar,  # legacy NOT NULL column
        "slug": slugify(item.get("title_en") or title_ar, item.get("source") or "x", source_id),
        "title_ar": title_ar,
        "title_en": item.get("title_en") or None,
        "image_url": item.get("cover_image_url"),
        "provider_name_ar": item.get("provider_name_ar"),
        "provider_name_en": item.get("provider_name_en"),
        "host_countries": item.get("host_countries") or [],
        "study_levels": list(item.get("study_levels") or []),
        "fields_of_study_scope": scope,
        "fields_of_study": item.get("fields_of_study") or [],
        "funding_type": item.get("funding_type"),
        "financial_coverage_ar": item.get("financial_coverage_ar"),
        "financial_coverage_en": item.get("financial_coverage_en"),
        "funding_min_months": dur.get("min_months"),
        "funding_max_months": dur.get("max_months"),
        "deadline": deadline,
        "no_deadline": deadline is None and "deadline_missing" not in (item.get("review_flags") or []),  # unknown is not "no deadline"
        "source_updated_at": _parse_ts(item.get("updated_at")),
        "description_ar": item.get("description_ar"),
        "description_en": item.get("description_en"),
        "benefits": item.get("benefits") or [],
        "required_documents": item.get("required_documents") or [],
        "apply_link": item.get("application_url"),
        "apply_email": item.get("apply_email"),
        "apply_phone": item.get("apply_phone"),
        "palestinians_eligible": item.get("palestinians_eligible"),
        "audience": item.get("audience"),
        "review_flags": flags,
        "evidence": evidence,
        "ingestion_type": "scraped",  # the column default used by the backend
        "scraped_at": datetime.now(timezone.utc),
        "status": "pending",
    }


def validate(row):
    """Problems that would make the database reject (or corrupt) the row."""
    p = []
    if row["source"] not in SOURCES:
        p.append(f"unknown source {row['source']!r}")
    if row["audience"] not in AUDIENCES:
        p.append(f"bad audience {row['audience']!r}")
    if not row["source_id"]:
        p.append("source_id is empty")
    for col, limit in MAX_LEN.items():
        if len(row[col] or "") > limit:
            p.append(f"{col} longer than {limit}")
    if not (row["title_ar"] or "").strip():
        p.append("title_ar is empty")
    if row["fields_of_study_scope"] not in ("ALL", "SPECIFIC"):
        p.append("bad fields_of_study_scope")
    if row["funding_type"] not in FUNDING | {None}:
        p.append(f"bad funding_type {row['funding_type']!r}")
    if set(row["study_levels"]) - LEVELS:
        p.append(f"bad study_levels {row['study_levels']}")
    if row["deadline"]:
        try:
            date.fromisoformat(str(row["deadline"]))
        except ValueError:
            p.append(f"bad deadline {row['deadline']!r}")
    lo, hi = row["funding_min_months"], row["funding_max_months"]
    if (lo is None) != (hi is None) or (lo is not None and not 1 <= lo <= hi <= 120):
        p.append(f"bad funding months {lo}-{hi}")
    return p


def adapt(row):
    """Wrap jsonb values for psycopg2."""
    return {k: (Json(v, dumps=_dumps) if k in JSONB else v) for k, v in row.items()}


EXISTING_SQL = (
    "SELECT source_id, source_updated_at FROM scholarships WHERE source = %s "
    # rows whose LLM step failed are retried even if the source did not change
    "AND NOT (review_flags ?| ARRAY['llm_failed', 'llm_skipped'])"
)


def existing_versions(conn, source):
    with conn.cursor() as cur:
        cur.execute(EXISTING_SQL, (source,))
        return {sid: ts for sid, ts in cur.fetchall()}


def max_telegram_id(conn, channel):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(split_part(source_id, '/', 2)::int) FROM scholarships "
            "WHERE source = 'telegram' AND source_id LIKE %s", (f"{channel}/%",))
        return cur.fetchone()[0] or 0