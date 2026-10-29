-- TEST FIXTURE: the real table as it was before the scraper's columns (types copied from the database).
-- tests/schema_migration.sql is applied on top of it.
DROP TABLE IF EXISTS scholarships;
CREATE TABLE scholarships (
  id serial PRIMARY KEY,
  source varchar(20) NOT NULL, source_id varchar(50),
  title text NOT NULL, slug varchar(100), source_url text,
  organization_name text, country varchar(100), deadline date, no_deadline bool DEFAULT false,
  image_url text, description_html text, apply_link text, apply_email varchar(255), apply_phone varchar(50),
  pdf_url text, attachments text[], is_extension bool DEFAULT false,
  status varchar(20) DEFAULT 'pending', scraped_at timestamptz, reviewed_at timestamptz, reviewed_by varchar(100),
  study_level text[], funding_type varchar(100), majors json, required_documents json,
  updated_at timestamptz, rejection_reason text, admin_id int4, rejected_at timestamptz,
  eligibility_criteria json, ingestion_type varchar(20) NOT NULL DEFAULT 'scraped',
  title_ar text, title_en text, university_name text, funding_amount text, language_requirements text,
  opportunity_type varchar(30), eligible_nationalities text, academic_year text, gpa_requirement text,
  age_requirement text, opening_date date, evidence jsonb, source_confidence numeric,
  application_status varchar(20) NOT NULL DEFAULT 'unknown', allowed_nationalities text[],
  allowed_gender varchar(20), target_residencies text[], min_gpa_percentage numeric, max_age int4,
  required_languages text[], is_req_experience bool, is_softskills_required bool
);