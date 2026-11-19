#GOAL Validate vendor envelopes and normalize eligible LinkedIn profile facts.

"""Read Scout vendor envelopes without contacting the providers."""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
import re
from unicodedata import normalize
from urllib.parse import quote, unquote, urlsplit

from ..normalize import US_STATE_NAMES as _STATES
from . import sec_ingestion
from .sec_ingestion import Record, read_json_envelopes
SOURCE_CODE = "linkedin"
_PROVIDERS = {"coresignal", "pdl", "brightdata"}
_HOST = re.compile(r"(?:www\.|[a-z]{2,3}\.)?linkedin\.com", re.I)
_SLUG = re.compile(r"[\w-]+")
_LEGACY = re.compile(r"pub:([^:]+):([a-f\d]{1,3}):([a-f\d]{1,3}):([a-f\d]{1,3})")
_APPROVED = {"approved", "verified", "human_approved", "human_verified"}
_CANDIDATE = {"candidate", "name_based", "name_based_match", "search_match", "unverified_search_match"}
_REJECTED = {"mismatch", "wrong", "rejected", "conflict", "superseded", "revoked"}
_MONTHS = "january february march april may june july august september october november december".split()
_COUNTRIES = {"united states": "US", "united states of america": "US", "usa": "US", "us": "US",
              "united kingdom": "GB", "uk": "GB", "canada": "CA", "australia": "AU"}
_STATE_CODES = frozenset(_STATES.values())


class LoadError(sec_ingestion.LoadError):
    def __init__(self, *, artifact_uri, reason, record_index=None, profile_id=None, row_number=None, record=None, **_):
        self.record_index = record_index if record_index is not None else row_number
        self.profile_id = profile_id or (record.external_id.partition(":")[2] if record is not None else None)
        super().__init__(source_code=SOURCE_CODE, artifact_uri=artifact_uri, reason=reason)

    def as_dict(self):
        return {"source_code": self.source_code, "artifact_uri": self.artifact_uri, "record_index": self.record_index,
                "profile_id": self.profile_id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Position:
    organization_name: str
    title: str
    seniority: str
    started_on: date | None
    ended_on: date | None
    is_current: bool
    company_slug: str | None
    company_id: str | None
    is_primary: bool


@dataclass(frozen=True, slots=True)
class Education:
    school: str
    degree: str | None
    field: str | None
    ended_year: int | None


@dataclass(frozen=True, slots=True)
class Location:
    kind: str
    locality: str | None
    region: str | None
    country: str


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    full_name: str
    given_name: str | None
    family_name: str | None
    headline: str | None
    positions: tuple[Position, ...]
    education: tuple[Education, ...]
    location: Location | None


def _text(value):
    return value.strip() or None if isinstance(value, str) else None


def _object(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return (item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()


def _first(value):
    return value[0] if isinstance(value, list) and value else None


def normalize_provider(value):
    provider = (_text(value) or "").lower()
    if provider not in _PROVIDERS:
        raise ValueError("provider must be coresignal, pdl, or brightdata")
    return provider


def _linkedin_path(value):
    try:
        url = urlsplit(value if "://" in value else f"https://{value}")
        valid = (url.scheme in {"http", "https"} and _HOST.fullmatch(url.hostname or "")
                 and url.username is None and url.password is None and url.port is None)
    except ValueError as exc:
        raise ValueError("invalid LinkedIn URL") from exc
    if not valid:
        raise ValueError("invalid LinkedIn URL")
    return url.path.removesuffix("/").split("/")[1:]


def canonical_profile_id(value):
    """Canonical Scout person slug, including every component of a legacy /pub path."""
    text = _text(value) or ""
    if "/" in text:
        path = _linkedin_path(text)
        if len(path) == 2 and path[0] == "in":
            text = unquote(path[1], errors="strict")
        elif len(path) == 5 and path[0].lower() == "pub":
            text = ":".join(["pub", unquote(path[1], errors="strict"), *path[2:]])
        else:
            raise ValueError("expected a personal LinkedIn profile")
    text = normalize("NFC", text).lower()
    legacy = _LEGACY.fullmatch(text)
    if _SLUG.fullmatch(text) or (legacy and _SLUG.fullmatch(legacy[1])):
        return text
    raise ValueError("expected a canonical LinkedIn profile slug or URL")


def canonical_url(value):
    slug = canonical_profile_id(value)
    if slug.startswith("pub:"):
        _, name, *identifier = slug.split(":")
        return f"https://www.linkedin.com/pub/{quote(name)}/{'/'.join(identifier)}"
    return f"https://www.linkedin.com/in/{quote(slug)}"


def _company_slug(value):
    if not _text(value):
        return None
    if "/" not in value:
        slug = normalize("NFC", value.strip()).lower()
    else:
        try:
            path = _linkedin_path(value.strip())
            slug = normalize("NFC", unquote(path[1], errors="strict")).lower() if len(path) == 2 and path[0] == "company" else ""
        except ValueError:
            return None
    return slug if _SLUG.fullmatch(slug) else None


def retrieved_at(payload):
    value = _text(payload.get("retrieved_at"))
    if value is None:
        raise ValueError("retrieved_at must be an ISO 8601 timestamp with a timezone")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("retrieved_at must be an ISO 8601 timestamp with a timezone") from exc
    if timestamp.utcoffset() is None:
        raise ValueError("retrieved_at must be an ISO 8601 timestamp with a timezone")
    return timestamp.astimezone(UTC)


def _body(payload):
    raw = payload.get("raw_payload", payload.get("raw_response"))
    return _object(_first(raw) if isinstance(raw, list) else raw)


def _person(payload):
    body = _body(payload)
    return _object(body.get("data", body)) if payload.get("provider") == "pdl" else body


def _returned_ids(payload):
    person = _person(payload)
    fields = ["linkedin_url", "professional_network_url", "canonical_profile_id", "canonical_id", "public_identifier"]
    if payload.get("provider") == "brightdata":
        fields.insert(0, "url")
        # Bright Data can also report numeric internal identifiers beside a vanity URL.
        if not any(person.get(field) for field in fields):
            fields.append("linkedin_id")
    return [_text(person.get(field)) for field in fields if _text(person.get(field))]


def _approval(payload):
    provenance = _object(payload.get("provenance"))
    return _object(payload.get("identity_approval", provenance.get("identity_approval")))


def _targets(payload):
    direct = _object(payload.get("input"))
    provenance_input = _object(_object(payload.get("provenance")).get("input"))
    sources = (payload.get("canonical_profile_id"), payload.get("canonical_id"), direct.get("url"),
               provenance_input.get("url"), payload.get("requested_linkedin_url"), _approval(payload).get("approved_url"))
    return [text for target in sources if (text := _text(target)) and not text.startswith("resolve:")]


def profile_id(payload):
    targets = _targets(payload)
    if targets:
        return canonical_profile_id(targets[0])
    returned = _returned_ids(payload)
    if returned:
        return canonical_profile_id(returned[0])
    raise ValueError("envelope has no canonical LinkedIn profile identifier")


def _name(payload, person):
    first, last = _text(person.get("first_name")), _text(person.get("last_name"))
    key = "name" if payload.get("provider") == "brightdata" else "full_name"
    return (_text(person.get(key)) or _text(person.get("full_name"))
            or " ".join(part for part in (first, last) if part)), first, last


def response_status(payload):
    """Recognize a reusable full response or a reason no profile facts may be extracted."""
    body = _body(payload)
    provider = payload.get("provider")
    status = payload.get("http_status")
    if provider == "brightdata" and (status == 202 or body.get("snapshot_id")):
        return "pending"
    if status != 200 or not body or body.get("error") or body.get("errors"):
        return "incomplete"
    person = _person(payload)
    if provider == "pdl" and (body.get("status", 200) != 200 or ("likelihood" in body and (
        type(body["likelihood"]) not in {int, float} or not 6 <= body["likelihood"] <= 10
    ))):
        return "incomplete"
    if person.get("error") or person.get("errors") or person.get("is_deleted") in (1, True, "1", "true"):
        return "incomplete"
    returned = _returned_ids(payload)
    if not returned:
        return "incomplete"
    expected = profile_id(payload)
    try:
        if any(canonical_profile_id(value) != expected for value in (*_targets(payload), *returned)):
            return "mismatch"
    except ValueError:
        return "mismatch"
    return "complete" if _name(payload, person)[0] else "incomplete"


def eligibility(payload):
    provenance = _object(payload.get("provenance"))
    statuses = {
        value.lower() for item in (payload.get("match_status"), payload.get("identity_status"),
                                   provenance.get("identity_status"), payload.get("approval_status"))
        if (value := _text(item))
    }
    if statuses & _REJECTED:
        return "mismatch" if "mismatch" in statuses else "unreviewed"
    response = response_status(payload)
    if response != "complete":
        return response

    approval = _approval(payload)
    scout_review = False
    if _text(approval.get("reviewed_by")) and _text(approval.get("reviewed_at")) and approval.get("approved_url"):
        try:
            scout_review = (canonical_profile_id(approval["approved_url"]) == profile_id(payload)
                            and retrieved_at({"retrieved_at": approval["reviewed_at"]}) is not None)
        except ValueError:
            pass
    # Plain matching statuses (matched/matched_url) establish URL agreement only.
    if scout_review:
        return "accepted"
    if statuses & _CANDIDATE or payload.get("search_origin") or provenance.get("search_origin"):
        return "withheld_candidate"
    explicit = {_text(payload.get("match_status")) or "", _text(payload.get("approval_status")) or ""}
    return "accepted" if {value.lower() for value in explicit} & _APPROVED else "unreviewed"


def read_records(artifact):
    index = None
    try:
        for index, envelope in read_json_envelopes(artifact, LoadError):
            canonical = None
            try:
                if not isinstance(envelope, dict):
                    raise ValueError("expected a Scout vendor envelope object")
                payload = dict(envelope)
                if _targets(payload):
                    canonical = profile_id(payload)
                payload["provider"] = normalize_provider(payload.get("provider", payload.get("provider_name")))
                if not _text(payload.get("id")):
                    raise ValueError("id must be a nonempty Scout envelope identifier")
                if "raw_payload" not in payload:
                    if "raw_response" not in payload:
                        raise ValueError("raw_payload is required")
                    payload["raw_payload"] = payload["raw_response"]
                canonical = canonical or profile_id(payload)
                timestamp = retrieved_at(payload)
                status = payload.get("http_status")
                if "http_status" not in payload or (status is not None and (type(status) is not int or not 100 <= status <= 599)):
                    raise ValueError("http_status must be an HTTP status code or null for a failed request")
                if "canonical_profile_id" in payload and payload["canonical_profile_id"] != canonical:
                    payload.setdefault("supplied_canonical_profile_id", payload["canonical_profile_id"])
                payload["canonical_profile_id"] = canonical
                if "match_status" not in payload:
                    provenance = _object(payload.get("provenance"))
                    identity_status = _text(payload.get("identity_status", provenance.get("identity_status")))
                    payload["match_status"] = identity_status if identity_status and identity_status.lower() not in _APPROVED else "unreviewed"
                yield Record(None, None, index, f"{payload['provider']}:{canonical}", timestamp.date(), payload)
            except ValueError as exc:
                raise LoadError(artifact_uri=artifact.uri, record_index=index, profile_id=canonical, reason=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise LoadError(artifact_uri=artifact.uri, record_index=index, reason=str(exc)) from exc


def _date(value=None, year=None, month=None, day=None):
    parts = _object(value)
    if parts:
        year, month, day = parts.get("year"), parts.get("month"), parts.get("day")
    elif (text := _text(value)):
        if match := re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text):
            year, month, day = match.groups()
        elif match := re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", text):
            year = match[2]
            month = next((i for i, name in enumerate(_MONTHS, 1) if match[1].lower() in (name, name[:3])), None)
            if month is None:
                return None
    if year is None and type(value) is int:
        year = value
    if not year:
        return None
    try:
        return date(int(year), int(month or 1), int(day or 1))
    except (ValueError, TypeError):
        return None


def seniority(title):
    title = title.casefold()
    for level, expression in (
        ("owner", r"\b(?:founder|co-founder|owner|proprietor|managing partner)\b"),
        ("c_suite", r"\b(?:chief|c[efiotmshpdr]o|general counsel)\b|^(?:group |global )?president\b"),
        ("vp", r"\b(?:vice[ -]+president|v\.?p\.?|svp|evp|avp)\b"),
        ("board", r"\b(?:board|chairperson|chairman|chairwoman|non-executive director)\b"),
        ("director", r"\b(?:director|head)\b"),
        ("manager", r"\b(?:manager|supervisor|lead)\b"),
    ):
        if re.search(expression, title):
            return level
    return "individual"


def _affirmative(value):
    return value is True or value == 1 or (_text(value) or "").casefold() in {"1", "yes", "true"}


def _position(job, provider):
    company = _object(job.get("company"))
    company_name = _text(company.get("name")) or _text(job.get("company_name")) or _text(job.get("company"))
    title = _text(_object(job.get("title")).get("name")) or _text(job.get("position_title")) or _text(job.get("title"))
    if not company_name or not title:
        return None

    start, end = job.get("start_date", job.get("date_from")), job.get("end_date", job.get("date_to"))
    duration = _text(job.get("duration")) or ""
    if provider == "brightdata" and duration:
        period = re.split(r"\s+[–—-]\s+", duration.split("·", 1)[0], maxsplit=1)
        if len(period) == 2:
            start, end = start or period[0], end or period[1]
    started = _date(start, job.get("date_from_year"), job.get("date_from_month"), job.get("date_from_day"))
    ended = _date(end, job.get("date_to_year"), job.get("date_to_month"), job.get("date_to_day"))
    current = not ended and (
        _affirmative(job.get("active_experience", job.get("is_current")))
        or (_text(end) or "").casefold() in {"present", "current", "now"}
        or (provider == "pdl" and "end_date" in job and end in (None, ""))
    )
    if started and ended and ended < started:
        # Partial or inconsistent raw dates cannot assert a valid interval.
        ended = None
    company_id = company.get("id") or job.get("company_id")
    return Position(
        company_name, title, seniority(title), started, ended, current,
        _company_slug(company.get("linkedin_url") or job.get("company_linkedin_url") or job.get("company_url")
                      or company.get("linkedin_slug") or job.get("company_slug")),
        str(company_id) if type(company_id) in {int, str} and company_id else None,
        _affirmative(job.get("is_primary")),
    )


def _education(entry):
    school = _text(_object(entry.get("school")).get("name")) or _text(entry.get("institution_name")) or _text(entry.get("title")) or _text(entry.get("school"))
    if not school:
        return None
    ended = _date(entry.get("end_date", entry.get("date_to")), entry.get("date_to_year") or entry.get("end_year"))
    return Education(school, _text(entry.get("degree")) or _text(_first(entry.get("degrees"))),
                     _text(entry.get("field_of_study")) or _text(entry.get("field")) or _text(_first(entry.get("majors"))),
                     ended.year if ended else None)


def _location(person):
    detail = _object(person.get("location"))
    locality = _text(detail.get("locality")) or _text(detail.get("city")) or _text(person.get("location_locality"))
    region = _text(detail.get("region")) or _text(person.get("location_region")) or _text(person.get("state"))
    country = _text(detail.get("country_code")) or _text(detail.get("country")) or _text(person.get("country_code")) or _text(person.get("location_country"))
    label = (_text(person.get("location_full")) or _text(person.get("location_name")) or _text(person.get("location"))
             or _text(person.get("city")))
    pieces = [part.strip() for part in (label or "").split(",") if part.strip()]
    if pieces and pieces[-1].casefold() in _COUNTRIES:
        country = country or pieces[-1]
        pieces.pop()
    if len(pieces) > 1:
        region = region or pieces[-1]
    locality = locality or (pieces[0] if pieces else None)
    if not any((locality, region, country)):
        return None
    state_code = _STATES.get((region or "").casefold()) or ((region or "").upper() if (region or "").upper() in _STATE_CODES else None)
    country_code = _COUNTRIES.get((country or "").casefold()) or (country.upper() if country and re.fullmatch("[a-zA-Z]{2}", country) else None)
    if not country_code and not country and state_code:
        country_code = "US"
    return Location("unknown", locality, (state_code or region) if country_code == "US" else region, country_code or "ZZ")


def profile_facts(payload):
    person = _person(payload)
    provider = payload["provider"]
    name, given, family = _name(payload, person)
    headline = _text(person.get("headline")) or _text(person.get("position")) or _text(person.get("job_title"))
    positions = [position for job in _items(person.get("experience")) if (position := _position(job, provider))]
    current = _object(person.get("current_company"))
    current_name = (_text(current.get("name")) or _text(person.get("current_company_name"))
                    or _text(person.get("current_company")) or _text(person.get("job_company_name")))
    if current_name:
        target = re.sub(r"[^\w]+", " ", current_name.casefold()).strip()
        known = [index for index, position in enumerate(positions) if position.is_current and (
            re.sub(r"[^\w]+", " ", position.organization_name.casefold()).strip() == target
        )]
        if len(known) == 1 and not any(position.is_primary and position.is_current for position in positions):
            positions[known[0]] = replace(positions[known[0]], is_primary=True)
        elif not known and provider == "brightdata":
            title = _text(current.get("title")) or _text(current.get("position")) or headline
            if title:
                positions.append(Position(current_name, title, seniority(title), None, None, True,
                                          _company_slug(current.get("link") or current.get("url")),
                                          str(current["id"]) if type(current.get("id")) in {int, str} else None, True))
    education = tuple(item for entry in _items(person.get("education")) if (item := _education(entry)))
    return ProfileFacts(name, given, family, headline, tuple(dict.fromkeys(positions)),
                        tuple(dict.fromkeys(education)), _location(person))
