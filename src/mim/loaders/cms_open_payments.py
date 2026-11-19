#GOAL Preserve payment disclosures and link exact individual NPIs.

"""Keep Open Payments disclosures and link known individual NPIs exactly."""

from datetime import date
from decimal import Decimal
from functools import partial
import hashlib
import json
from pathlib import Path
import re

from .government_csv import Columns, date_from, npi, read_records
from .sec_ingestion import LoadError as IngestionError, load_artifact, rebuild


SOURCE_CODE = "cms_open_payments"
LoadError = partial(IngestionError, source_code=SOURCE_CODE)
_IDENTITY_HEADERS = {"covered_recipient_npi", "physician_npi", "npi", "covered_recipient_profile_id", "physician_profile_id",
                     "recipient_type", "covered_recipient_type"}
_DATE_HEADERS = {"date_of_payment", "payment_date", "payment_publication_date", "program_year", "observed_on"}


def _convert(row, member):
    fields = Columns(row)
    identifier = fields.get("record_id", "payment_id")
    if not identifier:
        canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        identifier = hashlib.sha256(canonical.encode()).hexdigest()
    observed = date_from(fields, "date_of_payment", "payment_date", "payment_publication_date", "observed_on")
    if observed is None:
        year = fields.require("program_year")
        if not re.fullmatch(r"[0-9]{4}", year):
            raise ValueError("Program Year must be a four-digit year")
        observed = date(int(year), 12, 31)
    if amount := fields.get("total_amount_of_payment_usdollars", "amount_usd", "payment_amount"):
        if not Decimal(amount).is_finite():
            raise ValueError("payment amount must be a finite decimal")
    printed_npi = fields.get("covered_recipient_npi", "physician_npi", "npi")
    provider = npi(printed_npi) if printed_npi else None
    if "hospital" in fields.get("covered_recipient_type", "recipient_type").casefold():
        provider = None
    return identifier, observed, {"row": row, "recipient_npi": provider}


def link_payments(conn):
    conn.execute("""
        WITH RECURSIVE merged(origin, current, next, path) AS (
            SELECT id, id, merged_into_id, ARRAY[id] FROM core.people WHERE merged_into_id IS NOT NULL
            UNION ALL
            SELECT m.origin, p.id, p.merged_into_id, m.path || p.id
            FROM merged m JOIN core.people p ON p.id = m.next WHERE NOT p.id = ANY(m.path)
        ), survivors AS (
            SELECT origin, current FROM merged WHERE next IS NULL
        )
        INSERT INTO core.record_links (raw_record_id, person_id, role, method, confidence, linked_at)
        SELECT r.id, COALESCE(s.current, i.person_id), 'subject', 'exact_identifier', 1, r.observed_on::timestamp AT TIME ZONE 'UTC'
        FROM core.raw_records r JOIN core.person_identifiers i
            ON i.scheme = 'npi' AND i.value = r.payload ->> 'recipient_npi'
        JOIN core.people p ON p.id = i.person_id
        LEFT JOIN survivors s ON s.origin = i.person_id
        WHERE r.source_code = %s AND (p.merged_into_id IS NULL OR s.current IS NOT NULL) AND NOT EXISTS (
            SELECT 1 FROM core.record_links l WHERE l.raw_record_id = r.id AND l.role = 'subject'
        )
        """, (SOURCE_CODE,))


def _after_load(conn, provenance):
    link_payments(conn)


def load_fixture(conn, path, *, archive_dir):
    def priority(name):
        if re.match(r"op_dtl_(?:gnrl|rsrch|ownrshp)_", name):
            return 2
        return int(not any(word in name for word in ("deleted", "removed", "supplement")))

    def records(artifact):
        return read_records(artifact, Path(path).name, LoadError, _convert,
                            lambda keys: bool(keys & _IDENTITY_HEADERS and keys & _DATE_HEADERS), member_priority=priority)

    return load_artifact(conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=LoadError,
                         read_records=records, project=None, sync_signals=_after_load, file_suffixes={".csv", ".zip"}, bulk=True)


def rebuild_from_raw(conn):
    return rebuild(conn, source_code=SOURCE_CODE, error=LoadError)
