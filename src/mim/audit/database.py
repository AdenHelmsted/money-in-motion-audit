#GOAL Verify provenance, contact dates, and merge integrity across all registered sources.

"""Read-only structural audit of the Money in Motion database."""

from psycopg import sql

from ..loaders.sec_ingestion import _INGESTION_LOCK


def integrity_violations(conn):
    rows = conn.execute("""
        SELECT
            (SELECT count(*) FROM core.raw_records r LEFT JOIN core.fetches f ON f.id = r.fetch_id
                WHERE f.id IS NULL OR r.source_code <> f.source_code OR f.status <> 'succeeded') AS raw_with_invalid_fetch,
            (SELECT count(*) FROM core.people p WHERE NOT EXISTS (
                SELECT 1 FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
                WHERE l.person_id = p.id)) AS people_without_raw_link,
            (SELECT count(*) FROM core.organizations o WHERE NOT EXISTS (
                SELECT 1 FROM core.record_links l JOIN core.raw_records r ON r.id = l.raw_record_id
                WHERE l.organization_id = o.id)) AS organizations_without_raw_link,
            (SELECT count(*) FROM core.signals s WHERE NOT EXISTS (
                SELECT 1 FROM core.signal_evidence e JOIN core.raw_records r ON r.id = e.raw_record_id
                WHERE e.signal_id = s.id)) AS signals_without_evidence,
            (SELECT count(*) FROM core.contact_points
                WHERE (verification = 'unverified') <> (verified_at IS NULL)
                    OR (verified_at AT TIME ZONE 'UTC')::date > last_seen_on
                    OR NOT isfinite(verified_at)
                    OR last_seen_on < first_seen_on) AS invalid_contact_dates
        """)
    violations = dict(zip((field.name for field in rows.description), rows.fetchone(), strict=True))
    for (name,) in conn.execute("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema = 'core' AND column_name = 'raw_record_id' ORDER BY table_name
        """):
        violations[f"{name}_without_raw"] = conn.execute(sql.SQL("""
            SELECT count(*) FROM {} d LEFT JOIN core.raw_records r ON r.id = d.raw_record_id WHERE r.id IS NULL
        """).format(sql.Identifier("core", name))).fetchone()[0]
    for name in ("people", "organizations"):
        table = sql.Identifier("core", name)
        violations[f"{name}_reaching_merge_cycles"] = conn.execute(sql.SQL("""
            WITH RECURSIVE chain(origin, id, target) AS (
                SELECT id, id, merged_into_id FROM {table} WHERE merged_into_id IS NOT NULL
                UNION ALL
                SELECT c.origin, t.id, t.merged_into_id
                FROM chain c JOIN {table} t ON t.id = c.target
            ) CYCLE id SET is_cycle USING path
            SELECT count(*) FROM chain WHERE is_cycle
        """).format(table=table)).fetchone()[0]
    return violations


def audit_database(conn):
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY")
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (_INGESTION_LOCK,))
        violations = integrity_violations(conn)
        tables = {
            name: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("core", name))).fetchone()[0]
            for (name,) in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'core' ORDER BY tablename")
        }
        counts = conn.execute("""
            WITH fetch_counts AS (
                SELECT source_code, count(*) AS fetches FROM core.fetches GROUP BY source_code
            ), raw_counts AS (
                SELECT r.source_code, count(*) AS records,
                    count(*) FILTER (WHERE NOT EXISTS (
                        SELECT 1 FROM core.record_links l WHERE l.raw_record_id = r.id
                    )) AS unlinked_raw_records
                FROM core.raw_records r GROUP BY r.source_code
            )
            SELECT s.code, COALESCE(f.fetches, 0), COALESCE(r.records, 0), COALESCE(r.unlinked_raw_records, 0)
            FROM core.sources s LEFT JOIN fetch_counts f ON f.source_code = s.code
            LEFT JOIN raw_counts r ON r.source_code = s.code ORDER BY s.code
        """)
        sources = {
            source: {"fetches": fetches, "raw_records": records, "unlinked_raw_records": unlinked}
            for source, fetches, records, unlinked in counts
        }
        signal_kinds = dict(conn.execute("SELECT kind, count(*) FROM core.signals GROUP BY kind"))
    return {"status": "failed" if any(violations.values()) else "passed", "violations": violations,
            "tables": tables, "sources": sources, "signal_kinds": signal_kinds}
