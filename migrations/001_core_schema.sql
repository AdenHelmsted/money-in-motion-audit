-- #GOAL Establish the standalone evidence, entity, contact, and signal schema.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy name matching during resolution

CREATE SCHEMA core;

-- 1. Where data comes from -------------------------------------------------------

CREATE TABLE core.sources (
  code             text PRIMARY KEY CHECK (code ~ '^[a-z0-9_]+$'),  -- sec_form345, sec_form_d, linkedin, nppes
  name             text NOT NULL,
  homepage_url     text NOT NULL CHECK (homepage_url ~ '^https://'),
  license          text NOT NULL CHECK (license IN ('public_domain', 'government_open', 'vendor_contract')),
  permitted_uses   text NOT NULL,   -- plain words, written before the first load
  refresh_cadence  text NOT NULL CHECK (refresh_cadence IN ('daily', 'weekly', 'monthly', 'quarterly', 'annual', 'on_demand'))
);

CREATE TABLE core.fetches (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_code      text NOT NULL REFERENCES core.sources,
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  status           text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
  artifact_uri     text,            -- the file or request read
  artifact_sha256  text,
  records_seen     integer,
  records_new      integer,
  error            text,
  CHECK ((status = 'running') = (finished_at IS NULL)),
  CHECK ((status = 'failed') = (error IS NOT NULL))
);

-- One row per distinct version of a source record. A changed LinkedIn
-- profile is a new row, and the diff between rows is a job change.
CREATE TABLE core.raw_records (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_code      text NOT NULL REFERENCES core.sources,
  external_id      text NOT NULL,   -- the source's own key: accession number + line, LinkedIn profile id, NPI
  payload          jsonb NOT NULL,
  payload_sha256   text NOT NULL,
  observed_on      date NOT NULL,   -- when the source says it was true (the filing date), not when we fetched it
  fetch_id         bigint NOT NULL REFERENCES core.fetches,
  UNIQUE (source_code, external_id, payload_sha256)
);
CREATE INDEX raw_records_lookup_idx ON core.raw_records (source_code, external_id, observed_on DESC);

-- 2. Who: people and organizations ----------------------------------------------

CREATE TABLE core.people (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name        text NOT NULL,
  given_name       text,
  family_name      text,
  merged_into_id   uuid REFERENCES core.people,   -- set when resolution finds a duplicate; rows are never deleted
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (merged_into_id IS NULL OR merged_into_id <> id)
);
CREATE INDEX people_name_trgm_idx ON core.people USING gin (full_name gin_trgm_ops);

CREATE TABLE core.organizations (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name             text NOT NULL,
  kind             text NOT NULL CHECK (kind IN ('public_company', 'private_company', 'medical_practice', 'nonprofit', 'government', 'other')),
  naics_code       text CHECK (naics_code ~ '^[0-9]{2,6}$'),
  locality         text,
  region           text,            -- US state code
  merged_into_id   uuid REFERENCES core.organizations,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX organizations_name_trgm_idx ON core.organizations USING gin (name gin_trgm_ops);

-- External ids are the strongest matching key: the same CIK or NPI means the same entity.
CREATE TABLE core.person_identifiers (
  person_id        uuid NOT NULL REFERENCES core.people,
  scheme           text NOT NULL CHECK (scheme IN ('sec_cik', 'linkedin', 'npi', 'finra_crd')),
  value            text NOT NULL,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  PRIMARY KEY (scheme, value)
);
CREATE INDEX person_identifiers_person_idx ON core.person_identifiers (person_id);

CREATE TABLE core.organization_identifiers (
  organization_id  uuid NOT NULL REFERENCES core.organizations,
  scheme           text NOT NULL CHECK (scheme IN ('sec_cik', 'ein', 'ticker', 'linkedin', 'npi', 'domain')),
  value            text NOT NULL,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  PRIMARY KEY (scheme, value)
);
CREATE INDEX organization_identifiers_org_idx ON core.organization_identifiers (organization_id);

-- How each raw record was tied to an entity. This is the resolution audit
-- trail, so a wrong merge can be found and undone.
CREATE TABLE core.record_links (
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  person_id        uuid REFERENCES core.people,
  organization_id  uuid REFERENCES core.organizations,
  role             text NOT NULL CHECK (role IN ('subject', 'issuer', 'employer', 'related_person', 'sponsor')),
  method           text NOT NULL CHECK (method IN ('exact_identifier', 'name_and_employer', 'name_and_location', 'manual')),
  confidence       numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  linked_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((person_id IS NULL) <> (organization_id IS NULL))
);
CREATE INDEX record_links_raw_idx ON core.record_links (raw_record_id);
CREATE INDEX record_links_person_idx ON core.record_links (person_id) WHERE person_id IS NOT NULL;

-- 3. What we know about them -----------------------------------------------------

CREATE TABLE core.positions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL REFERENCES core.people,
  organization_id  uuid REFERENCES core.organizations,   -- null until the employer is resolved
  organization_name text NOT NULL,                       -- as the source wrote it
  title            text NOT NULL,
  seniority        text CHECK (seniority IN ('owner', 'c_suite', 'vp', 'director', 'manager', 'individual', 'board')),
  started_on       date,
  ended_on         date,
  is_current       boolean NOT NULL,                     -- not derived from ended_on: an undated past role has no end date either
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  CHECK (NOT is_current OR ended_on IS NULL),
  CHECK (ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on)
);
CREATE INDEX positions_person_idx ON core.positions (person_id);
CREATE INDEX positions_org_current_idx ON core.positions (organization_id) WHERE is_current;

CREATE TABLE core.education (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL REFERENCES core.people,
  school           text NOT NULL,
  degree           text,
  field            text,
  ended_year       smallint,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records
);
CREATE INDEX education_person_idx ON core.education (person_id);

CREATE TABLE core.locations (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL REFERENCES core.people,
  kind             text NOT NULL CHECK (kind IN ('home', 'work', 'unknown')),
  locality         text,
  region           text,            -- US state code
  postal_code      text,
  country          char(2) NOT NULL DEFAULT 'US',
  observed_on      date NOT NULL,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records
);
CREATE INDEX locations_region_idx ON core.locations (region, locality);

CREATE TABLE core.contact_points (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL REFERENCES core.people,
  kind             text NOT NULL CHECK (kind IN ('email', 'phone', 'postal_address', 'linkedin_url', 'website')),
  label            text NOT NULL CHECK (label IN ('work', 'personal', 'unknown')),
  value            text NOT NULL,   -- as the source gave it
  value_normalized text NOT NULL,   -- lowercased email, E.164 phone, standardized address
  verification     text NOT NULL DEFAULT 'unverified' CHECK (verification IN ('unverified', 'valid', 'risky', 'invalid')),
  verified_at      timestamptz,
  first_seen_on    date NOT NULL,
  last_seen_on     date NOT NULL,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  UNIQUE (person_id, kind, value_normalized),
  CHECK ((verification = 'unverified') = (verified_at IS NULL)),
  CHECK (last_seen_on >= first_seen_on)
);

-- Form 4 lines, typed: the basis for liquidity signals and concentration estimates.
CREATE TABLE core.equity_transactions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL REFERENCES core.people,
  issuer_id        uuid NOT NULL REFERENCES core.organizations,
  transaction_on   date NOT NULL,
  code             text NOT NULL,   -- the SEC transaction code (S sale, P purchase, M option exercise, A grant ...)
  shares           numeric(20,4) NOT NULL,
  price_per_share_cents bigint,
  shares_owned_after numeric(20,4),
  under_10b5_1_plan boolean,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records UNIQUE
);
CREATE INDEX equity_transactions_person_idx ON core.equity_transactions (person_id, transaction_on DESC);

CREATE TABLE core.relationships (
  person_a_id      uuid NOT NULL REFERENCES core.people,
  person_b_id      uuid NOT NULL REFERENCES core.people,
  kind             text NOT NULL CHECK (kind IN ('co_officers', 'co_founders', 'board_together', 'co_filers', 'colleagues')),
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  PRIMARY KEY (person_a_id, person_b_id, kind, raw_record_id),
  CHECK (person_a_id < person_b_id)   -- one row per pair
);

-- 4. Money in motion ----------------------------------------------------------------

CREATE TABLE core.signals (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id        uuid NOT NULL REFERENCES core.people,
  organization_id  uuid REFERENCES core.organizations,
  kind             text NOT NULL CHECK (kind IN (
                     'insider_sale', 'insider_option_exercise', 'private_raise', 'company_acquired',
                     'job_change', 'promoted_to_executive', 'retired', 'founded_company',
                     'new_practice', 'plan_sponsor_owner')),
  occurred_on      date NOT NULL,
  amount_cents     bigint CHECK (amount_cents >= 0),   -- the sale's proceeds or the raise's size, when known
  summary          text NOT NULL,                      -- one plain sentence an advisor reads
  confidence       numeric(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  detector_version text NOT NULL,                      -- which rule produced it, so a fixed rule can re-run
  detected_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX signals_recent_idx ON core.signals (kind, occurred_on DESC);
CREATE INDEX signals_person_idx ON core.signals (person_id, occurred_on DESC);

CREATE TABLE core.signal_evidence (
  signal_id        uuid NOT NULL REFERENCES core.signals ON DELETE CASCADE,
  raw_record_id    bigint NOT NULL REFERENCES core.raw_records,
  PRIMARY KEY (signal_id, raw_record_id)
);