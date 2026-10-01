"""Backend selection and local Supabase settings.

Priority is explicit ``--backend``, then ``backend`` in the local config file,
then the process default. The contract default is ``supabase``. The daily CLI
keeps ``sqlite`` until the formal switch flips ``CLOUD_DEFAULT_ENABLED``.
``MARKETREVIEW_HOME`` never selects a backend.

Config lives in ``~/.marketreview/config`` (``backend``, ``supabase_url``,
optional ``supabase_publishable_key``). The Secret Key lives only in
``~/.marketreview/supabase.secret``. If that file is absent,
``SUPABASE_SECRET_KEY`` inside ``~/.marketreview/supabase.config`` may be used.
If the secret file exists but is empty or otherwise invalid, loading fails with
``CONFIG_MISSING`` and does not fall back to the legacy file. Repository
templates under ``config/*.example`` are copied by the user only when the
target is missing; they never ship filled credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import BackendSelectionError

CONTRACT_DEFAULT_BACKEND = "supabase"
# Issue #10 turns this on after backup, merge, and acceptance. Until then the
# daily CLI stays on SQLite so an unmigrated project cannot interrupt reviews.
CLOUD_DEFAULT_ENABLED = False
VALID_BACKENDS = frozenset({"sqlite", "supabase"})
CONFIG_FILENAME = "config"
SECRET_FILENAME = "supabase.secret"
LEGACY_CREDENTIALS_FILENAME = "supabase.config"
KNOWN_CONFIG_KEYS = frozenset(
    {
        "backend",
        "supabase_url",
        "supabase_secret_key",
        "supabase_publishable_key",
    }
)


def effective_default_backend() -> str:
    if CLOUD_DEFAULT_ENABLED:
        return CONTRACT_DEFAULT_BACKEND
    return "sqlite"


def default_config_dir() -> Path:
    return Path.home() / ".marketreview"


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    secret_key: str
    publishable_key: str | None = None


@dataclass(frozen=True)
class LocalConfig:
    backend: str | None
    supabase_url: str | None
    supabase_publishable_key: str | None = None


def resolve_backend_name(
    *,
    explicit: str | None,
    configured: str | None,
    default: str,
) -> str:
    if explicit is not None:
        raw = explicit
    elif configured is not None:
        raw = configured
    else:
        raw = default
    name = raw.strip()
    if name not in VALID_BACKENDS:
        raise BackendSelectionError(f"backend 只能是 sqlite 或 supabase：{name!r}")
    return name


def reject_sqlite_path_on_supabase(backend: str, db_path: str | None) -> None:
    if backend == "supabase" and db_path is not None:
        raise BackendSelectionError(
            "--db 只指定本地 SQLite 文件；当前后端是 supabase，"
            "已在打开数据库和请求网络前停止。",
            code="BACKEND_CONFLICT",
        )


def load_local_config(config_dir: Path) -> LocalConfig:
    path = config_dir / CONFIG_FILENAME
    if not path.exists():
        return LocalConfig(backend=None, supabase_url=None, supabase_publishable_key=None)
    values = _read_assignments(path)
    backend = values.get("backend")
    url = values.get("supabase_url")
    publishable = values.get("supabase_publishable_key")
    return LocalConfig(
        backend=None if backend is None else backend,
        supabase_url=None if url is None else url,
        supabase_publishable_key=None if publishable is None else publishable,
    )


def load_supabase_settings(config_dir: Path) -> SupabaseSettings:
    local = load_local_config(config_dir)
    legacy = _read_assignments(config_dir / LEGACY_CREDENTIALS_FILENAME)
    url = local.supabase_url or legacy.get("supabase_url")
    # Only fall back to the legacy file when supabase.secret is absent.
    secret = _read_secret(config_dir)
    if secret is None:
        secret = legacy.get("supabase_secret_key")
    publishable = local.supabase_publishable_key or legacy.get("supabase_publishable_key")
    if not url or not secret:
        raise BackendSelectionError(
            "云端后端缺少项目 URL 或 Secret Key，已停止，不会改用本地数据库。",
            code="CONFIG_MISSING",
        )
    if not url.startswith("https://"):
        raise BackendSelectionError(
            "Supabase 项目 URL 必须是 https 地址。",
            code="CONFIG_MISSING",
        )
    return SupabaseSettings(
        url=url.rstrip("/"),
        secret_key=secret,
        publishable_key=publishable,
    )


def redact_secret(text: str, secret: str | None) -> str:
    if not secret or not text:
        return text
    return text.replace(secret, "[redacted]")


def _read_secret(config_dir: Path) -> str | None:
    path = config_dir / SECRET_FILENAME
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text or "\n" in text:
        raise BackendSelectionError(
            "Secret Key 文件必须是单行文本。",
            code="CONFIG_MISSING",
        )
    if "=" in text:
        values = _parse_assignment_text(text)
        secret = values.get("supabase_secret_key")
        if not secret:
            raise BackendSelectionError(
                "Secret Key 文件存在但没有有效密钥，已停止，不会改用旧配置。",
                code="CONFIG_MISSING",
            )
        return secret
    return text


def _read_assignments(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BackendSelectionError(
            f"无法读取本机配置：{path.name}: {exc.strerror or exc}",
            code="CONFIG_MISSING",
        ) from exc
    return _parse_assignment_text(text)


def _parse_assignment_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            raise BackendSelectionError(
                "本机配置的每一行都必须是 key=value。",
                code="CONFIG_MISSING",
            )
        key, value = stripped.split("=", 1)
        normalized = key.strip().lower()
        parsed = value.strip().strip('"').strip("'")
        if normalized in KNOWN_CONFIG_KEYS:
            values[normalized] = parsed
    return values


def config_dir_from_env(default: Path | None = None) -> Path:
    override = os.environ.get("MARKETREVIEW_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return default if default is not None else default_config_dir()
