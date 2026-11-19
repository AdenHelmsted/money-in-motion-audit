#GOAL Verify insider attribution, evidence, exact replay, rebuild, and SEC request controls.

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import httpx
import psycopg
from psycopg import sql
import pytest

from mim.loaders import sec_form345_cli
from mim.loaders.sec_form345 import LoadError, load_fixture, rebuild_from_raw
from conftest import INGESTION_TABLES, archived_path, core_snapshot


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/sec_form345/gate"


def accession(number):
    return f"0001234567-25-{number:06}"


def external_id(number, table="NONDERIV_TRANS", key=1):
    return f"{accession(number)}:{table}:{key}"


def filing_fixture(destination, number):
    destination.mkdir()
    key = f"{accession(number)}\t".encode("ascii")
    for table in FIXTURE.glob("*.tsv"):
        header, *rows = table.read_bytes().splitlines(keepends=True)
        contents = header + b"".join(row for row in rows if row.startswith(key))
        (destination / table.name).write_bytes(contents)
    return destination


@pytest.fixture
def loaded(gate_db, tmp_path):
    result = load_fixture(gate_db, FIXTURE, archive_dir=tmp_path / "archive")
    assert result["status"] == "succeeded"
    assert result["records_seen"] == result["records_new"] == 19
    return gate_db, result


def test_equity_signals_pairing_and_decimal_math(loaded):
    connection, _ = loaded
    rows = {
        record_id: (code, day, shares, price, remaining, plan)
        for record_id, code, day, shares, price, remaining, plan in connection.execute(
            """
            SELECT r.external_id, t.code, t.transaction_on, t.shares, t.price_per_share_cents,
                   t.shares_owned_after, t.under_10b5_1_plan
            FROM core.equity_transactions t JOIN core.raw_records r ON r.id = t.raw_record_id
            """
        ).fetchall()
    }
    assert rows[external_id(1)] == (
        "S", date(2025, 1, 3), Decimal("2.5000"), 101, Decimal("100.1250"), True,
    )
    assert rows[external_id(1, key=2)][:3] == ("M", date(2025, 1, 3), Decimal("12.5000"))
    assert rows[external_id(1, "DERIV_TRANS")][:3] == ("M", date(2025, 1, 3), Decimal("12.5000"))
    assert [rows[external_id(1, key=key)][0] for key in range(3, 7)] == ["A", "F", "G", "P"]
    assert rows[external_id(3)][3:] == (None, Decimal("125.5000"), True)
    assert rows[external_id(3, key=2)][-1] is None
    assert rows[external_id(4)][-1] is False
    assert rows[external_id(5)][-1] is False
    assert all(not key.startswith((accession(2), accession(10), accession(11))) for key in rows)

    signals = connection.execute(
        """
        SELECT s.kind, s.occurred_on, s.amount_cents, array_agg(r.external_id ORDER BY r.external_id)
        FROM core.signals s
        JOIN core.signal_evidence e ON e.signal_id = s.id
        JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE s.detector_version = 'sec_form345_v1'
        GROUP BY s.id ORDER BY s.occurred_on, s.kind, s.amount_cents
        """
    ).fetchall()
    assert len(signals) == connection.execute("SELECT count(*) FROM core.signals").fetchone()[0] == 7
    assert [(kind, day, amount, evidence) for kind, day, amount, evidence in signals if kind == "insider_sale"] == [
        ("insider_sale", date(2025, 1, 3), 251, [external_id(1)]),
        ("insider_sale", date(2025, 1, 7), None, [external_id(3)]),
        ("insider_sale", date(2025, 1, 9), 2760, [external_id(5)]),
        ("insider_sale", date(2025, 1, 10), 200, [external_id(6)]),
        ("insider_sale", date(2025, 1, 10), 300, [external_id(7)]),
    ]
    assert [(day, evidence) for kind, day, _, evidence in signals if kind == "insider_option_exercise"] == [
        (date(2025, 1, 3), [external_id(1, "DERIV_TRANS"), external_id(1, key=2)]),
        (date(2025, 1, 7), [external_id(3, key=2)]),
    ]


def test_raw_evidence_identity_and_joint_filers(loaded):
    connection, _ = loaded
    records = {
        key: (day, payload, digest)
        for key, day, payload, digest in connection.execute(
            "SELECT external_id, observed_on, payload, payload_sha256 FROM core.raw_records"
        ).fetchall()
    }
    expected = {external_id(1, key=key) for key in range(1, 7)} | {
        external_id(1, "DERIV_TRANS"), external_id(2, "NONDERIV_HOLDING"),
        external_id(2, "DERIV_HOLDING"), external_id(3), external_id(3, key=2),
        *(external_id(number) for number in range(4, 11)), f"{accession(11)}:SUBMISSION",
    }
    assert set(records) == expected

    day, sale, _ = records[external_id(1)]
    assert day == date(2025, 1, 6)
    assert sale["SUBMISSION"]["REMARKS"] == "Initial grant and sale report"
    assert sale["SUBMISSION"]["ISSUERCIK"] == "1234567"
    assert sale["REPORTINGOWNER"][0]["RPTOWNERNAME"] == "Alice Éxample"
    assert sale["REPORTINGOWNER"][0]["RPTOWNER_STREET2"] == "Ste 4"
    assert sale["NONDERIV_TRANS"]["TRANS_SHARES"] == "2.5000"
    assert [note["FOOTNOTE_ID"] for note in sale["FOOTNOTES"]] == ["F1"]

    day, holding, digest = records[external_id(2, "NONDERIV_HOLDING")]
    assert day == date(2025, 1, 7)
    assert holding["NONDERIV_HOLDING"]["SHRS_OWND_FOLWNG_TRANS"] == "100"
    assert holding["FOOTNOTES"][0]["FOOTNOTE_TXT"] == "Held directly by the reporting owner."
    canonical = json.dumps(holding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert digest == hashlib.sha256(canonical).hexdigest()

    assert connection.execute(
        "SELECT value FROM core.person_identifiers WHERE scheme = 'sec_cik' ORDER BY value"
    ).fetchall() == [("0002345678",), ("0200000003",), ("0300000002",), ("0400000001",)]
    assert connection.execute(
        "SELECT scheme, value FROM core.organization_identifiers ORDER BY scheme, value"
    ).fetchall() == [("sec_cik", "0001234567"), ("sec_cik", "0009999999"), ("ticker", "EXMP")]

    links = connection.execute(
        """
        SELECT p.value, i.value, l.role, l.method, l.confidence
        FROM core.record_links l
        JOIN core.raw_records r ON r.id = l.raw_record_id
        LEFT JOIN core.person_identifiers p ON p.person_id = l.person_id AND p.scheme = 'sec_cik'
        LEFT JOIN core.organization_identifiers i ON i.organization_id = l.organization_id AND i.scheme = 'sec_cik'
        WHERE r.external_id = %s ORDER BY l.role, p.value
        """,
        (external_id(10),),
    ).fetchall()
    assert links == [
        (None, "0001234567", "issuer", "exact_identifier", Decimal("1.00")),
        *((owner, None, "related_person", "exact_identifier", Decimal("1.00"))
          for owner in ("0002345678", "0200000003", "0300000002")),
    ]
    assert connection.execute(
        """
        SELECT role FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.external_id = %s ORDER BY role
        """,
        (external_id(2, "NONDERIV_HOLDING"),),
    ).fetchall() == [("issuer",), ("subject",)]

    cofilers = connection.execute(
        """
        SELECT relationship.kind, relationship.person_a_id < relationship.person_b_id,
               LEAST(a.value, b.value), GREATEST(a.value, b.value)
        FROM core.relationships relationship
        JOIN core.raw_records r ON r.id = relationship.raw_record_id
        JOIN core.person_identifiers a ON a.person_id = relationship.person_a_id AND a.scheme = 'sec_cik'
        JOIN core.person_identifiers b ON b.person_id = relationship.person_b_id AND b.scheme = 'sec_cik'
        WHERE r.external_id = %s ORDER BY 3, 4
        """,
        (external_id(10),),
    ).fetchall()
    assert cofilers == [
        ("co_filers", True, "0002345678", "0200000003"),
        ("co_filers", True, "0002345678", "0300000002"),
        ("co_filers", True, "0200000003", "0300000002"),
    ]

    assert connection.execute(
        """
        SELECT DISTINCT p.seniority
        FROM core.positions p JOIN core.raw_records r ON r.id = p.raw_record_id
        WHERE r.external_id = %s ORDER BY p.seniority
        """,
        (external_id(10),),
    ).fetchall() == [("board",), ("c_suite",), ("owner",)]
    assert connection.execute(
        """
        SELECT DISTINCT l.kind, l.locality, l.region, l.postal_code, l.observed_on
        FROM core.locations l JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.external_id = %s
        """,
        (external_id(1),),
    ).fetchall() == [("work", "Chicago", "IL", "60606", date(2025, 1, 6))]


def test_repeat_and_rebuild_preserve_every_core_row(loaded, tmp_path):
    connection, first = loaded
    before = core_snapshot(connection)
    archive_dir = tmp_path / "archive"
    for path in (FIXTURE, shutil.copytree(FIXTURE, tmp_path / "copied")):
        repeated = load_fixture(connection, path, archive_dir=archive_dir)
        assert repeated["status"] == "skipped"
        assert repeated["artifact_uri"] == first["artifact_uri"]
        assert core_snapshot(connection) == before

    archived_path(first["artifact_uri"]).unlink()
    derived = sorted(before.keys() - INGESTION_TABLES)
    connection.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(
        sql.Identifier("core", name) for name in derived
    )))
    assert all(not rows for name, rows in core_snapshot(connection).items() if name in derived)
    rebuild_from_raw(connection)
    assert core_snapshot(connection) == before


def test_later_valid_amendment_updates_an_existing_signal(gate_db, tmp_path):
    archive_dir = tmp_path / "archive"
    original = filing_fixture(tmp_path / "original", 4)
    amendment = filing_fixture(tmp_path / "amendment", 5)
    load_fixture(gate_db, original, archive_dir=archive_dir)
    signal_id, amount = gate_db.execute("SELECT id, amount_cents FROM core.signals").fetchone()
    assert amount == 2300

    load_fixture(gate_db, amendment, archive_dir=archive_dir)
    assert gate_db.execute("SELECT id, amount_cents FROM core.signals").fetchall() == [(signal_id, 2760)]
    assert gate_db.execute(
        """
        SELECT r.external_id
        FROM core.signal_evidence e JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE e.signal_id = %s
        """,
        (signal_id,),
    ).fetchall() == [(external_id(5),)]
    assert gate_db.execute("SELECT external_id FROM core.raw_records ORDER BY external_id").fetchall() == [
        (external_id(4),), (external_id(5),),
    ]


def test_reissued_rows_preserve_raw_versions_and_one_sale(loaded, tmp_path):
    connection, _ = loaded
    archive_dir = tmp_path / "archive"
    reissue = shutil.copytree(FIXTURE, tmp_path / "reissue")
    trade_table = reissue / "NONDERIV_TRANS.tsv"
    trade_table.write_bytes(trade_table.read_bytes().replace(b"\t1.005\t", b"\t2.005\t", 1))

    result = load_fixture(connection, reissue, archive_dir=archive_dir)
    assert (result["status"], result["records_seen"], result["records_new"]) == ("succeeded", 19, 1)
    assert connection.execute(
        "SELECT count(DISTINCT payload_sha256) FROM core.raw_records WHERE external_id = %s",
        (external_id(1),),
    ).fetchone() == (2,)
    assert connection.execute(
        """
        SELECT s.amount_cents, r.payload -> 'NONDERIV_TRANS' ->> 'TRANS_PRICEPERSHARE'
        FROM core.signals s JOIN core.signal_evidence e ON e.signal_id = s.id
        JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE r.external_id = %s
        """,
        (external_id(1),),
    ).fetchall() == [(501, "2.005")]


def test_zip_is_archived_verbatim_and_footnotes_are_optional(gate_db, tmp_path):
    original = tmp_path / "incoming.zip"
    with ZipFile(original, "w") as zipped:
        for table in sorted(FIXTURE.glob("*.tsv")):
            if table.name != "FOOTNOTES.tsv":
                contents = table.read_bytes().replace(b"\tF1\t", b"\t\t")
                zipped.writestr(table.name, contents)
    original_bytes = original.read_bytes()

    result = load_fixture(gate_db, original, archive_dir=tmp_path / "archive")
    assert result["status"] == "succeeded"
    archived = archived_path(result["artifact_uri"])
    assert archived != original
    original.unlink()
    assert archived.read_bytes() == original_bytes
    assert gate_db.execute("SELECT artifact_sha256 FROM core.fetches").fetchone() == (
        hashlib.sha256(original_bytes).hexdigest(),
    )
    assert gate_db.execute(
        "SELECT count(*) FROM core.raw_records WHERE payload -> 'FOOTNOTES' = '[]'::jsonb"
    ).fetchone() == (19,)


def test_published_tsv_irregularities_preserve_evidence(gate_db, tmp_path):
    fixture = shutil.copytree(FIXTURE, tmp_path / "quarter")
    owners = fixture / "REPORTINGOWNER.tsv"
    owners.write_bytes(owners.read_bytes() + owners.read_bytes().splitlines()[1] + b"\n")
    notes = fixture / "FOOTNOTES.tsv"
    contents = notes.read_bytes().replace(b"Weighted average price applies to fractional shares.", b'"Weighted" average price')
    notes.write_bytes(contents.replace(b"The sale was made pursuant to Rule 10b5-1.", b"")
                      + contents.splitlines()[1] + b"\n")

    trade_file = fixture / "NONDERIV_TRANS.tsv"
    lines = trade_file.read_text().splitlines()
    header = lines[0].split("\t")
    rows = [line.split("\t") for line in lines[1:]]
    rows[0][header.index("TRANS_SHARES_FN")] = "F1 missing"
    required_fields = {
        9: ("TRANS_DATE", ""),
        10: ("TRANS_ACQUIRED_DISP_CD", ""),
        11: ("TRANS_PRICEPERSHARE", "-1"),
        12: ("TRANS_SHARES", "-1"),
    }
    affected = []
    for ordinal, (field, value) in required_fields.items():
        row = rows[ordinal]
        row[header.index(field)] = value
        affected.append(f"{row[0]}:NONDERIV_TRANS:{row[1]}")
    trade_file.write_text(lines[0] + "\n" + "\n".join("\t".join(row) for row in rows) + "\n")

    derivative = fixture / "DERIV_TRANS.tsv"
    derivative.write_bytes(derivative.read_bytes().replace(b"\tM\t12.5\t", b"\tM\t\t", 1))
    load_fixture(gate_db, fixture, archive_dir=tmp_path / "archive")
    assert gate_db.execute("""
        SELECT payload -> 'FOOTNOTES' -> 0 ->> 'FOOTNOTE_TXT', jsonb_array_length(payload -> 'REPORTINGOWNER'),
               payload -> 'NONDERIV_TRANS' ->> 'TRANS_SHARES_FN'
        FROM core.raw_records WHERE external_id = %s
    """, (external_id(1),)).fetchone() == ('"Weighted" average price', 1, "F1 missing")
    assert gate_db.execute("""
        SELECT count(*), count(t.id)
        FROM core.raw_records r LEFT JOIN core.equity_transactions t ON t.raw_record_id = r.id
        WHERE r.external_id = ANY(%s)
    """, (affected,)).fetchone() == (len(affected), 0)
    assert gate_db.execute("""
        SELECT count(*) FROM core.signal_evidence e JOIN core.signals s ON s.id = e.signal_id
        WHERE s.kind = 'insider_option_exercise' AND s.occurred_on = DATE '2025-01-03'
    """).fetchone() == (2,)


@pytest.mark.parametrize("defect", ["header", "number"])
def test_invalid_tsv_context_and_full_rollback(gate_db, tmp_path, defect):
    bad = shutil.copytree(FIXTURE, tmp_path / "bad")
    table = bad / "NONDERIV_TRANS.tsv"
    if defect == "header":
        table.write_bytes(table.read_bytes().replace(b"\tTRANS_SHARES\t", b"\tNOT_SHARES\t", 1))
        expected_accession, expected_row, reason = None, 1, "TRANS_SHARES"
    else:
        table.write_bytes(table.read_bytes().replace(b"\t99\t", b"\tnot-a-number\t", 1))
        expected_accession, expected_row, reason = accession(8), 14, "TRANS_SHARES"

    before = core_snapshot(gate_db)
    with pytest.raises(LoadError) as raised:
        load_fixture(gate_db, bad, archive_dir=tmp_path / "archive")
    error = raised.value.as_dict()
    assert set(error) == {"source_code", "artifact_uri", "table_name", "accession_number", "row_number", "reason"}
    assert error["source_code"] == "sec_form345"
    assert archived_path(error["artifact_uri"]).is_file()
    assert error["table_name"] == "NONDERIV_TRANS"
    assert error["accession_number"] == expected_accession
    assert error["row_number"] == expected_row
    assert reason in error["reason"]
    assert core_snapshot(gate_db) == before


def test_database_constraint_rolls_back_raw_and_derived(gate_db, tmp_path):
    gate_db.execute("ALTER TABLE core.equity_transactions ADD CONSTRAINT gate_reject_f CHECK (code <> 'F')")
    before = core_snapshot(gate_db)
    with pytest.raises(LoadError) as raised:
        load_fixture(gate_db, FIXTURE, archive_dir=tmp_path / "archive")
    error = raised.value.as_dict()
    assert archived_path(error["artifact_uri"]).is_file()
    assert (error["source_code"], error["table_name"], error["accession_number"], error["row_number"]) == (
        "sec_form345", "NONDERIV_TRANS", accession(1), 5,
    )
    assert "gate_reject_f" in error["reason"]
    assert core_snapshot(gate_db) == before


def test_missing_source_rejects_load(gate_db, tmp_path):
    gate_db.execute("DELETE FROM core.sources WHERE code = 'sec_form345'")
    before = core_snapshot(gate_db)
    archive_dir = tmp_path / "archive"
    with pytest.raises(LoadError) as raised:
        load_fixture(gate_db, FIXTURE, archive_dir=archive_dir)
    error = raised.value.as_dict()
    assert error["source_code"] == "sec_form345"
    assert "missing" in error["reason"].lower()
    assert not archive_dir.exists()
    assert core_snapshot(gate_db) == before


def test_concurrent_loads_share_successful_fetch(gate_db, tmp_path):
    dsn = gate_db.info.dsn

    def load():
        with psycopg.connect(dsn, autocommit=True) as connection:
            return load_fixture(connection, FIXTURE, archive_dir=tmp_path / "archive")["status"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        attempts = [workers.submit(load) for _ in range(2)]
        assert sorted(attempt.result(timeout=30) for attempt in attempts) == ["skipped", "succeeded"]
    assert gate_db.execute("SELECT count(*) FROM core.fetches").fetchone() == (1,)
    assert gate_db.execute("SELECT count(*) FROM core.raw_records").fetchone() == (19,)


def test_cli_fixture_and_rebuild(gate_db, tmp_path):
    cli = [sys.executable, "-m", "mim.loaders.sec_form345", "--dsn", gate_db.info.dsn]
    loaded = subprocess.run(
        [*cli, "--fixture", str(FIXTURE), "--archive-dir", str(tmp_path / "archive")],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert loaded.returncode == 0, loaded.stderr
    before = core_snapshot(gate_db)
    rebuilt = subprocess.run(
        [*cli, "--rebuild-from-raw"], cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert core_snapshot(gate_db) == before


def test_live_guard_requires_identity_and_limits_request_rate(monkeypatch, tmp_path):
    url = (
        "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
        "2025q1_form345.zip"
    )
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(LoadError, match="SEC_USER_AGENT"):
        sec_form345_cli._live_options(url)

    agent = "Example Research edgar@example.invalid"
    monkeypatch.setenv("SEC_USER_AGENT", agent)
    for alternate in (url, url.replace("structureddata/data/", "dera/data/"), url.replace("structureddata/data/", "data/")):
        assert sec_form345_cli._live_options(alternate) == agent
    clock = 0.0
    attempts = []

    def advance(seconds):
        nonlocal clock
        clock += seconds

    def respond(request):
        attempts.append((clock, str(request.url), request.headers["User-Agent"]))
        return httpx.Response(200, stream=httpx.ByteStream(b"fake ZIP bytes"))

    client = httpx.Client
    monkeypatch.setattr(sec_form345_cli, "_NEXT_REQUEST_AT", 0.0)
    monkeypatch.setattr(sec_form345_cli, "monotonic", lambda: clock)
    monkeypatch.setattr(sec_form345_cli, "sleep", advance)
    monkeypatch.setattr(httpx, "Client", lambda **options: client(transport=httpx.MockTransport(respond), **options))

    for count in range(3):
        output = tmp_path / f"download{count}.zip"
        sec_form345_cli._download(url, agent, output)
        assert output.read_bytes() == b"fake ZIP bytes"
    assert attempts == [(0.0, url, agent), (0.125, url, agent), (0.25, url, agent)]
