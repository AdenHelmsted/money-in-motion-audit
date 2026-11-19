#GOAL Check baseline schema and source registration in the offline harness.

import hashlib
import json
from pathlib import Path
import socket

from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb
import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_network_is_blocked():
    for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_DGRAM)):
        with pytest.raises(OSError, match="Unix sockets only"):
            socket.socket(family, kind)
    with pytest.raises(OSError, match="disable network lookups"):
        socket.getaddrinfo("example.invalid", 443)
    with socket.socket(socket.AF_UNIX):
        pass


def test_schema_and_source_seed(db):
    tables = db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'core' ORDER BY tablename"
    ).fetchall()
    assert [name for (name,) in tables] == [
        "contact_points", "education", "equity_transactions", "fetches", "locations",
        "organization_identifiers", "organizations", "people", "person_identifiers", "positions",
        "raw_records", "record_links", "relationships", "signal_evidence", "signals", "sources",
    ]
    extensions = db.execute(
        "SELECT extname FROM pg_extension WHERE extname IN ('pgcrypto', 'pg_trgm') ORDER BY extname"
    ).fetchall()
    assert extensions == [("pg_trgm",), ("pgcrypto",)]

    source = db.execute("SELECT * FROM core.sources ORDER BY code").fetchall()
    assert {row[0]: (row[3], row[5]) for row in source} == {
        "cms_open_payments": ("government_open", "annual"),
        "contact_vendor": ("vendor_contract", "on_demand"),
        "dol_form5500": ("government_open", "monthly"),
        "linkedin": ("vendor_contract", "on_demand"),
        "nppes": ("government_open", "monthly"),
        "sec_form345": ("government_open", "quarterly"),
        "sec_form_d": ("public_domain", "quarterly"),
    }
    for code, _, homepage, _, permitted_uses, _ in source:
        domain = {"contact_vendor": "docs.apollo.io", "linkedin": "www.linkedin.com", "nppes": "download.cms.gov",
                  "cms_open_payments": "openpaymentsdata.cms.gov", "dol_form5500": "www.dol.gov"}.get(code, "www.sec.gov")
        assert homepage.startswith(f"https://{domain}/")
        assert permitted_uses
    db.execute((ROOT / "migrations/002_sec_form345_source.sql").read_text(encoding="utf-8"))
    db.execute((ROOT / "migrations/003_sec_form_d_source.sql").read_text(encoding="utf-8"))
    db.execute((ROOT / "migrations/004_linkedin.sql").read_text(encoding="utf-8"))
    db.execute((ROOT / "migrations/005_contact_points.sql").read_text(encoding="utf-8"))
    db.execute((ROOT / "migrations/006_healthcare_and_plans.sql").read_text(encoding="utf-8"))
    assert db.execute("SELECT * FROM core.sources ORDER BY code").fetchall() == source


def test_synthetic_sale_traces_to_raw_evidence(db):
    fixture = ROOT / "tests/fixtures/sec_form345/one_sale.json"
    payload = json.loads(fixture.read_bytes())
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_digest = hashlib.sha256(payload_json).hexdigest()

    with db.transaction(force_rollback=True):
        fetch_id = db.execute(
            """
            INSERT INTO core.fetches
                (source_code, status, finished_at, artifact_uri, artifact_sha256, records_seen, records_new)
            VALUES ('sec_form345', 'succeeded', now(), %s, %s, 1, 1) RETURNING id
            """,
            (fixture.as_uri(), hashlib.sha256(fixture.read_bytes()).hexdigest()),
        ).fetchone()[0]
        raw_insert = """
            INSERT INTO core.raw_records
                (source_code, external_id, payload, payload_sha256, observed_on, fetch_id)
            VALUES ('sec_form345', %s, %s, %s, '2026-11-10', %s) RETURNING id
        """
        raw_values = ("0001234567-26-000001:non_derivative:1", Jsonb(payload), payload_digest, fetch_id)
        raw_id = db.execute(raw_insert, raw_values).fetchone()[0]
        with pytest.raises(UniqueViolation), db.transaction():
            db.execute(raw_insert, raw_values)

        person_id = db.execute(
            "INSERT INTO core.people (full_name) VALUES (%s) RETURNING id",
            (payload["reporting_owner"]["RPTOWNERNAME"],),
        ).fetchone()[0]
        issuer_id = db.execute(
            "INSERT INTO core.organizations (name, kind) VALUES (%s, 'public_company') RETURNING id",
            (payload["submission"]["ISSUERNAME"],),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO core.person_identifiers VALUES (%s, 'sec_cik', %s, %s)",
            (person_id, payload["reporting_owner"]["RPTOWNERCIK"], raw_id),
        )
        db.execute(
            "INSERT INTO core.organization_identifiers VALUES (%s, 'sec_cik', %s, %s)",
            (issuer_id, payload["submission"]["ISSUERCIK"], raw_id),
        )
        db.execute(
            """
            INSERT INTO core.record_links (raw_record_id, person_id, organization_id, role, method, confidence)
            VALUES (%s, %s, NULL, 'subject', 'exact_identifier', 1),
                   (%s, NULL, %s, 'issuer', 'exact_identifier', 1)
            """,
            (raw_id, person_id, raw_id, issuer_id),
        )
        db.execute(
            """
            INSERT INTO core.equity_transactions
                (person_id, issuer_id, transaction_on, code, shares, price_per_share_cents, raw_record_id)
            VALUES (%s, %s, '2026-11-09', 'S', 2500, 5412, %s)
            """,
            (person_id, issuer_id, raw_id),
        )
        signal_id = db.execute(
            """
            INSERT INTO core.signals
                (person_id, organization_id, kind, occurred_on, amount_cents, summary, confidence, detector_version)
            VALUES (%s, %s, 'insider_sale', '2026-11-09', 13530000, 'Alex Example reported a sale.', 1, 'test')
            RETURNING id
            """,
            (person_id, issuer_id),
        ).fetchone()[0]
        db.execute("INSERT INTO core.signal_evidence VALUES (%s, %s)", (signal_id, raw_id))
        with pytest.raises(ForeignKeyViolation), db.transaction():
            db.execute("INSERT INTO core.signal_evidence VALUES (%s, -1)", (signal_id,))

        assert db.execute(
            """
            SELECT p.full_name, o.name, s.amount_cents, r.payload, r.payload_sha256,
                   r.observed_on - t.transaction_on AS days_before_filing
            FROM core.signals s
            JOIN core.signal_evidence e ON e.signal_id = s.id
            JOIN core.raw_records r ON r.id = e.raw_record_id
            JOIN core.equity_transactions t ON t.raw_record_id = r.id
            JOIN core.people p ON p.id = s.person_id
            JOIN core.organizations o ON o.id = s.organization_id
            WHERE s.id = %s
            """,
            (signal_id,),
        ).fetchone() == ("Alex Example", "Example Instruments Inc", 13530000, payload, payload_digest, 1)
