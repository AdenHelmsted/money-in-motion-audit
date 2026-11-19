#GOAL Prove replay and rebuild for one SEC insider artifact and prepare filing evidence for review.

import csv
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from time import monotonic
from urllib.parse import unquote, urlsplit

from psycopg import sql

from ..local_postgres import local_database
from ..loaders import sec_form345
from ..loaders.sec_form345_cli import _download, _live_options
from ..loaders.sec_ingestion import LoadError
from .database import integrity_violations


_ROOT = Path(__file__).resolve().parents[3]
_KINDS = ("insider_sale", "insider_option_exercise")
_SAMPLE_PER_KIND = 5


def _write_json(path, report):
    path.write_text(json.dumps(report, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")


def _output_directory(output):
    if output is not None:
        directory = output.absolute()
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        return directory
    default = _ROOT / ".local/sec-pilot"
    default.mkdir(parents=True, mode=0o700, exist_ok=True)
    return Path(mkdtemp(prefix=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-"), dir=default))


def _snapshot(conn):
    snapshot = {}
    names = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'core' ORDER BY tablename")
    for (name,) in names:
        table = sql.Identifier("core", name)
        count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()[0]
        digest = hashlib.sha256()
        query = sql.SQL('COPY (SELECT to_jsonb(t)::text COLLATE "C" AS value FROM {} t ORDER BY value) TO STDOUT')
        with conn.cursor() as cursor, cursor.copy(query.format(table)) as stream:
            for block in stream:
                digest.update(block)
        snapshot[name] = {"rows": count, "sha256": digest.hexdigest()}
    return snapshot


def _integrity(conn):
    violations = integrity_violations(conn)
    violations["inconsistent_trade_evidence"] = conn.execute("""
        SELECT count(*) FROM core.signal_evidence e JOIN core.signals s ON s.id = e.signal_id
        JOIN core.raw_records r ON r.id = e.raw_record_id
        LEFT JOIN core.equity_transactions t ON t.raw_record_id = r.id
        WHERE r.source_code <> 'sec_form345' OR t.id IS NULL
            OR t.person_id IS DISTINCT FROM s.person_id OR t.issuer_id IS DISTINCT FROM s.organization_id
        """).fetchone()[0]
    return violations


def _coverage(conn):
    signals = dict(conn.execute("SELECT kind, count(*) FROM core.signals GROUP BY kind"))
    joint, amended, unlinked = conn.execute("""
        SELECT count(DISTINCT split_part(external_id, ':', 1)) FILTER (
                   WHERE jsonb_array_length(payload -> 'REPORTINGOWNER') > 1),
               count(DISTINCT split_part(external_id, ':', 1)) FILTER (
                   WHERE payload -> 'SUBMISSION' ->> 'DOCUMENT_TYPE' LIKE '%/A'),
               count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM core.record_links l WHERE l.raw_record_id = raw.id))
        FROM core.raw_records raw WHERE source_code = 'sec_form345'
        """).fetchone()
    return {"signals_by_kind": signals, "joint_filing_accessions": joint, "amendment_accessions": amended,
            "raw_rows_without_entity_link": unlinked}


def _review_sample(conn, digest):
    return conn.execute("""
        WITH ranked AS (
            SELECT s.*, row_number() OVER (PARTITION BY kind ORDER BY md5(id::text || %s), id) AS rank
            FROM core.signals s WHERE kind = ANY(%s)
        )
        SELECT jsonb_build_object(
                   'signal_id', s.id, 'kind', s.kind, 'occurred_on', s.occurred_on,
                   'person', p.full_name, 'issuer', o.name, 'amount_cents', s.amount_cents,
                   'detector_version', s.detector_version, 'summary', s.summary,
                   'evidence', jsonb_agg(jsonb_build_object(
                       'raw_record_id', r.id, 'external_id', r.external_id, 'payload_sha256', r.payload_sha256,
                       'observed_on', r.observed_on, 'payload', r.payload,
                       'typed_trade', jsonb_build_object('code', t.code, 'transaction_on', t.transaction_on,
                           'shares', t.shares::text, 'price_per_share_cents', t.price_per_share_cents,
                           'under_10b5_1_plan', t.under_10b5_1_plan)) ORDER BY r.id))
        FROM ranked s JOIN core.people p ON p.id = s.person_id
        JOIN core.organizations o ON o.id = s.organization_id
        JOIN core.signal_evidence e ON e.signal_id = s.id JOIN core.raw_records r ON r.id = e.raw_record_id
        LEFT JOIN core.equity_transactions t ON t.raw_record_id = r.id
        WHERE s.rank <= %s
        GROUP BY s.id, s.kind, s.rank, s.occurred_on, s.amount_cents, s.detector_version, s.summary, p.full_name, o.name
        ORDER BY s.kind, s.rank
        """, (digest, list(_KINDS), _SAMPLE_PER_KIND)).fetchall()


def _csv_cell(value):
    # Preserve exact source text in evidence.json. Spreadsheet cells are for display and manual review.
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value


def _write_review(directory, samples):
    _write_json(directory / "evidence.json", samples)
    with (directory / "review.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["signal_id", "kind", "occurred_on", "filing_date", "person", "owner_cik", "issuer", "issuer_cik", "amount_cents",
                         "accessions", "review_status", "reviewer", "filing_url_checked", "notes"])
        for sample in samples:
            first = sample["evidence"][0]["payload"]
            accessions = sorted({evidence["external_id"].split(":", 1)[0] for evidence in sample["evidence"]})
            owners = sorted({str(owner["RPTOWNERCIK"]).zfill(10) for owner in first["REPORTINGOWNER"]})
            values = (sample["signal_id"], sample["kind"], sample["occurred_on"], first["SUBMISSION"]["FILING_DATE"],
                      sample["person"], "; ".join(owners),
                      sample["issuer"], str(first["SUBMISSION"]["ISSUERCIK"]).zfill(10), sample["amount_cents"],
                      "; ".join(accessions), "pending", "", "", "")
            writer.writerow(map(_csv_cell, values))


def run_pilot(*, fixture=None, live=None, output=None):
    agent = _live_options(live) if live is not None else None
    directory = _output_directory(output)
    archive = directory / "archive"
    report = {"source_code": sec_form345.SOURCE_CODE, "input": live or str(fixture.absolute()),
              "started_at": datetime.now(UTC).isoformat(), "stage": "setup", "status": "running", "manual_review": "pending"}
    summary = directory / "summary.json"
    try:
        with local_database(_ROOT / "migrations", database="mim_insider_pilot") as conn:
            report["postgresql_version"] = conn.info.server_version
            if live is not None:
                report["stage"] = "download"
                with TemporaryDirectory(prefix="mim-sec-pilot-input-") as staging:
                    downloaded = Path(staging) / "quarterly.zip"
                    _download(live, agent, downloaded)
                    _audit(conn, downloaded, archive, directory, report)
            else:
                _audit(conn, fixture, archive, directory, report)
        report["stage"] = "complete"
        report["status"] = "machine_checks_passed"
    except Exception as exc:
        # Keep the failed stage and structured loader context in the same package as the archived evidence.
        report["status"] = "failed"
        report["error"] = exc.as_dict() if isinstance(exc, LoadError) else {"reason": str(exc)}
        _write_json(summary, report)
        if isinstance(exc, LoadError):
            raise
        raise ValueError(f"SEC pilot failed at {report['stage']}. See {summary}") from exc
    _write_json(summary, report)
    return {"status": report["status"], "manual_review": report["manual_review"], "output": str(directory),
            "sample_count": report["sample_count"]}


def _audit(conn, fixture, archive, directory, report):
    report["stage"] = "load"
    started = monotonic()
    loaded = sec_form345.load_fixture(conn, fixture, archive_dir=archive)
    artifact = Path(unquote(urlsplit(loaded["artifact_uri"]).path))
    report.update(artifact=str(artifact.relative_to(directory)), artifact_sha256=loaded["artifact_sha256"],
                  records_seen=loaded["records_seen"], records_new=loaded["records_new"],
                  load_seconds=round(monotonic() - started, 3))
    before = _snapshot(conn)
    report["tables"] = before
    report["coverage"] = _coverage(conn)
    report["integrity_violations"] = _integrity(conn)
    report["stage"] = "replay"
    replay = sec_form345.load_fixture(conn, artifact, archive_dir=archive)
    after_replay = _snapshot(conn)
    report["replay_status"] = replay["status"]
    report["replay_differences"] = [name for name in before if before[name] != after_replay.get(name)]

    report["stage"] = "rebuild"
    hidden = archive.with_name("archive-during-rebuild")
    archive.rename(hidden)
    try:
        started = monotonic()
        sec_form345.rebuild_from_raw(conn)
        report["rebuild_seconds"] = round(monotonic() - started, 3)
    finally:
        hidden.rename(archive)
    after_rebuild = _snapshot(conn)
    report["rebuild_differences"] = [name for name in before if before[name] != after_rebuild.get(name)]

    report["stage"] = "sample"
    samples = [sample for (sample,) in _review_sample(conn, loaded["artifact_sha256"])]
    report["sample_count"] = len(samples)
    _write_review(directory, samples)
    report["stage"] = "acceptance"
    if any(report["integrity_violations"].values()) or replay["status"] != "skipped" or report["replay_differences"] or report["rebuild_differences"]:
        raise ValueError("Evidence integrity, replay, or rebuild comparison failed")
    if {sample["kind"] for sample in samples} != set(_KINDS):
        raise ValueError("The pilot artifact must yield both a sale and an option exercise for review")
