#!/usr/bin/env python3
"""True dual-machine sync acceptance (M3 ↔ M1) against live Supabase.

Sentinel trade dates only: 2099-10-01 … 2099-10-04.
Does not touch daily ~/.marketreview/market_review.sqlite3.
Does not touch backup samples 2099-09-* or capacity day 2099-08-01.
Does not change CLOUD_DEFAULT_ENABLED.

Usage (same repo checkout + ~/.marketreview/supabase.config on both machines):

  # M3
  python3 scripts/acceptance_dual_machine_sync.py m3-seed
  python3 scripts/acceptance_dual_machine_sync.py m3-push-initial

  # M1  (after M3 initial push)
  python3 scripts/acceptance_dual_machine_sync.py m1-seed
  python3 scripts/acceptance_dual_machine_sync.py m1-push-conflict
  python3 scripts/acceptance_dual_machine_sync.py m1-adopt-local

  # M3 then M1
  python3 scripts/acceptance_dual_machine_sync.py m3-pull
  python3 scripts/acceptance_dual_machine_sync.py m1-pull

  # M3 explicit delete, then both pull again
  python3 scripts/acceptance_dual_machine_sync.py m3-explicit-delete
  python3 scripts/acceptance_dual_machine_sync.py m3-pull-after-delete
  python3 scripts/acceptance_dual_machine_sync.py m1-pull-after-delete

  # On each machine
  python3 scripts/acceptance_dual_machine_sync.py verify-local --role m3|m1

  # Cleanup cloud sentinel rows (either machine; management SQL via Data API delete RPCs)
  python3 scripts/acceptance_dual_machine_sync.py cleanup-cloud
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from marketreview.backend import config_dir_from_env, load_supabase_settings  # noqa: E402
from marketreview.repository import MarketReviewRepository  # noqa: E402
from marketreview.schema import PriceLimitEventInput  # noqa: E402
from marketreview.sqlite_schema import connect  # noqa: E402
from marketreview.supabase_store import UrllibRpcTransport  # noqa: E402
from marketreview.sync_groups import SCHEMA_VERSION, read_local_groups  # noqa: E402

DRILL_ID = "20261002T000200Z_step4_dual_physical"
DAY_A = "2099-10-01"  # M3 unique
DAY_B = "2099-10-02"  # M1 unique
DAY_OVERLAP = "2099-10-03"  # conflict pe_sh 1.0 vs 9.0
DAY_DELETE = "2099-10-04"  # explicit delete_on_cloud
SENTINEL_DAYS = (DAY_A, DAY_B, DAY_OVERLAP, DAY_DELETE)
FORBIDDEN_PREFIXES = ("2099-08-", "2099-09-")  # capacity / backup samples


def evid_dir() -> Path:
    path = Path.home() / ".marketreview" / "acceptance-evidence" / DRILL_ID
    path.mkdir(parents=True, exist_ok=True)
    (path / "reports").mkdir(exist_ok=True)
    return path


def db_path(role: str) -> Path:
    return evid_dir() / f"{role}.sqlite3"


def machine_meta() -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "system": platform.system(),
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def save_report(name: str, payload: Any) -> Path:
    path = evid_dir() / "reports" / f"{name}.json"
    wrapper = {"meta": machine_meta(), "payload": payload}
    path.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved": str(path), "meta": wrapper["meta"]}, ensure_ascii=False))
    return path


def cli(*args: str) -> dict[str, Any]:
    cmd = [sys.executable, str(SCRIPTS / "cli.py"), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"CLI empty stdout rc={proc.returncode} stderr={proc.stderr!r}")
    # CLI emits one JSON object
    line = raw.splitlines()[-1]
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CLI non-JSON: {raw[:500]}") from exc
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"CLI rc={proc.returncode} payload={parsed}")
    return parsed


def assert_sentinel_only(day: str) -> None:
    if day not in SENTINEL_DAYS:
        raise ValueError(f"only sentinel days allowed: {SENTINEL_DAYS}, got {day}")
    for prefix in FORBIDDEN_PREFIXES:
        if day.startswith(prefix):
            raise ValueError(f"refusing forbidden day prefix {prefix}: {day}")


def seed_review(path: Path, day: str, *, pe_sh: float, advancing: int) -> None:
    assert_sentinel_only(day)
    with MarketReviewRepository(path) as repo:
        repo.save_review(day, {"pe_sh": pe_sh, "advancing_count": advancing})


def seed_event(path: Path, day: str, *, code: str, name: str) -> None:
    assert_sentinel_only(day)
    with MarketReviewRepository(path) as repo:
        repo.save_price_limit_events(
            day,
            [
                PriceLimitEventInput(
                    market="sh",
                    code=code,
                    name=name,
                    direction="up",
                    closed_at_limit=True,
                    limit_rate_bp=1000,
                    streak_height=1,
                )
            ],
        )


def delete_local_review(path: Path, day: str) -> None:
    assert_sentinel_only(day)
    conn = connect(path)
    try:
        conn.execute("DELETE FROM daily_market_review WHERE trade_date = ?", (day,))
        conn.commit()
    finally:
        conn.close()


def dump_local_groups(path: Path) -> dict[str, Any]:
    conn = connect(path)
    try:
        return read_local_groups(conn)
    finally:
        conn.close()


def transport() -> UrllibRpcTransport:
    return UrllibRpcTransport(load_supabase_settings(config_dir_from_env()))


def cloud_get_day(day: str) -> dict[str, Any]:
    assert_sentinel_only(day)
    return transport().call(
        "marketreview_get_day",
        {
            "schema_version": SCHEMA_VERSION,
            "trade_date": day,
            "previous_trade_date": day,
        },
        write=False,
    )


def cloud_probe() -> dict[str, Any]:
    return transport().call(
        "marketreview_probe",
        {"schema_version": SCHEMA_VERSION},
        write=False,
    )


def cmd_m3_seed(_: argparse.Namespace) -> int:
    path = db_path("m3")
    if path.exists():
        path.unlink()
    seed_review(path, DAY_A, pe_sh=11.0, advancing=11)
    seed_event(path, DAY_A, code="600519", name="贵州茅台")
    seed_review(path, DAY_OVERLAP, pe_sh=1.0, advancing=10)
    seed_review(path, DAY_DELETE, pe_sh=4.0, advancing=4)
    groups = dump_local_groups(path)
    save_report("m3_seed", {"db": str(path), "groups": sorted(groups)})
    print(json.dumps({"ok": True, "role": "m3", "db": str(path), "groups": sorted(groups)}, ensure_ascii=False))
    return 0


def cmd_m3_push_initial(_: argparse.Namespace) -> int:
    path = db_path("m3")
    before = cloud_probe()
    report = cli("sync", "push", "--source", str(path))
    save_report("m3_push_initial", {"probe_before": before, "cli": report})
    data = report.get("data") or {}
    committed = set(data.get("committed") or [])
    expected = {
        f"event:{DAY_A}:sh:600519",
        f"review:{DAY_A}",
        f"review:{DAY_OVERLAP}",
        f"review:{DAY_DELETE}",
    }
    # partial is OK when cloud still has unrelated backup-sample groups pending download
    ok = (
        report.get("ok")
        and data.get("status") in {"completed", "partial"}
        and expected <= committed
        and not (data.get("conflicts") or [])
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "status": data.get("status"),
                "revision": data.get("revision"),
                "committed": sorted(committed),
                "download_pending": len(data.get("download") or []),
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_m1_seed(_: argparse.Namespace) -> int:
    path = db_path("m1")
    if path.exists():
        path.unlink()
    seed_review(path, DAY_B, pe_sh=22.0, advancing=22)
    seed_review(path, DAY_OVERLAP, pe_sh=9.0, advancing=90)  # conflicts with M3 pe_sh=1.0
    groups = dump_local_groups(path)
    save_report("m1_seed", {"db": str(path), "groups": sorted(groups)})
    print(json.dumps({"ok": True, "role": "m1", "db": str(path), "groups": sorted(groups)}, ensure_ascii=False))
    return 0


def cmd_m1_push_conflict(_: argparse.Namespace) -> int:
    path = db_path("m1")
    report = cli("sync", "push", "--source", str(path))
    save_report("m1_push_conflict", report)
    data = report.get("data") or {}
    conflicts = data.get("conflicts") or []
    committed = data.get("committed") or []
    ok = (
        report.get("ok")
        and data.get("status") == "partial"
        and any(c.get("group") == f"review:{DAY_OVERLAP}" for c in conflicts)
        and f"review:{DAY_B}" in committed
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "status": data.get("status"),
                "revision": data.get("revision"),
                "committed": committed,
                "conflict_groups": [c.get("group") for c in conflicts],
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_m1_adopt_local(_: argparse.Namespace) -> int:
    path = db_path("m1")
    report = cli("sync", "push", "--source", str(path), "--adopt-local", f"review:{DAY_OVERLAP}")
    save_report("m1_adopt_local", report)
    data = report.get("data") or {}
    committed = data.get("committed") or []
    ok = report.get("ok") and data.get("status") in {"completed", "partial"} and f"review:{DAY_OVERLAP}" in committed
    # after adopt, overlap should be gone from conflicts
    conflicts = data.get("conflicts") or []
    ok = ok and not any(c.get("group") == f"review:{DAY_OVERLAP}" for c in conflicts)
    print(
        json.dumps(
            {
                "ok": ok,
                "status": data.get("status"),
                "revision": data.get("revision"),
                "committed": committed,
                "conflicts": [c.get("group") for c in conflicts],
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def _pull(role: str, report_name: str) -> int:
    path = db_path(role)
    report = cli("sync", "pull", "--target", str(path))
    save_report(report_name, report)
    data = report.get("data") or {}
    ok = report.get("ok") and data.get("status") == "completed"
    print(
        json.dumps(
            {
                "ok": ok,
                "role": role,
                "status": data.get("status"),
                "revision": data.get("revision"),
                "groups": data.get("groups"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_m3_pull(_: argparse.Namespace) -> int:
    return _pull("m3", "m3_pull")


def cmd_m1_pull(_: argparse.Namespace) -> int:
    return _pull("m1", "m1_pull")


def cmd_m3_explicit_delete(_: argparse.Namespace) -> int:
    path = db_path("m3")
    # Ensure delete target still present locally after pull
    groups_before = dump_local_groups(path)
    if f"review:{DAY_DELETE}" not in groups_before:
        print(json.dumps({"ok": False, "error": f"missing local review:{DAY_DELETE} before delete"}, ensure_ascii=False))
        return 1
    delete_local_review(path, DAY_DELETE)
    preview = cli("sync", "push", "--source", str(path))
    save_report("m3_delete_preview", preview)
    report = cli("sync", "push", "--source", str(path), "--delete-on-cloud", f"review:{DAY_DELETE}")
    save_report("m3_explicit_delete", report)
    data = report.get("data") or {}
    committed = data.get("committed") or []
    # After delete, cloud day should be empty
    day = cloud_get_day(DAY_DELETE)
    cloud_absent = day.get("review") is None
    ok = (
        report.get("ok")
        and data.get("status") in {"completed", "partial"}
        and (f"review:{DAY_DELETE}" in committed or cloud_absent)
        and cloud_absent
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "status": data.get("status"),
                "revision": data.get("revision"),
                "committed": committed,
                "cloud_review_absent": cloud_absent,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_m3_pull_after_delete(_: argparse.Namespace) -> int:
    return _pull("m3", "m3_pull_after_delete")


def cmd_m1_pull_after_delete(_: argparse.Namespace) -> int:
    return _pull("m1", "m1_pull_after_delete")


def cmd_verify_local(args: argparse.Namespace) -> int:
    role = args.role
    path = db_path(role)
    local = dump_local_groups(path)
    probe = cloud_probe()
    days = {}
    for day in SENTINEL_DAYS:
        days[day] = cloud_get_day(day)

    expected_present = {
        f"review:{DAY_A}": True,
        f"event:{DAY_A}:sh:600519": True,
        f"review:{DAY_B}": True,
        f"review:{DAY_OVERLAP}": True,
        f"review:{DAY_DELETE}": False,
    }
    checks = []
    for ref, should_exist in expected_present.items():
        exists = ref in local and local[ref].get("exists", True)
        # read_local_groups may omit absent groups
        present = ref in local
        ok = present if should_exist else (not present)
        detail: dict[str, Any] = {"group": ref, "expected_present": should_exist, "local_present": present, "ok": ok}
        if ref == f"review:{DAY_OVERLAP}" and present:
            pe = (local[ref].get("review") or {}).get("pe_sh")
            detail["pe_sh"] = pe
            detail["pe_ok"] = pe == 9.0
            detail["ok"] = detail["ok"] and detail["pe_ok"]
        checks.append(detail)

    # Cloud cross-check for overlap pe and delete absence
    overlap_cloud = days[DAY_OVERLAP].get("review") or {}
    delete_cloud = days[DAY_DELETE].get("review")
    cloud_checks = {
        "overlap_pe_sh": overlap_cloud.get("pe_sh"),
        "overlap_pe_ok": overlap_cloud.get("pe_sh") == 9.0,
        "delete_absent": delete_cloud is None,
        "day_a_events": len(days[DAY_A].get("events") or []),
        "day_b_review": bool(days[DAY_B].get("review")),
    }
    ok = all(c["ok"] for c in checks) and cloud_checks["overlap_pe_ok"] and cloud_checks["delete_absent"]
    payload = {
        "role": role,
        "db": str(path),
        "probe_revision": probe.get("revision"),
        "local_groups": sorted(local),
        "checks": checks,
        "cloud_checks": cloud_checks,
        "ok": ok,
    }
    save_report(f"verify_{role}", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_cleanup_cloud(_: argparse.Namespace) -> int:
    """Delete sentinel business rows via product delete RPCs (not management SQL)."""
    from datetime import datetime, timezone

    t = transport()
    batch_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    results = []
    for day in SENTINEL_DAYS:
        day_before = t.call(
            "marketreview_get_day",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": day,
                "previous_trade_date": day,
            },
            write=False,
        )
        events = day_before.get("events") or []
        for ev in events:
            t.call(
                "marketreview_delete_event",
                {
                    "schema_version": SCHEMA_VERSION,
                    "trade_date": day,
                    "batch_time": batch_time,
                    "market": ev["market"],
                    "code": ev["code"],
                    "direction": ev["direction"],
                },
                write=True,
            )
            results.append({"day": day, "deleted_event": f"{ev['market']}:{ev['code']}:{ev['direction']}"})
        if day_before.get("review") is not None:
            t.call(
                "marketreview_delete_review",
                {
                    "schema_version": SCHEMA_VERSION,
                    "trade_date": day,
                    "batch_time": batch_time,
                },
                write=True,
            )
            results.append({"day": day, "deleted_review": True})
        # Prefer bulk events wipe if any leftover
        t.call(
            "marketreview_delete_price_limit_events",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": day,
                "batch_time": batch_time,
            },
            write=True,
        )
        day_after = t.call(
            "marketreview_get_day",
            {
                "schema_version": SCHEMA_VERSION,
                "trade_date": day,
                "previous_trade_date": day,
            },
            write=False,
        )
        results.append(
            {
                "day": day,
                "after_review": day_after.get("review") is not None,
                "after_events": len(day_after.get("events") or []),
            }
        )
    probe = cloud_probe()
    sample = t.call(
        "marketreview_get_day",
        {
            "schema_version": SCHEMA_VERSION,
            "trade_date": "2099-09-01",
            "previous_trade_date": "2099-09-01",
        },
        write=False,
    )
    sample_ok = sample.get("review") is not None and len(sample.get("events") or []) >= 1
    payload = {
        "results": results,
        "probe_revision": probe.get("revision"),
        "backup_sample_2099_09_01_ok": sample_ok,
    }
    save_report("cleanup_cloud", payload)
    ok = (
        all(r.get("after_review") is False and r.get("after_events", 0) == 0 for r in results if "after_review" in r)
        and sample_ok
    )
    print(json.dumps({"ok": ok, **payload}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_status(_: argparse.Namespace) -> int:
    probe = cloud_probe()
    days = {day: cloud_get_day(day) for day in SENTINEL_DAYS}
    sample = transport().call(
        "marketreview_get_day",
        {
            "schema_version": SCHEMA_VERSION,
            "trade_date": "2099-09-01",
            "previous_trade_date": "2099-09-01",
        },
        write=False,
    )
    payload = {
        "probe": {k: probe.get(k) for k in ("revision", "schema_version", "format_version", "complete", "ledger_key")},
        "sentinel": {
            day: {
                "review": None if not days[day].get("review") else {"pe_sh": days[day]["review"].get("pe_sh"), "advancing_count": days[day]["review"].get("advancing_count")},
                "events": len(days[day].get("events") or []),
            }
            for day in SENTINEL_DAYS
        },
        "backup_sample_2099_09_01": {
            "review": bool(sample.get("review")),
            "events": len(sample.get("events") or []),
        },
        "evid": str(evid_dir()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    mapping = {
        "status": cmd_status,
        "m3-seed": cmd_m3_seed,
        "m3-push-initial": cmd_m3_push_initial,
        "m1-seed": cmd_m1_seed,
        "m1-push-conflict": cmd_m1_push_conflict,
        "m1-adopt-local": cmd_m1_adopt_local,
        "m3-pull": cmd_m3_pull,
        "m1-pull": cmd_m1_pull,
        "m3-explicit-delete": cmd_m3_explicit_delete,
        "m3-pull-after-delete": cmd_m3_pull_after_delete,
        "m1-pull-after-delete": cmd_m1_pull_after_delete,
        "cleanup-cloud": cmd_cleanup_cloud,
    }
    for name, func in mapping.items():
        sp = sub.add_parser(name)
        sp.set_defaults(func=func)
    v = sub.add_parser("verify-local")
    v.add_argument("--role", required=True, choices=["m3", "m1"])
    v.set_defaults(func=cmd_verify_local)
    return p


def main(argv: list[str] | None = None) -> int:
    # Refuse running against production daily db path accidentally via cwd tricks
    os.environ.setdefault("MARKETREVIEW_HOME", str(Path.home() / ".marketreview"))
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
