# Money in Motion orchestrator handoff

The ingestion gates cover SEC filings, reviewed LinkedIn evidence, authorized contact enrichment, NPPES, Open Payments, and Form 5500 in the standalone PostgreSQL schema. The [README](../README.md) has local setup, fixture ingestion, and rebuild commands.

Use the [current handoff status](CURRENT_HANDOFF_STATE.md) for the latest verification and the [one-quarter SEC insider pilot guide](SEC_PILOT.md) for the filing review. Give the auditor the report directory from `mim pilot-sec` and record the filing review there. Project responsibilities and source terms are indexed in the [architecture](ARCHITECTURE.md) and [source ledger](SOURCE_LEDGER.md).

The database boundary is public records and contract-approved vendor data. Helmsted client records, the main Helmsted Supabase database, and political donation records stay outside it. Keep the work focused on ingestion, resolution, and evidence.

## First ingestion gate

1. The loader accepts a quarterly ZIP or a directory of UTF-8 TSV files. It archives the artifact by content and records the archive URI and SHA-256. `core.raw_records` retains source values, joined submission and owner data, and the referenced footnotes. Accession, table, and source row key identify each raw record. [SEC quarterly downloads](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets) · [SEC file formats and table definitions](https://www.sec.gov/files/insider_transactions_readme.pdf)
2. Separate issuer and reporting-owner CIKs resolve organizations and people. `FILING_DATE` drives raw and location observations, and `TRANS_DATE` drives trades and signals. The loader uses decimal arithmetic for fractional shares and prices. [SEC submission, owner, and transaction fields](https://www.sec.gov/files/insider_transactions_readme.pdf)
3. A sale generates one signal, and paired exercise legs generate one signal with both legs as evidence. Holdings remain raw evidence. Joint filers retain entity links and co-filer relationships, with trades and signals withheld. A uniquely attributable amendment updates the original signal and replaces its evidence. Ambiguous amendments retain raw evidence and leave prior signals intact. [SEC transaction-code definitions](https://www.sec.gov/files/insider_transactions_readme.pdf)
4. The offline gate checks exact replay across all `core` tables, structured errors and atomic rollback, and reconstruction from raw records. Explicit `--live` downloads require an organization and contact email in `SEC_USER_AGENT` and have a process-wide cap of eight requests per second. [SEC EDGAR access and fair-access rules](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

Signal reconciliation uses the latest distinct payload stored for each accession and table key. An otherwise valid SEC row with no reported transaction code or share count remains raw evidence. Work locations retain the source fields in raw JSON and use `ZZ` where the loader has no country mapping. Deterministic IDs and filing-date UTC timestamps allow offline rebuilds to reproduce the derived rows.

## Form D ingestion gate

1. The Form D loader preserves joined filing, issuer, offering, related-person, recipient, and available signature context. It resolves issuers by CIK and recognizes a primary issuer when the filing identifies one unambiguously. Organizations already known by CIK are reused. [SEC Form D data dictionary](https://www.sec.gov/files/Form_D.pdf)
2. Person resolution tries an exact normalized name with prior evidence at that issuer, then a unique name and US-state match with trigram similarity of at least 0.85. The raw links retain the resolution method and confidence. For legacy SEC owner names without structured name fields, the matcher also considers surname-first order. Rebuild replays the sources in raw-record order to reproduce these decisions.
3. Each eligible officer or promoter’s `private_raise` signal cites both the offering and that person’s role. The amount is the issuer’s cumulative reported sales for the offering. Amendments use the previous accession when supplied and fall back to a unique earlier original sharing the issuer and file number or first-sale date. Separate offerings stay separate. Ambiguous issuer attribution and unresolved amendment chains retain raw evidence without creating signals. [SEC Form D data dictionary](https://www.sec.gov/files/Form_D.pdf) · [SEC Form D amendment guidance](https://www.sec.gov/about/divisions-offices/division-corporation-finance/frequently-asked-questions-answers-form-d)

An input with a missing filing date or issuer name fails validation because the database requires those fields. [SEC Form D data dictionary](https://www.sec.gov/files/Form_D.pdf)

## LinkedIn envelope import

The contract was checked against LinkedIn Scout commit `b3967db`, including the reader-visible README, operations guide, codebase guide, request cache, and vendor fixtures. The standalone importer reads reviewed files locally. Its raw records preserve the returned payload and the metadata needed to inspect approval and URL agreement. A Scout machine match alone leaves the row in evidence review.

The importer projects career facts only from complete, approved profiles whose returned identifier agrees with the envelope. Signals compare eligible snapshots within the same provider, ordered by observation date and retrieval timestamp, with both snapshots attached as evidence. Positions describe their source snapshot as of its observation date. The LinkedIn URL is the only contact point produced in this milestone.

The shared `mim` command handles all loads, cross-source rebuild, audit sampling, and lookup reservations. Prior module commands delegate to it for compatibility. The audit samples distinct accepted profiles and leaves manual precision unset. The vendor reservation workflow establishes an atomic lease before a caller queues work and can reuse saved evidence instead.

The full offline suite passed 38 tests on PostgreSQL 18.6 using fictional envelopes and filings. It covered concurrent reservations, exact replay, rollback, and rebuilding all three sources after removing their archived fixtures.

## Contact enrichment gate

Each envelope maps to an existing person through a local UUID or a registered identifier. Load follows any soft-merge chain and records the resolved person ID with the evidence so later rebuilds can reproduce that attribution. Rebuild depends on the underlying people from earlier source records. It preserves soft-merge pointers when it can recover both endpoints. Identifiers require source evidence to be rebuilt.

Complete matched HTTP 200 responses can hydrate normalized emails, E.164 phones, and postal addresses. Pending, unsuccessful, and mismatched responses retain their raw payload and the link to the requested person. Contact rows keep the source value and evidence for the winning verification. Every sighting contributes to the date range. A newer dated check supersedes an older check, and equally dated conflicting checks prefer `invalid`, then `risky`, then `valid`.

The full offline suite passed 45 tests on PostgreSQL 18.6. It verified exact replay, atomic rollback, and identical four-source reconstruction after the archived fixtures were deleted.

## Healthcare providers and retirement plan sponsors

- NPPES links individuals and organizations by NPI. Individual primary addresses populate work locations. Group primary city and region live on the organization, with the full address in raw evidence. Recent enumerations create person-level signals dated to enumeration. Primary practice moves cite both snapshots and use the later observation date. [CMS NPPES data dissemination](https://www.cms.gov/medicare/regulations-guidance/administrative-simplification/data-dissemination)
- Open Payments creates a person link only when the disclosed individual NPI matches an existing identifier. An unresolved disclosure is still tied to the original fetch. NPPES ingestion and rebuild recheck those records. [CMS Open Payments data dictionary](https://www.cms.gov/files/document/open-payments-methodology-data-dictionary-document-january-2026.pdf)
- DOL records retain the filing acknowledgement ID and link the sponsor by EIN. When the EIN is new, a unique existing organization with the same normalized name, city, and state can receive it. A signer is linked by normalized name and prior sponsor affiliation, or the input can provide explicit owner evidence. The owner signal requires Form 5500-SF, a pension classification, fewer than 100 total participants, and at least $1 million in reported year-end plan assets. It cites the filing and the ownership evidence. [DOL dataset guide](https://www.dol.gov/sites/dolgov/files/ebsa/pdf_files/form-5500-datasets-guide.pdf) · [2025 Form 5500-SF layout](https://askebsa.dol.gov/FOIA%20Files/2025/Latest/F_5500_SF_2025_Latest_layout.txt)

The full offline suite passed 54 tests on PostgreSQL 18.6 with synthetic records, including exact replay and identical seven-source reconstruction after deleting the archived fixtures. Full-size agency archives were outside this verification run.
