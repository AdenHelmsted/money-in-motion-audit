# Project goals and audit checklist

**Project goal.** Build a standalone PostgreSQL reference database for registered investment advisers. For each prospect, establish identity and career context, record dated financial or career events, and provide normalized contact points. A reviewer should be able to trace each projected fact to the source evidence and inspect how the person or organization was matched.

**Current gate.** Complete independent filing review for the machine-verified SEC Forms 3/4/5 2025 Q4 pilot before expanding to other real sources. See the [current handoff status](CURRENT_HANDOFF_STATE.md) for verified test counts, pilot results, and remaining acceptance work.

The competitive background in the supplied brief explains why the records matter. This project’s scope is the standalone evidence database. Scoring, intent and search features, outreach, outcome-based ranking, client introductions, and Helmsted client-data integration sit outside this gate.

## Milestones from the project brief

| Goal | Implemented evidence | Remaining acceptance work |
| --- | --- | --- |
| 1. Database and SEC insiders | Migration `001_core_schema.sql` matches the brief’s DDL. The loader resolves issuer and person CIKs, preserves holdings and filing context, and emits sales and option exercises. See `tests/test_sec_form345_gate.py` and `tests/test_sec_pilot.py`. | Load one real quarter. Verify the sampled owner, issuer, accession, dates, transaction codes, quantities, prices, and evidence against EDGAR. |
| 2. Form D | The loader links offerings and related people and handles supported cumulative amendments. See `tests/test_sec_form_d_gate.py`. | Validate a real release and review ambiguous name matches and amendment chains. |
| 3. LinkedIn | Reviewed vendor files create career, education, and location facts. Same-provider snapshots support job-change, promotion, and retirement signals. Reservation and sampling tools are present. See `tests/test_linkedin_gate.py`. | Confirm vendor terms and use the reservation workflow in a paid pilot. Independently review 50 random accepted matches. |
| 4. Contact points | Approved envelope shapes hydrate normalized email, phone, postal address, and dated verification evidence. Reviewed LinkedIn evidence supplies the LinkedIn URL. See `tests/test_contact_points_gate.py` and `tests/test_linkedin_gate.py`. | Validate real exports, choose an operating vendor, and record actual verification coverage and cost. |
| 5. Physicians and plan sponsors | NPPES links providers and practices by NPI and emits practice signals. Open Payments retains disclosures and links exact individual NPIs when they resolve. Form 5500 links sponsors by EIN and emits signals when qualifying plan assets and ownership are supported. See `tests/test_healthcare_and_plans_gate.py`. | Validate representative agency archives, payment-link coverage, and ownership evidence. The initial Form 5500 signal cutoff is $1 million in reported year-end plan assets for a qualifying small pension plan. |

## Operating rules from the project brief

| Rule | What the auditor should inspect |
| --- | --- |
| 1. Keep Helmsted client data out | The project uses a standalone schema and file loaders. Confirm that the inputs in any real pilot contain only public or contract-authorized vendor evidence. |
| 2. Cite evidence for every fact | Follow entity links and identifiers through `core.record_links` and the identifier tables. Follow facts through their raw-record references and signals through `core.signal_evidence`. Follow raw records through `core.fetches` to the archived artifact. Source registrations and fetch receipts are control metadata. |
| 3. Preserve raw evidence, reuse paid lookups, and support rebuild | ZIP and file inputs retain their original bytes. Raw records retain source fields and may carry joined context or local linkage metadata. `mim rebuild` uses stored raw evidence. The paid caller must use the reservation workflow to reuse eligible LinkedIn results. |
| 4. Make exact replay safe | Compare table counts and fingerprints, including `core.fetches`, before and after replay of the same artifact. The SEC pilot performs that check and runs a rebuild with the archive temporarily unavailable. |
| 5. Keep political donation data out | The source catalog covers the seven listed sources. Confirm the provenance of vendor data before adding it. |
| 6. Obtain LinkedIn evidence through a vendor | The importer consumes reviewed vendor files. Confirm that procurement and collection follow approved vendor channels. |
| 7. Identify live SEC access and rate-limit it | The SEC downloader requires an identifying organization and contact email and has a project cap of eight requests per second per process. Its request guard was tested offline. |
| 8. Register licenses and permitted uses before loading | Migrations 002–006 register the sources. The loader checks registration before ingesting. See [the source ledger](SOURCE_LEDGER.md) for practical terms and open vendor decisions. |
| 9. Obtain approval before productizing or sharing source data | Product and redistribution approval remains a business decision. Confirm the applicable terms before any external distribution of actual data. |

## Definition of done

- [ ] **Trace evidence on real inputs.** Confirm that projected facts and signals have source evidence. Report intentionally unresolved raw records separately. The offline gate runs `mim audit-db` across all seven sources. Review unresolved real records by source during each pilot.
- [ ] **Repeat and rebuild real inputs.** Preserve the SEC pilot results showing an exact replay with unchanged table fingerprints and a rebuild with identical derived rows. Extend the check to the remaining real-source pilots before closing the full project gate.
- [ ] **Complete the LinkedIn review.** Draw 50 random accepted matches, check each independently, and report confirmed matches divided by the number actually reviewed. Record outcomes and withheld counts. Fixture tests verify the sampler. Real-world precision remains unmeasured.
- [ ] **Complete the source ledger.** Setup and rebuild instructions and public access costs are documented. Record the selected vendor prices, retention and permitted uses under the applicable agreements, and source owners.

The [SEC pilot guide](SEC_PILOT.md) defines the first run and reviewer worksheet. The [architecture](ARCHITECTURE.md) explains the evidence flow and identifies where each layer of code lives. Python, shell, and SQL source files carry a `#GOAL` header so reviewers can identify its responsibility at the point of use.
