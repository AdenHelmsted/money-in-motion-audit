#GOAL Verify the auditor package and isolate its database from operator settings.

import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


FIXTURE = Path(__file__).parent / "fixtures/sec_form345/gate"


def _pilot(fixture, output):
    env = {**os.environ, "MIM_DATABASE_DSN": "invalid", "PGHOSTADDR": "192.0.2.10", "PGPORT": "1"}
    return subprocess.run([sys.executable, "-m", "mim", "pilot-sec", "--fixture", str(fixture), "--output", str(output)],
                          capture_output=True, text=True, timeout=45, env=env)


def test_sec_pilot_creates_a_portable_review_package(tmp_path):
    output = tmp_path / "audit"
    response = _pilot(FIXTURE, output)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["manual_review"] == "pending"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "machine_checks_passed"
    assert summary["replay_status"] == "skipped"
    assert summary["replay_differences"] == summary["rebuild_differences"] == []
    assert summary["tables"]["fetches"]["rows"] == 1
    assert summary["tables"]["raw_records"]["rows"] == summary["records_new"] == 19
    assert all(count == 0 for count in summary["integrity_violations"].values())
    with (output / summary["artifact"]).open("rb") as archive:
        assert hashlib.file_digest(archive, "sha256").hexdigest() == summary["artifact_sha256"]
    samples = json.loads((output / "evidence.json").read_text())
    with (output / "review.csv").open(newline="") as worksheet:
        rows = list(csv.DictReader(worksheet))
    assert len(rows) == len(samples) == summary["sample_count"] == 7
    assert {row["signal_id"] for row in rows} == {sample["signal_id"] for sample in samples}
    assert all(row["review_status"] == "pending" and not row["filing_url_checked"] for row in rows)
    assert any(len(sample["evidence"]) == 2 for sample in samples)
    assert _pilot(FIXTURE, output).returncode == 1
    assert (output / "summary.json").is_file()


def test_sec_pilot_preserves_failed_stage_and_loader_error(tmp_path):
    fixture = tmp_path / "empty"
    fixture.mkdir()
    output = tmp_path / "audit"
    response = _pilot(fixture, output)
    assert response.returncode == 1 and not response.stdout
    error = json.loads(response.stderr)
    assert error["table_name"] == "SUBMISSION"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "failed" and summary["stage"] == "load"
    assert summary["error"] == error
    assert not (output / "review.csv").exists()
