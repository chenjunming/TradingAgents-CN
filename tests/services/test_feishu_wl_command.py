from __future__ import annotations

from pathlib import Path

from app.services.a_share_sqlite_service import AShareSqliteService


def test_wl_like_upsert_and_delete(tmp_path: Path):
    db_path = tmp_path / "wl.db"
    svc = AShareSqliteService(db_path=str(db_path))

    svc.upsert_position("600519", "贵州茅台", 100, 1688.0, 168800.0)
    items = svc.list_positions()
    assert any(x.symbol == "600519" for x in items)

    deleted = svc.delete_position("600519")
    assert deleted is True
    assert all(x.symbol != "600519" for x in svc.list_positions())
