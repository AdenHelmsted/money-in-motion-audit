#GOAL Run the SEC Form D loader through the shared ingestion contract.

"""Load local SEC Form D quarterly datasets into the standalone database."""

from functools import partial

from .sec_form_d_input import SOURCE_CODE, Record, read_records
from .sec_form_d_projection import Projection
from .sec_form_d_signals import sync_signals
from .sec_ingestion import LoadError, load_artifact, rebuild


_error = partial(LoadError, source_code=SOURCE_CODE)
_TABLES = {"OFFERING": "ISSUERS", "RELATEDPERSON": "RELATEDPERSONS", "RECIPIENT": "RECIPIENTS"}


def _record(external_id, observed_on, payload, row_number=None):
    accession, kind, _ = external_id.split(":")
    return Record(_TABLES[kind], accession, row_number, external_id, observed_on, payload)


def load_fixture(conn, path, *, archive_dir):
    """Archive and atomically load a local Form D directory or ZIP."""
    return load_artifact(
        conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=_error,
        read_records=read_records, project=Projection, sync_signals=sync_signals,
    )


def rebuild_from_raw(conn):
    """Restore derived facts and reconcile signals for all sources."""
    return rebuild(conn, source_code=SOURCE_CODE, error=_error)


def main(argv=None):
    from mim.cli import legacy_load

    return legacy_load(SOURCE_CODE, argv)


if __name__ == "__main__":
    raise SystemExit(main())
