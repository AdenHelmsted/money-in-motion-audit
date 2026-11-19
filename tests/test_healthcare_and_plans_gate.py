#GOAL Verify NPI and EIN attribution, government signals, and cross-source rebuild.

from datetime import date
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

from conftest import archived_path, core_snapshot
from mim.loaders import cms_open_payments, contact_vendor, dol_form5500, linkedin, nppes, sec_form345, sec_form_d
from mim.loaders.sec_ingestion import LoadError


FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC = FIXTURES / "healthcare_and_plans"
MONTHLY = PUBLIC / "npidata_pfile_20050523-20250531.csv"
WEEKLY = PUBLIC / "npidata_pfile_20250602-20250608.csv"
SF = PUBLIC / "f_5500_sf_2025_latest.csv"
FULL = PUBLIC / "f_5500_2025_latest.csv"
PAYMENTS = PUBLIC / "payments.csv"


def _zip(path, *fixtures, extra=None):
    with ZipFile(path, "w") as archive:
        for fixture in fixtures:
            archive.write(fixture, f"nested/{fixture.name}")
        if extra:
            archive.writestr(*extra)
    return path


def _cli(conn, *args):
    return subprocess.run([sys.executable, "-m", "mim", *args, "--dsn", conn.info.dsn],
                          capture_output=True, text=True, timeout=30)


def test_nppes_individual_group_and_practice_signals(gate_db, tmp_path):
    archive = tmp_path / "archive"
    nppes.load_fixture(gate_db, MONTHLY, archive_dir=archive)
    nppes.load_fixture(gate_db, _zip(tmp_path / "weekly.zip", WEEKLY, extra=("pl_pfile_ignored.csv", "ignore\n")),
                       archive_dir=archive)
    assert gate_db.execute("""SELECT i.value, p.full_name FROM core.person_identifiers i JOIN core.people p ON p.id = i.person_id
                             WHERE i.scheme = 'npi' ORDER BY 1""").fetchall() == [("1234567810", "Niko Flint"), ("1234567893", "Inez Quill")]
    assert gate_db.execute("""SELECT i.value, o.kind, o.locality, o.region FROM core.organization_identifiers i
                             JOIN core.organizations o ON o.id = i.organization_id WHERE i.scheme = 'npi'""").fetchone() == (
                                 "1234567802", "medical_practice", "Tulsa", "OK")
    assert gate_db.execute("""SELECT locality, region, postal_code, country, observed_on FROM core.locations
                             WHERE person_id = (SELECT person_id FROM core.person_identifiers WHERE value = '1234567893')
                             ORDER BY observed_on""").fetchall() == [
                                 ("Austin", "TX", "78701-0001", "US", date(2025, 5, 31)),
                                 ("Round Rock", "TX", "78664", "US", date(2025, 6, 8))]
    assert gate_db.execute("""SELECT s.kind, s.occurred_on, count(e.raw_record_id) FROM core.signals s
                             JOIN core.signal_evidence e ON e.signal_id = s.id WHERE s.detector_version = 'nppes_v1'
                             GROUP BY s.id ORDER BY s.occurred_on""").fetchall() == [
                                 ("new_practice", date(2025, 3, 1), 1), ("new_practice", date(2025, 6, 8), 2)]
    assert gate_db.execute("SELECT count(*) FROM core.record_links WHERE role = 'subject' AND method = 'exact_identifier'").fetchone() == (5,)


def test_open_payments_backfill_is_exact_npi_only(gate_db, tmp_path):
    archive = tmp_path / "archive"
    result = cms_open_payments.load_fixture(gate_db, PAYMENTS, archive_dir=archive)
    assert result["records_new"] == 5
    assert gate_db.execute("SELECT count(*) FROM core.record_links").fetchone() == (0,)
    nppes.load_fixture(gate_db, MONTHLY, archive_dir=archive)
    rows = gate_db.execute("""
        SELECT r.external_id, i.value, l.role, l.method, l.confidence
        FROM core.raw_records r JOIN core.record_links l ON l.raw_record_id = r.id
        JOIN core.person_identifiers i ON i.person_id = l.person_id AND i.scheme = 'npi'
        WHERE r.source_code = 'cms_open_payments' ORDER BY r.external_id
        """).fetchall()
    assert len(rows) == 2 and all(row[1:] == ("1234567893", "subject", "exact_identifier", 1) for row in rows)
    assert rows[1][0] == "fictional-payment-1" and len(rows[0][0]) == 64
    assert gate_db.execute("""SELECT count(*) FROM core.raw_records r JOIN core.fetches f ON f.id = r.fetch_id
                             WHERE r.source_code = 'cms_open_payments' AND f.source_code = r.source_code""").fetchone() == (5,)


def test_npi_links_follow_the_survivor_and_rebuild(gate_db, tmp_path):
    archive = tmp_path / "archive"
    nppes.load_fixture(gate_db, MONTHLY, archive_dir=archive)
    original, survivor = [
        row[0] for row in gate_db.execute("SELECT person_id FROM core.person_identifiers ORDER BY value DESC")
    ]
    gate_db.execute("UPDATE core.people SET merged_into_id = %s WHERE id = %s", (survivor, original))
    nppes.load_fixture(gate_db, WEEKLY, archive_dir=archive)
    cms_open_payments.load_fixture(gate_db, PAYMENTS, archive_dir=archive)
    assert gate_db.execute("""
        SELECT DISTINCT l.person_id FROM core.record_links l
        JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.source_code = 'cms_open_payments' AND l.role = 'subject'
    """).fetchall() == [(survivor,)]
    assert gate_db.execute("""
        SELECT DISTINCT person_id FROM core.locations WHERE observed_on = DATE '2025-06-08'
    """).fetchall() == [(survivor,)]
    assert gate_db.execute("SELECT DISTINCT person_id FROM core.signals").fetchall() == [(survivor,)]
    before = core_snapshot(gate_db)
    nppes.rebuild_from_raw(gate_db)
    assert core_snapshot(gate_db) == before


def test_bulk_csv_rolls_back_completed_chunks(gate_db, tmp_path):
    path = tmp_path / "payments.csv"
    header = "Record ID,Physician NPI,Date of Payment,Payment Amount\n"
    rows = "".join(f"bulk-{index},1234567893,2025-01-01,1.25\n" for index in range(1001))
    path.write_text(header + rows + "bad-line,bad-npi,2025-01-01,1.25\n")
    before = core_snapshot(gate_db)
    with pytest.raises(LoadError) as raised:
        cms_open_payments.load_fixture(gate_db, path, archive_dir=tmp_path / "archive")
    assert raised.value.as_dict()["row_number"] == 1003
    assert core_snapshot(gate_db) == before
    path.write_text(header + rows + "bulk-0,1234567893,2025-01-01,1.25\n")
    result = cms_open_payments.load_fixture(gate_db, path, archive_dir=tmp_path / "archive")
    assert (result["records_seen"], result["records_new"]) == (1002, 1001)


def test_form5500_requires_short_pension_and_corroborated_owner(gate_db, tmp_path):
    short_form = tmp_path / SF.name
    short_form.write_text(SF.read_text().replace(",12,1250000.015,", ",12.0,1250000.015,")
                          .replace(",100,1250000,", ",100.0,-1250000,"))
    path = _zip(tmp_path / "plans.zip", short_form, FULL, extra=("f_sch_a_2025_latest.csv", "ignore\n"))
    result = dol_form5500.load_fixture(gate_db, path, archive_dir=tmp_path / "archive")
    assert result["records_new"] == 7
    assert gate_db.execute("SELECT value FROM core.organization_identifiers WHERE scheme = 'ein' ORDER BY value").fetchall() == [
        ("123456789",), ("987654321",)]
    row = gate_db.execute("""
        SELECT p.full_name, s.kind, s.amount_cents, s.occurred_on, array_agg(r.external_id ORDER BY r.external_id)
        FROM core.signals s JOIN core.people p ON p.id = s.person_id
        JOIN core.signal_evidence e ON e.signal_id = s.id JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE s.detector_version = 'form5500_v1' GROUP BY s.id, p.full_name
        """).fetchall()
    assert row == [("Dana Flint", "plan_sponsor_owner", 125000002, date(2026, 5, 1),
                    ["fictional-owner-proof", "fictional-small-pension"])]
    assert gate_db.execute("""SELECT count(*) FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
                             WHERE r.external_id = 'fictional-unproven-signer' AND l.person_id IS NOT NULL""").fetchone() == (0,)


def test_cli_exact_replay_and_seven_source_rebuild(gate_db, tmp_path):
    archive = tmp_path / "archive"
    sources = [(sec_form345, FIXTURES / "sec_form345/gate"), (sec_form_d, FIXTURES / "sec_form_d/gate"),
               (linkedin, FIXTURES / "linkedin/envelopes.jsonl"), (contact_vendor, FIXTURES / "contact_vendor/envelopes.jsonl"),
               (cms_open_payments, PAYMENTS), (nppes, MONTHLY), (nppes, WEEKLY), (dol_form5500, SF)]
    results = []
    for loader, path in sources:
        if loader in (cms_open_payments, nppes, dol_form5500):
            result = _cli(gate_db, "load", loader.SOURCE_CODE, "--fixture", str(path), "--archive-dir", str(archive))
            assert result.returncode == 0, result.stderr
            results.append(json.loads(result.stdout))
        else:
            results.append(loader.load_fixture(gate_db, path, archive_dir=archive))

    before = core_snapshot(gate_db)
    audited = _cli(gate_db, "audit-db")
    assert audited.returncode == 0, audited.stdout + audited.stderr
    report = json.loads(audited.stdout)
    assert set(report["sources"]) == {loader.SOURCE_CODE for loader, _ in sources}
    assert report["status"] == "passed"
    assert report["sources"]["cms_open_payments"]["unlinked_raw_records"] > 0
    for loader, path in sources:
        assert loader.load_fixture(gate_db, path, archive_dir=archive)["status"] == "skipped"
        assert core_snapshot(gate_db) == before
    for result in results:
        archived_path(result["artifact_uri"]).unlink()
    person = gate_db.execute("SELECT id FROM core.people ORDER BY id LIMIT 1").fetchone()[0]
    gate_db.execute("""
        INSERT INTO core.signals (person_id, kind, occurred_on, summary, confidence, detector_version)
        VALUES (%s, 'insider_sale', DATE '2025-01-01', 'stale derived row', 1, 'stale_version')
    """, (person,))
    contact = gate_db.execute("SELECT id FROM core.contact_points WHERE verified_at IS NOT NULL LIMIT 1").fetchone()[0]
    gate_db.execute("UPDATE core.contact_points SET verified_at = last_seen_on + interval '7 days' WHERE id = %s", (contact,))
    failed = _cli(gate_db, "audit-db")
    assert failed.returncode == 1
    violations = json.loads(failed.stdout)["violations"]
    assert violations["signals_without_evidence"] == violations["invalid_contact_dates"] == 1
    result = _cli(gate_db, "rebuild")
    assert result.returncode == 0, result.stderr
    assert core_snapshot(gate_db) == before
    rebuilt = _cli(gate_db, "audit-db")
    assert rebuilt.returncode == 0 and json.loads(rebuilt.stdout) == report


def test_audit_detects_merge_cycles(gate_db, tmp_path):
    nppes.load_fixture(gate_db, MONTHLY, archive_dir=tmp_path / "archive")
    left, right = [row[0] for row in gate_db.execute("SELECT id FROM core.people ORDER BY id")]
    gate_db.execute("UPDATE core.people SET merged_into_id = %s WHERE id = %s", (right, left))
    gate_db.execute("UPDATE core.people SET merged_into_id = %s WHERE id = %s", (left, right))
    org = gate_db.execute("SELECT id FROM core.organizations").fetchone()[0]
    gate_db.execute("UPDATE core.organizations SET merged_into_id = id WHERE id = %s", (org,))
    result = _cli(gate_db, "audit-db")
    assert result.returncode == 1
    violations = json.loads(result.stdout)["violations"]
    assert violations["people_reaching_merge_cycles"] == 2
    assert violations["organizations_reaching_merge_cycles"] == 1
    before = core_snapshot(gate_db)
    rebuild = _cli(gate_db, "rebuild")
    assert rebuild.returncode == 1
    assert core_snapshot(gate_db) == before


def test_standard_signer_matches_an_owner_at_existing_sponsor(gate_db, tmp_path):
    archive = tmp_path / "archive"
    sec_form_d.load_fixture(gate_db, FIXTURES / "sec_form_d/gate", archive_dir=archive)
    envelope = json.loads((FIXTURES / "linkedin/envelopes.jsonl").read_text().splitlines()[0])
    role = envelope["raw_payload"]["experience"][0]
    role.update(company_name="Example Research LLC", position_title="Owner")
    profile = tmp_path / "owner.json"
    profile.write_text(json.dumps(envelope), encoding="utf-8")
    linkedin.load_fixture(gate_db, profile, archive_dir=archive)
    plan = tmp_path / "F_5500_SF_2025_Latest.csv"
    plan.write_text("ACK_ID,SF_SPONS_EIN,SF_SPONSOR_NAME,SF_SPONS_US_CITY,SF_SPONS_US_STATE,SF_SPONS_SIGNED_NAME,"
                    "SF_TOT_PARTCP_BOY_CNT,SF_TOT_ASSETS_EOY_AMT,SF_TYPE_PENSION_BNFT_CODE,DATE_RECEIVED,FILING_STATUS\n"
                    "standard-plan,101234567,Example Research LLC,Seattle,WA,Mara Vale,9,1000000,2J,2026-05-01,FILING_RECEIVED\n",
                    encoding="utf-8")
    dol_form5500.load_fixture(gate_db, plan, archive_dir=archive)
    assert gate_db.execute("""
        SELECT p.full_name, i.value, array_agg(r.source_code ORDER BY r.source_code)
        FROM core.signals s JOIN core.people p ON p.id = s.person_id
        JOIN core.organization_identifiers i ON i.organization_id = s.organization_id AND i.scheme = 'sec_cik'
        JOIN core.signal_evidence e ON e.signal_id = s.id JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE s.detector_version = 'form5500_v1' GROUP BY s.id, p.full_name, i.value
        """).fetchall() == [("Mara Vale", "0002220001", ["dol_form5500", "linkedin"])]


@pytest.mark.parametrize(("loader", "fixture"), [(nppes, MONTHLY), (cms_open_payments, PAYMENTS), (dol_form5500, SF)])
def test_missing_registration_precedes_archive(gate_db, tmp_path, loader, fixture):
    gate_db.execute("DELETE FROM core.sources WHERE code = %s", (loader.SOURCE_CODE,))
    with pytest.raises(LoadError, match="core.sources") as caught:
        loader.load_fixture(gate_db, fixture, archive_dir=tmp_path / "archive")
    assert caught.value.as_dict()["source_code"] == loader.SOURCE_CODE
    assert not (tmp_path / "archive").exists()


def test_invalid_public_csv_rolls_back_with_row_context(gate_db, tmp_path):
    bad = tmp_path / SF.name
    bad.write_text(SF.read_text().replace("1250000.015", "not-an-amount"), encoding="utf-8")
    before = core_snapshot(gate_db)
    result = _cli(gate_db, "load", "dol_form5500", "--fixture", str(bad), "--archive-dir", str(tmp_path / "archive"))
    assert result.returncode == 1, result.stdout
    error = json.loads(result.stderr)
    assert error["source_code"] == "dol_form5500" and error["accession_number"] == "fictional-small-pension"
    assert error["row_number"] == 3 and error["table_name"] == SF.name
    assert core_snapshot(gate_db) == before
