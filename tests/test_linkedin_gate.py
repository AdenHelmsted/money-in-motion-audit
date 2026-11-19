#GOAL Verify reviewed LinkedIn facts, snapshot signals, reservations, and audit sampling.

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from random import Random
import shutil
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from mim.audit.linkedin_sample import build_report
from mim.loaders.linkedin import LoadError, load_fixture, rebuild_from_raw
from mim.loaders.linkedin_reservations import release_lookup, reserve_lookup
from mim.loaders.sec_form345 import load_fixture as load_form345
from mim.loaders.sec_form_d import load_fixture as load_form_d
from conftest import INGESTION_TABLES, archived_path, core_snapshot


FIXTURES = Path(__file__).resolve().parent / "fixtures"
GATE = FIXTURES / "linkedin/envelopes.jsonl"
MARA = "fictional-mara-vale"


def _envelopes():
    return [json.loads(line) for line in GATE.read_text(encoding="utf-8").splitlines()]


def _write_json(tmp_path, filename, envelope):
    path = tmp_path / filename
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return path


def _changed_profile(retrieved_at, *, retired=False):
    envelope = _envelopes()[0]
    envelope["id"] = str(uuid4())
    envelope["retrieved_at"] = retrieved_at
    experience = envelope["raw_payload"]["experience"]
    experience[0].update(active_experience=0, date_to="2025-04-30")
    experience.insert(0, {
        "company_name": "Fictional Juniper Devices",
        "position_title": "Chief Technology Officer",
        "date_from": "2025-05-01",
        "date_to": "2025-08-01" if retired else None,
        "active_experience": 0 if retired else 1,
    })
    envelope["raw_payload"]["headline"] = "Retired technology executive" if retired else "Chief Technology Officer"
    return envelope


def test_gate_archives_every_envelope_and_hydrates_only_approved_profiles(gate_db, tmp_path):
    result = load_fixture(gate_db, GATE, archive_dir=tmp_path / "archive")
    assert (result["status"], result["records_new"], result["records_seen"]) == ("succeeded", 7, 7)
    assert (result["accepted_matches"], result["withheld_candidates"], result["withheld_records"]) == (3, 1, 4)
    assert archived_path(result["artifact_uri"]).read_bytes() == GATE.read_bytes()
    assert result["artifact_sha256"] == hashlib.sha256(GATE.read_bytes()).hexdigest()

    rows = gate_db.execute(
        "SELECT external_id, observed_on, payload FROM core.raw_records WHERE source_code = 'linkedin'"
    ).fetchall()
    assert len(rows) == 7
    by_source_id = {payload["id"]: (external_id, day, payload) for external_id, day, payload in rows}
    for number, envelope in enumerate(_envelopes(), 1):
        _, day, payload = by_source_id[envelope["id"]]
        assert day == date(2025, 4, number)
        assert payload["raw_payload"] == envelope["raw_payload"]
        assert payload["provider"] == envelope["provider"]
        assert payload["http_status"] == envelope["http_status"]
    assert by_source_id[_envelopes()[0]["id"]][0] == f"coresignal:{MARA}"

    people = dict(gate_db.execute(
        "SELECT p.full_name, i.value FROM core.people p JOIN core.person_identifiers i ON i.person_id = p.id "
        "WHERE i.scheme = 'linkedin'"
    ).fetchall())
    assert people == {"Mara Vale": MARA, "Imani Quill": "fictional-imani-quill", "Niko Frost": "fictional-niko-frost"}
    assert gate_db.execute("SELECT count(*) FROM core.people").fetchone() == (3,)
    assert gate_db.execute(
        "SELECT count(*) FROM core.record_links WHERE role = 'subject' AND person_id IS NOT NULL"
    ).fetchone() == (3,)
    assert dict(gate_db.execute(
        "SELECT p.full_name, c.value_normalized FROM core.contact_points c JOIN core.people p ON p.id = c.person_id "
        "WHERE c.kind = 'linkedin_url' AND c.label = 'unknown'"
    ).fetchall()) == {name: f"https://www.linkedin.com/in/{slug}" for name, slug in people.items()}
    assert gate_db.execute("SELECT DISTINCT kind FROM core.contact_points").fetchall() == [("linkedin_url",)]
    assert gate_db.execute(
        "SELECT organization_name, title, seniority, is_current FROM core.positions ORDER BY organization_name"
    ).fetchall() == [
        ("Fictional Atlas Instruments", "Engineering Manager", "manager", True),
        ("Fictional Cedar Systems", "Operations Director", "director", True),
        ("Fictional Ember Laboratories", "Vice President of Research", "vp", True),
    ]
    assert gate_db.execute("SELECT school, ended_year FROM core.education ORDER BY school").fetchall() == [
        ("Fictional Central College", 2014), ("Fictional Lake Institute", 2012), ("Fictional Union University", 2011),
    ]
    assert gate_db.execute("SELECT locality, country FROM core.locations ORDER BY locality").fetchall() == [
        ("Chicago", "US"), ("Madison", "US"), ("Seattle", "US"),
    ]


def test_diff_has_two_evidence_rows_for_job_change_promotion_and_retirement(gate_db, tmp_path):
    original = _envelopes()[0]
    promotion = _changed_profile("2025-05-03T10:00:00Z")
    retirement = _changed_profile("2025-08-02T10:00:00Z", retired=True)
    for envelope in (original, promotion, retirement):
        path = _write_json(tmp_path, f"{envelope['id']}.json", envelope)
        assert load_fixture(gate_db, path, archive_dir=tmp_path / "archive")["accepted_matches"] == 1

    signals = gate_db.execute(
        """
        SELECT s.kind, s.occurred_on, array_agg(r.payload ->> 'id' ORDER BY r.observed_on)
        FROM core.signals s JOIN core.signal_evidence e ON e.signal_id = s.id
        JOIN core.raw_records r ON r.id = e.raw_record_id
        WHERE s.detector_version = 'linkedin_v1'
        GROUP BY s.id ORDER BY s.occurred_on, s.kind
        """
    ).fetchall()
    assert signals == [
        ("job_change", date(2025, 5, 3), [original["id"], promotion["id"]]),
        ("promoted_to_executive", date(2025, 5, 3), [original["id"], promotion["id"]]),
        ("retired", date(2025, 8, 2), [promotion["id"], retirement["id"]]),
    ]


def test_profile_identity_is_shared_across_vendors_but_diffs_are_not(gate_db, tmp_path):
    core, pdl, *_ = _envelopes()
    pdl["input"]["url"] = f"https://www.linkedin.com/in/{MARA}"
    pdl["raw_payload"]["data"].update(linkedin_url=f"linkedin.com/in/{MARA}", full_name="Mara Vale")
    path = _write_json(tmp_path, "two-vendors.json", [core, pdl])
    assert load_fixture(gate_db, path, archive_dir=tmp_path / "archive")["accepted_matches"] == 2
    assert gate_db.execute("SELECT count(*) FROM core.people").fetchone() == (1,)
    assert gate_db.execute("SELECT count(*) FROM core.signals WHERE detector_version = 'linkedin_v1'").fetchone() == (0,)


def test_approved_linkedin_mapping_agrees_with_merged_identifier(gate_db, tmp_path):
    archive = tmp_path / "archive"
    load_form345(gate_db, FIXTURES / "sec_form345/gate", archive_dir=archive)
    survivor = gate_db.execute("SELECT person_id FROM core.person_identifiers ORDER BY value LIMIT 1").fetchone()[0]
    original_profile, *_ = _envelopes()
    load_fixture(gate_db, _write_json(tmp_path, "profile.json", original_profile), archive_dir=archive)
    original = gate_db.execute("SELECT person_id FROM core.person_identifiers WHERE scheme = 'linkedin'").fetchone()[0]
    gate_db.execute("UPDATE core.people SET merged_into_id = %s WHERE id = %s", (survivor, original))
    refresh = {**original_profile, "retrieved_at": "2025-04-20T10:00:00Z", "person_mapping": {"person_id": str(survivor)}}
    result = load_fixture(gate_db, _write_json(tmp_path, "refresh.json", refresh), archive_dir=archive)
    assert result["accepted_matches"] == 1
    assert gate_db.execute("""
        SELECT l.person_id FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.source_code = 'linkedin' ORDER BY r.id DESC LIMIT 1
    """).fetchone() == (survivor,)
    before = core_snapshot(gate_db)
    rebuild_from_raw(gate_db)
    assert core_snapshot(gate_db) == before


def test_exact_replay_and_rebuild_all_sources(gate_db, tmp_path):
    archive_dir = tmp_path / "archive"
    results = [
        load_form345(gate_db, FIXTURES / "sec_form345/gate", archive_dir=archive_dir),
        load_form_d(gate_db, FIXTURES / "sec_form_d/gate", archive_dir=archive_dir),
    ]
    envelopes = _envelopes()
    linkedin = envelopes[0]
    linkedin["person_mapping"] = {"sec_cik": "2345678"}
    linkedin["raw_payload"]["experience"][0]["company_name"] = "example INSTRUMENTS inc"
    mapped = _write_json(tmp_path, "sec-mapping.json", envelopes)
    results.append(load_fixture(gate_db, mapped, archive_dir=archive_dir))
    assert gate_db.execute(
        """
        SELECT result.full_name FROM core.people result
        JOIN core.person_identifiers linkedin ON linkedin.person_id = result.id
        JOIN core.person_identifiers sec ON sec.person_id = result.id
        WHERE linkedin.scheme = 'linkedin' AND linkedin.value = %s AND sec.scheme = 'sec_cik' AND sec.value = '0002345678'
        """,
        (MARA,),
    ).fetchone() == ("Alice Éxample",)
    assert gate_db.execute(
        """
        SELECT i.value FROM core.positions p JOIN core.raw_records r ON p.raw_record_id = r.id
        JOIN core.organization_identifiers i ON i.organization_id = p.organization_id
        WHERE r.source_code = 'linkedin' AND i.scheme = 'sec_cik'
        """
    ).fetchone() == ("0001234567",)
    before = core_snapshot(gate_db)
    repeated = load_fixture(gate_db, shutil.copyfile(mapped, tmp_path / "copied.json"), archive_dir=archive_dir)
    assert repeated["status"] == "skipped" and repeated["records_new"] == 0
    assert core_snapshot(gate_db) == before

    for result in results:
        archived_path(result["artifact_uri"]).unlink()
    derived = sorted(before.keys() - INGESTION_TABLES)
    gate_db.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(
        sql.Identifier("core", name) for name in derived
    )))
    rebuild_from_raw(gate_db)
    assert core_snapshot(gate_db) == before


def test_malformed_envelope_rolls_back_all_core_tables(gate_db, tmp_path):
    valid, malformed, *_ = _envelopes()
    malformed = deepcopy(malformed)
    malformed.pop("raw_payload")
    path = _write_json(tmp_path, "malformed.json", [valid, malformed])
    before = core_snapshot(gate_db)
    with pytest.raises(LoadError) as caught:
        load_fixture(gate_db, path, archive_dir=tmp_path / "archive")
    error = caught.value.as_dict()
    assert set(error) == {"source_code", "artifact_uri", "record_index", "profile_id", "reason"}
    assert error["source_code"] == "linkedin"
    assert error["record_index"] == 2
    assert error["profile_id"] == "fictional-imani-quill"
    assert "raw_payload" in error["reason"]
    assert core_snapshot(gate_db) == before


def test_reservation_respects_ttl_refresh_and_token_ownership(gate_db, tmp_path):
    load_fixture(gate_db, GATE, archive_dir=tmp_path / "archive")
    now = datetime(2025, 4, 10, tzinfo=UTC)

    fresh = reserve_lookup(gate_db, "coresignal", f"linkedin.com/in/{MARA.upper()}", now=now)
    assert fresh.status == "reuse" and fresh.raw_record_id is not None
    forced = reserve_lookup(gate_db, "coresignal", MARA, refresh=True, now=now)
    assert forced.status == "reserved" and forced.token is not None
    assert reserve_lookup(gate_db, "coresignal", MARA, refresh=True, now=now).status == "in_flight"
    assert not release_lookup(gate_db, "coresignal", MARA, uuid4())
    assert release_lookup(gate_db, "coresignal", MARA, forced.token)
    assert reserve_lookup(gate_db, "coresignal", MARA, now=now + timedelta(days=100)).status == "reserved"
    fresh_time = datetime(2025, 4, 6, 9, tzinfo=UTC)
    receipt = reserve_lookup(gate_db, "brightdata", "fictional-pending", refresh=True, now=fresh_time)
    assert receipt.status == "reuse" and receipt.raw_record_id is not None
    expired = reserve_lookup(gate_db, "brightdata", "fictional-pending", refresh=True, now=now)
    assert expired.status == "reserved" and expired.raw_record_id is None


def test_concurrent_reservations_select_one_requester(gate_db):
    now = datetime(2025, 4, 10, tzinfo=UTC)
    dsn = gate_db.info.dsn

    def reserve():
        with psycopg.connect(dsn, autocommit=True) as connection:
            return reserve_lookup(connection, "pdl", "fictional-new-target", now=now).status

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: reserve(), range(2))) == ["in_flight", "reserved"]


def test_audit_reports_sample_and_unmeasured_precision(gate_db, tmp_path):
    load_fixture(gate_db, GATE, archive_dir=tmp_path / "archive")
    report = build_report(gate_db, sample_size=2, rng=Random(10))
    assert report["sample_denominator"] == len(report["sample"]) == 2
    assert report["accepted_matches_count"] == 3
    assert report["withheld_candidates_count"] == 1
    assert report["ineligible_envelopes_count"] == 4
    assert report["reviewed_count"] is report["verified_true_matches"] is report["manual_precision"] is None
    assert report["precision_formula"] == "verified_true_matches / reviewed_count"
    assert all(entry["verified_true_match"] is None for entry in report["sample"])

    output = tmp_path / "precision.json"
    completed = subprocess.run(
        [sys.executable, "-m", "mim.audit.linkedin_sample", "--dsn", gate_db.info.dsn,
         "--sample-size", "50", "--output", str(output)],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["sample_denominator"] == len(written["sample"]) == 3
    assert written["manual_precision"] is None


def test_unified_cli_load_rebuild_and_reservation(gate_db, tmp_path):
    def run(*args):
        completed = subprocess.run(
            [sys.executable, "-m", "mim", *args, "--dsn", gate_db.info.dsn],
            capture_output=True, text=True, timeout=20,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    options = ("--fixture", str(GATE), "--archive-dir", str(tmp_path / "archive"))
    assert run("load", "linkedin", *options)["accepted_matches"] == 3
    assert run("load", "linkedin", *options)["status"] == "skipped"
    assert run("rebuild")["status"] == "rebuilt"
    reserved = run("reserve-linkedin", "pdl", "fictional-cli-reservation", "--refresh")
    assert reserved["status"] == "reserved"
    assert run("release-linkedin", "pdl", "fictional-cli-reservation", reserved["token"]) == {"released": True}
