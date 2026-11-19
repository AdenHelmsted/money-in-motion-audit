#GOAL Reconcile cumulative raise disclosures and identify eligible related people.

"""Reconcile cumulative Form D reports into one signal per offering and person."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import partial
from itertools import groupby
from uuid import UUID

from .sec_form345_projection import filing_timestamp, stable_id
from .sec_form_d_input import (
    SOURCE_CODE, Record, issuer_cik, issuer_name, parse_accession, parse_amount, parse_boolean,
    parse_date, person_roles,
)
from .sec_form_d_projection import primary_issuer
from .sec_ingestion import (
    RECORD_ERRORS, LoadError, contextual_error, existing_signals, remove_stale_signals, save_signal, stored_rows,
)


DETECTOR = "sec_form_d_v1"
_error = partial(LoadError, source_code=SOURCE_CODE)


@dataclass(frozen=True, slots=True)
class Filing:
    accession: str
    filed_on: date
    first_sale: date | None
    issuer: str | None
    file_number: str
    previous: str | None
    amendment: bool
    amount_cents: int | None
    raw_id: int
    payload: dict
    people: tuple[tuple[int, UUID, Decimal], ...]


def _amount_cents(offering):
    if (" ".join(offering["INDUSTRYGROUPTYPE"].split()).casefold() == "pooled investment fund"
            or parse_boolean(offering.get("YETTOOCCUR", ""))):
        return None
    amount = parse_amount(offering["TOTALAMOUNTSOLD"])
    if amount is None or not 0 < amount <= Decimal("92233720368547758.07"):
        return None
    with localcontext() as context:
        context.prec = max(28, len(amount.as_tuple().digits) + 3)
        return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _filings(conn, provenance):
    related_links = dict(
        (raw_id, (person_id, confidence)) for raw_id, person_id, confidence in conn.execute(
            """
            SELECT l.raw_record_id, l.person_id, l.confidence
            FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
            WHERE r.source_code = %s AND l.role = 'related_person' AND l.person_id IS NOT NULL
            """,
            (SOURCE_CODE,),
        )
    )
    filings = {}
    records = stored_rows(conn, (SOURCE_CODE,), latest=True)
    for accession, group in groupby(records, key=lambda row: row[2].split(":", 1)[0]):
        rows = list(group)
        _, raw_id, external_id, payload, filed_on, uri = rows[-1]
        try:
            primary = primary_issuer(payload["ISSUERS"])
            offering = payload["OFFERING"]
            issuer = primary and issuer_cik(primary)
            key = f"{accession}:OFFERING:{int(primary['ISSUER_SEQ_KEY'])}" if primary else None
            previous = offering.get("PREVIOUSACCESSIONNUMBER", "").strip()
            try:
                previous = parse_accession(previous) if previous else None
            except ValueError:
                previous = None
            sale_date = offering.get("SALE_DATE", "").strip()
            people = []
            for _, line_id, line_key, line_payload, _, line_uri in rows:
                provenance.setdefault(line_id, (line_uri, None))
                if line_key == key:
                    raw_id = line_id
                related = line_payload.get("RELATEDPERSONS")
                if (related is not None and line_id in related_links and line_payload["OFFERING"] == offering
                        and line_payload["ISSUERS"] == payload["ISSUERS"]
                        and {"Executive Officer", "Promoter"}.intersection(person_roles(related))):
                    person_id, confidence = related_links[line_id]
                    people.append((line_id, person_id, confidence))
            filings[accession] = Filing(
                accession, filed_on, parse_date(sale_date) if sale_date else None, issuer,
                payload["FORMDSUBMISSION"].get("FILE_NUM", "").strip(),
                previous, bool(parse_boolean(offering["ISAMENDMENT"])),
                _amount_cents(offering), raw_id, payload, tuple(people),
            )
        except RECORD_ERRORS as exc:
            _, line = provenance.get(raw_id, (uri, None))
            record = Record("OFFERING", accession, line, external_id, filed_on, payload)
            raise contextual_error(_error, uri, exc, record) from exc
    return filings


def _latest_offerings(filings):
    by_file = defaultdict(list)
    by_sale = defaultdict(list)
    for filing in filings.values():
        if filing.issuer and not filing.amendment:
            if filing.file_number:
                by_file[filing.issuer, filing.file_number].append(filing)
            if filing.first_sale:
                by_sale[filing.issuer, filing.first_sale].append(filing)

    def root(filing, seen):
        if not filing.issuer or filing.accession in seen:
            return None
        if not filing.amendment:
            return filing.accession, 0
        if filing.previous:
            previous = filings.get(filing.previous)
            if previous is None:
                return filing.previous, 1
            if previous.issuer != filing.issuer or previous.filed_on > filing.filed_on:
                return None
            resolved = root(previous, seen | {filing.accession})
            return (resolved[0], resolved[1] + 1) if resolved else None

        # EDGAR predecessor IDs take priority. Without one, require a single earlier original.
        filed = [item for item in by_file[filing.issuer, filing.file_number] if item.filed_on <= filing.filed_on]
        if not filed and filing.first_sale:
            filed = [item for item in by_sale[filing.issuer, filing.first_sale] if item.filed_on <= filing.filed_on]
        return (filed[0].accession, 1) if len(filed) == 1 else None

    latest = {}
    for filing in filings.values():
        resolved = root(filing, set())
        if resolved is None:
            continue
        key = filing.issuer, resolved[0]
        order = filing.filed_on, resolved[1], filing.accession
        if key not in latest or order > latest[key][0]:
            latest[key] = order, filing
    return {key: filing for key, (_, filing) in latest.items()}


def sync_signals(conn, provenance):
    filings = _latest_offerings(_filings(conn, provenance))
    issuers = dict(conn.execute(
        "SELECT value, organization_id FROM core.organization_identifiers WHERE scheme = 'sec_cik'"
    ).fetchall())
    existing = existing_signals(conn, DETECTOR)
    desired_ids = set()
    for (cik, root), filing in filings.items():
        if filing.amount_cents is None or not filing.people:
            continue
        people = defaultdict(list)
        for raw_id, person_id, confidence in filing.people:
            people[person_id].append((raw_id, confidence))
        primary = primary_issuer(filing.payload["ISSUERS"])
        summary = f"{issuer_name(primary).strip()} reported securities sales in a Form D offering."
        for person_id, rows in people.items():
            signal_id = stable_id("form_d_signal", cik, root, person_id)
            desired_ids.add(signal_id)
            evidence = sorted({filing.raw_id, *(raw_id for raw_id, _ in rows)})
            try:
                values = (
                    person_id, issuers[cik], "private_raise", filing.first_sale or filing.filed_on, filing.amount_cents,
                    summary, max(confidence for _, confidence in rows), filing_timestamp(filing.filed_on),
                )
                save_signal(conn, DETECTOR, existing, signal_id, values, evidence)
            except RECORD_ERRORS as exc:
                uri, line = provenance.get(rows[0][0], (None, None))
                record = Record("RELATEDPERSONS", filing.accession, line, "", filing.filed_on, filing.payload)
                raise contextual_error(_error, uri, exc, record) from exc

    remove_stale_signals(conn, existing, desired_ids)
    return len(desired_ids)
