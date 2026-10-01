"""pg_dump backups of the marketreview schema and restore onto a blank database.

The dump covers the application schema, its functions, grants, and the public
marketreview_* wrappers. It does not dump role passwords or platform schemas.
Connection passwords stay in the process environment and are removed from errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .write_gate import same_json_value

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MIGRATIONS = ROOT / "sql" / "migrations"
DEFAULT_CONTRACT = ROOT / "contracts" / "supabase_rpc_v1.json"
DUMP_FLAGS = (
    "--format=plain",
    "--encoding=UTF8",
    "--no-owner",
    "--no-tablespaces",
    "--no-security-labels",
    "--schema=marketreview",
)
INFRA_TABLES = (
    "schema_meta",
    "ledger",
    "group_change_history",
    "sync_commit_result",
)
BUSINESS_TABLES = (
    "daily_market_review",
    "daily_price_limit_event",
    "daily_price_limit_event_detail",
    "daily_price_limit_event_sector",
    "daily_price_limit_event_reason",
)
ALL_TABLES = INFRA_TABLES + BUSINESS_TABLES
SNAPSHOT_TABLES = {
    "reviews": "daily_market_review",
    "events": "daily_price_limit_event",
    "details": "daily_price_limit_event_detail",
    "sectors": "daily_price_limit_event_sector",
    "reasons": "daily_price_limit_event_reason",
    "history": "group_change_history",
    "sync_results": "sync_commit_result",
}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Session pooler 用户形如 postgres.<project_ref>；仍禁止空白、引号与连接串分隔符。
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_HOST = re.compile(r"^[A-Za-z0-9._:-]+$")
_FORBIDDEN_DUMP = re.compile(
    r"(?i)(create\s+role|alter\s+role|password\s+'|pg_authid|auth\.users|"
    r"storage\.objects|vault\.secrets|supabase_migrations)"
)
_COPY_HEADER = re.compile(r"^COPY\s+((?:marketreview\.)?[a-z0-9_]+)\s+\((.*)\)\s+FROM stdin;")
_BIN_CANDIDATES = (
    "postgresql@18",
    "postgresql@17",
    "postgresql@16",
    "postgresql@15",
)
ROLES_SQL = """\
-- 恢复说明：anon、authenticated、service_role 只作为授权目标。
-- postgres 同为 stub：云端 dump 常含 DEFAULT PRIVILEGES FOR ROLE postgres；
-- 本机 Homebrew 等集群超级用户未必叫 postgres，缺少该角色会使保留 ACL 的恢复失败。
-- 本文件创建无登录权限的同名角色，不读取 pg_authid，也不包含密码。
-- 若集群里已有这些角色，则不修改其密码或登录属性。

DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
    CREATE ROLE postgres NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN;
  END IF;
END
$roles$;
"""


class PgBackupError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PgConn:
    host: str
    port: str
    user: str
    database: str
    password: str | None = None

    def env(self, *, database: str | None = None) -> dict[str, str]:
        env = {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGUSER": self.user,
            "PGDATABASE": database or self.database,
            "PGCLIENTENCODING": "UTF8",
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", ""),
        }
        if self.password:
            env["PGPASSWORD"] = self.password
        return env


def default_backup_root() -> Path:
    return Path.home() / ".marketreview" / "backups" / "supabase"


def resolve_pg_bin(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    override = os.environ.get("MARKETREVIEW_PG_BIN")
    if override:
        candidates.append(Path(override))
    found = shutil.which("pg_dump")
    if found:
        candidates.append(Path(found).resolve().parent)
    for prefix in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
        for name in _BIN_CANDIDATES:
            candidates.append(prefix / name / "bin")
    for directory in candidates:
        if (directory / "pg_dump").is_file() and (directory / "psql").is_file():
            return directory
    raise PgBackupError("找不到 pg_dump 和 psql。")


def connection_from_env(*, database: str) -> PgConn:
    password = os.environ.get("PGPASSWORD")
    if password == "":
        password = None
    if password and ("\n" in password or "\r" in password):
        raise PgBackupError("数据库密码不能包含换行。")
    return PgConn(
        host=_validate_host(os.environ.get("PGHOST", "127.0.0.1")),
        port=_validate_port(os.environ.get("PGPORT", "5432")),
        user=_validate_user(os.environ.get("PGUSER", "postgres")),
        database=_validate_ident(database, label="数据库名"),
        password=password,
    )


def redact(text: str, secret: str | None) -> str:
    if not secret or len(secret) < 4 or not text:
        return text
    return text.replace(secret, "[redacted]")


def copy_records(sql: str) -> dict[str, tuple[list[str], list[str]]]:
    records: dict[str, tuple[list[str], list[str]]] = {}
    lines = sql.splitlines()
    index = 0
    while index < len(lines):
        match = _COPY_HEADER.match(lines[index])
        if not match:
            index += 1
            continue
        table = match.group(1).split(".")[-1]
        columns = [part.strip() for part in match.group(2).split(",")]
        index += 1
        rows: list[str] = []
        while index < len(lines) and lines[index] != r"\.":
            rows.append(lines[index])
            index += 1
        records[table] = (columns, rows)
        index += 1
    return records


def validate_dump(sql: str) -> None:
    forbidden = _FORBIDDEN_DUMP.search(sql)
    if forbidden:
        raise PgBackupError(f"备份包含不应导出的内容：{forbidden.group(1)}")
    if "--table" in sql.splitlines()[0:30]:
        raise PgBackupError("备份命令不能只列出五张表。")
    for table in ALL_TABLES:
        if not re.search(rf"CREATE\s+TABLE\s+(marketreview\.)?{table}\b", sql):
            raise PgBackupError(f"备份缺少表结构：{table}")
        if not re.search(rf"^COPY\s+(marketreview\.)?{table}\s+\(", sql, re.M):
            raise PgBackupError(f"备份缺少表数据：{table}")
    for function_name in ("sync_commit", "lock_ledger"):
        if not re.search(rf"CREATE\s+FUNCTION\s+(marketreview\.)?{function_name}\b", sql):
            raise PgBackupError(f"备份缺少函数：{function_name}")
    if "result_payload" not in sql or "GRANT" not in sql or "REVOKE" not in sql:
        raise PgBackupError("备份缺少提交结果或授权语句。")
    if "service_role" not in sql:
        raise PgBackupError("备份缺少 service_role 授权。")


def create_backup(
    conn: PgConn,
    dest_root: Path,
    *,
    migrations_dir: Path = DEFAULT_MIGRATIONS,
    contract_path: Path = DEFAULT_CONTRACT,
    bin_dir: Path | None = None,
    created_at: datetime | None = None,
    retention: str = "daily",
    keep_recent: int = 30,
) -> Path:
    if retention not in {"daily", "migration-snapshot"}:
        raise PgBackupError("retention 只能是 daily 或 migration-snapshot。")
    tools = resolve_pg_bin(bin_dir)
    moment = _utc_second(created_at)
    dest_root.mkdir(parents=True, exist_ok=True)
    final = dest_root / moment.strftime("%Y%m%dT%H%M%SZ")
    if final.exists():
        raise PgBackupError(f"备份目录已存在，未覆盖：{final.name}")
    staging = dest_root / f".partial-{uuid.uuid4().hex}"
    staging.mkdir()
    published = False
    try:
        _fill_backup(
            conn,
            staging,
            tools=tools,
            migrations_dir=migrations_dir,
            contract_path=contract_path,
            created_at=moment,
            retention=retention,
        )
        _verify_checksums(staging)
        if final.exists():
            raise PgBackupError(f"备份目录已存在，未覆盖：{final.name}")
        os.replace(staging, final)
        published = True
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)
    prune_backups(dest_root, keep_recent=keep_recent)
    return final


def restore_into_blank(
    conn: PgConn,
    backup_dir: Path,
    database: str,
    *,
    admin_database: str = "postgres",
    bin_dir: Path | None = None,
) -> None:
    tools = resolve_pg_bin(bin_dir)
    database = _validate_ident(database, label="数据库名")
    admin_database = _validate_ident(admin_database, label="管理库名")
    _verify_checksums(backup_dir)
    if not (backup_dir / "BACKUP_OK").is_file():
        raise PgBackupError("备份未完成校验，不能恢复。")
    exists = psql(
        conn,
        f"SELECT 1 FROM pg_database WHERE datname = '{database}'",
        database=admin_database,
        bin_dir=tools,
    ).strip()
    if not exists:
        psql_script(
            conn,
            f"CREATE DATABASE {database};",
            database=admin_database,
            bin_dir=tools,
        )
    schema_count = psql(
        conn,
        "SELECT count(*) FROM pg_namespace WHERE nspname = 'marketreview'",
        database=database,
        bin_dir=tools,
    ).strip()
    if schema_count != "0":
        raise PgBackupError("目标不是空白库，未恢复。")
    psql_script(conn, (backup_dir / "roles.sql").read_text(encoding="utf-8"), database=database, bin_dir=tools)
    combined = "\n".join(
        [
            (backup_dir / "marketreview.sql").read_text(encoding="utf-8"),
            (backup_dir / "public_functions.sql").read_text(encoding="utf-8"),
        ]
    )
    psql_script(conn, combined, database=database, bin_dir=tools, single_transaction=True)


def verify_restore(
    conn: PgConn,
    backup_dir: Path,
    database: str,
    *,
    bin_dir: Path | None = None,
) -> dict[str, object]:
    tools = resolve_pg_bin(bin_dir)
    database = _validate_ident(database, label="数据库名")
    _verify_checksums(backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    expected_snapshot = json.loads((backup_dir / "snapshot.json").read_text(encoding="utf-8"))
    expected_functions = json.loads((backup_dir / "functions.json").read_text(encoding="utf-8"))
    expected_catalog = json.loads((backup_dir / "catalog.json").read_text(encoding="utf-8"))
    actual_snapshot = _snapshot(conn, database=database, bin_dir=tools)
    # dump/restore 经 float8 文本往返后，jsonb 浮点字面量可能差 1 ULP；
    # 业务核验沿用同步合同的浮点容差，不要求 JSON 文本逐字节相等。
    if not same_json_value(actual_snapshot, expected_snapshot):
        raise PgBackupError("恢复后的快照与备份不一致。")
    if _function_defs(conn, database=database, bin_dir=tools) != expected_functions:
        raise PgBackupError("恢复后的函数与备份不一致。")
    if "columns" not in expected_catalog:
        raise PgBackupError("备份缺少列非空属性清单，请重新导出备份后再核验。")
    if _catalog(conn, database=database, bin_dir=tools) != expected_catalog:
        raise PgBackupError("恢复后的索引、约束或列非空属性与备份不一致。")
    counts = _table_counts(conn, database=database, bin_dir=tools)
    if counts != manifest["row_counts"]:
        raise PgBackupError("恢复后的行数与备份清单不一致。")
    _assert_role_denied(conn, database, "anon", tools)
    _assert_role_denied(conn, database, "authenticated", tools)
    probe = rpc(conn, "marketreview_probe", {"schema_version": 1}, database=database, bin_dir=tools)
    if probe["revision"] != manifest["revision"]:
        raise PgBackupError("恢复后的 revision 与备份不一致。")
    _assert_sync_idempotent(conn, database, actual_snapshot, tools)
    return {
        "revision": manifest["revision"],
        "schema_version": manifest["schema_version"],
        "row_counts": counts,
    }


def prune_backups(dest_root: Path, *, keep_recent: int = 30) -> list[Path]:
    if keep_recent < 1:
        raise PgBackupError("至少保留一份备份。")
    bundles: list[tuple[str, Path]] = []
    if not dest_root.exists():
        return []
    for child in dest_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "BACKUP_OK").is_file() or not (child / "manifest.json").is_file():
            continue
        manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("retention") == "migration-snapshot":
            continue
        created_at = str(manifest.get("created_at", ""))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.+Z", created_at):
            continue
        bundles.append((created_at, child))
    bundles.sort(key=lambda item: (item[0], item[1].name))
    month_keepers: dict[str, Path] = {}
    for created_at, path in bundles:
        month_keepers[created_at[:7]] = path
    newest = {path for _, path in bundles[-keep_recent:]}
    keep = newest | set(month_keepers.values())
    removed: list[Path] = []
    for _, path in bundles:
        if path not in keep:
            shutil.rmtree(path)
            removed.append(path)
    return removed


def dump_marketreview_sql(conn: PgConn, dest: Path, *, database: str, bin_dir: Path | None = None) -> None:
    tools = resolve_pg_bin(bin_dir)
    _run_dump(conn, dest, database=database, bin_dir=tools)


def psql(conn: PgConn, sql: str, *, database: str | None = None, bin_dir: Path) -> str:
    return _run(
        [
            str(bin_dir / "psql"),
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            database or conn.database,
        ],
        conn.env(database=database),
        input_text=sql,
        password=conn.password,
    )


def psql_script(
    conn: PgConn,
    sql: str,
    *,
    database: str,
    bin_dir: Path,
    single_transaction: bool = False,
) -> None:
    command = [
        str(bin_dir / "psql"),
        "-X",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        database,
    ]
    if single_transaction:
        command.append("--single-transaction")
    _run(command, conn.env(database=database), input_text=sql, password=conn.password)


def rpc(
    conn: PgConn,
    name: str,
    payload: dict,
    *,
    database: str,
    bin_dir: Path,
    role: str = "service_role",
) -> dict:
    if not re.fullmatch(r"marketreview_[a-z0-9_]+", name):
        raise PgBackupError("函数名不合法。")
    if role not in {"service_role", "anon", "authenticated"}:
        raise PgBackupError("角色不合法。")
    text = psql(
        conn,
        f"SET ROLE {role};\nSELECT public.{name}({_json_literal(payload)});",
        database=database,
        bin_dir=bin_dir,
    )
    value = json.loads(text.strip())
    if not isinstance(value, dict):
        raise PgBackupError("函数返回的不是对象。")
    return value


def rpc_error(
    conn: PgConn,
    name: str,
    payload: dict,
    *,
    database: str,
    bin_dir: Path,
    role: str = "service_role",
) -> str:
    try:
        rpc(conn, name, payload, database=database, bin_dir=bin_dir, role=role)
    except PgBackupError as exc:
        return str(exc)
    raise PgBackupError("应当拒绝的调用成功了。")


def _fill_backup(
    conn: PgConn,
    staging: Path,
    *,
    tools: Path,
    migrations_dir: Path,
    contract_path: Path,
    created_at: datetime,
    retention: str,
) -> None:
    before = _snapshot(conn, database=conn.database, bin_dir=tools)
    functions = _function_defs(conn, database=conn.database, bin_dir=tools)
    catalog = _catalog(conn, database=conn.database, bin_dir=tools)
    dump_path = staging / "marketreview.sql"
    _run_dump(conn, dump_path, database=conn.database, bin_dir=tools)
    after = _snapshot(conn, database=conn.database, bin_dir=tools)
    if before != after:
        raise PgBackupError("备份期间数据发生变化，未发布备份。")
    dump_sql = dump_path.read_text(encoding="utf-8")
    validate_dump(dump_sql)
    records = copy_records(dump_sql)
    row_counts = {table: len(rows) for table, (_, rows) in records.items()}
    for table in ALL_TABLES:
        if table not in row_counts:
            raise PgBackupError(f"备份缺少表数据：{table}")
    schema_version = _single_int(records, "schema_meta", "schema_version")
    revision = _single_int(records, "ledger", "revision")
    if before["schema_version"] != schema_version or before["revision"] != revision:
        raise PgBackupError("备份快照与导出的版本不一致。")
    for key, table in SNAPSHOT_TABLES.items():
        if before["counts"][key] != row_counts[table]:
            raise PgBackupError(f"备份行数与快照不一致：{table}")
    public_names = _write_public_functions(staging / "public_functions.sql", functions, contract_path)
    (staging / "roles.sql").write_text(ROLES_SQL, encoding="utf-8")
    if re.search(r"(?i)password\s+'", ROLES_SQL):
        raise PgBackupError("角色说明包含密码。")
    copied = _copy_migrations(migrations_dir, staging / "migrations")
    contract_relative = _copy_contract(contract_path, staging / "contracts")
    (staging / "snapshot.json").write_text(_pretty(before), encoding="utf-8")
    (staging / "functions.json").write_text(_pretty(functions), encoding="utf-8")
    (staging / "catalog.json").write_text(_pretty(catalog), encoding="utf-8")
    content_hashes = {
        path.relative_to(staging).as_posix(): _sha256(path)
        for path in _files(staging)
    }
    manifest = {
        "format_version": 1,
        "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retention": retention,
        "schema_version": schema_version,
        "revision": revision,
        "database": conn.database,
        "host": conn.host,
        "port": str(conn.port),
        "row_counts": {table: row_counts[table] for table in ALL_TABLES},
        "pg_dump_flags": list(DUMP_FLAGS),
        "public_functions": public_names,
        "migrations": copied,
        "contract": contract_relative,
        "files": content_hashes,
    }
    (staging / "manifest.json").write_text(_pretty(manifest), encoding="utf-8")
    (staging / "BACKUP_OK").write_text("ok\n", encoding="utf-8")
    _write_checksums(staging)


def _write_public_functions(path: Path, functions: dict[str, str], contract_path: Path) -> list[str]:
    expected = _contract_functions(contract_path)
    present = sorted(name for name in functions if name.startswith("public.marketreview_"))
    missing = [name for name in expected if f"public.{name}" not in functions]
    if missing:
        raise PgBackupError("备份缺少公共函数：" + ", ".join(missing))
    blocks = [
        "-- 仅 public.marketreview_* 包装函数及其授权。不含角色密码。",
        "",
    ]
    for qualified in present:
        body = functions[qualified].strip()
        if not body.endswith(";"):
            body += ";"
        short = qualified.split(".", 1)[1]
        if not re.fullmatch(r"marketreview_[a-z0-9_]+", short):
            raise PgBackupError("公共函数名不合法。")
        blocks.append(body)
        blocks.append(
            f"REVOKE ALL ON FUNCTION public.{short}(jsonb) FROM PUBLIC, anon, authenticated;"
        )
        blocks.append(f"GRANT EXECUTE ON FUNCTION public.{short}(jsonb) TO service_role;")
        blocks.append("")
    text = "\n".join(blocks)
    if _FORBIDDEN_DUMP.search(text):
        raise PgBackupError("公共函数脚本包含不应导出的内容。")
    path.write_text(text, encoding="utf-8")
    return [name.split(".", 1)[1] for name in present]


def _contract_functions(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = payload.get("functions")
    if not isinstance(names, list) or not names:
        raise PgBackupError("函数合同不可用。")
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"marketreview_[a-z0-9_]+", name):
            raise PgBackupError("函数合同中的名称不合法。")
    return names


def _copy_migrations(source: Path, dest: Path) -> list[str]:
    files = sorted(source.glob("*.sql"))
    if not files:
        raise PgBackupError("找不到迁移文件。")
    dest.mkdir()
    names: list[str] = []
    for path in files:
        target = dest / path.name
        target.write_bytes(path.read_bytes())
        names.append(path.name)
    return names


def _copy_contract(source: Path, dest_dir: Path) -> str:
    if not source.is_file():
        raise PgBackupError("找不到函数合同文件。")
    # 先解析，确保写入包内的是可用合同，而不是任意同名文件。
    _contract_functions(source)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / source.name
    target.write_bytes(source.read_bytes())
    return f"contracts/{source.name}"


def _snapshot(conn: PgConn, *, database: str, bin_dir: Path) -> dict:
    return rpc(
        conn,
        "marketreview_sync_snapshot",
        {"schema_version": 1},
        database=database,
        bin_dir=bin_dir,
    )


def _function_defs(conn: PgConn, *, database: str, bin_dir: Path) -> dict[str, str]:
    rows = _lines(
        conn,
        """
        SELECT n.nspname || '.' || p.proname || '|' ||
               encode(convert_to(pg_get_functiondef(p.oid), 'UTF8'), 'hex')
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'marketreview'
           OR (n.nspname = 'public' AND p.proname ~ '^marketreview_[a-z0-9_]+$')
        ORDER BY 1
        """,
        database=database,
        bin_dir=bin_dir,
    )
    functions: dict[str, str] = {}
    for row in rows:
        qualified, _, blob = row.partition("|")
        if not qualified or not re.fullmatch(r"[0-9a-fA-F]+", blob):
            raise PgBackupError("函数定义读取失败。")
        functions[qualified] = bytes.fromhex(blob).decode("utf-8")
    return functions


def _catalog(conn: PgConn, *, database: str, bin_dir: Path) -> dict[str, list[str]]:
    indexes = _lines(
        conn,
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'marketreview' ORDER BY 1",
        database=database,
        bin_dir=bin_dir,
    )
    constraints = _lines(
        conn,
        """
        SELECT con.conname
        FROM pg_constraint AS con
        JOIN pg_namespace AS nsp ON nsp.oid = con.connamespace
        WHERE nsp.nspname = 'marketreview'
          AND con.contype <> 'n'
        ORDER BY 1
        """,
        database=database,
        bin_dir=bin_dir,
    )
    # PG18 把 NOT NULL 记成 contype='n' 的约束名；跨大版本核验改比 attnotnull。
    columns = _lines(
        conn,
        """
        SELECT c.relname || '.' || a.attname || '=' ||
               CASE WHEN a.attnotnull THEN 'not_null' ELSE 'nullable' END
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'marketreview'
          AND c.relkind = 'r'
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY 1
        """,
        database=database,
        bin_dir=bin_dir,
    )
    return {"indexes": indexes, "constraints": constraints, "columns": columns}


def _table_counts(conn: PgConn, *, database: str, bin_dir: Path) -> dict[str, int]:
    unions = " UNION ALL ".join(
        f"SELECT '{table}' AS table_name, count(*)::text FROM marketreview.{table}"
        for table in ALL_TABLES
    )
    rows = _lines(conn, f"SELECT table_name || '|' || cnt FROM ({unions}) AS counts(table_name, cnt) ORDER BY 1", database=database, bin_dir=bin_dir)
    counts: dict[str, int] = {}
    for row in rows:
        table, _, raw = row.partition("|")
        counts[table] = int(raw)
    return {table: counts[table] for table in ALL_TABLES}


def _assert_role_denied(conn: PgConn, database: str, role: str, bin_dir: Path) -> None:
    if role not in {"anon", "authenticated"}:
        raise PgBackupError("角色不合法。")
    for table in ALL_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            if _table_privilege(conn, database, role, table, privilege, bin_dir):
                raise PgBackupError(f"{role} 仍有 marketreview.{table} 的 {privilege} 权限。")
    for name in _contract_functions(DEFAULT_CONTRACT):
        if _function_privilege(conn, database, role, name, bin_dir):
            raise PgBackupError(f"{role} 仍能执行 public.{name}。")


def _table_privilege(
    conn: PgConn,
    database: str,
    role: str,
    table: str,
    privilege: str,
    bin_dir: Path,
) -> bool:
    if not _IDENT.fullmatch(table):
        raise PgBackupError("表名不合法。")
    if privilege not in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
        raise PgBackupError("权限名不合法。")
    text = psql(
        conn,
        f"SELECT has_table_privilege('{role}', 'marketreview.{table}', '{privilege}')",
        database=database,
        bin_dir=bin_dir,
    ).strip()
    return text.lower() in {"t", "true"}


def _function_privilege(
    conn: PgConn,
    database: str,
    role: str,
    name: str,
    bin_dir: Path,
) -> bool:
    if not re.fullmatch(r"marketreview_[a-z0-9_]+", name):
        raise PgBackupError("函数名不合法。")
    text = psql(
        conn,
        f"SELECT has_function_privilege('{role}', 'public.{name}(jsonb)', 'EXECUTE')",
        database=database,
        bin_dir=bin_dir,
    ).strip()
    return text.lower() in {"t", "true"}


def _assert_sync_idempotent(conn: PgConn, database: str, snapshot: dict, bin_dir: Path) -> None:
    results = snapshot.get("sync_results")
    if not isinstance(results, list) or not results:
        return
    before = _ledger_revision(conn, database, bin_dir)
    for stored in results:
        payload = stored.get("result_payload")
        if not isinstance(payload, dict) or "groups" not in payload:
            raise PgBackupError("提交结果不是完整内容。")
        replay = rpc(
            conn,
            "marketreview_sync_commit",
            {
                "schema_version": 1,
                "operation_id": stored["operation_id"],
                "project_id": stored["project_id"],
                "ledger_id": stored["ledger_id"],
                "request_digest": stored["request_digest"],
                "expected_revision": 0,
            },
            database=database,
            bin_dir=bin_dir,
        )
        if replay != payload:
            raise PgBackupError("同一提交的重放结果与完整 result_payload 不一致。")
        mismatch = rpc_error(
            conn,
            "marketreview_sync_commit",
            {
                "schema_version": 1,
                "operation_id": stored["operation_id"],
                "project_id": stored["project_id"],
                "ledger_id": stored["ledger_id"],
                "request_digest": "digest-mismatch",
                "expected_revision": before,
            },
            database=database,
            bin_dir=bin_dir,
        )
        if "OPERATION_DIGEST_MISMATCH" not in mismatch:
            raise PgBackupError("摘要不一致时没有拒绝。")
    if _ledger_revision(conn, database, bin_dir) != before:
        raise PgBackupError("幂等核验改变了 revision。")


def _ledger_revision(conn: PgConn, database: str, bin_dir: Path) -> int:
    text = psql(
        conn,
        "SELECT revision FROM marketreview.ledger WHERE ledger_key = 'main'",
        database=database,
        bin_dir=bin_dir,
    ).strip()
    return int(text)


def _run_dump(conn: PgConn, dest: Path, *, database: str, bin_dir: Path) -> None:
    if "--table" in DUMP_FLAGS:
        raise PgBackupError("备份不能只列出五张表。")
    command = [str(bin_dir / "pg_dump"), *DUMP_FLAGS, "--file", str(dest)]
    _run(command, conn.env(database=database), password=conn.password)


def _single_int(records: dict[str, tuple[list[str], list[str]]], table: str, column: str) -> int:
    columns, rows = records[table]
    if len(rows) != 1 or column not in columns:
        raise PgBackupError(f"无法从备份读取 {table}.{column}。")
    return int(rows[0].split("\t")[columns.index(column)])


def _lines(conn: PgConn, sql: str, *, database: str, bin_dir: Path) -> list[str]:
    text = psql(conn, sql, database=database, bin_dir=bin_dir)
    return [line for line in text.splitlines() if line]


def _files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.name != "CHECKSUMS"),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _write_checksums(root: Path) -> None:
    lines = [
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in _files(root)
    ]
    (root / "CHECKSUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify_checksums(root: Path) -> None:
    listing = (root / "CHECKSUMS").read_text(encoding="utf-8")
    seen: dict[str, str] = {}
    for line in listing.splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PgBackupError("校验清单格式不正确。")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise PgBackupError("校验清单包含非法路径。")
        path = root / relative
        if not path.is_file() or _sha256(path) != digest:
            raise PgBackupError(f"校验失败：{relative}")
        seen[relative] = digest
    required = {
        "marketreview.sql",
        "public_functions.sql",
        "roles.sql",
        "snapshot.json",
        "functions.json",
        "catalog.json",
        "manifest.json",
        "BACKUP_OK",
        "contracts/supabase_rpc_v1.json",
    }
    missing = required - set(seen)
    if missing:
        raise PgBackupError("校验清单缺少文件：" + ", ".join(sorted(missing)))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    contract = manifest.get("contract")
    if contract != "contracts/supabase_rpc_v1.json":
        raise PgBackupError("备份清单缺少合同副本路径。")
    if contract not in seen:
        raise PgBackupError(f"校验清单缺少合同副本：{contract}")
    for relative, digest in manifest["files"].items():
        if seen.get(relative) != digest:
            raise PgBackupError(f"清单哈希不一致：{relative}")
    # 包内合同必须能解析出与 public wrappers 一致的函数名集合。
    bundled = _contract_functions(root / contract)
    if sorted(manifest.get("public_functions") or []) != sorted(bundled):
        raise PgBackupError("备份合同副本与 public wrappers 清单不一致。")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _json_literal(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if "$mrjson$" in text:
        raise PgBackupError("请求内容无法安全发送。")
    return f"$mrjson${text}$mrjson$::jsonb"


def _utc_second(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise PgBackupError("备份时间必须带时区。")
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _validate_ident(value: str, *, label: str) -> str:
    if not _IDENT.fullmatch(value):
        raise PgBackupError(f"{label}不合法。")
    return value


def _validate_user(value: str) -> str:
    if not _USER.fullmatch(value) or "://" in value or "@" in value or "/" in value:
        raise PgBackupError("数据库用户不合法。")
    return value


def _validate_host(value: str) -> str:
    if not _HOST.fullmatch(value) or "://" in value or "@" in value:
        raise PgBackupError("数据库主机名不合法。")
    return value


def _validate_port(value: str) -> str:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise PgBackupError("数据库端口不合法。")
    return value


def _run(command: list[str], env: dict[str, str], *, input_text: str | None = None, password: str | None) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        detail = redact(f"{completed.stderr}\n{completed.stdout}", password).strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        program = Path(command[0]).name
        raise PgBackupError(f"{program} 失败：{detail or '无输出'}")
    return completed.stdout
