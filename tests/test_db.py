"""to_row/validate are always tested; the SQL tests run only with TEST_DATABASE_URL.
They build the REAL current table (legacy_schema.sql) and apply the migration to it."""
import os
from pathlib import Path

import pytest

from scholarships import db

ITEM = {
    "source": "for9a", "source_id": 42, "source_url": "https://for9a.com/x",
    "title_ar": "منحة", "title_en": "Grant", "cover_image_url": "https://i/x.jpg",
    "provider_name_ar": "جامعة", "provider_name_en": "Uni",
    "host_countries": [{"code": "GB", "name_ar": "بريطانيا", "name_en": "United Kingdom"}],
    "study_levels": ["MASTER"], "fields_of_study_scope": "SPECIFIC",
    "fields_of_study": [{"code": "AI", "name_ar": "ذكاء", "name_en": "AI"}],
    "funding_type": "FULL", "funding_duration": {"min_months": 12, "max_months": 24},
    "deadline": "2999-01-01", "updated_at": "2026-10-01T10:00:00+00:00",
    "benefits": [{"order": 1, "text_ar": "راتب", "text_en": None}],
    "required_documents": [], "application_url": "https://apply", "review_flags": ["x"],
    "audience": "PALESTINIAN", "palestinians_eligible": True,
    "llm": {"provider": "groq", "model": "m", "evidence": {"funding_type": "q"}, "failed_attempts": ["secret"]},
}


def test_to_row_maps_contract_to_columns():
    r = db.to_row(ITEM)
    assert r["source_id"] == "42" and r["image_url"] == "https://i/x.jpg"
    assert r["apply_link"] == "https://apply" and r["funding_min_months"] == 12
    assert r["no_deadline"] is False and r["status"] == "pending"
    assert r["source_updated_at"].tzinfo is not None
    assert r["evidence"] == {"provider": "groq", "model": "m", "quotes": {"funding_type": "q"}}
    assert db.validate(r) == []
    assert r["title"] == "منحة" and r["ingestion_type"] == "scraped"
    assert r["slug"] == "grant-f42"


def test_slug_is_stable_and_unique_per_source():
    assert db.slugify("Fully Funded: Eiffel!", "for9a", 7) == "fully-funded-eiffel-f7"
    assert db.slugify("منحة فرنسا", "telegram", "eduscope2/194") == "منحة-فرنسا-t194"
    assert len(db.slugify("x" * 500, "for9a", 1)) <= 100


def test_too_long_contact_is_dropped_not_the_scholarship():
    r = db.to_row({**ITEM, "apply_phone": "1" * 80})
    assert r["apply_phone"] is None and "apply_phone_too_long" in r["review_flags"]
    assert db.validate(r) == []


def test_missing_scope_is_assumed_all_and_flagged():
    r = db.to_row({**ITEM, "fields_of_study_scope": None})
    assert r["fields_of_study_scope"] == "ALL" and "fields_scope_assumed_all" in r["review_flags"]


def test_no_deadline_when_deadline_empty():
    r = db.to_row({**ITEM, "deadline": None})
    assert r["deadline"] is None and r["no_deadline"] is True


def test_unknown_deadline_is_not_no_deadline():
    r = db.to_row({**ITEM, "deadline": None, "review_flags": ["deadline_missing"]})
    assert r["deadline"] is None and r["no_deadline"] is False


@pytest.mark.parametrize("patch", [
    {"title_ar": ""}, {"source_id": "x" * 51}, {"audience": None}, {"audience": "MARS"}, {"source": "other"}, {"funding_type": "HALF"},
    {"study_levels": ["MBA"]}, {"deadline": "2026-13-01"},
    {"funding_duration": {"min_months": 24, "max_months": 12}},
    {"funding_duration": {"min_months": 0, "max_months": 5}},
])
def test_validate_rejects(patch):
    assert db.validate(db.to_row({**ITEM, **patch}))


needs_db = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="no TEST_DATABASE_URL")


@pytest.fixture
def conn():
    c = db.connect(os.environ["TEST_DATABASE_URL"])
    with c.cursor() as cur:
        here = Path(__file__).parent
        cur.execute((here / "legacy_schema.sql").read_text())
        cur.execute((here / "schema_migration.sql").read_text())
    c.commit()
    yield c
    c.close()


def upsert(conn, item):
    with conn.cursor() as cur:
        cur.execute(db.UPSERT_SQL, db.adapt(db.to_row(item)))
        res = cur.fetchone()
    conn.commit()
    return res


@needs_db
def test_insert_update_and_reviewed_rows_are_protected(conn):
    assert upsert(conn, ITEM)[0] is True                                  # inserted
    assert upsert(conn, {**ITEM, "title_en": "Changed"})[0] is False      # updated
    with conn.cursor() as cur:
        cur.execute("SELECT title_en, status, host_countries->0->>'code', study_levels FROM scholarships")
        assert cur.fetchone() == ("Changed", "pending", "GB", ["MASTER"])
        cur.execute("UPDATE scholarships SET status='published'")
    conn.commit()
    assert upsert(conn, {**ITEM, "title_en": "Scraper again"}) is None    # protected
    with conn.cursor() as cur:
        cur.execute("SELECT title_en, status FROM scholarships")
        assert cur.fetchone() == ("Changed", "published")


@needs_db
def test_existing_versions_and_llm_failed_is_retried(conn):
    upsert(conn, ITEM)
    upsert(conn, {**ITEM, "source_id": 43, "review_flags": ["llm_failed"]})
    known = db.existing_versions(conn, "for9a")
    assert set(known) == {"42"} and known["42"] == db._parse_ts(ITEM["updated_at"])


@needs_db
def test_max_telegram_id(conn):
    for i in (5, 120, 99):
        upsert(conn, {**ITEM, "source": "telegram", "source_id": f"eduscope2/{i}"})
    assert db.max_telegram_id(conn, "eduscope2") == 120
    assert db.max_telegram_id(conn, "other") == 0


@needs_db
def test_audience_values_are_enforced(conn):
    import psycopg2
    assert upsert(conn, {**ITEM, "audience": "PALESTINIAN"})[0] is True
    with pytest.raises(psycopg2.errors.CheckViolation):
        upsert(conn, {**ITEM, "source_id": 9, "audience": "OTHER"})
    conn.rollback()


@needs_db
def test_required_columns_and_slug_are_not_overwritten(conn):
    upsert(conn, ITEM)
    upsert(conn, {**ITEM, "title_en": "A different english title"})
    with conn.cursor() as cur:
        cur.execute("SELECT title, slug, ingestion_type, application_status, required_documents FROM scholarships")
        title, slug, ing, app, docs = cur.fetchone()
    assert (title, slug, ing, app, docs) == ("منحة", "grant-f42", "scraped", "unknown", [])


@needs_db
def test_migration_is_safe_on_existing_rows():
    c = db.connect(os.environ["TEST_DATABASE_URL"])
    here = Path(__file__).parent
    with c.cursor() as cur:
        cur.execute((here / "legacy_schema.sql").read_text())
        cur.execute("INSERT INTO scholarships (source, source_id, title, status) VALUES ('for9a','1','old','published')")
        cur.execute((here / "schema_migration.sql").read_text())
        cur.execute("SELECT fields_of_study_scope, audience, review_flags FROM scholarships")
        assert cur.fetchone() == ("ALL", None, [])
    c.commit()
    c.close()