#GOAL Resolve plan sponsors by EIN and require corroborated ownership for person signals.

"""Ingest DOL plan filings and emit corroborated small-plan owner signals."""

from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from pathlib import Path
import re

from .government_csv import Columns, date_from, link, read_records
from .sec_form345_projection import filing_timestamp, stable_id
from .sec_form_d_projection import _normalized_name
from .sec_ingestion import (LoadError as IngestionError, Record, existing_signals, load_artifact, rebuild,
                            remove_stale_signals, resolve_active_org, resolve_active_person, save_signal, stored_rows)


SOURCE_CODE = "dol_form5500"
DETECTOR = "form5500_v1"
SIGNIFICANT_ASSETS_CENTS = 100_000_000
LoadError = partial(IngestionError, source_code=SOURCE_CODE)
_ACK = ("ack_id", "acknowledgement_id", "acknowledgment_id")
_EIN = ("sf_spons_ein", "spons_dfe_ein", "sf_sponsor_ein", "sf_sponsor_dfe_ein", "sponsor_dfe_ein", "sponsor_ein", "ein")
_SPONSOR = ("sf_sponsor_name", "sf_sponsor_dfe_name", "sf_spons_name", "sponsor_dfe_name", "spons_dfe_name", "sponsor_name", "plan_sponsor_name")
_PARTICIPANTS = ("sf_tot_partcp_boy_cnt", "sf_partcp_boy_cnt", "tot_partcp_boy_cnt",
                 "participants_boy", "participant_count", "participants", "sf_tot_partcp_eoy_cnt", "sf_partcp_eoy_cnt",
                 "sf_tot_act_rtd_sep_benef_cnt", "tot_partcp_eoy_cnt", "tot_act_rtd_sep_benef_cnt")
_ASSETS = ("sf_tot_assets_eoy_amt", "tot_assets_eoy_amt", "total_assets_eoy", "plan_assets", "total_assets", "assets_eoy")
_SPONSOR_SIGNER = ("sf_spons_manual_signed_name", "spons_manual_signed_name", "sf_spons_signed_name", "spons_signed_name",
                   "sf_sponsor_signed_name", "sponsor_signed_name",
                   "sf_sponsor_sign_name", "sf_spons_sign_name", "sponsor_sign_name", "sponsor_signer_name")
_ADMIN_SIGNER = ("sf_admin_manual_signed_name", "admin_manual_signed_name", "sf_admin_signed_name", "admin_signed_name",
                 "sf_admin_sign_name", "admin_sign_name", "plan_signer_name", "signer_name")
_OWNER_TITLES = {"owner", "co owner", "sole proprietor", "proprietor", "business owner", "owner president"}


def _location(fields):
    city = fields.get("sf_spons_loc_us_city", "sf_spons_us_city", "spons_dfe_loc_us_city", "spons_dfe_mail_us_city",
                      "spons_dfe_us_city", "sf_sponsor_us_city", "sponsor_us_city", "sponsor_city", "city")
    region = fields.get("sf_spons_loc_us_state", "sf_spons_us_state", "spons_dfe_loc_us_state", "spons_dfe_mail_us_state",
                        "spons_dfe_us_state", "sf_sponsor_us_state", "sponsor_us_state", "sponsor_state", "state")
    return city or None, region.upper() or None


def _pension(fields):
    codes = fields.get("sf_type_pension_bnft_code", "sf_pension_bnft_code", "type_pension_bnft_code", "pension_benefit_codes")
    return bool(re.search(r"[123][A-Z]", codes.upper())) or _normalized_name(fields.get("plan_type", "benefit_type")) in {
        "pension", "pension plan", "retirement", "retirement plan", "401k", "401 k", "defined benefit", "defined contribution",
    }


def _counts(fields):
    participants = fields.get(*_PARTICIPANTS).replace(",", "")
    if participants and not re.fullmatch(r"[0-9]+(?:\.0+)?", participants):
        raise ValueError("participant count must be a nonnegative integer")
    amount = fields.get(*_ASSETS).replace(",", "").removeprefix("$").strip()
    cents = None
    if amount:
        number = Decimal(amount)
        if not number.is_finite() or number > Decimal("92233720368547758.07"):
            raise ValueError("plan assets must be a finite amount representable in cents")
        if number >= 0:
            cents = int((number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return int(participants.partition(".")[0]) if participants else None, cents


def _convert(row, member):
    fields = Columns(row)
    ack = fields.require(*_ACK)
    identifier = fields.require(*_EIN).replace("-", "")
    if not re.fullmatch(r"[0-9]{9}", identifier):
        raise ValueError("sponsor EIN must contain nine digits")
    fields.require(*_SPONSOR)
    observed = date_from(fields, "date_received", "filing_received_date", "filing_date", "observed_on",
                         "sf_plan_year_end_date", "plan_year_end_date", "sf_tax_prd", "tax_prd")
    if observed is None:
        raise ValueError("Form 5500 requires a filing or plan-year-end date")
    form = fields.get("form_type", "form_number").replace("-", "").replace(" ", "").upper()
    form = form.removeprefix("FORM")
    if form and form not in {"5500", "5500SF", "SF"}:
        raise ValueError("unsupported Form 5500 variant")
    short = form in {"5500SF", "SF"} if form else bool(re.search(r"5500[_ -]?sf", member.lower()) or
                                                       any(name.startswith("sf_") for name in fields.values))
    participants, assets = _counts(fields)
    return ack, observed, {"row": row, "ein": identifier, "short_form": short, "pension": _pension(fields),
                           "participants": participants, "assets_cents": assets}


def _owner_name(fields):
    owner = fields.get("business_owner_name", "sponsor_owner_name", "owner_name")
    if owner:
        return owner
    title = _normalized_name(fields.get("signer_title", "sponsor_signer_title", "sf_sponsor_signer_title"))
    return fields.get(*_SPONSOR_SIGNER, "plan_signer_name", "signer_name") if title in _OWNER_TITLES else ""


def _prior_owner(conn, person, organization, raw_id, observed):
    return conn.execute("""
        SELECT p.raw_record_id FROM core.positions p JOIN core.raw_records r ON r.id = p.raw_record_id
        WHERE p.person_id = %s AND p.organization_id = %s AND p.raw_record_id <= %s AND r.observed_on <= %s
          AND p.is_current AND p.seniority = 'owner' AND p.title !~* 'promoter'
          AND (p.started_on IS NULL OR p.started_on <= %s)
          AND NOT EXISTS (SELECT 1 FROM core.positions ended JOIN core.raw_records evidence ON evidence.id = ended.raw_record_id
                          WHERE ended.person_id = p.person_id AND ended.organization_id = p.organization_id
                            AND lower(ended.title) = lower(p.title) AND ended.ended_on <= %s
                            AND evidence.source_code = r.source_code AND evidence.observed_on BETWEEN r.observed_on AND %s
                            AND ended.raw_record_id > p.raw_record_id AND ended.raw_record_id <= %s)
        ORDER BY r.observed_on DESC, p.raw_record_id DESC LIMIT 1
        """, (person, organization, raw_id, observed, observed, observed, observed, raw_id)).fetchone()


class Projection:
    def __init__(self, conn):
        self.conn = conn

    def _sponsor(self, record, raw_id, fields):
        conn = self.conn
        ein = record.payload["ein"]
        organization = conn.execute("SELECT organization_id FROM core.organization_identifiers WHERE scheme = 'ein' AND value = %s",
                                    (ein,)).fetchone()
        if organization:
            return resolve_active_org(conn, organization[0])
        name = fields.require(*_SPONSOR)
        city, region = _location(fields)
        matches = conn.execute("""
            SELECT id, name FROM core.organizations o WHERE o.merged_into_id IS NULL
                AND lower(o.locality) = lower(%s) AND upper(o.region) = %s
                AND NOT EXISTS (SELECT 1 FROM core.organization_identifiers i
                               WHERE i.organization_id = o.id AND i.scheme = 'ein' AND i.value <> %s)
            """, (city, region, ein)).fetchall() if city and region else []
        candidates = [org for org, existing_name in matches if _normalized_name(existing_name) == _normalized_name(name)]
        organization_id = candidates[0] if len(candidates) == 1 else stable_id("dol_sponsor", ein)
        if len(candidates) != 1:
            code = fields.get("sf_business_code", "business_code", "naics_code")
            conn.execute("""
                INSERT INTO core.organizations (id, name, kind, naics_code, locality, region, created_at)
                VALUES (%s, %s, 'other', %s, %s, %s, %s)
                """, (organization_id, name, code if re.fullmatch(r"[0-9]{2,6}", code) else None,
                      city, region, filing_timestamp(record.observed_on)))
        conn.execute("INSERT INTO core.organization_identifiers (organization_id, scheme, value, raw_record_id) VALUES (%s, 'ein', %s, %s)",
                     (organization_id, ein, raw_id))
        return organization_id

    def project(self, record, raw_id):
        conn = self.conn
        fields = Columns(record.payload["row"])
        organization = self._sponsor(record, raw_id, fields)
        day = record.observed_on
        link(conn, raw_id, day, role="sponsor", organization=organization)
        owner = _owner_name(fields)
        name = owner or fields.get(*_SPONSOR_SIGNER, *_ADMIN_SIGNER)
        if not (normalized := _normalized_name(name)):
            return
        rows = conn.execute("""
            SELECT DISTINCT p.id, p.full_name FROM core.people p
            JOIN core.positions position ON position.person_id = p.id
            JOIN core.raw_records r ON r.id = position.raw_record_id
            WHERE p.merged_into_id IS NULL AND position.organization_id = %s AND position.raw_record_id < %s
                AND r.observed_on <= %s AND position.is_current AND (position.started_on IS NULL OR position.started_on <= %s)
            """, (organization, raw_id, day, day)).fetchall()
        matches = [person for person, old_name in rows if _normalized_name(old_name) == normalized]
        if len(matches) > 1 or not matches and not owner:
            return
        person = matches[0] if matches else stable_id("dol_owner", record.payload["ein"], normalized)
        if not matches:
            conn.execute("INSERT INTO core.people (id, full_name, created_at) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                         (person, name, filing_timestamp(day)))
        person = resolve_active_person(conn, person)
        link(conn, raw_id, day, role="subject", person=person, method="name_and_employer", confidence=Decimal("0.90"))
        if owner:
            conn.execute("""
                INSERT INTO core.positions (id, person_id, organization_id, organization_name, title, seniority, is_current, raw_record_id)
                VALUES (%s, %s, %s, %s, 'Owner', 'owner', true, %s) ON CONFLICT (id) DO NOTHING
                """, (stable_id("dol_owner_position", raw_id), person, organization, fields.require(*_SPONSOR), raw_id))


def sync_signals(conn, provenance):
    existing = existing_signals(conn, DETECTOR)
    desired = set()
    for _, raw_id, ack, payload, observed, _ in stored_rows(conn, (SOURCE_CODE,), latest=True):
        count, amount = payload["participants"], payload["assets_cents"]
        fields = Columns(payload["row"])
        filing_status = fields.get("filing_status").upper()
        if (not payload["short_form"] or not payload["pension"] or count is None or count >= 100
                or amount is None or amount < SIGNIFICANT_ASSETS_CENTS or filing_status not in {"", "FILING_RECEIVED"}):
            continue
        links = dict(conn.execute("""
            SELECT role, COALESCE(person_id, organization_id) FROM core.record_links
            WHERE raw_record_id = %s AND role IN ('subject', 'sponsor')
            """, (raw_id,)))
        if "subject" not in links:
            continue
        proof = _prior_owner(conn, links["subject"], links["sponsor"], raw_id, observed)
        if proof is None:
            continue
        signal_id = stable_id("dol_plan_sponsor_owner", ack)
        person = resolve_active_person(conn, links["subject"])
        sponsor = resolve_active_org(conn, links["sponsor"])
        values = (person, sponsor, "plan_sponsor_owner", observed, amount,
                  "The corroborated owner’s business reported a small pension plan with substantial year-end plan assets.",
                  Decimal("0.90"), filing_timestamp(observed))
        save_signal(conn, DETECTOR, existing, signal_id, values, (raw_id, proof[0]))
        desired.add(signal_id)
    remove_stale_signals(conn, existing, desired)
    return len(desired)


def _record(external_id, observed_on, payload, row_number=None):
    return Record("dol_form5500", external_id, row_number, external_id, observed_on, payload)


def load_fixture(conn, path, *, archive_dir):
    def priority(name):
        if re.match(r"f_5500_(?:sf_)?\d{4}(?:_.*)?\.csv$", name) and "part" not in name:
            return 2
        return int(not any(word in name for word in ("schedule", "f_sch", "layout", "part7", "part_7")))

    def records(artifact):
        return read_records(artifact, Path(path).name, LoadError, _convert,
                            lambda keys: bool(keys.intersection(_ACK) and keys.intersection(_EIN) and keys.intersection(_SPONSOR)),
                            member_priority=priority)

    return load_artifact(conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=LoadError,
                         read_records=records, project=Projection, sync_signals=sync_signals, file_suffixes={".csv", ".zip"}, bulk=True)


def rebuild_from_raw(conn):
    return rebuild(conn, source_code=SOURCE_CODE, error=LoadError)
