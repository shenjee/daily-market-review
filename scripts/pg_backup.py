#!/usr/bin/env python3
"""Back up or restore the marketreview PostgreSQL schema.

Passwords come only from the process environment. This command does not read
the Supabase Secret Key and does not change the daily SQLite backend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from marketreview.pg_backup import (  # noqa: E402
    DEFAULT_MIGRATIONS,
    PgBackupError,
    connection_from_env,
    create_backup,
    default_backup_root,
    restore_into_blank,
    verify_restore,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份或恢复 marketreview PostgreSQL。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="用 pg_dump 导出应用对象")
    backup.add_argument("--database", required=True)
    backup.add_argument("--dest", type=Path, default=None)
    backup.add_argument("--migrations", type=Path, default=DEFAULT_MIGRATIONS)
    backup.add_argument("--keep-long-term", action="store_true")

    restore = subparsers.add_parser("restore-blank", help="恢复到空白数据库")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database", required=True)
    restore.add_argument("--admin-database", default="postgres")

    verify = subparsers.add_parser("verify", help="核验已恢复的数据库")
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--database", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            path = create_backup(
                connection_from_env(database=args.database),
                args.dest or default_backup_root(),
                migrations_dir=args.migrations,
                retention="migration-snapshot" if args.keep_long_term else "daily",
            )
            sys.stdout.write(f"{path}\n")
            return 0
        conn = connection_from_env(database=args.admin_database if args.command == "restore-blank" else args.database)
        if args.command == "restore-blank":
            restore_into_blank(
                conn,
                args.backup,
                args.database,
                admin_database=args.admin_database,
            )
            sys.stdout.write("恢复完成\n")
            return 0
        summary = verify_restore(conn, args.backup, args.database)
        sys.stdout.write(f"revision={summary['revision']}\n")
        return 0
    except PgBackupError as exc:
        sys.stderr.write(f"{exc.message}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
