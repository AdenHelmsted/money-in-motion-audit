-- #GOAL Register the SEC insider dataset and its permitted research use.

INSERT INTO core.sources (code, name, homepage_url, license, permitted_uses, refresh_cadence)
VALUES (
    'sec_form345',
    'SEC Insider Transactions Data Sets (Forms 3, 4, and 5)',
    'https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets',
    'government_open',
    'Retain and analyze public EDGAR filing records for internal prospect research. Cite the underlying filing as evidence.',
    'quarterly'
)
ON CONFLICT (code) DO NOTHING;
