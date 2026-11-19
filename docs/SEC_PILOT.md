# SEC insider pilot

**Goal.** Load one real SEC Forms 3/4/5 quarter into a disposable database and give a reviewer enough evidence to confirm who traded, which issuer was involved, when the trade occurred, and which values the filing actually reports.

## Run it in WSL

Use **2025 Q4 345** for the first pilot. Download that ZIP from the official SEC listing and save it in Windows Downloads. Run the following from this repository with the existing `uv` and PostgreSQL setup. The pilot can also find the PostgreSQL 18 binaries previously staged in the WSL user cache. [SEC Insider Transactions Data Sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)

```sh
export PATH="$HOME/.local/bin:$PATH"
uv run --offline --locked mim pilot-sec \
  --fixture /mnt/c/Users/nowel/Downloads/2025q4_form345.zip
```

Use the downloaded file’s actual path if the browser saves it under a different name. The command creates a new audit folder under `.local/sec-pilot/` and prints its path. Add `--output NEW_DIR` to place the package elsewhere. It starts and removes its own private PostgreSQL instance.

For automated download, use `mim pilot-sec --live "$SEC_ZIP_URL"` with the official quarter link and an exported `SEC_USER_AGENT` containing your organization and a monitored contact email. The SEC client sends that identity and stays within the project’s eight-request-per-second cap per process. [SEC access and fair-access policy](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

To rehearse with fictional records, run:

```sh
uv run --offline --locked mim pilot-sec --fixture tests/fixtures/sec_form345/gate
```

The synthetic rehearsal passed with 19 raw records and seven review rows. The official 2025 Q4 run passed machine checks with 107,290 raw records, zero integrity violations, and identical replay and rebuild fingerprints across all 16 core tables. Use the existing `.local/sec-pilot/20261119T074058Z-2610kshg/` package for the 10-row independent filing review. See the [current handoff status](CURRENT_HANDOFF_STATE.md).

## Give the auditor one folder

| File | What to check |
| --- | --- |
| `summary.json` | Input, archive SHA-256, counts and fingerprints for every core table, and differences after replay and rebuild. `machine_checks_passed` means those checks passed. A failed run reports its stage and error. |
| `review.csv` | Up to five sale and five exercise signals, selected deterministically by signal kind and artifact hash. Fill in the status, reviewer, filing URL, and notes. |
| `evidence.json` | The sampled signals, typed trade values, and exact archived source rows used in each decision. Join to the worksheet by `signal_id`. |
| `archive/` | The content-addressed ZIP. Compare its SHA-256 to the summary before reviewing the evidence. |

## Review against EDGAR

1. Open the original filing using the accession, issuer CIK, and filing date in the worksheet and evidence. Confirm the reporting owner’s name and CIK, issuer name and CIK, and filing accession. Record the EDGAR filing URL you actually checked. [EDGAR full-text search](https://www.sec.gov/edgar/search/) · [SEC insider data dictionary](https://www.sec.gov/files/insider_transactions_readme.pdf)
2. Check the transaction date, code, shares, unit price, and plan flag where present. For a sale, compare the reported amount inputs and the computed cents. For an exercise with paired legs, inspect both source rows. A blank amount stays unknown.
3. Set each result to `match`, `mismatch`, or `unresolved`. Record the field that disagreed or the evidence needed to resolve it. Keep the completed worksheet with the original evidence folder.
4. Accept this pilot when replay returns `skipped`, both difference lists are empty, the integrity counts are zero, and the reviewer has recorded results for the sample. Investigate any identity or amount discrepancy before creating the next pilot. `summary.json` records the machine result at generation time. The completed worksheet records the human decision.

The pilot rebuilds in its private database with the archive temporarily moved away, then returns the archive to the package. Scope is one insider dataset. The LinkedIn 50-profile precision review and the other source pilots remain separate work.
