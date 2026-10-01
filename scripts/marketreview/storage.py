"""Open the one backend selected for a daily command."""

from __future__ import annotations

from pathlib import Path

from urllib.parse import urlparse

from .backend import (
    config_dir_from_env,
    effective_default_backend,
    load_local_config,
    load_supabase_settings,
    reject_sqlite_path_on_supabase,
    resolve_backend_name,
)
from .paths import production_cloud_state_dir, resolve_db_path
from .repository import MarketReviewRepository
from .supabase_store import RpcTransport, SupabaseRepository, UrllibRpcTransport


def open_repository(
    *,
    backend: str | None,
    db_path: str | None,
    config_dir: Path | None = None,
    transport: RpcTransport | None = None,
    state_dir: Path | None = None,
):
    directory = config_dir if config_dir is not None else config_dir_from_env()
    configured = load_local_config(directory).backend
    selected = resolve_backend_name(
        explicit=backend,
        configured=configured,
        default=effective_default_backend(),
    )
    reject_sqlite_path_on_supabase(selected, db_path)
    if selected == "sqlite":
        return MarketReviewRepository(resolve_db_path(db_path), state_dir=state_dir)
    settings = load_supabase_settings(directory)
    client = transport or UrllibRpcTransport(settings)
    project_ref = urlparse(settings.url).hostname or settings.url
    if state_dir is not None:
        cloud_state = state_dir
    elif config_dir is not None:
        cloud_state = config_dir / "supabase-state"
    else:
        cloud_state = production_cloud_state_dir()
    return SupabaseRepository(
        client,
        state_dir=cloud_state,
        project_ref=project_ref,
    )
