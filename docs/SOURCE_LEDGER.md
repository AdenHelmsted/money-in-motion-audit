# Source ledger

**Goal.** Record what each source contributes, its access model, and the basis for retaining it. The registered values are in migrations 002–006. Business ownership for these sources has yet to be assigned.

| Source | Access cost | Registered use and retention basis | Cadence |
| --- | --- | --- | --- |
| SEC Forms 3/4/5 | No access fee for public download | `government_open`. Retain and analyze public filing evidence for internal research. Cite the original filing. [SEC access](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) | Quarterly |
| SEC Form D | No access fee for public download | `public_domain` catalog classification. Retain and analyze public offering disclosures for internal research. Cite the original filing. [SEC Form D datasets](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets) | Quarterly |
| LinkedIn vendor evidence | Paid usage. Provider, unit price, and budget pending | `vendor_contract`. Use reviewed Coresignal, People Data Labs, or Bright Data evidence only under contract terms that authorize raw retention and internal use. Record deletion and redistribution limits before a paid pilot. | On demand |
| Contact enrichment | Paid usage. Operating vendor, unit price, and budget pending | `vendor_contract`. Retain and normalize data for resolved prospects only as permitted by the vendor agreement. Record retention, deletion, and approved uses before a paid pilot. | On demand |
| NPPES | No access fee for public download | `government_open`. Retain publicly disseminated NPI and practice evidence for internal research. Cite the release. [CMS NPPES files](https://download.cms.gov/nppes/NPI_Files.html) | Monthly with weekly inputs supported |
| CMS Open Payments | No access fee for public download | `government_open`. Retain public disclosures with their payment context. Attribute to a person when the individual NPI resolves. [CMS Open Payments downloads](https://openpaymentsdata.cms.gov/datasets/download) | Annual |
| DOL Form 5500 and 5500-SF | No access fee for standard public download | `government_open`. Retain public plan filings and corroborate owner attribution. Cite the filing and the ownership evidence. [DOL Form 5500 datasets](https://www.dol.gov/agencies/ebsa/about-ebsa/our-activities/public-disclosure/foia/form-5500-datasets) | Monthly |

Track storage and operator time during the public-data pilot. Vendor price, retention period, and rights to share or redistribute data depend on the applicable agreement. Record those decisions per vendor before enabling vendor traffic.
