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
``CONFIG_MISSING`` and does not fall back to the legacy file.

When cloud settings are loaded and a target file is still missing, the Skill
``config/*.example`` templates are copied automatically (never overwriting).
Placeholder values are rejected with fill-in instructions. Templates never
ship filled credentials.
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
CONFIG_EXAMPLE_NAME = "marketreview.config.example"
SECRET_EXAMPLE_NAME = "supabase.secret.example"
PLACEHOLDER_MARKERS = ("REPLACE_ME", "PROJECT_REF")
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


def default_skill_root() -> Path:
    override = os.environ.get("MARKETREVIEW_SKILL_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # scripts/marketreview/backend.py → Skill / 仓库根目录
    return Path(__file__).resolve().parents[2]


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


def ensure_cloud_config_templates(
    config_dir: Path,
    *,
    skill_root: Path | None = None,
) -> list[str]:
    """Copy missing cloud config templates. Never overwrite existing files.

    Uses ``O_CREAT|O_EXCL`` so a concurrent creator wins and this process skips.
    Secret files are created with mode ``0o600``; permission failures raise.

    Skips creating a new ``config`` / ``supabase.secret`` when the legacy
    ``supabase.config`` already supplies a usable URL or secret, so placeholders
    cannot shadow working credentials.
    """
    root = skill_root if skill_root is not None else default_skill_root()
    examples = root / "config"
    config_example = examples / CONFIG_EXAMPLE_NAME
    secret_example = examples / SECRET_EXAMPLE_NAME
    if not config_example.is_file() or not secret_example.is_file():
        raise BackendSelectionError(
            "Skill 安装目录缺少云端配置模板（config/*.example），无法自动创建本机配置。",
            code="CONFIG_MISSING",
        )

    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    legacy = _read_assignments(config_dir / LEGACY_CREDENTIALS_FILENAME)
    created: list[str] = []

    config_path = config_dir / CONFIG_FILENAME
    if _is_placeholder(legacy.get("supabase_url")):
        if _write_new_file_exclusive(
            config_path,
            config_example.read_bytes(),
            mode=0o644,
        ):
            created.append(str(config_path))

    secret_path = config_dir / SECRET_FILENAME
    if _is_placeholder(legacy.get("supabase_secret_key")):
        if _write_new_file_exclusive(
            secret_path,
            secret_example.read_bytes(),
            mode=0o600,
        ):
            created.append(str(secret_path))

    return created


def _write_new_file_exclusive(path: Path, content: bytes, *, mode: int) -> bool:
    """Atomically create ``path`` with ``mode``. Return False if it already exists."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        return False
    except OSError as exc:
        raise BackendSelectionError(
            f"无法创建本机配置文件 {path.name}：{exc.strerror or exc}",
            code="CONFIG_MISSING",
        ) from exc

    try:
        try:
            os.fchmod(fd, mode)
        except OSError as exc:
            os.close(fd)
            _unlink_best_effort(path)
            raise BackendSelectionError(
                f"无法将 {path.name} 权限设为 {mode:04o}：{exc.strerror or exc}",
                code="CONFIG_MISSING",
            ) from exc
        written = 0
        while written < len(content):
            written += os.write(fd, content[written:])
        os.close(fd)
    except BackendSelectionError:
        raise
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        _unlink_best_effort(path)
        raise BackendSelectionError(
            f"无法写入本机配置文件 {path.name}：{exc.strerror or exc}",
            code="CONFIG_MISSING",
        ) from exc
    return True


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def load_supabase_settings(config_dir: Path) -> SupabaseSettings:
    created = ensure_cloud_config_templates(config_dir)
    local = load_local_config(config_dir)
    legacy = _read_assignments(config_dir / LEGACY_CREDENTIALS_FILENAME)
    url = local.supabase_url or legacy.get("supabase_url")
    # Only fall back to the legacy file when supabase.secret is absent.
    secret = _read_secret(config_dir)
    if secret is None:
        secret = legacy.get("supabase_secret_key")
    publishable = local.supabase_publishable_key or legacy.get("supabase_publishable_key")

    if _usable_https_url(url) and _usable_secret(secret):
        assert url is not None and secret is not None
        return SupabaseSettings(
            url=url.rstrip("/"),
            secret_key=secret,
            publishable_key=None if _is_placeholder(publishable) else publishable,
        )

    raise BackendSelectionError(
        _missing_cloud_config_message(config_dir, created=created, url=url, secret=secret),
        code="CONFIG_MISSING",
    )


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    text = value.strip()
    if not text:
        return True
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


def _usable_https_url(url: str | None) -> bool:
    return bool(url) and not _is_placeholder(url) and url.startswith("https://")  # type: ignore[union-attr]


def _usable_secret(secret: str | None) -> bool:
    return bool(secret) and not _is_placeholder(secret)


def _missing_cloud_config_message(
    config_dir: Path,
    *,
    created: list[str],
    url: str | None,
    secret: str | None,
) -> str:
    parts = [
        "云端配置未填完整，已停止，不会改用本地数据库。",
    ]
    if created:
        parts.append("已自动创建模板：" + "、".join(created) + "。")
    parts.append(
        f"请编辑 {config_dir / CONFIG_FILENAME}："
        "填写 supabase_url、supabase_publishable_key；backend 可暂保持 sqlite。"
    )
    parts.append(
        f"请编辑 {config_dir / SECRET_FILENAME}："
        "单独一行 Secret Key（sb_secret_...），并保持权限 600。"
    )
    if url and not _usable_https_url(url):
        parts.append("当前 supabase_url 仍是占位符或不是 https。")
    if secret is not None and not _usable_secret(secret):
        parts.append("当前 Secret Key 仍是占位符或无效。")
    elif secret is None:
        parts.append("尚未提供 Secret Key。")
    return "".join(parts)


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
