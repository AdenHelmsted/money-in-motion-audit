#GOAL Sample accepted LinkedIn matches for independent human review.

"""Generate an offline sample for manually checking accepted LinkedIn matches."""

import json
from pathlib import Path
from random import SystemRandom
from tempfile import NamedTemporaryFile

from ..loaders.linkedin_input import (
    SOURCE_CODE, canonical_url, eligibility, normalize_provider, profile_id, retrieved_at,
)


def _records(conn):
    with conn.transaction(), conn.cursor(name="mim_linkedin_audit") as cursor:
        cursor.execute(
            """
            SELECT r.id, r.payload, subject.person_id, p.full_name, subject.method
            FROM core.raw_records r
            LEFT JOIN LATERAL (
                SELECT l.person_id, l.method FROM core.record_links l
                WHERE l.raw_record_id = r.id AND l.role = 'subject' AND l.person_id IS NOT NULL
                ORDER BY l.confidence DESC, l.person_id LIMIT 1
            ) subject ON TRUE
            LEFT JOIN core.people p ON p.id = subject.person_id
            WHERE r.source_code = %s
            """,
            (SOURCE_CODE,),
        )
        yield from cursor


def build_report(conn, sample_size=50, *, rng=None):
    if type(sample_size) is not int or not 0 <= sample_size <= 50:
        raise ValueError("sample_size must be an integer between 0 and 50")

    latest = {}
    withheld = ineligible = 0
    for raw_id, payload, person_id, full_name, method in _records(conn):
        status = eligibility(payload)
        if status != "accepted":
            ineligible += 1
            if status == "withheld_candidate":
                withheld += 1
            continue

        canonical_id = profile_id(payload)
        timestamp = retrieved_at(payload)
        order = timestamp, raw_id
        if canonical_id in latest and order <= latest[canonical_id][0]:
            continue
        latest[canonical_id] = order, {
            "canonical_profile_id": canonical_id,
            "canonical_profile_url": canonical_url(canonical_id),
            "provider": normalize_provider(payload["provider"]),
            "retrieved_at": timestamp.isoformat(),
            "raw_record_id": raw_id,
            "person_id": str(person_id) if person_id is not None else None,
            "person_name": full_name,
            "link_method": method,
            "verified_true_match": None,
            "notes": None,
        }

    candidates = [latest[canonical_id][1] for canonical_id in sorted(latest)]
    sample = (rng or SystemRandom()).sample(candidates, min(sample_size, len(candidates)))
    return {
        "sample_denominator": len(sample),
        "accepted_matches_count": len(candidates),
        "withheld_candidates_count": withheld,
        "ineligible_envelopes_count": ineligible,
        "reviewed_count": None,
        "verified_true_matches": None,
        "manual_precision": None,
        "precision_formula": "verified_true_matches / reviewed_count",
        "sample": sample,
    }


def _write_report(report, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary_path = Path(output.name)
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv=None):
    from mim.cli import legacy_audit

    return legacy_audit(argv)


if __name__ == "__main__":
    raise SystemExit(main())
