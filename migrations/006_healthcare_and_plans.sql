-- #GOAL Register the public provider, payment, and retirement-plan sources.

INSERT INTO core.sources (code, name, homepage_url, license, permitted_uses, refresh_cadence)
VALUES
    ('nppes', 'CMS National Plan and Provider Enumeration System',
     'https://download.cms.gov/nppes/NPI_Files.html', 'government_open',
     'Retain and use publicly disseminated provider identifiers and practice information for internal research. Cite the release as evidence.', 'monthly'),
    ('cms_open_payments', 'CMS Open Payments',
     'https://openpaymentsdata.cms.gov/datasets/download', 'government_open',
     'Retain and link public payment disclosures by provider identifier for internal research. Cite the disclosure and retain its reported payment context.', 'annual'),
    ('dol_form5500', 'DOL Form 5500 and Form 5500-SF',
     'https://www.dol.gov/agencies/ebsa/about-ebsa/our-activities/public-disclosure/foia/form-5500-datasets', 'government_open',
     'Retain and analyze public plan filings for internal research. Cite the filing and corroborate any person-level ownership claim.', 'monthly')
ON CONFLICT (code) DO NOTHING;
