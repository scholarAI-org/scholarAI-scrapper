-- TEST FIXTURE: the schema changes the scraper's tests apply on top of tests/legacy_schema.sql.
-- The real schema is owned by the backend; this only reproduces what the scraper needs.
BEGIN;

-- 1) New columns (nullable first, so existing rows are not rejected)
ALTER TABLE scholarships
  ADD COLUMN IF NOT EXISTS provider_name_ar       text,
  ADD COLUMN IF NOT EXISTS provider_name_en       text,
  ADD COLUMN IF NOT EXISTS host_countries         jsonb   NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS study_levels           text[]  NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS fields_of_study_scope  text,
  ADD COLUMN IF NOT EXISTS fields_of_study        jsonb   NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS financial_coverage_ar  text,
  ADD COLUMN IF NOT EXISTS financial_coverage_en  text,
  ADD COLUMN IF NOT EXISTS funding_min_months     integer,
  ADD COLUMN IF NOT EXISTS funding_max_months     integer,
  ADD COLUMN IF NOT EXISTS description_ar         text,
  ADD COLUMN IF NOT EXISTS description_en         text,
  ADD COLUMN IF NOT EXISTS benefits               jsonb   NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS review_flags           jsonb   NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS palestinians_eligible  boolean,
  ADD COLUMN IF NOT EXISTS audience               text,
  ADD COLUMN IF NOT EXISTS source_updated_at      timestamptz;

-- 2) Existing rows get a value before the column becomes NOT NULL
UPDATE scholarships SET fields_of_study_scope = 'ALL' WHERE fields_of_study_scope IS NULL;
ALTER TABLE scholarships ALTER COLUMN fields_of_study_scope SET NOT NULL;

-- 3) Constraints
ALTER TABLE scholarships
  ADD CONSTRAINT scholarships_source_uniq        UNIQUE (source, source_id),
  ADD CONSTRAINT scholarships_scope_chk          CHECK (fields_of_study_scope IN ('ALL','SPECIFIC')),
  ADD CONSTRAINT scholarships_funding_type_chk   CHECK (funding_type IN ('FULL','PARTIAL','NONE')),
  ADD CONSTRAINT scholarships_audience_chk       CHECK (audience IN ('PALESTINIAN','GAZA')),
  ADD CONSTRAINT scholarships_funding_months_chk CHECK (
        (funding_min_months IS NULL AND funding_max_months IS NULL)
     OR (funding_min_months BETWEEN 1 AND 120
         AND funding_max_months BETWEEN 1 AND 120
         AND funding_min_months <= funding_max_months));

COMMIT;

-- 4) Dedicated database user for the scraper (never the admin user)
-- CREATE ROLE scholarai_scraper LOGIN PASSWORD '<generate a long random one>';
-- GRANT SELECT, INSERT, UPDATE ON scholarships TO scholarai_scraper;
-- GRANT USAGE, SELECT ON SEQUENCE scholarships_id_seq TO scholarai_scraper;  -- if id is serial