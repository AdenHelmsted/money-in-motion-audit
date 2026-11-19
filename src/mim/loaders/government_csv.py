#GOAL Stream public CSV artifacts and retain original columns and row error context.

"""Stream public-data CSV members and keep their original columns as evidence."""

import csv
from datetime import date, datetime
from decimal import DecimalException
from functools import lru_cache
from io import TextIOWrapper
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from .sec_ingestion import Record


def key(value):
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


@lru_cache(maxsize=64)
def _column_keys(names):
    return tuple(key(name) for name in names)


class Columns:
    def __init__(self, values):
        self.values = dict(zip(_column_keys(tuple(values)), values.values(), strict=True))

    def get(self, *names):
        return next((self.values[name].strip() for name in names if self.values.get(name, "").strip()), "")

    def require(self, *names):
        if not (value := self.get(*names)):
            raise ValueError(f"{names[0]} is required")
        return value


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise ValueError("invalid source date") from exc


def date_from(columns, *fields):
    return parse_date(value) if (value := columns.get(*fields)) else None


def npi(value):
    if not re.fullmatch(r"[0-9]{10}", value):
        raise ValueError("NPI must contain exactly 10 digits")
    return value


def read_records(artifact, filename, error, convert, headers, *, member_priority=None):
    def read(stream, member):
        number, external_id = 1, None
        try:
            reader = csv.reader(stream, strict=True)
            names = next(reader, [])
            keys = set(_column_keys(tuple(names)))
            if "" in keys or len(keys) != len(names) or not headers(keys):
                raise ValueError("missing, duplicate, or unsupported CSV headers")
            while True:
                number, external_id = reader.line_num + 1, None
                values = next(reader, None)
                if values is None:
                    return
                if not values:
                    continue
                if len(values) != len(names) or any("\x00" in value for value in values):
                    raise ValueError("CSV row does not match its header or contains a NUL")
                row = dict(zip(names, values, strict=True))
                columns = Columns(row)
                external_id = columns.get("ack_id", "acknowledgement_id", "acknowledgment_id", "record_id", "payment_id", "npi") or None
                external_id, observed, payload = convert(row, member)
                yield Record(member, external_id, number, external_id, observed, payload)
        except (ValueError, DecimalException, csv.Error, OSError, BadZipFile) as exc:
            raise error(artifact_uri=artifact.uri, table_name=member, accession_number=external_id,
                        row_number=number, reason=str(exc)) from exc

    if artifact.path.suffix == ".csv":
        with artifact.path.open(encoding="utf-8-sig", newline="") as stream:
            yield from read(stream, filename)
        return
    try:
        with ZipFile(artifact.path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith(".csv")]
            if member_priority and members:
                priorities = [member_priority(Path(item.filename).name.lower()) for item in members]
                preferred = max(priorities)
                members = [item for item, rank in zip(members, priorities, strict=True) if preferred > 0 and rank == preferred]
            if not members:
                raise ValueError("ZIP has no supported data CSV members")
            for member in sorted(members, key=lambda item: item.filename):
                with archive.open(member) as raw, TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                    yield from read(stream, Path(member.filename).name)
    except (ValueError, BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        raise error(artifact_uri=artifact.uri, reason=str(exc)) from exc


def link(conn, raw_id, day, *, role, person=None, organization=None, method="exact_identifier", confidence=1):
    conn.execute(
        """
        INSERT INTO core.record_links
            (raw_record_id, person_id, organization_id, role, method, confidence, linked_at)
        SELECT %(raw)s, %(person)s::uuid, %(org)s::uuid, %(role)s, %(method)s, %(confidence)s,
               %(day)s::date::timestamp AT TIME ZONE 'UTC'
        WHERE NOT EXISTS (SELECT 1 FROM core.record_links WHERE raw_record_id = %(raw)s AND role = %(role)s
              AND person_id IS NOT DISTINCT FROM %(person)s::uuid AND organization_id IS NOT DISTINCT FROM %(org)s::uuid)
        """,
        {"raw": raw_id, "person": person, "org": organization, "role": role,
         "method": method, "confidence": confidence, "day": day},
    )
