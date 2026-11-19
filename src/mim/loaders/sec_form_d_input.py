#GOAL Parse Form D filings and retain the source columns used as evidence.

"""Read SEC Form D bulk tables while preserving their original field values."""

from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from lzma import LZMAError
from pathlib import PurePosixPath
import re
from typing import Iterator
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

from .sec_form345_input import parse_date as _parse_long_date
from .sec_ingestion import Artifact, Record, LoadError


SOURCE_CODE = "sec_form_d"

_REQUIRED_TABLES = ("FORMDSUBMISSION", "ISSUERS", "OFFERING", "RELATEDPERSONS")
_OPTIONAL_TABLES = ("RECIPIENTS", "SIGNATURES")
_SEQUENCE_FIELDS = {
    "ISSUERS": "ISSUER_SEQ_KEY",
    "RELATEDPERSONS": "RELATEDPERSON_SEQ_KEY",
    "RECIPIENTS": "RECIPIENT_SEQ_KEY",
    "SIGNATURES": "SIGNATURE_SEQ_KEY",
}
_REQUIRED_HEADERS = {
    "FORMDSUBMISSION": {"FILING_DATE"},
    "ISSUERS": {"ISSUER_SEQ_KEY", "CITY", "STATEORCOUNTRY"},
    "OFFERING": {
        "INDUSTRYGROUPTYPE", "ISAMENDMENT", "SALE_DATE", "YETTOOCCUR", "TOTALOFFERINGAMOUNT", "TOTALAMOUNTSOLD",
    },
    "RELATEDPERSONS": {"RELATEDPERSON_SEQ_KEY", "FIRSTNAME", "LASTNAME", "CITY", "STATEORCOUNTRY"},
    "RECIPIENTS": {"RECIPIENT_SEQ_KEY"},
    "SIGNATURES": {"SIGNATURE_SEQ_KEY"},
}
_ISSUER_CIK_FIELDS = ("CIK", "ISSUERCIK")
_ISSUER_NAME_FIELDS = ("ENTITYNAME", "ISSUERNAME")
_ROLE_FIELDS = ("RELATIONSHIP_1", "RELATIONSHIP_2", "RELATIONSHIP_3", "RELATIONSHIP")
_ROLES = ("Executive Officer", "Director", "Promoter")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_CIK = re.compile(r"[0-9]{1,10}")
_SEQUENCE = re.compile(r"[0-9]{1,38}")
_AMOUNT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_SHORT_DATE = re.compile(r"([0-9]{2}-[A-Za-z]{3}-)([0-9]{2})")
_ARCHIVE_ERRORS = (BadZipFile, EOFError, LZMAError, NotImplementedError, OSError, RuntimeError, ZlibError)


@dataclass(frozen=True, slots=True)
class _Row:
    artifact_uri: str
    table_name: str
    row_number: int
    fields: dict[str, str]

    @property
    def accession_number(self):
        return self.fields["ACCESSIONNUMBER"]

    def error(self, reason):
        return LoadError(
            source_code=SOURCE_CODE, artifact_uri=self.artifact_uri, table_name=self.table_name,
            accession_number=self.accession_number or None, row_number=self.row_number, reason=reason,
        )


def parse_date(value: str) -> date:
    """Read a Form D filing date or ISO sale date independently of locale."""
    value = value.strip()
    if match := _SHORT_DATE.fullmatch(value):
        prefix, year = match.groups()
        value = f"{prefix}20{year}"
    return _parse_long_date(value)


def parse_boolean(value: str) -> bool | None:
    value = value.strip().casefold()
    if not value:
        return None
    if value in {"1", "true", "y", "yes"}:
        return True
    if value in {"0", "false", "n", "no"}:
        return False
    raise ValueError("expected a boolean flag")


def parse_amount(value: str) -> Decimal | None:
    value = value.strip()
    if not value or value.casefold() == "indefinite":
        return None
    if not _AMOUNT.fullmatch(value):
        raise ValueError("expected a decimal amount, Indefinite, or an empty field")
    return Decimal(value)


def parse_accession(value: str) -> str:
    value = value.strip()
    if _ACCESSION.fullmatch(value):
        return value
    if re.fullmatch(r"[0-9]{18}", value):
        return f"{value[:10]}-{value[10:12]}-{value[12:]}"
    raise ValueError("expected the SEC accession format (10-2-6 digits)")


def issuer_cik(row: dict[str, str]) -> str:
    value = next((row[key].strip() for key in _ISSUER_CIK_FIELDS if row.get(key, "").strip()), "")
    if not _CIK.fullmatch(value):
        raise ValueError("CIK must have 1–10 digits")
    return value.zfill(10)


def issuer_name(row: dict[str, str]) -> str:
    value = next((row[key].strip() for key in _ISSUER_NAME_FIELDS if row.get(key, "").strip()), "")
    if not value:
        raise ValueError("ENTITYNAME must not be blank")
    return value


def person_roles(row: dict[str, str]) -> tuple[str, ...]:
    roles = {
        " ".join(part.casefold().split())
        for field in _ROLE_FIELDS
        for part in re.split(r"[,;/|&]|\band\b", row.get(field, ""), flags=re.IGNORECASE)
    }
    return tuple(role for role in _ROLES if role.casefold() in roles)


def read_records(artifact: Artifact) -> Iterator[Record]:
    try:
        with ZipFile(artifact.path) as archive:
            members = _members(archive, artifact)
            yield from _join(archive, members, artifact)
    except _ARCHIVE_ERRORS as exc:
        raise LoadError(source_code=SOURCE_CODE, artifact_uri=artifact.uri, reason=str(exc)) from exc


def _members(archive, artifact):
    members = {}
    for member in archive.infolist():
        if member.is_dir():
            continue
        name = PurePosixPath(member.filename)
        table = name.stem.upper()
        if table == "FORMSUBMISSION":
            table = "FORMDSUBMISSION"
        if name.suffix.lower() not in {".tsv", ".txt"} or table not in (*_REQUIRED_TABLES, *_OPTIONAL_TABLES):
            continue
        if table in members:
            raise LoadError(source_code=SOURCE_CODE, artifact_uri=artifact.uri, table_name=table,
                            reason="Duplicate Form D table in the archive")
        members[table] = member
    for table in _REQUIRED_TABLES:
        if table not in members:
            raise LoadError(source_code=SOURCE_CODE, artifact_uri=artifact.uri, table_name=table,
                            reason=f"Missing {table} TSV table")
    return members


def _validate_headers(table, headers):
    if "" in headers or any(name != name.strip() or "\x00" in name for name in headers):
        raise ValueError("Invalid TSV header")
    fields = set(headers)
    if len(fields) != len(headers):
        raise ValueError("Duplicate TSV headers")
    missing = (_REQUIRED_HEADERS[table] | {"ACCESSIONNUMBER"}) - fields
    if table == "ISSUERS":
        for options in (_ISSUER_CIK_FIELDS, _ISSUER_NAME_FIELDS):
            if not fields.intersection(options):
                missing.add(options[0])
    if table == "RELATEDPERSONS" and not fields.intersection(_ROLE_FIELDS):
        missing.add(_ROLE_FIELDS[0])
    if missing:
        raise ValueError(f"Missing required TSV headers: {', '.join(sorted(missing))}")


def _rows(archive, members, artifact, table):
    if table not in members:
        return
    accession = accession_index = None
    row_number = 1
    try:
        with archive.open(members[table]) as source:
            # Decode by physical line so encoding failures retain a TSV line number.
            def lines():
                nonlocal accession
                for number, line in enumerate(source, 1):
                    if number == row_number and accession_index == 0:
                        first = line.partition(b"\t")[0].decode("ascii", errors="replace").strip('"')
                        if _ACCESSION.fullmatch(first):
                            accession = first
                    yield line.decode("utf-8-sig" if number == 1 else "utf-8")

            reader = csv.reader(lines(), delimiter="\t", strict=True)
            headers = next(reader, [])
            _validate_headers(table, headers)
            accession_index = headers.index("ACCESSIONNUMBER")
            while True:
                row_number = reader.line_num + 1
                accession = None
                cells = next(reader, None)
                if cells is None:
                    return
                accession = cells[accession_index] if accession_index < len(cells) else None
                if len(cells) != len(headers):
                    raise ValueError(f"Expected {len(headers)} TSV fields, found {len(cells)}")
                if any("\x00" in cell for cell in cells):
                    raise ValueError("TSV row contains a NUL character")
                if not _ACCESSION.fullmatch(accession):
                    raise ValueError("ACCESSIONNUMBER must have the SEC accession format (10-2-6 digits)")
                yield _Row(artifact.uri, table, row_number, dict(zip(headers, cells, strict=True)))
    except (*_ARCHIVE_ERRORS, ValueError, csv.Error) as exc:
        raise LoadError(source_code=SOURCE_CODE, artifact_uri=artifact.uri, table_name=table,
                        accession_number=accession or None, row_number=row_number, reason=str(exc)) from exc


def _validate(row, field, parse, *, required=False):
    value = row.fields.get(field, "")
    if not value.strip() and not required:
        return None
    try:
        parsed = parse(value)
        if parsed is None and required:
            raise ValueError("must not be blank")
        return parsed
    except ValueError as exc:
        raise row.error(f"{field}: {exc}") from exc


def _single_rows(archive, members, artifact, table, submissions=None):
    rows = {}
    for row in _rows(archive, members, artifact, table):
        accession = row.accession_number
        if accession in rows:
            raise row.error("Duplicate accession number")
        if submissions is not None and accession not in submissions:
            raise row.error("No FORMDSUBMISSION row for this accession")
        rows[accession] = row
    return rows


def _group_rows(archive, members, artifact, table, submissions):
    rows = defaultdict(dict)
    sequence_field = _SEQUENCE_FIELDS[table]
    for row in _rows(archive, members, artifact, table):
        accession = row.accession_number
        if accession not in submissions:
            raise row.error("No FORMDSUBMISSION row for this accession")
        sequence = row.fields[sequence_field]
        if not _SEQUENCE.fullmatch(sequence):
            raise row.error(f"{sequence_field} must be an unsigned integer with up to 38 digits")
        sequence = int(sequence)
        if sequence in rows[accession]:
            raise row.error(f"Duplicate {sequence_field} for this accession")
        if table == "ISSUERS":
            try:
                issuer_cik(row.fields)
                issuer_name(row.fields)
            except ValueError as exc:
                raise row.error(str(exc)) from exc
            _validate(row, "IS_PRIMARYISSUER_FLAG", parse_boolean)
        rows[accession][sequence] = row
    return rows


def _join(archive, members, artifact):
    submissions = _single_rows(archive, members, artifact, "FORMDSUBMISSION")
    offerings = _single_rows(archive, members, artifact, "OFFERING", submissions)
    grouped = {
        table: _group_rows(archive, members, artifact, table, submissions)
        for table in _SEQUENCE_FIELDS
    }

    for accession, submission in sorted(submissions.items()):
        observed_on = _validate(submission, "FILING_DATE", parse_date, required=True)
        offering = offerings.get(accession)
        if offering is None:
            raise submission.error("No OFFERING row for this accession")
        issuers = grouped["ISSUERS"].get(accession)
        if not issuers:
            raise submission.error("No ISSUERS rows for this accession")
        _validate(offering, "ISAMENDMENT", parse_boolean, required=True)
        _validate(offering, "YETTOOCCUR", parse_boolean)
        _validate(offering, "SALE_DATE", parse_date)
        for field in ("TOTALOFFERINGAMOUNT", "TOTALAMOUNTSOLD"):
            _validate(offering, field, parse_amount)

        payload = {
            "FORMDSUBMISSION": submission.fields,
            "ISSUERS": [row.fields for _, row in sorted(issuers.items())],
            "OFFERING": offering.fields,
        }
        signatures = grouped["SIGNATURES"].get(accession)
        if signatures:
            payload["SIGNATURES"] = [row.fields for _, row in sorted(signatures.items())]

        for sequence, row in sorted(issuers.items()):
            yield Record("ISSUERS", accession, row.row_number, f"{accession}:OFFERING:{sequence}", observed_on, payload)
        for table, kind in (("RELATEDPERSONS", "RELATEDPERSON"), ("RECIPIENTS", "RECIPIENT")):
            for sequence, row in sorted(grouped[table].get(accession, {}).items()):
                yield Record(table, accession, row.row_number, f"{accession}:{kind}:{sequence}",
                             observed_on, {**payload, table: row.fields})
