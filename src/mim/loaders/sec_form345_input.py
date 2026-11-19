#GOAL Preserve SEC insider source rows and join their filing and footnote evidence.

"""Archive local SEC DERA batches and join filing evidence without altering TSV values."""

from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import chain
from lzma import LZMAError
from pathlib import Path, PurePosixPath
import re
from typing import Iterator
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

from . import sec_ingestion
from .sec_ingestion import Artifact, Record


SOURCE_CODE = "sec_form345"

_TRANSACTIONS = ("NONDERIV_TRANS", "DERIV_TRANS")
_HOLDINGS = ("NONDERIV_HOLDING", "DERIV_HOLDING")
_DETAIL_TABLES = _TRANSACTIONS + _HOLDINGS
_REQUIRED_TABLES = ("SUBMISSION", "REPORTINGOWNER", *_DETAIL_TABLES)
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_CIK = re.compile(r"[0-9]{1,10}")
_SURROGATE_KEY = re.compile(r"[0-9]{1,38}")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_MONTHS = {name: number for number, name in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), start=1
)}
_SUBMISSION_DATES = ("FILING_DATE", "PERIOD_OF_REPORT", "DATE_OF_ORIG_SUB")
_DETAIL_DATES = ("TRANS_DATE", "DEEMED_EXECUTION_DATE", "EXCERCISE_DATE", "EXERCISE_DATE", "EXPIRATION_DATE")
_DETAIL_NUMBERS = (
    "TRANS_SHARES", "TRANS_PRICEPERSHARE", "SHRS_OWND_FOLWNG_TRANS", "VALU_OWND_FOLWNG_TRANS",
    "CONV_EXERCISE_PRICE", "TRANS_TOTAL_VALUE", "UNDLYNG_SEC_SHARES", "TRANS_UNDLNG_SHARES",
    "UNDERLYING_SECURITY_SHARES", "UNDLYNG_SEC_VALUE",
)
_FLAG_VALUES = {"0": False, "1": True, "false": False, "true": True}
_PLAN_FIELDS = ("AFF_10B5_ONE", "AFF10B5ONE")
_DERIV_TITLES = ("UNDLYNG_SEC_TITLE", "UNDERLYING_SECURITY_TITLE")
_ARCHIVE_ERRORS = (BadZipFile, EOFError, LZMAError, NotImplementedError, OSError, RuntimeError, ZlibError)
_REQUIRED_HEADERS = {
    "SUBMISSION": (
        "ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME", "ISSUERTRADINGSYMBOL",
    ),
    "REPORTINGOWNER": ("ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP"),
    "FOOTNOTES": ("ACCESSION_NUMBER", "FOOTNOTE_ID", "FOOTNOTE_TXT"),
    **{
        table: (
            "ACCESSION_NUMBER", f"{table}_SK", "SECURITY_TITLE", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
            "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD",
        )
        for table in _TRANSACTIONS
    },
    **{
        table: ("ACCESSION_NUMBER", f"{table}_SK", "SECURITY_TITLE")
        for table in _HOLDINGS
    },
}


class LoadError(sec_ingestion.LoadError):
    def __init__(self, **details):
        super().__init__(source_code=SOURCE_CODE, **details)


@dataclass(frozen=True, slots=True)
class _Row:
    artifact_uri: str
    table_name: str
    row_number: int
    columns: dict[str, str]

    @property
    def accession_number(self):
        return self.columns["ACCESSION_NUMBER"]

    def error(self, reason):
        return LoadError(
            artifact_uri=self.artifact_uri, table_name=self.table_name, row_number=self.row_number,
            accession_number=self.accession_number or None, reason=reason,
        )


def archive_artifact(path: Path, archive_dir: Path) -> Artifact:
    return sec_ingestion.archive_artifact(path, archive_dir, error=LoadError)


def parse_date(value: str) -> date:
    """Read an SEC date, independent of locale, or an ISO date in local fixtures."""
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    match = re.fullmatch(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4})", value)
    if match is not None:
        day, month, year = match.groups()
        if month.upper() in _MONTHS:
            return date(int(year), _MONTHS[month.upper()], int(day))
    raise ValueError("expected DD-MON-YYYY or YYYY-MM-DD")


def read_records(artifact: Artifact) -> Iterator[Record]:
    try:
        with ZipFile(artifact.path) as archive:
            members = _table_members(archive, artifact)
            yield from _join_records(archive, members, artifact)
    except _ARCHIVE_ERRORS as exc:
        raise LoadError(artifact_uri=artifact.uri, reason=str(exc)) from exc


def _table_members(archive, artifact):
    members = {}
    expected = {*_REQUIRED_TABLES, "FOOTNOTES"}
    for member in archive.infolist():
        if member.is_dir():
            continue
        name = PurePosixPath(member.filename)
        table = name.stem.upper()
        if name.suffix.lower() != ".tsv" or table not in expected:
            continue
        if table in members:
            raise LoadError(artifact_uri=artifact.uri, table_name=table, reason="Duplicate TSV member")
        members[table] = member
    for table in _REQUIRED_TABLES:
        if table not in members:
            raise LoadError(artifact_uri=artifact.uri, table_name=table, reason=f"Missing {table}.tsv")
    return members


def _read_rows(archive, members, artifact, table):
    if table not in members:
        return
    row_number = 1
    accession = None
    accession_index = None
    try:
        with archive.open(members[table]) as source:
            # Decoding one physical line at a time gives meaningful line numbers
            # even for invalid UTF-8 or TSV records with quoted newlines.
            def lines():
                nonlocal accession
                for number, line in enumerate(source, 1):
                    if number == row_number and accession_index == 0:
                        first_column = line.partition(b"\t")[0].decode("ascii", errors="replace").strip('"')
                        if _ACCESSION.fullmatch(first_column):
                            accession = first_column
                    yield line.decode("utf-8-sig" if number == 1 else "utf-8")

            decoded = lines()
            header = next(decoded, "")
            headers = next(csv.reader([header], delimiter="\t", strict=True), [])
            missing = set(_REQUIRED_HEADERS[table]).difference(headers)
            if "" in headers or any(name != name.strip() or "\x00" in name for name in headers):
                raise LoadError(artifact_uri=artifact.uri, table_name=table, row_number=1, reason="Invalid TSV header")
            if len(headers) != len(set(headers)):
                raise LoadError(artifact_uri=artifact.uri, table_name=table, row_number=1, reason="Duplicate TSV headers")
            if table == "DERIV_TRANS" and not set(_DERIV_TITLES).intersection(headers):
                missing.add("UNDLYNG_SEC_TITLE")
            if missing:
                raise LoadError(
                    artifact_uri=artifact.uri, table_name=table, row_number=1,
                    reason=f"Missing required TSV headers: {', '.join(sorted(missing))}",
                )
            accession_index = headers.index("ACCESSION_NUMBER")
            next_row_number = 2
            while True:
                row_number = next_row_number
                accession = None
                line = next(decoded, None)
                if line is None:
                    return
                physical_cells = line.removesuffix("\n").removesuffix("\r").split("\t")
                reader = csv.reader([line], delimiter="\t", strict=True)
                try:
                    cells = next(reader)
                except csv.Error:
                    # Some SEC text fields begin with a literal, unescaped quote.
                    # A complete physical TSV row already provides unambiguous boundaries.
                    if len(physical_cells) == len(headers):
                        cells = physical_cells
                    else:
                        reader = csv.reader(chain([line], decoded), delimiter="\t", strict=True)
                        cells = next(reader)
                next_row_number += reader.line_num
                if len(cells) != len(headers):
                    accession = cells[accession_index] if accession_index < len(cells) else None
                    raise LoadError(
                        artifact_uri=artifact.uri, table_name=table, accession_number=accession or None,
                        row_number=row_number, reason=f"Expected {len(headers)} TSV fields, found {len(cells)}",
                    )
                row = _Row(artifact.uri, table, row_number, dict(zip(headers, cells, strict=True)))
                if any("\x00" in cell for cell in cells):
                    raise row.error("TSV row contains a NUL character")
                if not _ACCESSION.fullmatch(row.accession_number):
                    raise row.error("ACCESSION_NUMBER must have the SEC accession format (10-2-6 digits)")
                yield row
    except (*_ARCHIVE_ERRORS, UnicodeDecodeError, csv.Error) as exc:
        raise LoadError(
            artifact_uri=artifact.uri, table_name=table, accession_number=accession,
            row_number=row_number, reason=str(exc),
        ) from exc


def _validate_dates(row, fields, required=None):
    parsed = {}
    for field in fields:
        value = row.columns.get(field, "")
        if value or field == required:
            try:
                parsed[field] = parse_date(value)
            except ValueError as exc:
                raise row.error(f"{field} has an invalid date: {value!r}") from exc
    return parsed


def _require_text(row, *fields):
    for field in fields:
        if not row.columns[field].strip():
            raise row.error(f"{field} must not be blank")


def _validate_submission(row):
    values = row.columns
    _require_text(row, "ISSUERNAME")
    if not _CIK.fullmatch(values["ISSUERCIK"]):
        raise row.error("ISSUERCIK must have 1–10 digits")
    if values["DOCUMENT_TYPE"] not in {"3", "4", "5", "3/A", "4/A", "5/A"}:
        raise row.error("DOCUMENT_TYPE must identify a Form 3, 4, 5, or their amendments")
    provided = [values[field].lower() for field in _PLAN_FIELDS if values.get(field)]
    if any(value not in _FLAG_VALUES for value in provided):
        raise row.error("AFF_10B5_ONE must be blank, 0, 1, false, or true")
    if len({_FLAG_VALUES[value] for value in provided}) > 1:
        raise row.error("AFF_10B5_ONE and AFF10B5ONE disagree")
    return _validate_dates(row, _SUBMISSION_DATES, required="FILING_DATE")["FILING_DATE"]


def _validate_detail(row):
    values = row.columns
    key = values[f"{row.table_name}_SK"]
    if not _SURROGATE_KEY.fullmatch(key):
        raise row.error(f"{row.table_name}_SK must be an unsigned integer with up to 38 digits")
    _require_text(row, "SECURITY_TITLE")
    if row.table_name in _TRANSACTIONS:
        code = values["TRANS_CODE"]
        if code and not re.fullmatch(r"[A-Z]", code):
            raise row.error("TRANS_CODE must be blank or a single uppercase letter")
        if values["TRANS_ACQUIRED_DISP_CD"] not in {"", "A", "D"}:
            raise row.error("TRANS_ACQUIRED_DISP_CD must be blank, A, or D")
    _validate_dates(row, _DETAIL_DATES)
    for field in _DETAIL_NUMBERS:
        value = values.get(field, "")
        if value:
            try:
                if not _DECIMAL.fullmatch(value) or not Decimal(value).is_finite():
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise row.error(f"{field} has an invalid decimal: {value!r}") from exc
    return str(int(key))


def _join_records(archive, members, artifact):
    submissions = {}
    owners = defaultdict(dict)
    notes = defaultdict(dict)
    for row in _read_rows(archive, members, artifact, "SUBMISSION"):
        if row.accession_number in submissions:
            raise row.error("Duplicate submission accession number")
        submissions[row.accession_number] = (row, _validate_submission(row))

    for table, grouped, id_field in (
        ("REPORTINGOWNER", owners, "RPTOWNERCIK"), ("FOOTNOTES", notes, "FOOTNOTE_ID"),
    ):
        for row in _read_rows(archive, members, artifact, table):
            if row.accession_number not in submissions:
                raise row.error("No SUBMISSION row for this accession")
            _require_text(row, id_field)
            row_id = row.columns[id_field]
            if table == "REPORTINGOWNER":
                _require_text(row, "RPTOWNERNAME")
                if not _CIK.fullmatch(row_id):
                    raise row.error("RPTOWNERCIK must have 1–10 digits")
                row_id = row_id.zfill(10)
            # Keep the first published row for each normalized filing key.
            grouped[row.accession_number].setdefault(row_id, row)

    for accession, (submission, _) in submissions.items():
        if not owners.get(accession):
            raise submission.error("No REPORTINGOWNER row for this accession")

    with_details = set()
    for table in _DETAIL_TABLES:
        seen = set()
        for row in _read_rows(archive, members, artifact, table):
            accession = row.accession_number
            if accession not in submissions:
                raise row.error("No SUBMISSION row for this accession")
            key = _validate_detail(row)
            if (accession, key) in seen:
                raise row.error(f"Duplicate {table}_SK for this accession")
            seen.add((accession, key))
            with_details.add(accession)
            yield _record(submissions[accession], owners[accession], notes.get(accession, {}), row, key)

    for accession, (submission, observed_on) in submissions.items():
        if accession not in with_details:
            yield _record((submission, observed_on), owners[accession], notes.get(accession, {}))


def _record(submission_and_date, owners, notes, detail=None, key=None):
    submission, observed_on = submission_and_date
    owner_rows = [owners[owner] for owner in sorted(owners)]
    references = set()
    evidenced_rows = [submission, *owner_rows]
    if detail is not None:
        evidenced_rows.append(detail)
    for row in evidenced_rows:
        for field, value in row.columns.items():
            if field.endswith("_FN") and value:
                for note_id in re.split(r"[\s,;|]+", value.strip(" \t\r\n,;|")):
                    if note_id in notes:
                        references.add(note_id)

    accession = submission.accession_number
    payload = {
        "SUBMISSION": submission.columns,
        "REPORTINGOWNER": [row.columns for row in owner_rows],
        "FOOTNOTES": [notes[note_id].columns for note_id in sorted(references)],
    }
    if detail is None:
        return Record("SUBMISSION", accession, submission.row_number, f"{accession}:SUBMISSION", observed_on, payload)
    payload[detail.table_name] = detail.columns
    return Record(
        detail.table_name, accession, detail.row_number, f"{accession}:{detail.table_name}:{key}", observed_on, payload,
    )
