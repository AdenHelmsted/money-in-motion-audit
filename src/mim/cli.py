#GOAL Keep source loads, rebuilds, and audit commands behind one entry point.

"""Commands for local evidence ingestion, rebuild, and review."""

from argparse import ArgumentParser
from dataclasses import asdict
from importlib import import_module
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import UUID

import psycopg

from .loaders.sec_ingestion import LoadError


_SOURCES = ("sec_form345", "sec_form_d", "linkedin", "contact_vendor", "nppes", "cms_open_payments", "dol_form5500")


def _dsn_option(parser):
    parser.add_argument("--dsn", help="PostgreSQL connection string (defaults to MIM_DATABASE_DSN or libpq settings)")


def _loader_options(parser, *, live=False, legacy=False):
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", type=Path, metavar="PATH", help="load a local artifact")
    if legacy:
        mode.add_argument("--rebuild-from-raw", action="store_true", help="replay stored evidence for every source")
    if live:
        mode.add_argument("--live", metavar="SEC_ZIP_URL", help="download one SEC Forms 3/4/5 ZIP using SEC_USER_AGENT")
    parser.add_argument("--archive-dir", type=Path, metavar="PATH", help="artifact archive (defaults to .local/SOURCE)")


def _audit_options(parser):
    parser.add_argument("--sample-size", type=int, choices=range(51), default=50, metavar="N")
    parser.add_argument("--output", type=Path, default=Path(".cache/linkedin_precision_report.json"))


def _connection(args):
    dsn = args.dsn or os.environ.get("MIM_DATABASE_DSN")
    settings = {} if dsn else {"dbname": "money_in_motion"}
    return psycopg.connect(dsn or "", autocommit=True, **settings)


def _run(args):
    source = getattr(args, "source", None)
    fixture = getattr(args, "fixture", None)
    uri = fixture.absolute().as_uri() if fixture else None
    try:
        if args.command == "pilot-sec":
            from .audit.sec_pilot import run_pilot

            result = run_pilot(fixture=fixture, live=args.live, output=args.output)
            print(json.dumps(result, sort_keys=True))
            return 0
        live = getattr(args, "live", None)
        if live is not None:
            if source != "sec_form345":
                raise ValueError("--live applies to the sec_form345 loader")
            from .loaders.sec_form345_cli import _download, _live_options

            agent = _live_options(live)
            uri = live
        with _connection(args) as conn:
            if args.command in {"load", "rebuild"}:
                loader = import_module(f".loaders.{source}", __package__)
                archive = getattr(args, "archive_dir", None) or Path(".local", source)
                if args.command == "rebuild":
                    result = loader.rebuild_from_raw(conn)
                elif live is not None:
                    if conn.execute("SELECT 1 FROM core.sources WHERE code = %s", (source,)).fetchone() is None:
                        raise LoadError(source_code=source, artifact_uri=uri, reason=f"{source} is missing from core.sources")
                    with TemporaryDirectory(prefix="mim-sec-") as directory:
                        downloaded = Path(directory, "quarterly.zip")
                        _download(live, agent, downloaded)
                        result = loader.load_fixture(conn, downloaded, archive_dir=archive)
                else:
                    result = loader.load_fixture(conn, fixture, archive_dir=archive)
            elif args.command == "audit-db":
                from .audit.database import audit_database

                result = audit_database(conn)
                print(json.dumps(result, sort_keys=True))
                return int(result["status"] == "failed")
            elif args.command == "audit-linkedin":
                from .audit.linkedin_sample import _write_report, build_report

                result = build_report(conn, args.sample_size)
                _write_report(result, args.output)
                result = {"sample_denominator": result["sample_denominator"], "output": str(args.output)}
            else:
                from .loaders.linkedin_reservations import release_lookup, reserve_lookup

                if args.command == "reserve-linkedin":
                    result = asdict(reserve_lookup(conn, args.provider, args.profile_id, refresh=args.refresh,
                                                   refresh_ttl_days=args.refresh_ttl_days))
                else:
                    result = {"released": release_lookup(conn, args.provider, args.profile_id, args.token)}
    except LoadError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        return 1
    except (ValueError, OSError, psycopg.Error) as error:
        reason = str(error) if isinstance(error, ValueError) else f"Operation failed ({type(error).__name__})"
        if isinstance(error, psycopg.Error) and error.sqlstate:
            reason += f" (SQLSTATE {error.sqlstate})"
        result = {"source_code": source, "artifact_uri": uri, "reason": reason}
        if source == "contact_vendor":
            result.update(record_index=None, person_mapping=None)
        elif source == "linkedin":
            result.update(record_index=None, profile_id=getattr(args, "profile_id", None))
        elif source:
            result.update(table_name=None, accession_number=None, row_number=None)
        print(json.dumps(result), file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def legacy_load(source, argv=None):
    parser = ArgumentParser(description=f"Load or rebuild {source} evidence.")
    _loader_options(parser, live=source == "sec_form345", legacy=True)
    _dsn_option(parser)
    args = parser.parse_args(argv)
    args.source = source
    args.command = "rebuild" if args.rebuild_from_raw else "load"
    return _run(args)


def legacy_audit(argv=None):
    parser = ArgumentParser(description="Sample accepted LinkedIn matches for manual review.")
    _audit_options(parser)
    _dsn_option(parser)
    parser.set_defaults(command="audit-linkedin", source="linkedin")
    return _run(parser.parse_args(argv))


def main(argv=None):
    parser = ArgumentParser(prog="mim", description="Load and inspect Money in Motion evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("load", help="load a source artifact")
    load.add_argument("source", choices=_SOURCES)
    _loader_options(load, live=True)
    rebuild = commands.add_parser("rebuild", help="restore all source facts from raw records")
    rebuild.set_defaults(source="sec_form345")
    audit = commands.add_parser("audit-linkedin", help="sample accepted profiles for human review")
    audit.set_defaults(source="linkedin")
    _audit_options(audit)
    audit_db = commands.add_parser("audit-db", help="verify database integrity and count source evidence")

    pilot = commands.add_parser("pilot-sec", help="audit one SEC insider artifact in a disposable database")
    pilot.set_defaults(source="sec_form345")
    pilot_input = pilot.add_mutually_exclusive_group(required=True)
    pilot_input.add_argument("--fixture", type=Path, metavar="PATH", help="local SEC insider ZIP or TSV directory")
    pilot_input.add_argument("--live", metavar="SEC_ZIP_URL", help="download one official ZIP with SEC_USER_AGENT")
    pilot.add_argument("--output", type=Path, metavar="NEW_DIR", help="audit package (defaults to .local/sec-pilot)")

    reserve = commands.add_parser("reserve-linkedin", help="reuse cached evidence or reserve a vendor lookup")
    release = commands.add_parser("release-linkedin", help="release a lookup reservation by its token")
    for command in (reserve, release):
        command.set_defaults(source="linkedin")
        command.add_argument("provider")
        command.add_argument("profile_id")
    reserve.add_argument("--refresh", action="store_true", help="refresh completed evidence even within its TTL")
    reserve.add_argument("--refresh-ttl-days", type=int, default=90)
    release.add_argument("token", type=UUID)

    for command in (load, rebuild, audit, audit_db, reserve, release):
        _dsn_option(command)
    return _run(parser.parse_args(argv))
