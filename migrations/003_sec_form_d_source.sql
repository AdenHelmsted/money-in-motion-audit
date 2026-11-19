-- #GOAL Register the public Form D dataset and its permitted research use.

INSERT INTO core.sources (code, name, homepage_url, license, permitted_uses, refresh_cadence)
VALUES (
    'sec_form_d',
    'SEC Form D Data Sets',
    'https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets',
    'public_domain',
    'Retain and analyze public Form D filing records for internal prospect research. Cite the underlying filing as evidence.',
    'quarterly'
)
ON CONFLICT (code) DO NOTHING;
