#GOAL Resolve insider and issuer identities and project typed filing facts.

"""Project SEC filing evidence into typed core facts."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, localcontext
from itertools import combinations
import json
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from .sec_form345_input import Record, parse_date
from .sec_ingestion import resolve_active_org, resolve_active_person


_US_STATES = frozenset(
    "AK AL AR AZ CA CO CT DC DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MN MO MS MT "
    "NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY".split()
)
_CANADA = dict(zip(
    "A0 A1 A2 A3 A4 A5 A6 A7 A8 A9 B0 Z4".split(),
    ("AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK", "YT", None),
    strict=True,
))
_MAX_BIGINT = 2**63 - 1
_MAX_SHARES = Decimal("9999999999999999.9999")
_PLAN_REFERENCE = re.compile(r"\b10b5\s*[-‐‑–]\s*1\b", re.IGNORECASE)
_NEGATED_PLAN = re.compile(r"\b(?:not|no|never|without)\b[^.;!?]{0,60}$", re.IGNORECASE)


def stable_id(kind, *parts):
    name = json.dumps(parts, default=str, ensure_ascii=False, separators=(",", ":"))
    return uuid5(NAMESPACE_URL, f"urn:money-in-motion:sec_form345:{kind}:{name}")


def filing_timestamp(day):
    return datetime.combine(day, time.min, tzinfo=UTC)


def cik(text):
    return text.strip().zfill(10)


def _value(row, column):
    return row.get(column, "").strip() or None


def _decimal(row, column):
    value = _value(row, column)
    return Decimal(value) if value is not None else None


def _round(value, unit):
    if value is None:
        return None
    return value.quantize(unit, rounding=ROUND_HALF_UP)


def _plan_status(payload):
    submission = payload["SUBMISSION"]
    flag = _value(submission, "AFF_10B5_ONE") or _value(submission, "AFF10B5ONE")
    if flag is not None:
        return flag.lower() in {"1", "true"}
    for note in payload["FOOTNOTES"]:
        text = note.get("FOOTNOTE_TXT", "")
        if any(not _NEGATED_PLAN.search(text[:match.start()]) for match in _PLAN_REFERENCE.finditer(text)):
            return True
    return None


@dataclass(frozen=True)
class Trade:
    record: Record
    raw_id: int
    transaction_on: date
    code: str
    side: str | None
    security: str
    reported_shares: Decimal
    shares: Decimal
    price_cents: int | None
    shares_after: Decimal | None
    plan: bool | None
    sale_amount_cents: int | None


def trade_from_raw(record, raw_id):
    table = record.table_name
    if table not in {"NONDERIV_TRANS", "DERIV_TRANS"}:
        return None
    row = record.payload[table]
    code = _value(row, "TRANS_CODE")
    shares = _decimal(row, "TRANS_SHARES")
    if shares is None and table == "DERIV_TRANS":
        shares = next((value for column in ("UNDLYNG_SEC_SHARES", "UNDERLYING_SECURITY_SHARES", "TRANS_UNDLNG_SHARES")
                       if (value := _decimal(row, column)) is not None), None)
    transaction_date = _value(row, "TRANS_DATE")
    side = _value(row, "TRANS_ACQUIRED_DISP_CD")
    price = _decimal(row, "TRANS_PRICEPERSHARE")
    if (not code or not transaction_date or not side or shares is None or not 0 < shares <= _MAX_SHARES
            or (price is not None and (price < 0 or price > Decimal("92233720368547758.07")))
            or (table == "DERIV_TRANS" and code not in {"S", "M"})):
        return None
    shares_after = _decimal(row, "SHRS_OWND_FOLWNG_TRANS")
    if shares_after is not None and not -_MAX_SHARES <= shares_after <= _MAX_SHARES:
        shares_after = None

    title = row["SECURITY_TITLE"]
    if table == "DERIV_TRANS":
        title = _value(row, "UNDLYNG_SEC_TITLE") or _value(row, "UNDERLYING_SECURITY_TITLE") or title
    with localcontext() as context:
        context.prec = max(64, len(shares.as_tuple().digits) + (len(price.as_tuple().digits) if price is not None else 0) + 3)
        price_cents = int(_round(price * 100, Decimal(1))) if price is not None else None
        amount = None
        if code == "S" and price is not None:
            total_cents = _round(shares * price * 100, Decimal(1))
            if total_cents <= _MAX_BIGINT:
                amount = int(total_cents)
        return Trade(
            record, raw_id, parse_date(transaction_date), code, side,
            " ".join(title.split()).casefold(), shares, _round(shares, Decimal("0.0001")),
            price_cents, _round(shares_after, Decimal("0.0001")),
            _plan_status(record.payload), amount,
        )


def _position(owner):
    title = _value(owner, "RPTOWNER_TITLE")
    relationships = set(re.findall(r"[A-Z0-9]+", owner.get("RPTOWNER_RELATIONSHIP", "").upper()))
    normalized = (title or "").casefold()
    if re.search(r"\b(?:chief|ceo|cfo|coo|cto|cio|cmo|cso|cpo|general counsel)\b", normalized):
        seniority, default = "c_suite", "Officer"
    elif re.search(r"\b(?:vice[ -]+president|[es]?vp)\b", normalized):
        seniority, default = "vp", "Officer"
    elif re.search(r"\bpresident\b", normalized):
        seniority, default = "c_suite", "Officer"
    elif "DIRECTOR" in relationships or re.search(r"\b(?:chair(?:man|woman|person)?|board)\b", normalized):
        seniority, default = "board", "Director"
    elif "TENPERCENTOWNER" in relationships or re.search(r"\b(?:owner|founder|partner)\b", normalized):
        seniority, default = "owner", "Ten-percent owner"
    elif re.search(r"\bdirector\b", normalized):
        seniority, default = "director", "Director"
    elif re.search(r"\bmanager\b", normalized):
        seniority, default = "manager", "Manager"
    elif "OFFICER" in relationships:
        seniority, default = None, "Officer"
    else:
        seniority, default = None, _value(owner, "RPTOWNER_TXT")
    return title or default, seniority


def _location(owner):
    city = _value(owner, "RPTOWNER_CITY")
    postal = _value(owner, "RPTOWNER_ZIPCODE")
    state = (_value(owner, "RPTOWNER_STATE") or "").upper()
    if not any((city, postal, state, _value(owner, "RPTOWNER_STREET1"), _value(owner, "RPTOWNER_STREET2"))):
        return None
    if state in _US_STATES:
        return city, state, postal, "US"
    if state in _CANADA:
        return city, _CANADA[state], postal, "CA"
    return city, None, postal, "US" if state == "X1" else "ZZ"


class Projection:
    def __init__(self, conn):
        self.conn = conn
        self.people = {}
        self.issuers = {}
        self.tickers = set()
        self.positions = set()
        self.locations = set()

    def _person(self, owner, raw_id, timestamp):
        owner_cik = cik(owner["RPTOWNERCIK"])
        if owner_cik not in self.people:
            row = self.conn.execute(
                "SELECT person_id FROM core.person_identifiers WHERE scheme = 'sec_cik' AND value = %s",
                (owner_cik,),
            ).fetchone()
            if row is None:
                person_id = stable_id("person", owner_cik)
                self.conn.execute(
                    "INSERT INTO core.people (id, full_name, created_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (person_id, owner["RPTOWNERNAME"].strip(), timestamp),
                )
                self.conn.execute(
                    "INSERT INTO core.person_identifiers (person_id, scheme, value, raw_record_id) "
                    "VALUES (%s, 'sec_cik', %s, %s)",
                    (person_id, owner_cik, raw_id),
                )
            else:
                person_id = resolve_active_person(self.conn, row[0])
            self.people[owner_cik] = person_id
        return self.people[owner_cik]

    def _issuer(self, filing, raw_id, timestamp):
        issuer_cik = cik(filing["ISSUERCIK"])
        if issuer_cik not in self.issuers:
            row = self.conn.execute(
                "SELECT organization_id FROM core.organization_identifiers WHERE scheme = 'sec_cik' AND value = %s",
                (issuer_cik,),
            ).fetchone()
            if row is None:
                issuer_id = stable_id("issuer", issuer_cik)
                self.conn.execute(
                    "INSERT INTO core.organizations (id, name, kind, created_at) VALUES (%s, %s, 'public_company', %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (issuer_id, filing["ISSUERNAME"].strip(), timestamp),
                )
                self.conn.execute(
                    "INSERT INTO core.organization_identifiers (organization_id, scheme, value, raw_record_id) "
                    "VALUES (%s, 'sec_cik', %s, %s)",
                    (issuer_id, issuer_cik, raw_id),
                )
            else:
                issuer_id = resolve_active_org(self.conn, row[0])
            self.issuers[issuer_cik] = issuer_id
        issuer_id = self.issuers[issuer_cik]
        ticker = (filing.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        if ticker and ticker not in {"NONE", "N/A"} and ticker not in self.tickers:
            # The ticker key is globally unique and can be reassigned by an exchange. Resolve by CIK.
            self.conn.execute(
                "INSERT INTO core.organization_identifiers (organization_id, scheme, value, raw_record_id) "
                "VALUES (%s, 'ticker', %s, %s) ON CONFLICT (scheme, value) DO NOTHING",
                (issuer_id, ticker, raw_id),
            )
            self.tickers.add(ticker)
        return issuer_id

    def _link(self, raw_id, person_id, issuer_id, role, timestamp):
        self.conn.execute(
            """
            INSERT INTO core.record_links (raw_record_id, person_id, organization_id, role, method, confidence, linked_at)
            SELECT %(raw)s, %(person)s::uuid, %(issuer)s::uuid, %(role)s, 'exact_identifier', 1, %(linked)s
            WHERE NOT EXISTS (
                SELECT 1 FROM core.record_links WHERE raw_record_id = %(raw)s AND role = %(role)s
                    AND method = 'exact_identifier' AND person_id IS NOT DISTINCT FROM %(person)s::uuid
                    AND organization_id IS NOT DISTINCT FROM %(issuer)s::uuid
            )
            """,
            {"raw": raw_id, "person": person_id, "issuer": issuer_id, "role": role, "linked": timestamp},
        )

    def _owner_facts(self, record, raw_id, owner, person_id, issuer_id):
        title, seniority = _position(owner)
        if title:
            issuer_name = record.payload["SUBMISSION"]["ISSUERNAME"]
            position_id = stable_id("position", record.accession_number, person_id, issuer_id, issuer_name, title, seniority)
            if position_id not in self.positions:
                self.conn.execute(
                    """
                    INSERT INTO core.positions
                        (id, person_id, organization_id, organization_name, title, seniority, is_current, raw_record_id)
                    VALUES (%s, %s, %s, %s, %s, %s, true, %s) ON CONFLICT (id) DO NOTHING
                    """,
                    (position_id, person_id, issuer_id, issuer_name, title, seniority, raw_id),
                )
                self.positions.add(position_id)

        address = _location(owner)
        if address is not None:
            location_id = stable_id("location", record.accession_number, person_id, record.observed_on, *address)
            if location_id not in self.locations:
                self.conn.execute(
                    """
                    INSERT INTO core.locations
                        (id, person_id, kind, locality, region, postal_code, country, observed_on, raw_record_id)
                    VALUES (%s, %s, 'work', %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
                    """,
                    (location_id, person_id, *address, record.observed_on, raw_id),
                )
                self.locations.add(location_id)

    def project(self, record, raw_id):
        timestamp = filing_timestamp(record.observed_on)
        issuer_id = self._issuer(record.payload["SUBMISSION"], raw_id, timestamp)
        self._link(raw_id, None, issuer_id, "issuer", timestamp)
        owners = record.payload["REPORTINGOWNER"]
        people: list[UUID] = []
        for owner in owners:
            person_id = self._person(owner, raw_id, timestamp)
            people.append(person_id)
            self._link(raw_id, person_id, None, "subject" if len(owners) == 1 else "related_person", timestamp)
            self._owner_facts(record, raw_id, owner, person_id, issuer_id)

        for left, right in combinations(sorted(set(people)), 2):
            self.conn.execute(
                "INSERT INTO core.relationships (person_a_id, person_b_id, kind, raw_record_id) "
                "VALUES (%s, %s, 'co_filers', %s) ON CONFLICT DO NOTHING",
                (left, right, raw_id),
            )

        if len(people) == 1 and (trade := trade_from_raw(record, raw_id)) is not None:
            self.conn.execute(
                """
                INSERT INTO core.equity_transactions
                    (id, person_id, issuer_id, transaction_on, code, shares, price_per_share_cents,
                     shares_owned_after, under_10b5_1_plan, raw_record_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (raw_record_id) DO NOTHING
                """,
                (stable_id("trade", raw_id), people[0], issuer_id, trade.transaction_on, trade.code,
                 trade.shares, trade.price_cents, trade.shares_after, trade.plan, raw_id),
            )
