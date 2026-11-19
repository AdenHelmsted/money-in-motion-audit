#GOAL Resolve Form D issuers and related people and record the matching evidence.

"""Resolve Form D issuers and related people into evidence-backed core facts."""

from decimal import Decimal
import unicodedata

from .sec_form345_projection import _CANADA, _US_STATES, filing_timestamp, stable_id
from .sec_form_d_input import SOURCE_CODE, issuer_cik, issuer_name, parse_boolean, person_roles
from .sec_ingestion import resolve_active_org, resolve_active_person


_ROLE_SENIORITY = {"Executive Officer": "c_suite", "Director": "board", "Promoter": "owner"}
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
_APOSTROPHES = frozenset({"'", "’", "ʼ"})


def _text(row, field):
    return (row.get(field) or "").strip() or None


def _normalized_name(name):
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    letters = (
        char if char.isalnum() else " "
        for char in decomposed
        if not unicodedata.category(char).startswith("M") and char not in _APOSTROPHES
    )
    return " ".join("".join(letters).split())


def _legacy_family_name(full_name):
    # Forms 3/4/5 only store an unstructured name. Handle both `Last, First`
    # and `First Last, Jr.` when the canonical family name has not been set.
    first, comma, remainder = full_name.partition(",")
    if comma and _normalized_name(remainder) not in _NAME_SUFFIXES:
        return _normalized_name(first)
    parts = _normalized_name(first).split()
    if parts and parts[-1] in _NAME_SUFFIXES:
        parts.pop()
    return parts[-1] if parts else ""


def _same_family(full_name, family_name, sec_owner, expected):
    if family_name:
        return _normalized_name(family_name) == expected
    if _legacy_family_name(full_name) == expected:
        return True
    # EDGAR ownership filings present natural-person names last, first, middle.
    # Permit a complete surname prefix for these unstructured owner names only.
    return sec_owner and "," not in full_name and _normalized_name(full_name).startswith(expected + " ")


def primary_issuer(issuers):
    """Return the sole issuer or the uniquely designated primary issuer."""
    if len({issuer_cik(issuer) for issuer in issuers}) == 1:
        return issuers[0]
    primary = [issuer for issuer in issuers if parse_boolean(issuer.get("IS_PRIMARYISSUER_FLAG") or "") is True]
    return primary[0] if len(primary) == 1 else None


def _work_location(person):
    city = _text(person, "CITY")
    state = (_text(person, "STATEORCOUNTRY") or "").upper()
    postal = _text(person, "ZIPCODE")
    if not any((city, state, postal, _text(person, "STREET1"), _text(person, "STREET2"))):
        return None
    if state in _US_STATES:
        return city, state, postal, "US"
    if state in _CANADA:
        return city, _CANADA[state], postal, "CA"
    return city, None, postal, "US" if state in {"US", "X1"} else "ZZ"


class Projection:
    def __init__(self, conn):
        self.conn = conn
        self.trigram_threshold_set = False
        self.issuers = {}
        self.issuer_addresses = set()

    def _issuer(self, issuer, raw_id, timestamp):
        cik = issuer_cik(issuer)
        locality = _text(issuer, "CITY")
        region = _text(issuer, "STATEORCOUNTRY")
        address_key = cik, locality, region
        if cik not in self.issuers:
            existing = self.conn.execute(
                "SELECT organization_id FROM core.organization_identifiers WHERE scheme = 'sec_cik' AND value = %s",
                (cik,),
            ).fetchone()
            if existing is None:
                organization_id = stable_id("issuer", cik)
                self.conn.execute(
                    """
                    INSERT INTO core.organizations (id, name, kind, locality, region, created_at)
                    VALUES (%s, %s, 'private_company', %s, %s, %s)
                    """,
                    (organization_id, issuer_name(issuer), locality, region, timestamp),
                )
                self.conn.execute(
                    """
                    INSERT INTO core.organization_identifiers (organization_id, scheme, value, raw_record_id)
                    VALUES (%s, 'sec_cik', %s, %s)
                    """,
                    (organization_id, cik, raw_id),
                )
                self.issuer_addresses.add(address_key)
            else:
                organization_id = resolve_active_org(self.conn, existing[0])
            self.issuers[cik] = organization_id

        organization_id = self.issuers[cik]
        if address_key not in self.issuer_addresses:
            # An issuer already known through Form 4 can also file a Form D.
            # Its SEC CIK stays canonical and this filing supplies any missing address fields.
            self.conn.execute(
                """
                UPDATE core.organizations SET locality = COALESCE(locality, %(locality)s),
                    region = COALESCE(region, %(region)s)
                WHERE id = %(issuer)s AND ((locality IS NULL AND %(locality)s::text IS NOT NULL)
                    OR (region IS NULL AND %(region)s::text IS NOT NULL))
                """,
                {"issuer": organization_id, "locality": locality, "region": region},
            )
            self.issuer_addresses.add(address_key)
        return organization_id

    def _link(self, raw_id, person_id, organization_id, role, method, confidence, timestamp):
        self.conn.execute(
            """
            INSERT INTO core.record_links (raw_record_id, person_id, organization_id, role, method, confidence, linked_at)
            SELECT %(raw)s, %(person)s::uuid, %(issuer)s::uuid, %(role)s, %(method)s, %(confidence)s, %(timestamp)s
            WHERE NOT EXISTS (
                SELECT 1 FROM core.record_links WHERE raw_record_id = %(raw)s AND role = %(role)s
                    AND person_id IS NOT DISTINCT FROM %(person)s::uuid
                    AND organization_id IS NOT DISTINCT FROM %(issuer)s::uuid
            )
            """,
            {"raw": raw_id, "person": person_id, "issuer": organization_id, "role": role,
             "method": method, "confidence": confidence, "timestamp": timestamp},
        )

    def _matching_employees(self, organization_id, raw_id, name, surname_first):
        if organization_id is None:
            return []
        candidates = self.conn.execute(
            """
            SELECT p.id, p.full_name, p.family_name, EXISTS (
                SELECT 1 FROM core.person_identifiers i JOIN core.raw_records r ON r.id = i.raw_record_id
                WHERE i.person_id = p.id AND i.scheme = 'sec_cik' AND r.source_code = 'sec_form345'
                    AND r.id < %(raw)s
            ) AS sec_owner
            FROM core.people p
            JOIN (
                SELECT person_id FROM core.positions WHERE organization_id = %(issuer)s AND raw_record_id < %(raw)s
                UNION
                SELECT person.person_id FROM core.record_links company
                JOIN core.record_links person ON person.raw_record_id = company.raw_record_id
                WHERE company.organization_id = %(issuer)s AND company.role IN ('issuer', 'employer')
                    AND person.person_id IS NOT NULL AND person.role IN ('subject', 'related_person')
                    AND company.raw_record_id < %(raw)s
                    AND (company.role = 'employer' OR NOT EXISTS (
                        SELECT 1 FROM core.record_links other WHERE other.raw_record_id = company.raw_record_id
                            AND other.role = 'issuer' AND other.organization_id <> company.organization_id
                    ))
            ) matches ON matches.person_id = p.id
            WHERE p.merged_into_id IS NULL
            """,
            {"issuer": organization_id, "raw": raw_id},
        )
        normalized = _normalized_name(name)
        surname_first = _normalized_name(surname_first)
        matches = []
        for person_id, full_name, family, sec_owner in candidates:
            existing = _normalized_name(full_name)
            if existing == normalized or (sec_owner and not family and existing == surname_first):
                matches.append(person_id)
        return matches

    def _matching_residents(self, state, raw_id, name, family_name, employer_matches):
        if state not in _US_STATES:
            return []
        if not self.trigram_threshold_set:
            self.conn.execute("SET LOCAL pg_trgm.similarity_threshold = 0.8")
            self.trigram_threshold_set = True
        candidates = self.conn.execute(
            """
            SELECT p.id, p.full_name, p.family_name, EXISTS (
                SELECT 1 FROM core.person_identifiers i JOIN core.raw_records r ON r.id = i.raw_record_id
                WHERE i.person_id = p.id AND i.scheme = 'sec_cik' AND r.source_code = 'sec_form345'
                    AND r.id < %(raw)s
            ) AS sec_owner
            FROM core.people p
            WHERE p.merged_into_id IS NULL
                AND core.form_d_trigram_name(p.full_name) %% core.form_d_trigram_name(%(name)s)
                AND similarity(core.form_d_trigram_name(p.full_name), core.form_d_trigram_name(%(name)s)) >= 0.85
                AND EXISTS (
                    SELECT 1 FROM core.locations l WHERE l.person_id = p.id AND l.region = %(state)s
                        AND l.country = 'US' AND l.raw_record_id < %(raw)s
                )
            """,
            {"state": state, "name": name, "raw": raw_id},
        )
        normalized = _normalized_name(family_name)
        return [
            person_id for person_id, full_name, family, sec_owner in candidates
            if _same_family(full_name, family, sec_owner, normalized)
            and (not employer_matches or person_id in employer_matches)
        ]

    def _person(self, record, raw_id, related, organization_id, timestamp):
        # Reprojection preserves an existing explicit decision for this raw row.
        # The actual match queries below only consider evidence preceding it.
        prior_link = self.conn.execute(
            """
            SELECT person_id, method, confidence FROM core.record_links
            WHERE raw_record_id = %s AND role = 'related_person' AND person_id IS NOT NULL LIMIT 2
            """,
            (raw_id,),
        ).fetchall()
        if len(prior_link) > 1:
            raise ValueError("Conflicting related-person links for this Form D raw record")
        if prior_link:
            person, method, confidence = prior_link[0]
            return resolve_active_person(self.conn, person), method, confidence

        given = _text(related, "FIRSTNAME")
        middle = _text(related, "MIDDLENAME")
        family = _text(related, "LASTNAME")
        name = " ".join(part for part in (given, middle, family) if part)
        surname_first = " ".join(part for part in (family, given, middle) if part)
        employer_matches = self._matching_employees(organization_id, raw_id, name, surname_first)
        if len(employer_matches) == 1:
            return resolve_active_person(self.conn, employer_matches[0]), "name_and_employer", Decimal("0.90")
        state = (_text(related, "STATEORCOUNTRY") or "").upper()
        location_matches = self._matching_residents(state, raw_id, name, family or "", set(employer_matches))
        if len(location_matches) == 1:
            return resolve_active_person(self.conn, location_matches[0]), "name_and_location", Decimal("0.80")

        person_id = stable_id("form_d_person", record.external_id, _normalized_name(name))
        self.conn.execute(
            """
            INSERT INTO core.people (id, full_name, given_name, family_name, created_at)
            VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
            """,
            (person_id, name, given, family, timestamp),
        )
        return resolve_active_person(self.conn, person_id), "name_and_employer", Decimal("1.00")

    def _facts(self, record, raw_id, related, person_id, issuer, organization_id):
        if issuer is not None:
            for title in person_roles(related):
                self.conn.execute(
                    """
                    INSERT INTO core.positions
                        (id, person_id, organization_id, organization_name, title, seniority, is_current, raw_record_id)
                    VALUES (%s, %s, %s, %s, %s, %s, true, %s) ON CONFLICT (id) DO NOTHING
                    """,
                    (stable_id("form_d_position", record.external_id, person_id, organization_id, title), person_id,
                     organization_id, issuer_name(issuer), title, _ROLE_SENIORITY[title], raw_id),
                )

        location = _work_location(related)
        if location is not None:
            self.conn.execute(
                """
                INSERT INTO core.locations
                    (id, person_id, kind, locality, region, postal_code, country, observed_on, raw_record_id)
                VALUES (%s, %s, 'work', %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
                """,
                (stable_id("form_d_location", record.external_id, person_id, record.observed_on, *location),
                 person_id, *location, record.observed_on, raw_id),
            )

    def _co_officers(self, record, raw_id, person_id):
        # A revision to an existing row is not another co-officer. Use the most
        # recent previously seen raw row for each other related-person key.
        self.conn.execute(
            """
            INSERT INTO core.relationships (person_a_id, person_b_id, kind, raw_record_id)
            SELECT pairs.person_a_id, pairs.person_b_id, 'co_officers', %(raw)s
            FROM (
                SELECT DISTINCT LEAST(l.person_id, %(person)s) AS person_a_id,
                    GREATEST(l.person_id, %(person)s) AS person_b_id
                FROM (
                    SELECT DISTINCT ON (external_id) id FROM core.raw_records
                    WHERE source_code = %(source)s AND external_id LIKE %(filing)s
                        AND id < %(raw)s AND external_id <> %(external)s
                    ORDER BY external_id, id DESC
                ) earlier
                JOIN core.record_links l ON l.raw_record_id = earlier.id
                WHERE l.role = 'related_person' AND l.person_id IS NOT NULL AND l.person_id <> %(person)s
            ) pairs
            WHERE NOT EXISTS (
                SELECT 1 FROM core.relationships r JOIN core.raw_records evidence ON evidence.id = r.raw_record_id
                WHERE r.person_a_id = pairs.person_a_id AND r.person_b_id = pairs.person_b_id
                    AND r.kind = 'co_officers' AND evidence.source_code = %(source)s
                    AND evidence.external_id LIKE %(filing)s
            ) ON CONFLICT DO NOTHING
            """,
            {"raw": raw_id, "person": person_id, "source": SOURCE_CODE,
             "filing": f"{record.accession_number}:RELATEDPERSON:%", "external": record.external_id},
        )

    def project(self, record, raw_id):
        timestamp = filing_timestamp(record.observed_on)
        issuers = record.payload["ISSUERS"]
        _, kind, sequence = record.external_id.split(":")
        # Offering rows attest to their sequence directly. Related-person and
        # recipient rows retain the complete set of offering issuer links.
        issuer_rows = issuers if kind != "OFFERING" else [
            issuer for issuer in issuers if int(issuer["ISSUER_SEQ_KEY"]) == int(sequence)
        ]
        for issuer in issuer_rows:
            organization_id = self._issuer(issuer, raw_id, timestamp)
            self._link(raw_id, None, organization_id, "issuer", "exact_identifier", Decimal("1.00"), timestamp)

        if kind != "RELATEDPERSON":
            return

        related = record.payload["RELATEDPERSONS"]
        given, family = _text(related, "FIRSTNAME"), _text(related, "LASTNAME")
        if not given or not family or "related person is an entity" in {
            _normalized_name(given), _normalized_name(family),
        }:
            return
        issuer = primary_issuer(issuers)
        organization_id = self.issuers[issuer_cik(issuer)] if issuer is not None else None
        person_id, method, confidence = self._person(record, raw_id, related, organization_id, timestamp)
        self._link(raw_id, person_id, None, "related_person", method, confidence, timestamp)
        self._facts(record, raw_id, related, person_id, issuer, organization_id)
        self._co_officers(record, raw_id, person_id)
