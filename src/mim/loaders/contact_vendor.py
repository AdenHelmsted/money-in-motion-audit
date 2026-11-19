#GOAL Attribute authorized contact records and deduplicate dated verification evidence.

"""Load contract-authorized contact evidence for existing people."""

import re
from uuid import UUID

from ..normalize.contact import (
    normalize_email, normalize_label, normalize_phone, normalize_postal_address, normalize_verification, original_value,
)
from .linkedin_input import canonical_profile_id, retrieved_at
from .sec_form345_projection import stable_id
from .sec_ingestion import LoadError as IngestionError
from .sec_ingestion import RECORD_ERRORS, Record, contextual_error, load_artifact, read_json_envelopes, rebuild, resolve_active_person


SOURCE_CODE = "contact_vendor"
_TARGET = "resolved_person_id"
_MAPPING_KEYS = {"person_id", "sec_cik", "linkedin", "npi"}
_INELIGIBLE = {"pending", "processing", "unmatched", "not_matched", "no_match", "mismatch", "not_found",
               "none", "low", "error", "failed", "incomplete", "not_attempted"}
_PROVIDERS = {"people_data_labs": "pdl", "peopledatalabs": "pdl", "apollo_io": "apollo"}
_NORMALIZERS = {"email": normalize_email, "phone": normalize_phone, "postal_address": normalize_postal_address}
_KINDS = {"email_address": "email", "phone_number": "phone", "address": "postal_address", "postal": "postal_address"}
_FIELDS = {
    "email": {"email": "unknown", "work_email": "work", "personal_email": "personal", "personal_emails": "personal",
              "recommended_personal_email": "personal", "emails": "unknown", "email_addresses": "unknown"},
    "phone": {"phone": "unknown", "phone_number": "unknown", "mobile_phone": "unknown", "mobile_phone_number": "unknown",
              "work_phone": "work", "personal_phone": "personal", "phone_numbers": "unknown", "phones": "unknown"},
    "postal_address": {"postal_address": "unknown", "address": "unknown", "postal_addresses": "unknown",
                       "addresses": "unknown", "street_addresses": "unknown", "street_address": "unknown"},
}


class LoadError(IngestionError):
    def __init__(self, *, artifact_uri, reason, record_index=None, person_mapping=None, record=None, row_number=None, **_):
        self.record_index = record_index if record_index is not None else row_number
        self.person_mapping = person_mapping if record is None else record.payload.get("person_mapping")
        super().__init__(source_code=SOURCE_CODE, artifact_uri=artifact_uri, reason=reason)

    def as_dict(self):
        return {"source_code": self.source_code, "artifact_uri": self.artifact_uri, "record_index": self.record_index,
                "person_mapping": self.person_mapping, "reason": self.reason}


def _mapping_value(scheme, value):
    if scheme == "person_id":
        return UUID(str(value))
    if scheme == "linkedin":
        return canonical_profile_id(value)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{scheme} must be an identifier")
    digits = str(value).strip()
    length = "{1,10}" if scheme == "sec_cik" else "{10}"
    if not re.fullmatch(rf"[0-9]{length}", digits):
        raise ValueError(f"invalid {scheme} identifier")
    return digits.zfill(10)


def _target_person(conn, mapping):
    if not isinstance(mapping, dict) or not mapping or mapping.keys() - _MAPPING_KEYS:
        raise ValueError("person_mapping must contain a supported local identifier")
    targets = set()
    for scheme, value in mapping.items():
        value = _mapping_value(scheme, value)
        if scheme == "person_id":
            identity = value
        else:
            linked = conn.execute(
                "SELECT person_id FROM core.person_identifiers WHERE scheme = %s AND value = %s", (scheme, value),
            ).fetchone()
            if linked is None:
                raise ValueError(f"person_mapping {scheme} does not match a local person")
            identity = linked[0]
        targets.add(resolve_active_person(conn, identity))
    if len(targets) != 1:
        raise ValueError("person_mapping refers to multiple people")
    return targets.pop()


def _read_records(artifact, conn):
    for index, envelope in read_json_envelopes(artifact, LoadError):
        mapping = None
        try:
            if not isinstance(envelope, dict):
                raise ValueError("expected a contact vendor envelope object")
            mapping = envelope.get("person_mapping")
            provider = envelope.get("provider")
            if not isinstance(provider, str):
                raise ValueError("provider must be nonempty text")
            provider = re.sub(r"[ .-]+", "_", provider.strip().lower())
            if not re.fullmatch(r"[a-z0-9_]+", provider):
                raise ValueError("provider must identify a contact enrichment vendor")
            provider = _PROVIDERS.get(provider, provider)
            identifier = envelope.get("id")
            if type(identifier) not in {str, int} or not str(identifier).strip() or "\x00" in str(identifier):
                raise ValueError("id must identify the vendor response")
            status = envelope.get("http_status")
            if "http_status" not in envelope or (status is not None and (type(status) is not int or not 100 <= status <= 599)):
                raise ValueError("http_status must be an HTTP status code or null")
            if "raw_payload" not in envelope:
                raise ValueError("raw_payload is required")
            if _TARGET in envelope:
                raise ValueError(f"{_TARGET} is reserved for local resolution evidence")
            captured = retrieved_at(envelope)
            person_id = _target_person(conn, mapping)
            payload = {**envelope, _TARGET: str(person_id)}
            yield _record(f"{provider}:{str(identifier).strip()}", captured.date(), payload, index)
        except RECORD_ERRORS as exc:
            record = _record("", None, {"person_mapping": mapping}, index)
            raise contextual_error(LoadError, artifact.uri, exc, record) from exc


def _state(value):
    return re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else ""


def _blocked(body):
    return (any(body.get(key) is False for key in ("matched", "is_match", "complete", "success")) or body.get("pending") is True
            or bool(body.get("error") or body.get("errors"))
            or any(_state(body.get(field)) in _INELIGIBLE for field in ("status", "match_status", "match_confidence", "request_status"))
            or type(body.get("status")) is int and body["status"] != 200)


def _complete_payload(envelope):
    raw = envelope["raw_payload"]
    if envelope["http_status"] != 200 or not isinstance(raw, dict) or not raw or _blocked(envelope) or _blocked(raw):
        return None
    phone = raw.get("phone_enrichment")
    if isinstance(phone, dict) and _state(phone.get("status")) in {"pending", "processing"}:
        return None
    profile = raw.get("person", raw.get("data", raw))
    if not isinstance(profile, dict) or not profile or _blocked(profile):
        return None
    return profile


def _items(value):
    if value is None or value == "":
        return ()
    return value if isinstance(value, list) else (value,)


def _contacts(profile, provider):
    for field in ("contact_points", "contacts"):
        for item in _items(profile.get(field)):
            if not isinstance(item, dict):
                raise ValueError(f"{field} entries must be objects")
            kind = _state(item.get("kind", item.get("type")))
            kind = _KINDS.get(kind, kind)
            if kind not in _NORMALIZERS:
                raise ValueError("unsupported contact kind")
            yield kind, item, "unknown", None, None

    for kind, fields in _FIELDS.items():
        for field, label in fields.items():
            value = profile.get(field)
            if provider == "pdl" and type(value) is bool:
                continue  # PDL free-plan placeholders disclose availability only.
            for item in _items(value):
                status = profile.get(f"{field}_status") if field in {"email", "phone", "phone_number", "work_email", "mobile_phone"} else None
                timestamp = profile.get(f"{field}_verified_at") if status is not None else None
                yield kind, item, label, status, timestamp

    street = profile.get("location_street_address")
    if isinstance(street, str) and street.strip():
        source = {target: profile[f"location_{field}"] for target, field in (
            ("street_address", "street_address"), ("address_line_2", "address_line_2"),
            ("locality", "locality"), ("region", "region"), ("postal_code", "postal_code"), ("country", "country"),
        ) if isinstance(profile.get(f"location_{field}"), str)}
        yield "postal_address", source, "unknown", None, None


def _details(kind, item, default_label, default_status, default_timestamp, captured):
    if isinstance(item, dict):
        keys = ("value", "email", "address") if kind == "email" else (
            ("value", "raw_number", "number", "phone", "phone_number", "sanitized_number") if kind == "phone"
            else ("value", "formatted_address", "formatted", "address")
        )
        value = next((item[key] for key in keys if item.get(key) not in (None, "")), item if kind == "postal_address" else None)
        label = item.get("label", item.get("type", default_label))
        status = item.get("verification", item.get("verification_status", item.get("status", item.get("is_verified", default_status))))
        timestamp = item.get("verified_at", item.get("verification_timestamp", item.get("last_verified_at", default_timestamp)))
        if isinstance(status, dict):
            timestamp = status.get("verified_at", status.get("timestamp", timestamp))
            status = status.get("status")
    else:
        value, label, status, timestamp = item, default_label, default_status, default_timestamp
    normalized = _NORMALIZERS[kind](value)
    verification = normalize_verification(status)
    verified_at = None
    if verification != "unverified":
        verified_at = retrieved_at({"retrieved_at": timestamp}) if timestamp not in (None, "") else captured
    return normalize_label(label), original_value(value), normalized, verification, verified_at


class Projection:
    def __init__(self, conn):
        self.conn = conn

    def _matches_profile(self, profile, mapping, person, raw_id):
        returned = profile.get("linkedin_url") or profile.get("linkedin")
        if not isinstance(returned, str) or not returned.strip():
            return True
        try:
            slug = canonical_profile_id(returned)
        except ValueError:
            return False
        if "linkedin" in mapping:
            return slug == canonical_profile_id(mapping["linkedin"])
        known = self.conn.execute(
            "SELECT value FROM core.person_identifiers WHERE person_id = %s AND scheme = 'linkedin' AND raw_record_id < %s",
            (person, raw_id),
        ).fetchall()
        return not known or (slug,) in known

    def project(self, record, raw_id):
        payload = record.payload
        person = resolve_active_person(self.conn, UUID(payload[_TARGET]))
        captured = retrieved_at(payload)
        self.conn.execute(
            """
            INSERT INTO core.record_links (raw_record_id, person_id, role, method, confidence, linked_at)
            SELECT %(raw)s, %(person)s, 'subject', 'exact_identifier', 1, %(captured)s
            WHERE NOT EXISTS (SELECT 1 FROM core.record_links
                WHERE raw_record_id = %(raw)s AND person_id = %(person)s AND role = 'subject')
            """, {"raw": raw_id, "person": person, "captured": captured},
        )
        profile = _complete_payload(payload)
        if not profile or not self._matches_profile(profile, payload["person_mapping"], person, raw_id):
            return
        provider = record.external_id.split(":", 1)[0]
        for kind, item, label, status, timestamp in _contacts(profile, provider):
            label, value, normalized, verification, verified = _details(kind, item, label, status, timestamp, captured)
            day = record.observed_on
            self.conn.execute(
                _UPSERT, (stable_id(SOURCE_CODE, person, kind, normalized), person, kind, label, value, normalized,
                          verification, verified, day, day, raw_id),
            )


_UPSERT = """
    INSERT INTO core.contact_points AS old (id, person_id, kind, label, value, value_normalized,
        verification, verified_at, first_seen_on, last_seen_on, raw_record_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (person_id, kind, value_normalized) DO UPDATE SET
        first_seen_on = LEAST(old.first_seen_on, EXCLUDED.first_seen_on),
        last_seen_on = GREATEST(old.last_seen_on, EXCLUDED.last_seen_on),
        (label, value, verification, verified_at, raw_record_id) = (
            SELECT CASE WHEN (wins OR old.label = 'unknown') AND EXCLUDED.label <> 'unknown' THEN EXCLUDED.label ELSE old.label END,
                   CASE WHEN wins THEN EXCLUDED.value ELSE old.value END,
                   CASE WHEN wins THEN EXCLUDED.verification ELSE old.verification END,
                   CASE WHEN wins THEN EXCLUDED.verified_at ELSE old.verified_at END,
                   CASE WHEN wins THEN EXCLUDED.raw_record_id ELSE old.raw_record_id END
            FROM (SELECT CASE WHEN old.verified_at IS NULL AND EXCLUDED.verified_at IS NULL THEN
                    (EXCLUDED.last_seen_on, EXCLUDED.raw_record_id) > (old.last_seen_on, old.raw_record_id)
                ELSE (COALESCE(EXCLUDED.verified_at, '-infinity'::timestamptz),
                      array_position(ARRAY['unverified','valid','risky','invalid'], EXCLUDED.verification), EXCLUDED.raw_record_id)
                   > (COALESCE(old.verified_at, '-infinity'::timestamptz),
                      array_position(ARRAY['unverified','valid','risky','invalid'], old.verification), old.raw_record_id)
                END AS wins) AS choice
        )
"""


def _record(external_id, observed_on, payload, row_number=None):
    return Record(None, None, row_number, external_id, observed_on, payload)


def load_fixture(conn, path, *, archive_dir):
    return load_artifact(conn, path, archive_dir=archive_dir, source_code=SOURCE_CODE, error=LoadError,
                         read_records=lambda artifact: _read_records(artifact, conn), project=Projection,
                         sync_signals=None, file_suffixes={".json", ".jsonl"})


def rebuild_from_raw(conn):
    return rebuild(conn, source_code=SOURCE_CODE, error=LoadError)


if __name__ == "__main__":
    from mim.cli import legacy_load

    raise SystemExit(legacy_load(SOURCE_CODE))
