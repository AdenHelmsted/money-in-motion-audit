# Money in Motion handoff status

## Verified on November 19, 2026

- PostgreSQL 18.6 passed all 62 tests in `./scripts/test-offline`. The seven-source gate audits a populated database, replays every artifact, deletes the archives, and runs `mim rebuild` against the populated database. It compares all core rows, including fetch history.
- The README setup and fixture commands succeeded from a fresh private database. `mim audit-db` reported zero violations before and after rebuild. It reports intentionally unresolved inputs separately.
- The official 2025 Q4 Forms 3/4/5 ZIP loaded 107,290 raw records and emitted 20,230 sale signals and 9,688 option-exercise signals. Integrity violations were zero, exact replay skipped, and all 16 core table fingerprints matched after rebuild with the ZIP unavailable. Load took 170.7 seconds and rebuild took 114.3 seconds on this host. The report is at `.local/sec-pilot/20261119T074058Z-2610kshg/summary.json` in the project. The worksheet contains five sales and five exercises. All five sale amounts were also recalculated from the archived row values. Independent review against the original filings is pending. [SEC quarterly datasets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)
- A separate 100,000-row synthetic Open Payments run loaded in 5.3 seconds, skipped exact replay, rebuilt in 1.2 seconds, and passed the audit. Full-size agency input capacity remains unmeasured.

## Deploy this revision

Apply migration `007_form_d_name_index.sql` if 001–006 are already installed. The migration adds an index for Form D person candidates and keeps the similarity rule at 0.85. Fresh setup applies all seven migration files. See the [README](../README.md).

All seven sources have local loaders and offline gates. The review fixes cover SEC TSV irregularities, nullable and invalid trade data, fractional-cent sale amounts, Form D entity rows and amendment references, DOL count and asset formatting, merge-aware attribution, and Bright Data pending receipt expiry. Bulk public CSV loads stage up to 1,000 rows at a time within one atomic artifact transaction. Rebuild preserves fetches and raw evidence, and preserves soft merges when both identities can be reconstructed.

## Open acceptance checks

1. Finish the independent review of the 10 SEC sample rows in the existing pilot package. Use the filing accession and raw payload to check each identity, date, event, and amount. [SEC pilot guide](SEC_PILOT.md)
2. Then run one representative real Form D, NPPES, Open Payments, and DOL release in disposable databases. Record load and rebuild time, peak process memory, WAL and disk usage, records withheld, and audit results. Test Open Payments before NPPES at representative volume and verify exact-NPI backfill.
3. Confirm the vendor, approved uses, retention and deletion rules, billing units, and spending cap before paid enrichment. After real approved LinkedIn evidence is available, independently review 50 random distinct accepted matches. Record verified matches, the actual number reviewed, and withheld candidate count. LinkedIn precision and the real contact enrichment pilot are pending. [Source ledger](SOURCE_LEDGER.md)
