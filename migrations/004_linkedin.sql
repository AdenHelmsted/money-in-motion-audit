-- #GOAL Register reviewed LinkedIn vendor evidence and durable lookup reservations.

INSERT INTO core.sources (code, name, homepage_url, license, permitted_uses, refresh_cadence)
VALUES (
    'linkedin',
    'LinkedIn profile evidence from authorized vendors',
    'https://www.linkedin.com/',
    'vendor_contract',
    'Retain vendor payloads and derive career facts for internal prospect research only when the applicable provider contract authorizes those uses. Follow each provider contract for retention and deletion.',
    'on_demand'
)
ON CONFLICT (code) DO NOTHING;

CREATE SCHEMA IF NOT EXISTS ingestion;

CREATE TABLE IF NOT EXISTS ingestion.linkedin_lookup_reservations (
    provider              text NOT NULL CHECK (provider IN ('coresignal', 'pdl', 'brightdata')),
    canonical_profile_id  text NOT NULL CHECK (canonical_profile_id <> ''),
    token                 uuid NOT NULL,
    expires_at            timestamptz NOT NULL,
    PRIMARY KEY (provider, canonical_profile_id)
);
