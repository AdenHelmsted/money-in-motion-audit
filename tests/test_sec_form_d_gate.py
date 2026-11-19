#GOAL Verify Form D facts, matching, cumulative amendments, and replay.

from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
from zipfile import ZipFile

from psycopg import sql
import pytest

from mim.loaders.sec_form_d import LoadError, load_fixture, rebuild_from_raw
from mim.loaders.sec_form345 import load_fixture as load_form345
from mim.loaders.sec_form345 import rebuild_from_raw as rebuild_from_form345
from conftest import INGESTION_TABLES, archived_path, core_snapshot


FIXTURES = Path(__file__).resolve().parent / "fixtures"
GATE = FIXTURES / "sec_form_d/gate"
AMENDMENT = FIXTURES / "sec_form_d/amendment"
FORM345 = FIXTURES / "sec_form345/gate"
ORIGINAL = "0002220001-25-000001"
SECOND_OFFERING = "0002220001-25-000008"
AMENDED = "0002220001-25-000010"
EMPLOYER = "0001234567-25-000101"
STATE = "0002220003-25-000003"
POOLED = "0002220006-25-000006"
MULTIPLE_ISSUERS = "0002220007-25-000007"


def raise_signals(connection):
    return connection.execute(
        """
        SELECT s.id, p.full_name, s.occurred_on, s.amount_cents,
               array_agg(r.external_id ORDER BY r.external_id) AS evidence
        FROM core.signals s JOIN core.people p ON p.id = s.person_id
        JOIN core.signal_evidence e ON e.signal_id = s.id
        JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE s.kind = 'private_raise' AND s.detector_version = 'sec_form_d_v1'
        GROUP BY s.id, p.full_name ORDER BY s.occurred_on, p.full_name
        """
    ).fetchall()


@pytest.fixture
def loaded(gate_db, tmp_path):
    result = load_fixture(gate_db, GATE, archive_dir=tmp_path / "archive")
    assert (result["status"], result["records_seen"], result["records_new"]) == ("succeeded", 22, 22)
    return gate_db, result


def test_new_raise_raw_evidence_officers_and_rounding(loaded):
    connection, result = loaded
    license_code, cadence, permitted_uses = connection.execute(
        "SELECT license, refresh_cadence, permitted_uses FROM core.sources WHERE code = 'sec_form_d'"
    ).fetchone()
    assert (license_code, cadence) == ("public_domain", "quarterly")
    assert permitted_uses.strip()
    fetched = connection.execute(
        "SELECT status, artifact_uri, artifact_sha256, records_seen, records_new FROM core.fetches"
    ).fetchone()
    assert fetched == ("succeeded", result["artifact_uri"], result["artifact_sha256"], 22, 22)
    assert hashlib.sha256(archived_path(fetched[1]).read_bytes()).hexdigest() == fetched[2]

    records = {
        key: (day, payload, digest)
        for key, day, payload, digest in connection.execute(
            "SELECT external_id, observed_on, payload, payload_sha256 FROM core.raw_records WHERE source_code = 'sec_form_d'"
        ).fetchall()
    }
    assert {key for key in records if key.startswith(ORIGINAL)} == {
        f"{ORIGINAL}:OFFERING:1", f"{ORIGINAL}:RECIPIENT:1",
        *(f"{ORIGINAL}:RELATEDPERSON:{sequence}" for sequence in range(1, 4)),
    }
    day, officer, digest = records[f"{ORIGINAL}:RELATEDPERSON:1"]
    assert day == date(2025, 2, 3)
    assert officer["FORMDSUBMISSION"]["FILE_NUM"] == "021-100001"
    assert officer["ISSUERS"][0]["CIK"] == "2220001"
    assert officer["OFFERING"]["TOTALOFFERINGAMOUNT"] == "Indefinite"
    assert officer["OFFERING"]["TOTALAMOUNTSOLD"] == "1234567.125"
    assert officer["RELATEDPERSONS"]["MIDDLENAME"] == "Quinn"
    canonical = json.dumps(officer, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert digest == hashlib.sha256(canonical).hexdigest()
    recipient = records[f"{ORIGINAL}:RECIPIENT:1"][1]
    assert recipient["RECIPIENTS"]["RECIPIENTNAME"] == "Sample Placement Services"
    assert recipient["SIGNATURES"][0]["SIGNATURETITLE"] == "Chief Executive Officer"

    assert connection.execute(
        """
        SELECT o.kind, o.locality, o.region, l.role, l.method, l.confidence
        FROM core.organizations o JOIN core.organization_identifiers i ON i.organization_id = o.id
        JOIN core.record_links l ON l.organization_id = o.id
        JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE i.scheme = 'sec_cik' AND i.value = '0002220001' AND r.external_id = %s
        """,
        (f"{ORIGINAL}:OFFERING:1",),
    ).fetchall() == [("private_company", "Seattle", "WA", "issuer", "exact_identifier", Decimal("1.00"))]
    assert connection.execute(
        """
        SELECT p.full_name, p.given_name, p.family_name, pos.seniority, pos.is_current,
               l.role, l.method, l.confidence
        FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
        JOIN core.people p ON p.id = l.person_id
        JOIN core.positions pos ON pos.person_id = p.id AND pos.raw_record_id = r.id
        WHERE r.external_id LIKE %s ORDER BY p.full_name
        """,
        (f"{ORIGINAL}:RELATEDPERSON:%",),
    ).fetchall() == [
        ("Ada Quinn Stone", "Ada", "Stone", "c_suite", True,
         "related_person", "name_and_employer", Decimal("1.00")),
        ("Isaac River", "Isaac", "River", "owner", True,
         "related_person", "name_and_employer", Decimal("1.00")),
        ("Lila Frost", "Lila", "Frost", "board", True,
         "related_person", "name_and_employer", Decimal("1.00")),
    ]
    assert connection.execute(
        """
        SELECT p.full_name, l.kind, l.locality, l.country, l.observed_on
        FROM core.locations l JOIN core.people p ON p.id = l.person_id
        JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.external_id LIKE %s ORDER BY p.full_name
        """,
        (f"{ORIGINAL}:RELATEDPERSON:%",),
    ).fetchall() == [
        ("Ada Quinn Stone", "work", "Seattle", "US", date(2025, 2, 3)),
        ("Isaac River", "work", "Seattle", "US", date(2025, 2, 3)),
        ("Lila Frost", "work", "Overseas City", "ZZ", date(2025, 2, 3)),
    ]
    pairs = connection.execute(
        """
        SELECT a.full_name, b.full_name, rel.person_a_id < rel.person_b_id
        FROM core.relationships rel JOIN core.people a ON a.id = rel.person_a_id
        JOIN core.people b ON b.id = rel.person_b_id
        JOIN core.raw_records r ON r.id = rel.raw_record_id
        WHERE rel.kind = 'co_officers' AND r.external_id LIKE %s
        """,
        (f"{ORIGINAL}:%",),
    ).fetchall()
    assert len(pairs) == 3 and all(ordered for _, _, ordered in pairs)
    assert {frozenset((a, b)) for a, b, _ in pairs} == {
        frozenset(("Ada Quinn Stone", "Isaac River")),
        frozenset(("Ada Quinn Stone", "Lila Frost")),
        frozenset(("Isaac River", "Lila Frost")),
    }

    signals = raise_signals(connection)
    assert len(signals) == connection.execute("SELECT count(*) FROM core.signals").fetchone()[0] == 5
    first, second = signals[:2]
    for (_, person, day, amount, evidence), expected_person, person_seq in (
        (first, "Ada Quinn Stone", 1), (second, "Isaac River", 2),
    ):
        assert (person, day, amount) == (expected_person, date(2025, 1, 31), 123456713)
        assert evidence == [f"{ORIGINAL}:OFFERING:1", f"{ORIGINAL}:RELATEDPERSON:{person_seq}"]
    assert signals[-1][1:4] == ("Ada Quinn Stone", date(2025, 2, 12), 701)
    assert signals[-1][-1] == [f"{SECOND_OFFERING}:OFFERING:1", f"{SECOND_OFFERING}:RELATEDPERSON:1"]


def test_form4_people_resolve_by_employer_and_trigram_state(gate_db, tmp_path):
    archive_dir = tmp_path / "archive"
    load_form345(gate_db, FORM345, archive_dir=archive_dir)
    insiders = dict(gate_db.execute("SELECT value, person_id FROM core.person_identifiers").fetchall())
    existing_issuer = gate_db.execute(
        "SELECT organization_id FROM core.organization_identifiers WHERE scheme = 'sec_cik' AND value = '0001234567'"
    ).fetchone()[0]
    assert gate_db.execute("SELECT similarity('Morgan Example', 'Morgan R. Example') >= 0.85").fetchone() == (True,)
    load_fixture(gate_db, GATE, archive_dir=archive_dir)

    for accession, cik, method, confidence in (
        (EMPLOYER, "0002345678", "name_and_employer", "0.90"),
        (STATE, "0200000003", "name_and_location", "0.80"),
    ):
        assert gate_db.execute(
            """
            SELECT l.person_id, l.method, l.confidence FROM core.record_links l
            JOIN core.raw_records r ON r.id = l.raw_record_id
            WHERE r.external_id = %s AND l.role = 'related_person'
            """,
            (f"{accession}:RELATEDPERSON:1",),
        ).fetchall() == [(insiders[cik], method, Decimal(confidence))]
    assert gate_db.execute(
        """
        SELECT l.organization_id FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.external_id = %s AND l.role = 'issuer'
        """,
        (f"{EMPLOYER}:OFFERING:1",),
    ).fetchall() == [(existing_issuer,)]
    assert gate_db.execute(
        "SELECT count(*) FROM core.people WHERE lower(full_name) LIKE 'alice %' OR lower(full_name) LIKE 'morgan %'"
    ).fetchone() == (2,)


def test_excluded_offerings_retain_raw_and_entities(loaded):
    connection, _ = loaded
    assert connection.execute(
        """
        SELECT p.full_name, array_agg(i.value ORDER BY i.value)
        FROM core.record_links related JOIN core.people p ON p.id = related.person_id
        JOIN core.raw_records rp ON related.raw_record_id = rp.id
        JOIN core.raw_records ro ON split_part(ro.external_id, ':', 1) = split_part(rp.external_id, ':', 1)
        JOIN core.record_links issuer ON issuer.raw_record_id = ro.id AND issuer.role = 'issuer'
        JOIN core.organization_identifiers i ON i.organization_id = issuer.organization_id AND i.scheme = 'sec_cik'
        WHERE related.role = 'related_person' AND rp.source_code = 'sec_form_d' AND ro.source_code = 'sec_form_d'
          AND split_part(ro.external_id, ':', 2) = 'OFFERING' AND p.full_name = ANY(%s)
        GROUP BY p.full_name ORDER BY p.full_name
        """,
        (["Avery Hill", "Jamie Slate", "Casey Cedar", "Quinn Vale", "Emery Lake"],),
    ).fetchall() == [
        ("Avery Hill", ["0002220006"]), ("Casey Cedar", ["0002220005"]),
        ("Emery Lake", ["0002220009"]), ("Jamie Slate", ["0002220004"]),
        ("Quinn Vale", ["0002220007", "0002220017"]),
    ]
    assert connection.execute(
        "SELECT payload -> 'OFFERING' ->> 'INDUSTRYGROUPTYPE' FROM core.raw_records WHERE external_id = %s",
        (f"{POOLED}:OFFERING:1",),
    ).fetchone() == ("Pooled Investment Fund",)
    assert not {"Avery Hill", "Jamie Slate", "Casey Cedar", "Quinn Vale", "Emery Lake", "Lila Frost"} & {
        person for _, person, _, _, _ in raise_signals(connection)
    }
    assert connection.execute(
        "SELECT external_id FROM core.raw_records WHERE external_id LIKE %s ORDER BY external_id",
        (f"{MULTIPLE_ISSUERS}:OFFERING:%",),
    ).fetchall() == [(f"{MULTIPLE_ISSUERS}:OFFERING:1",), (f"{MULTIPLE_ISSUERS}:OFFERING:2",)]


def test_multiple_issuers_with_one_designated_primary(gate_db, tmp_path):
    batch = shutil.copytree(GATE, tmp_path / "primary")
    issuers = batch / "ISSUERS.tsv"
    issuers.write_bytes(issuers.read_bytes().replace(
        f"{MULTIPLE_ISSUERS}\tNo\t1\t".encode(), f"{MULTIPLE_ISSUERS}\tYes\t1\t".encode(), 1,
    ))
    load_fixture(gate_db, batch, archive_dir=tmp_path / "archive")

    assert gate_db.execute(
        """
        SELECT s.amount_cents, i.value, array_agg(r.external_id ORDER BY r.external_id)
        FROM core.signals s JOIN core.people p ON p.id = s.person_id
        JOIN core.organization_identifiers i ON i.organization_id = s.organization_id AND i.scheme = 'sec_cik'
        JOIN core.signal_evidence e ON e.signal_id = s.id
        JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE s.detector_version = 'sec_form_d_v1' AND p.full_name = 'Quinn Vale'
        GROUP BY s.id, i.value
        """
    ).fetchall() == [(300000, "0002220007", [
        f"{MULTIPLE_ISSUERS}:OFFERING:1", f"{MULTIPLE_ISSUERS}:RELATEDPERSON:1",
    ])]


@pytest.mark.parametrize("reference", ["accession", "file_number", "first_sale"])
def test_amendment_updates_only_the_offering_it_references(loaded, tmp_path, reference):
    connection, _ = loaded
    before = {(person, day): (signal_id, amount, evidence)
              for signal_id, person, day, amount, evidence in raise_signals(connection)}
    path = AMENDMENT
    if reference != "accession":
        path = shutil.copytree(AMENDMENT, tmp_path / "amendment")
        offering = path / "OFFERING.tsv"
        offering.write_bytes(offering.read_bytes().replace(b"\t0002220001-25-000001\t", b"\t\t", 1))
        if reference == "first_sale":
            submission = path / "FORMDSUBMISSION.tsv"
            submission.write_bytes(submission.read_bytes().replace(b"021-100001", b"", 1))
    result = load_fixture(connection, path, archive_dir=tmp_path / "archive")
    assert (result["status"], result["records_new"]) == ("succeeded", 4)
    after = {(person, day): (signal_id, amount, evidence)
             for signal_id, person, day, amount, evidence in raise_signals(connection)}
    assert set(after) == set(before)
    for person, seq in (("Ada Quinn Stone", 1), ("Isaac River", 2)):
        key = (person, date(2025, 1, 31))
        signal_id, amount, evidence = after[key]
        assert signal_id == before[key][0]
        assert amount == 130000100
        assert evidence == [f"{AMENDED}:OFFERING:1", f"{AMENDED}:RELATEDPERSON:{seq}"]
    independent = ("Ada Quinn Stone", date(2025, 2, 12))
    assert after[independent] == before[independent]


@pytest.mark.parametrize("rebuild", [rebuild_from_raw, rebuild_from_form345])
def test_exact_replay_and_rebuild_for_both_sec_sources(gate_db, tmp_path, rebuild):
    archive_dir = tmp_path / "archive"
    artifacts = [
        load_form345(gate_db, FORM345, archive_dir=archive_dir),
        load_fixture(gate_db, GATE, archive_dir=archive_dir),
        load_fixture(gate_db, AMENDMENT, archive_dir=archive_dir),
    ]
    before = core_snapshot(gate_db)
    for path, original in zip((shutil.copytree(GATE, tmp_path / "copied"), AMENDMENT), artifacts[1:], strict=True):
        replay = load_fixture(gate_db, path, archive_dir=archive_dir)
        assert replay["status"] == "skipped"
        assert replay["artifact_uri"] == original["artifact_uri"]
        assert core_snapshot(gate_db) == before

    for artifact in artifacts:
        archived_path(artifact["artifact_uri"]).unlink()
    derived = sorted(before.keys() - INGESTION_TABLES)
    gate_db.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(
        sql.Identifier("core", name) for name in derived
    )))
    assert all(not rows for name, rows in core_snapshot(gate_db).items() if name in derived)
    rebuild(gate_db)
    assert core_snapshot(gate_db) == before


def test_zip_preserves_exact_bytes_and_optional_tables_can_be_absent(gate_db, tmp_path):
    source = tmp_path / "quarter.zip"
    with ZipFile(source, "w") as archive:
        for table in sorted(GATE.glob("*.tsv")):
            if table.stem not in {"RECIPIENTS", "SIGNATURES"}:
                archive.write(table, arcname=f"{table.stem}.txt")
    contents = source.read_bytes()
    result = load_fixture(gate_db, source, archive_dir=tmp_path / "archive")
    assert (result["status"], result["records_new"]) == ("succeeded", 21)
    assert archived_path(result["artifact_uri"]).read_bytes() == contents
    assert result["artifact_sha256"] == hashlib.sha256(contents).hexdigest()


@pytest.mark.parametrize("failure", ["malformed_tsv", "constraint"])
def test_atomic_failure_reports_row_context(gate_db, tmp_path, failure):
    source = GATE
    if failure == "malformed_tsv":
        source = shutil.copytree(GATE, tmp_path / "malformed")
        offering = source / "OFFERING.tsv"
        offering.write_bytes(offering.read_bytes().replace(b"\t98.765\t", b"\tnot-a-decimal\t", 1))
    else:
        gate_db.execute(
            "ALTER TABLE core.positions ADD CONSTRAINT gate_reject_c_suite "
            "CHECK (seniority <> 'c_suite' OR organization_name <> 'Example Research LLC')"
        )
    before = core_snapshot(gate_db)

    with pytest.raises(LoadError) as raised:
        load_fixture(gate_db, source, archive_dir=tmp_path / "archive")
    error = raised.value.as_dict()
    assert set(error) == {"source_code", "artifact_uri", "table_name", "accession_number", "row_number", "reason"}
    assert error["source_code"] == "sec_form_d"
    assert archived_path(error["artifact_uri"]).is_file()
    if failure == "malformed_tsv":
        assert (error["table_name"], error["accession_number"], error["row_number"]) == ("OFFERING", EMPLOYER, 3)
        assert "TOTALAMOUNTSOLD" in error["reason"]
    else:
        assert (error["table_name"], error["accession_number"], error["row_number"]) == ("RELATEDPERSONS", ORIGINAL, 2)
        assert "gate_reject_c_suite" in error["reason"]
    assert core_snapshot(gate_db) == before


def test_registration_is_verified_before_ingestion(gate_db, tmp_path):
    gate_db.execute("DELETE FROM core.sources WHERE code = 'sec_form_d'")
    before = core_snapshot(gate_db)
    with pytest.raises(LoadError, match="core.sources") as raised:
        load_fixture(gate_db, GATE, archive_dir=tmp_path / "archive")
    assert raised.value.as_dict()["source_code"] == "sec_form_d"
    assert core_snapshot(gate_db) == before


def test_entity_rows_unstructured_previous_accession_and_overflow_remain_raw(gate_db, tmp_path):
    fixture = shutil.copytree(GATE, tmp_path / "quarter")
    people = fixture / "RELATEDPERSONS.tsv"
    people.write_text(people.read_text().replace("Ada\tQuinn\tStone", "Related person is an entity\t\t", 1))
    offerings = fixture / "OFFERING.tsv"
    first = "0002220001-25-000001"
    offerings.write_text(offerings.read_text().replace("false\t\t2025-01-31", "false\tNone reported\t2025-01-31", 1)
                         .replace("1234567.125", "92233720368547758.08", 1))
    load_fixture(gate_db, fixture, archive_dir=tmp_path / "archive")

    assert gate_db.execute("""
        SELECT payload -> 'OFFERING' ->> 'PREVIOUSACCESSIONNUMBER', payload -> 'RELATEDPERSONS' ->> 'LASTNAME'
        FROM core.raw_records WHERE external_id = %s
    """, (f"{first}:RELATEDPERSON:1",)).fetchone() == ("None reported", "")
    assert gate_db.execute("""
        SELECT count(*) FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.external_id = %s AND l.person_id IS NOT NULL
    """, (f"{first}:RELATEDPERSON:1",)).fetchone() == (0,)
    assert gate_db.execute("""
        SELECT count(*) FROM core.signal_evidence e JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE r.external_id LIKE %s
    """, (f"{first}:%",)).fetchone() == (0,)
