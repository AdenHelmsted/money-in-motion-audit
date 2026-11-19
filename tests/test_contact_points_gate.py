#GOAL Verify contact normalization, verification precedence, rollback, and rebuild.

from datetime import UTC, date, datetime
import json
from pathlib import Path
import subprocess
import sys

from psycopg import errors, sql
import pytest

from conftest import INGESTION_TABLES, archived_path, core_snapshot
from mim.loaders import contact_vendor, linkedin, sec_form345, sec_form_d
from mim.normalize.contact import normalize_email, normalize_phone, normalize_postal_address


FIXTURES = Path(__file__).parent / "fixtures"
CONTACTS = FIXTURES / "contact_vendor/envelopes.jsonl"


def _fixture(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _cli(db, *args):
    return subprocess.run([sys.executable, "-m", "mim", *args, "--dsn", db.info.dsn],
                          capture_output=True, text=True, timeout=25)


@pytest.fixture
def seeded(gate_db, tmp_path):
    archive = tmp_path / "archive"
    results = [
        sec_form345.load_fixture(gate_db, FIXTURES / "sec_form345/gate", archive_dir=archive),
        sec_form_d.load_fixture(gate_db, FIXTURES / "sec_form_d/gate", archive_dir=archive),
    ]
    envelopes = [json.loads(line) for line in (FIXTURES / "linkedin/envelopes.jsonl").read_text().splitlines()]
    envelopes[0]["person_mapping"] = {"sec_cik": "2345678"}
    results.append(linkedin.load_fixture(gate_db, _fixture(tmp_path / "linkedin.json", envelopes), archive_dir=archive))
    return gate_db, archive, results


def test_normalizers():
    assert normalize_email("  Jane.Doe@Example.COM ") == "jane.doe@example.com"
    assert normalize_phone("(512) 555-0123") == normalize_phone("1-512-555-0123") == "+15125550123"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("00 44 20 7946 0958") == "+442079460958"
    assert normalize_postal_address(" 12  Fiction Street, Austin, tx 012345678, usa ") == "12 FICTION STREET, AUSTIN, TX 01234-5678, US"
    assert normalize_postal_address({"street": "12 Fiction Street", "city": "Austin", "state": "Texas", "zip": "01234"}) == "12 FICTION STREET, AUSTIN, TX 01234, US"


def test_deduplication_verification_evidence_and_four_source_cli_rebuild(seeded, tmp_path):
    connection, archive, results = seeded
    records = [json.loads(line) for line in CONTACTS.read_text().splitlines()]
    first_file = _fixture(tmp_path / "first.json", records[:5])
    results.append(contact_vendor.load_fixture(connection, first_file, archive_dir=archive))
    email = connection.execute(
        """SELECT c.value, c.verification, c.verified_at, c.first_seen_on, c.last_seen_on, c.label, r.external_id
           FROM core.contact_points c JOIN core.raw_records r ON r.id = c.raw_record_id WHERE c.kind = 'email'"""
    ).fetchone()
    assert email == ("  Jane.Doe@Example.COM ", "valid", datetime(2025, 5, 2, tzinfo=UTC),
                     date(2025, 4, 30), date(2025, 6, 4), "work", "apollo:fictional-1")
    contacts = connection.execute(
        "SELECT kind, value_normalized, count(*) FROM core.contact_points WHERE kind <> 'linkedin_url' GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    assert contacts == [("email", "jane.doe@example.com", 1), ("phone", "+15125550123", 1),
                        ("phone", "+442079460958", 1), ("postal_address", "12 FICTION STREET, AUSTIN, TX 01234-5678, US", 1)]

    before = core_snapshot(connection)
    args = ("load", "contact_vendor", "--fixture", str(first_file), "--archive-dir", str(archive))
    replay = _cli(connection, *args)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["status"] == "skipped"
    assert core_snapshot(connection) == before

    # Load JSONL as well as singleton JSON. The new verification supersedes the earlier one.
    last_file = tmp_path / "last.jsonl"
    last_file.write_text(CONTACTS.read_text().splitlines()[-1] + "\n", encoding="utf-8")
    results.append(contact_vendor.load_fixture(connection, last_file, archive_dir=archive))
    assert connection.execute(
        """SELECT verification, verified_at, last_seen_on FROM core.contact_points WHERE kind = 'email'"""
    ).fetchone() == ("invalid", datetime(2025, 6, 7, 10, tzinfo=UTC), date(2025, 6, 7))
    assert connection.execute(
        """SELECT count(DISTINCT l.person_id), count(*) FROM core.record_links l
           JOIN core.raw_records r ON r.id = l.raw_record_id
           WHERE r.source_code = 'contact_vendor' AND l.role = 'subject' AND l.method = 'exact_identifier'"""
    ).fetchone() == (1, 6)
    with pytest.raises(errors.CheckViolation), connection.transaction():
        connection.execute("UPDATE core.contact_points SET verified_at = NULL WHERE kind = 'email'")

    before = core_snapshot(connection)
    for result in results:
        archived_path(result["artifact_uri"]).unlink()
    derived = sorted(before.keys() - INGESTION_TABLES)
    connection.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(sql.Identifier("core", name) for name in derived)))
    rebuilt = _cli(connection, "rebuild")
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert core_snapshot(connection) == before


@pytest.mark.parametrize(("failure", "item"), [
    ("email", {"kind": "email", "value": "jane.without.domain@"}),
    ("phone", {"kind": "phone", "value": "+1 512 555 0123 ext 93"}),
    ("person_mapping", {"kind": "email", "value": "jane@example.com"}),
])
def test_cli_structured_error_and_rollback(seeded, tmp_path, failure, item):
    connection, archive, _ = seeded
    good = json.loads(CONTACTS.read_text().splitlines()[0])
    bad = {**good, "id": "malformed", "raw_payload": {"contacts": [item]}}
    if failure == "person_mapping":
        bad["person_mapping"] = {"sec_cik": "1"}
    before = core_snapshot(connection)
    path = _fixture(tmp_path / "bad.json", [good, bad])
    result = _cli(connection, "load", "contact_vendor", "--fixture", str(path), "--archive-dir", str(archive))
    assert result.returncode == 1 and not result.stdout
    error = json.loads(result.stderr)
    assert error["source_code"] == "contact_vendor" and error["record_index"] == 2
    assert failure in error["reason"] and error["person_mapping"] == bad["person_mapping"]
    assert archived_path(error["artifact_uri"]).read_bytes() == path.read_bytes()
    assert core_snapshot(connection) == before


def test_soft_merge_and_npi_mapping(seeded, tmp_path):
    connection, archive, _ = seeded
    original = connection.execute("SELECT person_id FROM core.person_identifiers WHERE scheme = 'sec_cik' AND value = '0002345678'").fetchone()[0]
    survivor, raw_id = connection.execute(
        "SELECT person_id, raw_record_id FROM core.person_identifiers WHERE scheme = 'sec_cik' AND person_id <> %s LIMIT 1",
        (original,),
    ).fetchone()
    connection.execute("UPDATE core.people SET merged_into_id = %s WHERE id = %s", (survivor, original))
    connection.execute("INSERT INTO core.person_identifiers VALUES (%s, 'npi', '1234567893', %s)", (original, raw_id))
    envelope = json.loads(CONTACTS.read_text().splitlines()[0])
    envelope["person_mapping"] = {"person_id": str(original), "npi": "1234567893"}
    path = _fixture(tmp_path / "mapped.json", envelope)
    contact_vendor.load_fixture(connection, path, archive_dir=archive)
    assert connection.execute("SELECT person_id FROM core.contact_points WHERE kind = 'email'").fetchone() == (survivor,)
    assert connection.execute("SELECT payload ->> 'resolved_person_id' FROM core.raw_records WHERE source_code = 'contact_vendor'").fetchone() == (str(survivor),)


def test_pending_and_identifier_mismatch_stay_raw(seeded, tmp_path):
    connection, archive, _ = seeded
    envelope = json.loads(CONTACTS.read_text().splitlines()[0])
    cases = [
        {**envelope, "raw_payload": {**envelope["raw_payload"], "phone_enrichment": {"status": "pending"}}},
        {**envelope, "id": "wrong-person", "raw_payload": {"person": {
            "linkedin_url": "https://www.linkedin.com/in/fictional-wrong-person", "email": "wrong@example.com",
        }}},
    ]
    result = contact_vendor.load_fixture(connection, _fixture(tmp_path / "withheld.json", cases), archive_dir=archive)
    assert result["records_new"] == 2
    assert connection.execute("SELECT count(*) FROM core.contact_points WHERE kind = 'email'").fetchone() == (0,)
