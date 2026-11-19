#GOAL Resolve providers by NPI and cite enumeration and practice-location evidence.

"""Load NPPES provider evidence and detect primary practice changes."""

from functools import partial
from itertools import groupby
from pathlib import Path
import re

from . import cms_open_payments
from .government_csv import Columns, date_from, link, npi, parse_date, read_records
from .sec_form345_projection import filing_timestamp, stable_id
from .sec_ingestion import (
    LoadError as IngestionError, Record, existing_signals, load_artifact, rebuild, remove_stale_signals, resolve_active_org, resolve_active_person, save_signal,
)


SOURCE_CODE = "nppes"
DETECTOR = "nppes_v1"
LoadError = partial(IngestionError, source_code=SOURCE_CODE)
_PRACTICE = "provider_business_practice_location_address_"
_ADDRESS_FIELDS = (
    ("provider_first_line_business_practice_location_address", "practice_address_1"),
    ("provider_second_line_business_practice_location_address", "practice_address_2"),
    (f"{_PRACTICE}city_name", "practice_city"),
    (f"{_PRACTICE}state_name", "practice_state"),
    (f"{_PRACTICE}postal_code", "practice_postal_code"),
    (f"{_PRACTICE}country_code_if_outside_u_s", "practice_country"),
)
_STREET_WORDS = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "BOULEVARD": "BLVD", "DRIVE": "DR", "SUITE": "STE"}


def _convert(row, member):
    columns = Columns(row)
    identifier = npi(columns.require("npi"))
    entity = columns.get("entity_type_code")
    ended = date_from(columns, "npi_deactivation_date")
    date_from(columns, "npi_reactivation_date")
    enumerated = date_from(columns, "provider_enumeration_date", "enumeration_date")
    source_date = date_from(columns, "observed_on", "snapshot_date")
    match = re.fullmatch(r"npidata_pfile_\d{8}-(\d{8})\.csv", member.lower())
    observed = source_date or (parse_date(match[1]) if match else None)
    updated = date_from(columns, "last_update_date")
    observed = observed or updated or ended or enumerated
    if observed is None:
        raise ValueError("NPPES row requires a release, update, deactivation, or enumeration date")
    if entity not in {"1", "2"} and not (not entity and ended):
        raise ValueError("Entity Type Code must be 1, 2, or blank on a deactivated row")
    if _active(columns, observed):
        if entity == "1":
            columns.require("provider_last_name_legal_name", "last_name")
        else:
            columns.require("provider_organization_name_legal_business_name", "organization_name")
    return identifier, observed, row


def _active(columns, observed):
    ended = date_from(columns, "npi_deactivation_date")
    resumed = date_from(columns, "npi_reactivation_date")
    return columns.get("entity_type_code") in {"1", "2"} and (
        not ended or ended > observed or resumed is not None and ended < resumed <= observed
    )


def _address(columns):
    street, unit, city, region, postal, country = (columns.get(*names) for names in _ADDRESS_FIELDS)
    country = country.upper() if re.fullmatch(r"[A-Za-z]{2}", country) else "US" if not country else "ZZ"
    region = region.upper()
    if country == "US" and re.fullmatch(r"[0-9]{5}(?:[ -]?[0-9]{4})?", postal):
        digits = re.sub(r"[^0-9]", "", postal)
        postal = digits[:5] + (f"-{digits[5:]}" if len(digits) == 9 else "")
    return street, unit, city, region, postal, country


def _practice_changed(before, after):
    def normalize(address):
        return [" ".join(_STREET_WORDS.get(word, word) for word in re.findall(r"[\w]+", part.upper()))
                for part in address]

    if not all((before[0], before[2], after[0], after[2])):
        return False
    left, right = normalize(before), normalize(after)
    if before[-1] == after[-1] == "US":
        left[4], right[4] = left[4][:5], right[4][:5]
    return any(a and b and a != b for a, b in zip(left, right, strict=True))


class Projection:
    def __init__(self, conn):
        self.conn = conn

    def project(self, record, raw_id):
        columns = Columns(record.payload)
        if not _active(columns, record.observed_on):
            return
        individual = columns.get("entity_type_code") == "1"
        entity = "person" if individual else "organization"
        id_table = f"core.{entity}_identifiers"
        id_column = f"{entity}_id"
        conn = self.conn
        row = conn.execute(f"SELECT {id_column} FROM {id_table} WHERE scheme = 'npi' AND value = %s",
                           (record.external_id,)).fetchone()
        street, unit, city, region, postal, country = _address(columns)
        timestamp = filing_timestamp(record.observed_on)
        if row is None:
            entity_id = stable_id(f"nppes_{entity}", record.external_id)
            if individual:
                given = columns.get("provider_first_name", "first_name")
                middle = columns.get("provider_middle_name", "middle_name")
                family = columns.require("provider_last_name_legal_name", "last_name")
                conn.execute("INSERT INTO core.people (id, full_name, given_name, family_name, created_at) VALUES (%s, %s, %s, %s, %s)",
                             (entity_id, " ".join(filter(None, (given, middle, family))), given or None, family, timestamp))
            else:
                name = columns.require("provider_organization_name_legal_business_name", "organization_name")
                conn.execute("INSERT INTO core.organizations (id, name, kind, locality, region, created_at) "
                             "VALUES (%s, %s, 'medical_practice', %s, %s, %s)",
                             (entity_id, name, city or None, region or None, timestamp))
            conn.execute(f"INSERT INTO {id_table} ({id_column}, scheme, value, raw_record_id) VALUES (%s, 'npi', %s, %s)",
                         (entity_id, record.external_id, raw_id))
        else:
            resolve = resolve_active_person if individual else resolve_active_org
            entity_id = resolve(conn, row[0])

        if individual:
            link(conn, raw_id, record.observed_on, role="subject", person=entity_id)
            if any((street, unit, city, region, postal)):
                conn.execute("""
                    INSERT INTO core.locations (id, person_id, kind, locality, region, postal_code, country, observed_on, raw_record_id)
                    VALUES (%s, %s, 'work', %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
                    """, (stable_id("nppes_location", raw_id), entity_id, city or None, region or None, postal or None,
                          country, record.observed_on, raw_id))
        else:
            link(conn, raw_id, record.observed_on, role="subject", organization=entity_id)
            conn.execute("""
                UPDATE core.organizations SET locality = %s, region = %s
                WHERE id = %s AND %s = (SELECT r.id FROM core.raw_records r JOIN core.record_links l ON l.raw_record_id = r.id
                    WHERE r.source_code = %s AND r.external_id = %s AND l.organization_id = %s AND l.role = 'subject'
                    ORDER BY r.observed_on DESC, r.id DESC LIMIT 1)
                """, (city or None, region or None, entity_id, raw_id, SOURCE_CODE, record.external_id, entity_id))


def sync_signals(conn, provenance):
    existing = existing_signals(conn, DETECTOR)
    desired = set()
    with conn.cursor(name="mim_nppes_timeline") as cursor:
        cursor.execute("""
            SELECT r.external_id, r.id, r.observed_on, r.payload, l.person_id, p.merged_into_id
            FROM core.raw_records r JOIN core.person_identifiers i ON i.scheme = 'npi' AND i.value = r.external_id
            LEFT JOIN core.record_links l ON l.raw_record_id = r.id AND l.person_id IS NOT NULL AND l.role = 'subject'
            LEFT JOIN core.people p ON p.id = l.person_id
            WHERE r.source_code = %s ORDER BY r.external_id, r.observed_on, r.id
            """, (SOURCE_CODE,))
        for identifier, series in groupby(cursor, key=lambda row: row[0]):
            prior = None
            enumerations = set()
            for _, daily in groupby(series, key=lambda row: row[2]):
                _, raw_id, observed, payload, person, merged_into = list(daily)[-1]
                if person is None:
                    prior = None
                    continue
                if merged_into is not None:
                    person = resolve_active_person(conn, merged_into)
                columns = Columns(payload)
                address = _address(columns)
                enumerated = date_from(columns, "provider_enumeration_date", "enumeration_date")
                if enumerated and 0 <= (observed - enumerated).days <= 180 and enumerated not in enumerations:
                    signal = stable_id("nppes_enumeration", identifier, enumerated)
                    values = (person, None, "new_practice", enumerated, None,
                              "NPPES recorded a newly enumerated provider.", 1, filing_timestamp(observed))
                    save_signal(conn, DETECTOR, existing, signal, values, (raw_id,))
                    desired.add(signal)
                    enumerations.add(enumerated)
                if prior and _practice_changed(prior[1], address):
                    signal = stable_id("nppes_practice_change", identifier, prior[0], raw_id)
                    values = (person, None, "new_practice", observed, None,
                              "NPPES recorded a change in primary practice location.", 1, filing_timestamp(observed))
                    save_signal(conn, DETECTOR, existing, signal, values, (prior[0], raw_id))
                    desired.add(signal)
                prior = raw_id, address
    remove_stale_signals(conn, existing, desired)
    return len(desired)


def _after_load(conn, provenance):
    sync_signals(conn, provenance)
    cms_open_payments.link_payments(conn)


def _record(external_id, observed_on, payload, row_number=None):
    return Record("nppes", external_id, row_number, external_id, observed_on, payload)


def load_fixture(conn, path, *, archive_dir):
    def priority(name):
        if re.fullmatch(r"npidata_pfile_\d{8}-\d{8}\.csv", name):
            return 2
        return int(not name.startswith(("pl_pfile", "othername_pfile", "endpoint_pfile")) and "header" not in name)

    def records(artifact):
        return read_records(artifact, Path(path).name, LoadError, _convert,
                            lambda keys: {"npi", "entity_type_code"} <= keys, member_priority=priority)

    return load_artifact(conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=LoadError,
                         read_records=records, project=Projection, sync_signals=_after_load, file_suffixes={".csv", ".zip"}, bulk=True)


def rebuild_from_raw(conn):
    return rebuild(conn, source_code=SOURCE_CODE, error=LoadError)
