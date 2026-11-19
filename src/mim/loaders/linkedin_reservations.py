#GOAL Reuse fresh LinkedIn evidence and prevent concurrent duplicate lookup requests.

"""Reserve a vendor lookup or reuse an existing LinkedIn response."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
from typing import Literal
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus

from .linkedin_input import SOURCE_CODE, canonical_profile_id, normalize_provider, response_status, retrieved_at


DEFAULT_REFRESH_TTL_DAYS = 90
_BRIGHTDATA_SNAPSHOT = re.compile(r"s(?:d|r)?_[A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class LookupReservation:
    status: Literal["reserved", "reuse", "in_flight"]
    raw_record_id: int | None = None
    token: UUID | None = None
    expires_at: datetime | None = None


def _require_idle(conn):
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("LinkedIn lookup reservations require an idle database connection")


def _fresh_evidence(conn, provider, canonical_id, now, ttl, refresh):
    earliest = now - ttl
    candidates = conn.execute(
        """
        SELECT id, payload FROM core.raw_records
        WHERE source_code = %s AND external_id = %s AND observed_on BETWEEN %s AND %s
        """,
        (SOURCE_CODE, f"{provider}:{canonical_id}", earliest.date(), now.date()),
    )
    complete = pending = None
    for raw_id, payload in candidates:
        timestamp = retrieved_at(payload)
        if not earliest <= timestamp <= now:
            continue
        status = response_status(payload)
        candidate = timestamp, raw_id
        if status == "complete" and not refresh and (complete is None or candidate > complete):
            complete = candidate
        elif status == "pending" and provider == "brightdata" and timestamp >= now - timedelta(hours=24):
            raw_payload = payload["raw_payload"]
            snapshot = raw_payload.get("snapshot_id") if isinstance(raw_payload, dict) else None
            if isinstance(snapshot, str) and _BRIGHTDATA_SNAPSHOT.fullmatch(snapshot):
                if pending is None or candidate > pending:
                    pending = candidate
    selected = complete or pending
    return selected[1] if selected else None


def reserve_lookup(
    conn, provider, profile_id, *, refresh=False, refresh_ttl_days=DEFAULT_REFRESH_TTL_DAYS,
    lease_seconds=900, now=None,
) -> LookupReservation:
    """Reserve a paid lookup before enqueueing it, or return the existing evidence ID.

    The caller may enqueue only on ``reserved`` and must start the work before
    ``expires_at``. Pass the token to ``release_lookup`` after importing the
    result or ending a failed request. A reused Bright Data receipt can refer to
    an existing snapshot collection.
    """
    provider = normalize_provider(provider)
    canonical_id = canonical_profile_id(profile_id)
    if type(refresh_ttl_days) is not int or refresh_ttl_days < 0:
        raise ValueError("refresh_ttl_days must be a nonnegative integer")
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    if now is not None and (not isinstance(now, datetime) or now.utcoffset() is None):
        raise ValueError("now must be a timezone-aware datetime")
    _require_idle(conn)

    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{SOURCE_CODE}:lookup:{provider}:{canonical_id}",),
        )
        if conn.execute("SELECT 1 FROM core.sources WHERE code = %s", (SOURCE_CODE,)).fetchone() is None:
            raise ValueError("linkedin is missing from core.sources")
        timestamp = (now or conn.execute("SELECT clock_timestamp()").fetchone()[0]).astimezone(UTC)
        raw_id = _fresh_evidence(conn, provider, canonical_id, timestamp, timedelta(days=refresh_ttl_days), refresh)
        if raw_id is not None:
            return LookupReservation("reuse", raw_record_id=raw_id)

        key = provider, canonical_id
        active = conn.execute(
            """
            SELECT expires_at FROM ingestion.linkedin_lookup_reservations
            WHERE provider = %s AND canonical_profile_id = %s AND expires_at > %s
            """,
            (*key, timestamp),
        ).fetchone()
        if active is not None:
            return LookupReservation("in_flight", expires_at=active[0])

        token = uuid4()
        expires_at = timestamp + timedelta(seconds=lease_seconds)
        conn.execute(
            """
            INSERT INTO ingestion.linkedin_lookup_reservations (provider, canonical_profile_id, token, expires_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (provider, canonical_profile_id) DO UPDATE
            SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at
            """,
            (*key, token, expires_at),
        )
        return LookupReservation("reserved", token=token, expires_at=expires_at)


def release_lookup(conn, provider, profile_id, token) -> bool:
    """Release an import or failed request without affecting another worker's lease."""
    _require_idle(conn)
    key = normalize_provider(provider), canonical_profile_id(profile_id), UUID(str(token))
    with conn.transaction():
        return conn.execute(
            """
            DELETE FROM ingestion.linkedin_lookup_reservations
            WHERE provider = %s AND canonical_profile_id = %s AND token = %s
            """,
            key,
        ).rowcount == 1
