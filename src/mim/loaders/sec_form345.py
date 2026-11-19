#GOAL Run the SEC insider loader through the shared ingestion contract.

"""Load SEC DERA quarterly insider filings into the standalone database."""

from .sec_form345_input import SOURCE_CODE, LoadError, Record, read_records
from .sec_form345_projection import Projection
from .sec_form345_signals import sync_signals as sync_filing_signals
from .sec_ingestion import load_artifact, rebuild, stored_rows


def _record(external_id, observed_on, payload, row_number=None):
    accession, table, *_ = external_id.split(":")
    return Record(table, accession, row_number, external_id, observed_on, payload)


def _current_records(conn, provenance):
    for _, raw_id, external_id, payload, observed_on, uri in stored_rows(conn, (SOURCE_CODE,), latest=True):
        _, row_number = provenance.setdefault(raw_id, (uri, None))
        yield raw_id, _record(external_id, observed_on, payload, row_number)


def sync_signals(conn, provenance):
    return sync_filing_signals(conn, _current_records(conn, provenance), provenance)


def load_fixture(conn, path, *, archive_dir):
    """Archive and atomically load a local SEC quarterly directory or ZIP."""
    return load_artifact(
        conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=LoadError,
        read_records=read_records, project=Projection, sync_signals=sync_signals,
    )


def rebuild_from_raw(conn):
    """Restore derived facts and reconcile signals for all sources."""
    return rebuild(conn, source_code=SOURCE_CODE, error=LoadError)


if __name__ == "__main__":
    from .sec_form345_cli import main

    raise SystemExit(main())
