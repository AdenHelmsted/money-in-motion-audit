#GOAL Start a disposable PostgreSQL database pinned to a private Unix socket.

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import psycopg
from psycopg import sql


def _server_tools():
    env = {name: value for name, value in os.environ.items() if not name.startswith("PG")}
    if configured := env.get("MIM_POSTGRES_BIN"):
        return Path(configured).resolve(), env
    if pg_ctl := shutil.which("pg_ctl"):
        return Path(pg_ctl).resolve().parent, env
    if pg_config := shutil.which("pg_config"):
        location = Path(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
        if (location / "initdb").is_file():
            return location, env
    cached = Path.home() / ".cache/money-in-motion/pg18-ubuntu/root/usr/lib"
    location = cached / "postgresql/18/bin"
    if (location / "initdb").is_file():
        libraries = str(cached / "x86_64-linux-gnu")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, (libraries, env.get("LD_LIBRARY_PATH"))))
        return location, env
    raise ValueError("Install PostgreSQL 16+ with its server extensions or set MIM_POSTGRES_BIN")


def _run(args, env, *, timeout=45):
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{Path(args[0]).name} timed out") from exc
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"{Path(args[0]).name} failed: {details}")


def _connect(socket_dir, database):
    # libpq otherwise consults PGHOSTADDR even when a Unix socket was supplied.
    previous = {name: value for name, value in os.environ.items() if name.startswith("PG")}
    try:
        for name in previous:
            del os.environ[name]
        return psycopg.connect(host=str(socket_dir), port=5432, dbname=database, user="mim_local",
                               connect_timeout=5, autocommit=True, options="-c timezone=UTC")
    finally:
        os.environ.update(previous)


def migrate(conn, migrations):
    with conn.transaction():
        for path in sorted(migrations.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))


@contextmanager
def local_database(migrations, *, database="postgres"):
    bin_dir, env = _server_tools()
    initdb, pg_ctl = bin_dir / "initdb", bin_dir / "pg_ctl"
    if not initdb.is_file() or not pg_ctl.is_file():
        raise ValueError(f"PostgreSQL server binaries are missing from {bin_dir}")
    if not (migrations / "001_core_schema.sql").is_file():
        raise ValueError(f"Money in Motion migrations are missing from {migrations}")

    with TemporaryDirectory(prefix="mim-", dir="/tmp") as temporary:
        socket_dir = Path(temporary)
        data = socket_dir / "data"
        log = socket_dir / "postgres.log"
        _run([initdb, "-D", data, "--auth=trust", "--no-locale", "--encoding=UTF8", "-U", "mim_local"], env)
        try:
            try:
                _run([pg_ctl, "-D", data, "-l", log, "-w", "-t", "25", "start", "-o",
                      f"-c listen_addresses='' -k {socket_dir} -p 5432"], env)
            except ValueError as exc:
                details = log.read_text() if log.exists() else "Server log unavailable"
                raise ValueError(f"{exc}\n{details}") from exc
            if database != "postgres":
                with _connect(socket_dir, "postgres") as admin:
                    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
            with _connect(socket_dir, database) as conn:
                if conn.info.server_version < 160000:
                    raise ValueError("PostgreSQL 16+ is required")
                migrate(conn, migrations)
                yield conn
        finally:
            if (data / "postmaster.pid").is_file():
                _run([pg_ctl, "-D", data, "-w", "-t", "25", "stop", "-m", "fast"], env)
