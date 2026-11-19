-- #GOAL Register contract-authorized contact enrichment evidence.

INSERT INTO core.sources (code, name, homepage_url, license, permitted_uses, refresh_cadence)
VALUES (
    'contact_vendor', 'Authorized contact enrichment vendors', 'https://docs.apollo.io/reference/people-enrichment',
    'vendor_contract',
    'Retain and normalize contact evidence for already resolved prospects only where the applicable vendor agreement authorizes retention and internal research. Follow each vendor contract for deletion and permitted use.',
    'on_demand'
)
ON CONFLICT (code) DO NOTHING;
