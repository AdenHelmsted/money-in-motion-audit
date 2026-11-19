#GOAL Archive inputs, perform atomic replay-safe loads, and rebuild from raw evidence.

"""Content-addressed archives and atomic ingestion across source gates."""

from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import DecimalException
import hashlib
from itertools import batched
import json
from pathlib import Path
import shutil
import stat
from tempfile import NamedTemporaryFile
from zipfile import ZipFile, ZipInfo

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb


_EVIDENCE_TABLES = frozenset({"sources", "fetches", "raw_records"})
_MERGE_TABLES = ("people", "organizations")
_REPLAY_MERGES = ContextVar("mim_replay_merges", default={})
_INGESTION_LOCK = int.from_bytes(hashlib.sha256(b"money-in-motion:sec").digest()[:8], "big", signed=True)
RECORD_ERRORS = (psycopg.Error, DecimalException, KeyError, ValueError)


class LoadError(Exception):
    def __init__(self, *, source_code, artifact_uri, table_name=None, accession_number=None, row_number=None, reason):
        self.source_code = source_code
        self.artifact_uri = artifact_uri
        self.table_name = table_name
        self.accession_number = accession_number
        self.row_number = row_number
        self.reason = reason
        super().__init__(json.dumps(self.as_dict(), ensure_ascii=False))

    def as_dict(self):
        return {
            "source_code": self.source_code,
            "artifact_uri": self.artifact_uri,
            "table_name": self.table_name,
            "accession_number": self.accession_number,
            "row_number": self.row_number,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Artifact:
    uri: str
    sha256: str
    path: Path


@dataclass(frozen=True, slots=True)
class Record:
    table_name: str | None
    accession_number: str | None
    row_number: int | None
    external_id: str
    observed_on: date
    payload: dict


def read_json_envelopes(artifact, error):
    def reject_constant(value):
        raise ValueError(f"invalid JSON constant {value}")

    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON object key")
        return result

    def decode(text):
        return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)

    index = None
    try:
        if artifact.path.suffix.lower() == ".jsonl":
            with artifact.path.open("rb") as source:
                for index, line in enumerate(source, 1):
                    text = line.decode("utf-8-sig" if index == 1 else "utf-8")
                    if text.strip():
                        yield index, decode(text)
        else:
            index = 1
            document = decode(artifact.path.read_text(encoding="utf-8-sig"))
            yield from enumerate(document if isinstance(document, list) else [document], 1)
    except (ValueError, OSError) as exc:
        raise error(artifact_uri=artifact.uri, record_index=index, reason=str(exc)) from exc


def archive_artifact(path: Path, archive_dir: Path, *, error, file_suffixes=None) -> Artifact:
    uri = path.absolute().as_uri()
    suffix = path.suffix.lower()
    try:
        path = path.resolve()
        uri = path.as_uri()
        archive_dir = archive_dir.resolve()
        is_directory = file_suffixes is None and path.is_dir()
        if is_directory and archive_dir == path:
            raise ValueError("Choose an archive directory other than the input directory")
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_suffix = suffix if file_suffixes is not None else ".zip"
        with NamedTemporaryFile(dir=archive_dir, suffix=archive_suffix, delete=False) as temporary:
            staging = Path(temporary.name)
        try:
            if is_directory:
                _archive_directory(path, archive_dir, staging)
            elif path.is_file() and suffix in (file_suffixes or {".zip"}):
                shutil.copyfile(path, staging)
            else:
                formats = " or ".join(sorted(file_suffixes)) if file_suffixes else "directory or .zip"
                raise ValueError(f"Expected an existing {formats} input")

            with staging.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            destination = archive_dir / f"{digest}{archive_suffix}"
            uri = destination.as_uri()
            try:
                destination.hardlink_to(staging)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file():
                    raise ValueError("Existing archive is not a regular file")
                with destination.open("rb") as archived:
                    if hashlib.file_digest(archived, "sha256").hexdigest() != digest:
                        raise ValueError("Existing archive does not match its SHA-256 filename")
            return Artifact(uri, digest, destination)
        finally:
            staging.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise error(artifact_uri=uri, reason=str(exc)) from exc


def _archive_directory(path, archive_dir, staging):
    # Fixed metadata and uncompressed entries make the archive independent of file metadata and zlib.
    def raise_walk_error(error):
        raise error

    with ZipFile(staging, "w") as archive:
        for directory, subdirectories, files in path.walk(on_error=raise_walk_error):
            subdirectories[:] = sorted(name for name in subdirectories if directory / name != archive_dir)
            for name in sorted(files):
                file = directory / name
                relative = file.relative_to(path)
                if file.is_symlink() or not file.is_file():
                    raise ValueError(f"Expected a regular input file: {relative}")
                member = ZipInfo(relative.as_posix())
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | 0o644) << 16
                with file.open("rb") as source, archive.open(member, "w", force_zip64=True) as sink:
                    shutil.copyfileobj(source, sink)


def contextual_error(error, uri, exc, record=None):
    reason = exc.diag.message_primary if isinstance(exc, psycopg.Error) else str(exc)
    details = {"record": record} if record and record.table_name is None else {}
    return error(
        artifact_uri=uri, reason=reason or type(exc).__name__,
        table_name=record.table_name if record else None,
        accession_number=record.accession_number if record else None,
        row_number=record.row_number if record else None, **details,
    )


def check_connection(conn, error, uri=None):
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise error(artifact_uri=uri, reason="The loader requires an idle database connection")


def lock_source(conn, source_code, error, uri=None):
    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    # Sources share entities. Replay reconstructs their writes in raw ID order.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_INGESTION_LOCK,))
    if conn.execute("SELECT 1 FROM core.sources WHERE code = %s", (source_code,)).fetchone() is None:
        raise error(artifact_uri=uri, table_name="core.sources", reason=f"{source_code} is missing from core.sources")


def _resolve_active(conn, table, identity):
    seen = set()
    replay_edges = _REPLAY_MERGES.get().get(table, {})
    start = identity
    query = sql.SQL("SELECT merged_into_id FROM {} WHERE id = %s").format(sql.Identifier("core", table))
    while identity not in seen:
        seen.add(identity)
        row = conn.execute(query, (identity,)).fetchone()
        if row is None:
            # A survivor may be introduced by a later raw record. Replay can
            # only reference an entity after its source evidence has created it.
            if identity != start and replay_edges:
                return start
            raise ValueError(f"Unknown identity in core.{table}: {identity}")
        target = row[0] or replay_edges.get(identity)
        if target is None:
            return identity
        identity = target
    raise ValueError(f"Cycle in core.{table} merge chain at {identity}")


def resolve_active_person(conn, identity):
    return _resolve_active(conn, "people", identity)


def resolve_active_org(conn, identity):
    return _resolve_active(conn, "organizations", identity)


def _payload_digest(record):
    canonical = json.dumps(record.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert_raw(conn, source_code, record, fetch_id):
    return conn.execute(
        """
        INSERT INTO core.raw_records (source_code, external_id, payload, payload_sha256, observed_on, fetch_id)
        SELECT %(source)s, %(external)s, %(payload)s, %(sha)s, %(observed)s, %(fetch)s
        WHERE NOT EXISTS (
            SELECT 1 FROM core.raw_records
            WHERE source_code = %(source)s AND external_id = %(external)s AND payload_sha256 = %(sha)s
        )
        RETURNING id
        """,
        {"source": source_code, "external": record.external_id, "payload": Jsonb(record.payload),
         "sha": _payload_digest(record), "observed": record.observed_on, "fetch": fetch_id},
    ).fetchone()


def _insert_raw_batch(conn, source_code, records, fetch_id):
    # COPY avoids a database round trip for every raw row. The staging table
    # is reused per chunk and commits with the entire artifact.
    with conn.cursor().copy("COPY pg_temp.mim_raw_batch FROM STDIN") as writer:
        for ordinal, record in enumerate(records):
            writer.write_row((ordinal, record.external_id, Jsonb(record.payload), _payload_digest(record), record.observed_on))
    inserted = conn.execute("""
        WITH first_versions AS (
            SELECT DISTINCT ON (external_id, payload_sha256) * FROM pg_temp.mim_raw_batch
            ORDER BY external_id, payload_sha256, ordinal
        ), inserted AS (
            INSERT INTO core.raw_records (source_code, external_id, payload, payload_sha256, observed_on, fetch_id)
            SELECT %s, external_id, payload, payload_sha256, observed_on, %s
            FROM first_versions ORDER BY ordinal
            ON CONFLICT (source_code, external_id, payload_sha256) DO NOTHING
            RETURNING id, external_id, payload_sha256
        )
        SELECT f.ordinal, i.id FROM inserted i JOIN first_versions f USING (external_id, payload_sha256)
        ORDER BY f.ordinal
        """, (source_code, fetch_id)).fetchall()
    conn.execute("DELETE FROM pg_temp.mim_raw_batch")
    return inserted


def stored_rows(conn, sources, *, latest=False):
    if latest:
        query = """
            SELECT r.source_code, r.id, r.external_id, r.payload, r.observed_on, f.artifact_uri
            FROM (
                SELECT DISTINCT ON (source_code, external_id)
                    source_code, id, external_id, payload, observed_on, fetch_id
                FROM core.raw_records WHERE source_code = ANY(%s)
                ORDER BY source_code, external_id, id DESC
            ) r JOIN core.fetches f ON f.id = r.fetch_id
            ORDER BY r.source_code, split_part(r.external_id, ':', 1), r.id
        """
    else:
        query = """
            SELECT r.source_code, r.id, r.external_id, r.payload, r.observed_on, f.artifact_uri
            FROM core.raw_records r JOIN core.fetches f ON f.id = r.fetch_id
            WHERE r.source_code = ANY(%s) ORDER BY r.id
        """
    with conn.cursor(name="mim_raw_records") as cursor:
        cursor.execute(query, (list(sources),))
        yield from cursor


def load_artifact(conn, path, *, archive_dir, source_code, error, read_records, project, sync_signals,
                  file_suffixes=None, classify=None, bulk=False):
    path = Path(path)
    uri = path.absolute().as_uri()
    check_connection(conn, error, uri)
    record = None
    try:
        with conn.transaction():
            lock_source(conn, source_code, error, uri)
            artifact = archive_artifact(path, Path(archive_dir), error=error, file_suffixes=file_suffixes)
            uri = artifact.uri
            values = (source_code, uri, artifact.sha256)
            result = {"source_code": source_code, "artifact_uri": uri, "artifact_sha256": artifact.sha256}
            counts = None if classify is None else Counter()
            if conn.execute(
                "SELECT 1 FROM core.fetches WHERE source_code = %s AND artifact_uri = %s "
                "AND artifact_sha256 = %s AND status = 'succeeded'", values,
            ).fetchone() is not None:
                stats = {} if counts is None else _eligibility_stats(counts)
                return {**result, "status": "skipped", "records_seen": 0, "records_new": 0, **stats}

            fetch_id = conn.execute(
                "INSERT INTO core.fetches (source_code, artifact_uri, artifact_sha256) VALUES (%s, %s, %s) RETURNING id",
                values,
            ).fetchone()[0]
            projection = project(conn) if project else None
            provenance = {}
            seen = added = 0
            records = read_records(artifact)
            if bulk:
                conn.execute("""
                    CREATE TEMP TABLE mim_raw_batch (
                        ordinal integer, external_id text, payload jsonb, payload_sha256 text, observed_on date
                    ) ON COMMIT DROP
                    """)
                for batch in batched(records, 1000):
                    seen += len(batch)
                    record = batch[0]
                    inserted_rows = _insert_raw_batch(conn, source_code, batch, fetch_id)
                    for ordinal, raw_id in inserted_rows:
                        record = batch[ordinal]
                        if projection is not None:
                            projection.project(record, raw_id)
                    added += len(inserted_rows)
            else:
                for record in records:
                    seen += 1
                    if counts is not None:
                        counts[classify(record.payload)] += 1
                    inserted = _insert_raw(conn, source_code, record, fetch_id)
                    if inserted is not None:
                        raw_id = inserted[0]
                        if projection is not None:
                            projection.project(record, raw_id)
                            if sync_signals is not None:
                                provenance[raw_id] = (uri, record.row_number)
                        added += 1

            record = None
            if added and sync_signals is not None:
                sync_signals(conn, provenance)
            conn.execute(
                "UPDATE core.fetches SET status = 'succeeded', finished_at = clock_timestamp(), "
                "records_seen = %s, records_new = %s WHERE id = %s", (seen, added, fetch_id),
            )
            stats = {} if counts is None else _eligibility_stats(counts)
            return {**result, "status": "succeeded", "records_seen": seen, "records_new": added, **stats}
    except RECORD_ERRORS as exc:
        raise contextual_error(error, uri, exc, record) from exc


def _eligibility_stats(counts):
    return {
        "accepted_matches": counts["accepted"],
        "withheld_candidates": counts["withheld_candidate"],
        "withheld_records": counts.total() - counts["accepted"],
        "eligibility_counts": dict(counts),
    }


def existing_signals(conn, detector):
    return {
        signal_id: tuple(values) for signal_id, *values in conn.execute(
            """
            SELECT s.id, s.person_id, s.organization_id, s.kind, s.occurred_on, s.amount_cents, s.summary,
                   s.confidence, s.detected_at, array_agg(e.raw_record_id ORDER BY e.raw_record_id)
            FROM core.signals s LEFT JOIN core.signal_evidence e ON e.signal_id = s.id
            WHERE s.detector_version = %s GROUP BY s.id
            """,
            (detector,),
        )
    }


def save_signal(conn, detector, existing, signal_id, values, raw_ids):
    evidence = sorted(set(raw_ids))
    if existing.get(signal_id) == (*values, evidence):
        return
    conn.execute(
        """
        INSERT INTO core.signals
            (id, person_id, organization_id, kind, occurred_on, amount_cents, summary, confidence,
             detected_at, detector_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            person_id = EXCLUDED.person_id, organization_id = EXCLUDED.organization_id, kind = EXCLUDED.kind,
            occurred_on = EXCLUDED.occurred_on, amount_cents = EXCLUDED.amount_cents,
            summary = EXCLUDED.summary, confidence = EXCLUDED.confidence, detected_at = EXCLUDED.detected_at
        """,
        (signal_id, *values, detector),
    )
    if signal_id in existing:
        conn.execute("DELETE FROM core.signal_evidence WHERE signal_id = %s", (signal_id,))
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO core.signal_evidence (signal_id, raw_record_id) VALUES (%s, %s)",
            [(signal_id, raw_id) for raw_id in evidence],
        )


def remove_stale_signals(conn, existing, desired_ids):
    stale = list(existing.keys() - desired_ids)
    if stale:
        conn.execute("DELETE FROM core.signals WHERE id = ANY(%s)", (stale,))


def _clear_projections(conn):
    tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'core' ORDER BY tablename")
    derived = [sql.Identifier("core", name) for (name,) in tables if name not in _EVIDENCE_TABLES]
    conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.SQL(", ").join(derived)))
    merges = {}
    for name in _MERGE_TABLES:
        table = sql.Identifier("core", name)
        merges[name] = conn.execute(sql.SQL("SELECT id, merged_into_id FROM {} WHERE merged_into_id IS NOT NULL")
                                    .format(table)).fetchall()
    conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(derived)))
    return merges


def _restore_merges(conn, merges):
    for name, edges in merges.items():
        table = sql.Identifier("core", name)
        query = sql.SQL("UPDATE {} SET merged_into_id = %s WHERE id = %s").format(table)
        for identity, survivor in edges:
            if conn.execute(query, (survivor, identity)).rowcount != 1:
                raise ValueError(f"Rebuild cannot recover merged identity {identity} in core.{name}")
        for identity, _ in edges:
            _resolve_active(conn, name, identity)


def rebuild(conn, *, source_code, error):
    """Replay recorded evidence in arrival order before reconciling signals."""
    from . import cms_open_payments, contact_vendor, dol_form5500, linkedin, nppes, sec_form345, sec_form_d

    check_connection(conn, error)
    loaders = {
        sec_form345.SOURCE_CODE: (sec_form345, sec_form345.LoadError),
        sec_form_d.SOURCE_CODE: (sec_form_d, sec_form_d._error),
        linkedin.SOURCE_CODE: (linkedin, linkedin.LoadError),
        contact_vendor.SOURCE_CODE: (contact_vendor, contact_vendor.LoadError),
        nppes.SOURCE_CODE: (nppes, nppes.LoadError),
        cms_open_payments.SOURCE_CODE: (cms_open_payments, cms_open_payments.LoadError),
        dol_form5500.SOURCE_CODE: (dol_form5500, dol_form5500.LoadError),
    }
    record = uri = None
    record_error = error
    try:
        with conn.transaction():
            lock_source(conn, source_code, error)
            merges = _clear_projections(conn)
            projections = {source: factory(conn) for source, (module, _) in loaders.items()
                           if (factory := getattr(module, "Projection", None)) is not None}
            seen = 0
            token = _REPLAY_MERGES.set({name: dict(edges) for name, edges in merges.items()})
            try:
                for source, raw_id, external_id, payload, observed_on, uri in stored_rows(conn, loaders):
                    module, record_error = loaders[source]
                    record = None
                    if source in projections:
                        record = module._record(external_id, observed_on, payload)
                        projections[source].project(record, raw_id)
                    seen += 1
            finally:
                _REPLAY_MERGES.reset(token)

            record = uri = None
            record_error = error
            _restore_merges(conn, merges)
            signals = 0
            for module, record_error in loaders.values():
                if reconcile := getattr(module, "sync_signals", None):
                    signals += reconcile(conn, {})
            record_error = cms_open_payments.LoadError
            cms_open_payments.link_payments(conn)
            return {"source_code": source_code, "status": "rebuilt", "records_seen": seen, "signals": signals}
    except RECORD_ERRORS as exc:
        raise contextual_error(record_error, uri, exc, record) from exc
