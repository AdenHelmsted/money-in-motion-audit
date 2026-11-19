#GOAL Project reviewed LinkedIn snapshots and cite both sides of career changes.

"""Import reviewed, file-based Scout vendor evidence and derive career changes."""

from collections import defaultdict
from itertools import pairwise
import re
from uuid import UUID

from .linkedin_input import (
    SOURCE_CODE, LoadError, Record, canonical_url, eligibility, profile_facts, profile_id, read_records, retrieved_at,
)
from .sec_form345_projection import stable_id
from .sec_ingestion import (
    resolve_active_org, resolve_active_person,
    RECORD_ERRORS, contextual_error, existing_signals, load_artifact, rebuild, remove_stale_signals, save_signal,
    stored_rows,
)


DETECTOR = "linkedin_v1"
_EXECUTIVE = {"owner", "c_suite"}
_NON_EXECUTIVE = {"individual", "manager", "director", "vp"}
_RETIREMENT = re.compile(r"\b(?:retired|retiree|in retirement)\b", re.IGNORECASE)
_NEGATED_RETIREMENT = re.compile(r"\b(?:not|never|un)\s+(?:yet\s+)?retired\b", re.IGNORECASE)


class Projection:
    def __init__(self, conn):
        self.conn = conn

    def _person(self, payload, slug):
        mapping = payload.get("person_mapping", {})
        if not isinstance(mapping, dict):
            raise ValueError("person_mapping must be an object")
        targets = set()
        for value in (payload.get("person_id"), mapping.get("person_id")):
            if value is not None:
                person_id = UUID(str(value))
                targets.add(resolve_active_person(self.conn, person_id))
        for value in (payload.get("sec_cik"), mapping.get("sec_cik")):
            if value is not None:
                cik = str(value).strip()
                if not re.fullmatch(r"[0-9]{1,10}", cik):
                    raise ValueError("Approved sec_cik must contain one to ten digits")
                row = self.conn.execute(
                    """
                    SELECT person_id FROM core.person_identifiers
                    WHERE scheme = 'sec_cik' AND value = %s
                    """, (cik.zfill(10),),
                ).fetchone()
                if row is None:
                    raise ValueError("Approved sec_cik does not identify an active local person")
                targets.add(resolve_active_person(self.conn, row[0]))
        identified = self.conn.execute(
            "SELECT person_id FROM core.person_identifiers WHERE scheme = 'linkedin' AND value = %s", (slug,),
        ).fetchone()
        if identified is not None:
            targets.add(resolve_active_person(self.conn, identified[0]))
        if len(targets) > 1:
            raise ValueError("Approved person mapping conflicts with the canonical LinkedIn identity")
        return next(iter(targets), None), "manual" if mapping or any(k in payload for k in ("person_id", "sec_cik")) else "exact_identifier"

    def _organization(self, position):
        identifiers = {str(value).lower() for value in (position.company_id, position.company_slug) if value}
        if position.company_slug:
            company = f"linkedin.com/company/{position.company_slug}"
            identifiers.update((company, f"https://{company}", f"https://www.{company}"))
        matches = self.conn.execute(
            """
            SELECT DISTINCT organization_id FROM core.organization_identifiers
            WHERE scheme = 'linkedin' AND lower(rtrim(value, '/')) = ANY(%s)
            """, (list(identifiers),),
        ).fetchall() if identifiers else []
        if not matches:
            matches = self.conn.execute(
                """
                SELECT id FROM core.organizations WHERE merged_into_id IS NULL
                    AND btrim(regexp_replace(lower(name), '[^[:alnum:]]+', ' ', 'g'))
                      = btrim(regexp_replace(lower(%s), '[^[:alnum:]]+', ' ', 'g'))
                LIMIT 2
                """, (position.organization_name,),
            ).fetchall()
        active = {resolve_active_org(self.conn, row[0]) for row in matches}
        return next(iter(active)) if len(active) == 1 else None

    def project(self, record, raw_id):
        payload = record.payload
        if eligibility(payload) != "accepted":
            return
        facts = profile_facts(payload)
        slug = profile_id(payload)
        captured = retrieved_at(payload)
        person_id, method = self._person(payload, slug)
        if person_id is None:
            person_id = stable_id("linkedin_person", slug)
            self.conn.execute(
                "INSERT INTO core.people (id, full_name, given_name, family_name, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (person_id, facts.full_name, facts.given_name, facts.family_name, captured),
            )
        self.conn.execute(
            "INSERT INTO core.person_identifiers (person_id, scheme, value, raw_record_id) "
            "VALUES (%s, 'linkedin', %s, %s) ON CONFLICT (scheme, value) DO NOTHING",
            (person_id, slug, raw_id),
        )
        self.conn.execute(
            """
            INSERT INTO core.record_links (raw_record_id, person_id, role, method, confidence, linked_at)
            SELECT %(raw)s, %(person)s, 'subject', %(method)s, 1, %(captured)s
            WHERE NOT EXISTS (SELECT 1 FROM core.record_links
                WHERE raw_record_id = %(raw)s AND person_id = %(person)s AND role = 'subject')
            """, {"raw": raw_id, "person": person_id, "method": method, "captured": captured},
        )
        self._facts(record, raw_id, person_id, facts)

    def _facts(self, record, raw_id, person_id, facts):
        url = canonical_url(profile_id(record.payload))
        day = record.observed_on
        self.conn.execute(
            """
            INSERT INTO core.contact_points AS existing
                (id, person_id, kind, label, value, value_normalized, first_seen_on, last_seen_on, raw_record_id)
            VALUES (%s, %s, 'linkedin_url', 'unknown', %s, %s, %s, %s, %s)
            ON CONFLICT (person_id, kind, value_normalized) DO UPDATE SET
                first_seen_on = LEAST(existing.first_seen_on, EXCLUDED.first_seen_on),
                last_seen_on = GREATEST(existing.last_seen_on, EXCLUDED.last_seen_on),
                raw_record_id = CASE WHEN (EXCLUDED.last_seen_on, EXCLUDED.raw_record_id) >
                    (existing.last_seen_on, existing.raw_record_id)
                    THEN EXCLUDED.raw_record_id ELSE existing.raw_record_id END
            """, (stable_id("linkedin_url", person_id, url), person_id, url, url, day, day, raw_id),
        )
        for position in facts.positions:
            values = (position.organization_name, position.title, position.seniority, position.started_on,
                      position.ended_on, position.is_current)
            self.conn.execute(
                """
                INSERT INTO core.positions (id, person_id, organization_id, organization_name, title,
                    seniority, started_on, ended_on, is_current, raw_record_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
                """, (stable_id("linkedin_position", raw_id, *values), person_id, self._organization(position),
                      *values, raw_id),
            )
        for item in facts.education:
            values = (item.school, item.degree, item.field, item.ended_year)
            self.conn.execute(
                "INSERT INTO core.education (id, person_id, school, degree, field, ended_year, raw_record_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (stable_id("linkedin_education", raw_id, *values), person_id, *values, raw_id),
            )
        if facts.location is not None:
            location = facts.location
            values = (location.kind, location.locality, location.region, location.country)
            self.conn.execute(
                "INSERT INTO core.locations (id, person_id, kind, locality, region, country, observed_on, raw_record_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (stable_id("linkedin_location", raw_id, *values), person_id, *values, day, raw_id),
            )


def _primary(facts):
    active = [position for position in facts.positions if position.is_current]
    return next((position for position in active if position.is_primary), active[0] if active else None)


def _same_employer(left, right):
    comparable = [(a, b) for key in ("company_slug", "company_id")
                  if (a := getattr(left, key)) and (b := getattr(right, key))]
    if comparable:
        return any(str(a).casefold() == str(b).casefold() for a, b in comparable)
    return _normalized(left.organization_name) == _normalized(right.organization_name)


def _normalized(value):
    return " ".join(re.findall(r"[^\W_]+", (value or "").casefold()))


def _changes(before, after):
    former, current = _primary(before), _primary(after)
    if former is None:
        return
    if current is not None:
        if not _same_employer(former, current):
            yield "job_change", f"{after.full_name} changed current employer to {current.organization_name}.", current
        if former.seniority in _NON_EXECUTIVE and current.seniority in _EXECUTIVE:
            yield "promoted_to_executive", f"{after.full_name} moved into an executive or owner role.", current
    retirement = [after.headline or "", *(p.title for p in after.positions if p.is_current)]
    still_active = any(p.is_current and _same_employer(former, p) and p.seniority in _EXECUTIVE for p in after.positions)
    if (former.seniority in _EXECUTIVE and not still_active
            and any(_RETIREMENT.search(text) and not _NEGATED_RETIREMENT.search(text) for text in retirement)):
        yield "retired", f"{after.full_name} listed retirement after an executive or owner role.", None


def sync_signals(conn, provenance):
    source_links = dict(conn.execute(
        """
        SELECT l.raw_record_id, l.person_id FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
        WHERE r.source_code = %s AND l.role = 'subject' AND l.person_id IS NOT NULL
        """, (SOURCE_CODE,),
    ))
    orgs = {(raw_id, name, title): org_id for raw_id, name, title, org_id in conn.execute(
        """
        SELECT p.raw_record_id, p.organization_name, p.title, p.organization_id
        FROM core.positions p JOIN core.raw_records r ON r.id = p.raw_record_id
        WHERE r.source_code = %s AND p.is_current
        """, (SOURCE_CODE,),
    )}
    profiles = defaultdict(list)
    for _, raw_id, external_id, payload, day, uri in stored_rows(conn, (SOURCE_CODE,)):
        if raw_id in source_links:
            _, line = provenance.setdefault(raw_id, (uri, None))
            record = _record(external_id, day, payload, line)
            profiles[external_id].append((day, retrieved_at(payload), raw_id, record))
    existing = existing_signals(conn, DETECTOR)
    desired = set()
    for snapshots in profiles.values():
        for (_, _, previous_id, previous), (day, captured, current_id, record) in pairwise(sorted(snapshots)):
            try:
                before, after = profile_facts(previous.payload), profile_facts(record.payload)
                for kind, summary, position in _changes(before, after):
                    signal_id = stable_id("linkedin_signal", kind, previous_id, current_id)
                    org = orgs.get((current_id, position.organization_name, position.title)) if position else None
                    if org is not None:
                        org = resolve_active_org(conn, org)
                    person = resolve_active_person(conn, source_links[current_id])
                    values = (person, org, kind, day, None, summary, 1, captured)
                    save_signal(conn, DETECTOR, existing, signal_id, values, (previous_id, current_id))
                    desired.add(signal_id)
            except RECORD_ERRORS as exc:
                raise contextual_error(LoadError, provenance[current_id][0], exc, record) from exc
    remove_stale_signals(conn, existing, desired)
    return len(desired)


def _record(external_id, observed_on, payload, row_number=None):
    return Record(None, None, row_number, external_id, observed_on, payload)


def load_fixture(conn, path, *, archive_dir):
    return load_artifact(
        conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=LoadError, read_records=read_records,
        project=Projection, sync_signals=sync_signals, file_suffixes={".json", ".jsonl"}, classify=eligibility,
    )


def rebuild_from_raw(conn):
    return rebuild(conn, source_code=SOURCE_CODE, error=LoadError)


if __name__ == "__main__":
    from mim.cli import legacy_load

    raise SystemExit(legacy_load(SOURCE_CODE))
