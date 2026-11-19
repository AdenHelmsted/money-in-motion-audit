#GOAL Provide isolated PostgreSQL fixtures, replay comparisons, and offline networking checks.

import os
from pathlib import Path
import socket
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from mim.local_postgres import local_database, migrate


ROOT = Path(__file__).resolve().parents[1]
INGESTION_TABLES = {"sources", "fetches", "raw_records"}
_offline = pytest.MonkeyPatch()


def pytest_sessionstart():
    original_socket = socket.socket

    class UnixSocket(original_socket):
        def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
            super().__init__(family, type, proto, fileno)
            if self.family != socket.AF_UNIX:
                self.close()
                raise OSError("Offline tests allow Unix sockets only")

    def deny_lookup(*args, **kwargs):
        raise OSError("Offline tests disable network lookups")

    _offline.setattr(socket, "socket", UnixSocket)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        _offline.setattr(socket, name, deny_lookup)
    for name in os.environ:
        if name.startswith("PG"):
            _offline.delenv(name)


def pytest_sessionfinish():
    _offline.undo()


def core_snapshot(connection):
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'core' ORDER BY tablename"
    ).fetchall()
    return {
        name: connection.execute(
            sql.SQL("SELECT to_jsonb(rows)::text FROM {} AS rows ORDER BY 1").format(sql.Identifier("core", name))
        ).fetchall()
        for (name,) in tables
    }


def archived_path(artifact_uri):
    uri = urlsplit(artifact_uri)
    assert uri.scheme == "file" and not uri.netloc
    return Path(unquote(uri.path))


@pytest.fixture
def gate_db(db):
    database_name = f"mim_gate_{uuid4().hex}"
    database = sql.Identifier(database_name)
    db.execute(sql.SQL("CREATE DATABASE {}").format(database))
    try:
        with psycopg.connect(db.info.dsn, dbname=database_name, autocommit=True) as connection:
            migrate(connection, ROOT / "migrations")
            yield connection
    finally:
        db.execute(sql.SQL("DROP DATABASE {}").format(database))


@pytest.fixture(scope="session")
def db():
    with local_database(ROOT / "migrations") as connection:
        yield connection
