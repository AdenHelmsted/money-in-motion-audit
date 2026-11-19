#GOAL Produce deterministic email, phone, and postal matching keys.

"""Normalize vendor contact data without changing the source value."""

import json
import re

import phonenumbers

from . import US_STATE_NAMES


_EMAIL_LOCAL = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*", re.ASCII)
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_PHONE = re.compile(r"\+?[0-9(). \t-]+", re.ASCII)
_E164 = re.compile(r"\+[1-9][0-9]{6,14}", re.ASCII)
_ZIP = r"[0-9]{5}(?:[ -]?[0-9]{4})?"
_US_STATES = frozenset(
    "AK AL AR AZ CA CO CT DC DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MN MO MS MT "
    "NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY AS GU MP PR VI".split()
)
_STATE_OPTIONS = "|".join(re.escape(name.upper()) for name in sorted(US_STATE_NAMES, key=len, reverse=True))
_US_END = re.compile(rf"\b({_STATE_OPTIONS}|[A-Z]{{2}})[ ,]+({_ZIP})(?:[ ,]+(?:UNITED STATES(?: OF AMERICA)?|U\.S\.?|USA|US))?$")
_COUNTRIES = {"USA": "US", "U.S.": "US", "U.S.A.": "US", "UNITED STATES": "US",
              "UNITED STATES OF AMERICA": "US", "CANADA": "CA", "UNITED KINGDOM": "GB", "UK": "GB"}


def normalize_email(value):
    email = _as_text(value, "email").strip().lower()
    local, _, domain = email.partition("@")
    if not _EMAIL_LOCAL.fullmatch(local) or len(local) > 64 or not domain or "@" in domain:
        raise ValueError("invalid email syntax")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid email domain") from exc
    labels = domain.split(".")
    if len(labels) < 2 or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise ValueError("invalid email domain")
    normalized = f"{local}@{domain}"
    if len(normalized) > 254:
        raise ValueError("email exceeds the maximum length")
    return normalized


def normalize_phone(value):
    number = _as_text(value, "phone").strip()
    if not _PHONE.fullmatch(number) or number.count("(") != number.count(")"):
        raise ValueError("invalid phone syntax or extension")
    digits = re.sub(r"[^0-9]", "", number)
    international = number.startswith("+")
    if not international and digits.startswith(("00", "011")):
        digits = digits[3:] if digits.startswith("011") else digits[2:]
        international = True
    if not international and not (len(digits) == 10 or len(digits) == 11 and digits.startswith("1")):
        raise ValueError("phone requires a country prefix or a US 10-digit number")
    candidate = f"+{digits}" if international or len(digits) == 11 else f"+1{digits}"
    if not _E164.fullmatch(candidate):
        raise ValueError("invalid E.164 phone number")
    try:
        parsed = phonenumbers.parse(candidate)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("invalid E.164 phone number") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("invalid E.164 phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _as_text(value, name):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _space(value):
    return " ".join(value.split()).upper()


def _zip(value):
    if not re.fullmatch(_ZIP, value):
        raise ValueError("invalid US ZIP code")
    digits = re.sub(r"[ -]", "", value)
    return digits[:5] + (f"-{digits[5:]}" if len(digits) == 9 else "")


def _field(address, *names):
    for name in names:
        value = address.get(name)
        if value is not None and value != "":
            return _space(_as_text(value, f"address {name}"))
    return ""


def normalize_postal_address(value):
    if isinstance(value, dict):
        street = _field(value, "street", "street_address", "address_line_1", "line1", "address1", "street1")
        if not street:
            text = _field(value, "formatted_address", "formatted", "address")
            if text:
                return normalize_postal_address(text)
            raise ValueError("postal address requires a street address")
        unit = _field(value, "address_line_2", "line2", "address2", "street2", "unit")
        city = _field(value, "city", "locality")
        state = _field(value, "state", "region", "state_code")
        postal = _field(value, "zip", "zip_code", "postal_code", "zipcode")
        country = _field(value, "country_code", "country")
        country = _COUNTRIES.get(country, country)
        if country in {"", "US"}:
            state = US_STATE_NAMES.get(state.lower(), state)
        if country == "US" or not country and state in _US_STATES:
            if state and state not in _US_STATES:
                raise ValueError("invalid US state code")
            postal = _zip(postal) if postal else ""
            country = "US"
        return ", ".join(part for part in (street, unit, city, " ".join(filter(None, (state, postal))), country) if part)

    address = _space(_as_text(value, "postal address"))
    match = _US_END.search(address)
    if match is not None and (state := US_STATE_NAMES.get(match[1].lower(), match[1])) in _US_STATES:
        address = f"{address[:match.start()].rstrip(' ,')}, {state} {_zip(match[2])}, US"
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if not parts:
        raise ValueError("postal address must contain an address")
    if parts[-1] in _COUNTRIES:
        parts[-1] = _COUNTRIES[parts[-1]]
    return ", ".join(parts)


def normalize_label(value):
    token = re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else ""
    if token in {"work", "professional", "current_professional", "business", "office", "corporate", "work_email", "work_phone", "direct_dial"}:
        return "work"
    if token in {"personal", "home", "private", "personal_email", "personal_phone"}:
        return "personal"
    return "unknown"


def normalize_verification(value):
    if value is True:
        return "valid"
    token = re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else ""
    if token in {"valid", "verified", "validated", "deliverable", "confirmed", "safe"}:
        return "valid"
    if token in {"risky", "catch_all", "catchall", "accept_all", "acceptall"}:
        return "risky"
    if token in {"invalid", "undeliverable", "bounced", "hard_bounce", "disconnected"}:
        return "invalid"
    return "unverified"


def original_value(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else value
