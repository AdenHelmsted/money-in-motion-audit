# Money in Motion

Money in Motion is a standalone PostgreSQL project for collecting public and licensed RIA prospect records with inspectable source evidence. It loads SEC filings, reviewed LinkedIn evidence, authorized contact enrichment, public provider and payment data, and retirement plan filings. The [orchestrator handoff](docs/ORCHESTRATOR_HANDOFF.md) describes the evidence and replay rules.

**Start here for the current goal.** Run the [one-quarter SEC insider pilot](docs/SEC_PILOT.md) and review its evidence package. The [goals and acceptance checklist](docs/GOALS_AND_ACCEPTANCE.md) maps the project brief to the code and open audit work. See the [architecture](docs/ARCHITECTURE.md) and [source ledger](docs/SOURCE_LEDGER.md) for the evidence flow and source terms. The [handoff status](docs/CURRENT_HANDOFF_STATE.md) records the latest checks and decisions.

## Run the offline gate

Use Linux or Ubuntu in WSL as a regular user with Python 3.12+, `uv`, PostgreSQL 16+ server binaries, and the `pgcrypto` and `pg_trgm` extensions. Run these commands from this checkout. The initial dependency sync can fetch packages from PyPI.

```sh
uv sync --locked
./scripts/test-offline
```

The script starts pytest in an isolated Linux network namespace and runs all seven loaders against fictional input on a temporary PostgreSQL cluster. It checks full-table replay, malformed-row rollback, and rebuild from raw records. Direct pytest runs also guard Python networking.

If the PostgreSQL tools are installed outside `PATH`, set `MIM_POSTGRES_BIN` to their directory. On the current WSL machine, PostgreSQL 18.6 was extracted in the user cache and `uv` is in `~/.local/bin`. Reuse that setup with:

```sh
export PATH="$HOME/.local/bin:$PATH"
pg_runtime="$HOME/.cache/money-in-motion/pg18-ubuntu/root"
export MIM_POSTGRES_BIN="$pg_runtime/usr/lib/postgresql/18/bin"
export LD_LIBRARY_PATH="$pg_runtime/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
./scripts/test-offline
```

## Create a standalone development database

Point `MIM_POSTGRES_BIN` to the server binaries if they are outside `PATH`. From the checkout root, the following creates a fresh, private development cluster and applies the migrations atomically. Keep the printed state path to reconnect to or stop it later.

```sh
export PATH="${MIM_POSTGRES_BIN:+$MIM_POSTGRES_BIN:}$PATH"
state=$(mktemp -d /tmp/mim-dev.XXXXXX)
printf 'Cluster path %s\n' "$state"
initdb -D "$state/data" --auth=trust --no-locale --encoding=UTF8 -U mim_dev
pg_ctl -D "$state/data" -l "$state/postgres.log" -w start \
  -o "-c listen_addresses='' -k $state -p 5432"
unset PGSERVICE PGHOSTADDR
export PGHOST="$state" PGPORT=5432 PGUSER=mim_dev
createdb money_in_motion
psql -X -d money_in_motion -v ON_ERROR_STOP=1 --single-transaction \
  -f migrations/001_core_schema.sql -f migrations/002_sec_form345_source.sql \
  -f migrations/003_sec_form_d_source.sql -f migrations/004_linkedin.sql \
  -f migrations/005_contact_points.sql -f migrations/006_healthcare_and_plans.sql \
  -f migrations/007_form_d_name_index.sql
```

Stop it with `pg_ctl -D "$state/data" -w stop`. Apply any missing migration files in numeric order to an existing database. For a database already at migration 006, apply `migrations/007_form_d_name_index.sql` before loading Form D or rebuilding.

## Load, rebuild, and audit

Run from the checkout with the private database running. All commands accept `--dsn` and otherwise use `MIM_DATABASE_DSN` or local libpq settings.

```sh
uv run --offline --locked mim load sec_form345 --fixture tests/fixtures/sec_form345/gate
uv run --offline --locked mim load sec_form_d --fixture tests/fixtures/sec_form_d/gate
uv run --offline --locked mim load linkedin --fixture tests/fixtures/linkedin/envelopes.jsonl
uv run --offline --locked mim load contact_vendor --fixture tests/fixtures/contact_vendor/envelopes.jsonl
uv run --offline --locked mim load nppes --fixture tests/fixtures/healthcare_and_plans/npidata_pfile_20050523-20250531.csv
uv run --offline --locked mim load cms_open_payments --fixture tests/fixtures/healthcare_and_plans/payments.csv
uv run --offline --locked mim load dol_form5500 --fixture tests/fixtures/healthcare_and_plans/f_5500_sf_2025_latest.csv
uv run --offline --locked mim audit-db
uv run --offline --locked mim rebuild
uv run --offline --locked mim audit-db
uv run --offline --locked mim audit-linkedin --sample-size 50 --output .cache/linkedin_precision_report.json
```

SEC fixtures can be TSV directories or quarterly ZIPs. Form D also accepts the SEC’s tab-delimited `.txt` files. LinkedIn and contact imports accept a JSON object, JSON array, or JSONL. NPPES, Open Payments, and Form 5500 accept local CSVs or ZIPs containing the primary CSV tables. Artifacts default to `.local/<source>`. Exact replays skip database writes. Invalid inputs produce structured JSON on stderr and roll back the batch. Rebuild clears and restores derived tables in one transaction using stored raw records and leaves fetch history intact. It takes exclusive locks on derived tables. Run it during a maintenance window on a populated database. [SEC insider datasets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets) · [SEC Form D datasets](https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets)

LinkedIn facts require an explicit approved or verified match, or Scout’s attributed identity approval, plus a complete HTTP 200 response that agrees with the profile identifier. Use only vendor evidence whose contract allows raw retention and internal career analysis. An approved envelope can identify an existing local person via `person_mapping.person_id` or `person_mapping.sec_cik`. Import SEC evidence first when the mapping or cross-filing resolution depends on it.

`mim audit-db` prints a JSON report with integrity violations and counts by source, table, and signal kind. Structural violations return exit status 1. Withheld or unresolved inputs with valid fetch provenance appear in the unlinked raw counts for review.

Before scheduling a vendor lookup, call `mim reserve-linkedin PROVIDER PROFILE`. Schedule work only on `reserved`, retain its token, and call `mim release-linkedin PROVIDER PROFILE TOKEN` after importing the result or ending the attempt. Completed vendor evidence can be reused for 90 days by default. Bright Data pending receipts can be reused for 24 hours. `--refresh` bypasses completed evidence and still honors active reservations and fresh receipts. Reservation commands work entirely in PostgreSQL.

`mim audit-linkedin` generates a random sample of up to 50 distinct accepted profiles and leaves the manual result fields blank. Record verified matches divided by profiles actually reviewed, the review denominator, and the number of withheld candidates.

Contact imports require a `person_mapping` using an existing `person_id`, `sec_cik`, `linkedin`, or `npi`. The fictional fixture maps to the first SEC and LinkedIn examples above. Complete matched responses can add email, phone, and postal address records. Generic files can supply `raw_payload.contacts` entries with `kind`, `value`, and optional `label`, `verification`, and `verified_at`. Apollo and People Data Labs can also use their native person payload. Only import evidence the vendor contract permits you to retain. [Apollo People Enrichment](https://docs.apollo.io/reference/people-enrichment) · [People Data Labs person fields](https://support.peopledatalabs.com/hc/en-us/articles/30910938133147-Free-Plan-Person-Starter-Bundle)

NPPES uses the primary monthly or weekly provider file. It uses the release date from the standard filename when available and falls back to dates in the row. Open Payments links disclosures by individual NPI and backfills exact links when NPPES is loaded later. Disclosures without a resolvable individual NPI retain their raw record and fetch provenance. Agency CSVs use bounded COPY batches within one atomic artifact transaction. [NPPES downloads](https://download.cms.gov/nppes/NPI_Files.html) · [Open Payments data dictionary](https://www.cms.gov/files/document/open-payments-methodology-data-dictionary-document-january-2026.pdf)

The Form 5500 owner signal requires a short-form pension filing, fewer than 100 total participants, at least $1 million in reported year-end plan assets, and evidence that the named person owns the sponsor. Load any prior sponsor and owner evidence before loading the filing. The signal amount reports plan assets. [DOL Form 5500 datasets](https://www.dol.gov/agencies/ebsa/about-ebsa/our-activities/public-disclosure/foia/form-5500-datasets) · [2025 Form 5500-SF layout](https://askebsa.dol.gov/FOIA%20Files/2025/Latest/F_5500_SF_2025_Latest_layout.txt)

For explicit live Forms 3/4/5 downloads, set `SEC_USER_AGENT` to your organization name and contact email, then run `mim load sec_form345 --live "$SEC_ZIP_URL"`. The downloader caps requests at eight per second per process. [SEC fair access](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
