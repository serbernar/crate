from __future__ import annotations

import argparse
import sys

from . import db, paths


def cmd_migrate(args: argparse.Namespace) -> int:
    paths.ensure_home()
    conn = db.connect()
    start, end = db.migrate(conn)
    if start == end:
        print(f"schema up to date (version {end})")
    else:
        print(f"schema migrated {start} -> {end}")
    print(f"database: {paths.db_path()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spotify-triage")
    sub = parser.add_subparsers(dest="command", required=True)

    p_migrate = sub.add_parser("migrate", help="create or upgrade the local database")
    p_migrate.set_defaults(func=cmd_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
