from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.a_share_sqlite_service import AShareSqliteService
from app.services.advisor_service import AdvisorService
from app.services.market_calendar_service import market_calendar_service


def test_a_share_sqlite_import_and_list(tmp_path: Path):
    db_path = tmp_path / "local_investment.db"
    csv_path = tmp_path / "positions.csv"
    csv_path.write_text(
        "symbol,name,quantity,cost_price,market_value\n000001,平安银行,1000,10.5,12000\n",
        encoding="utf-8",
    )

    svc = AShareSqliteService(db_path=str(db_path))
    result = svc.import_positions_csv(str(csv_path))
    assert result.success_rows == 1
    assert result.failed_rows == 0

    rows = svc.list_positions()
    assert len(rows) == 1
    assert rows[0].symbol == "000001"
    assert rows[0].quantity == 1000
    assert rows[0].avg_cost == 10.5


def test_advisor_score_bounds():
    svc = AdvisorService()
    score, breakdown = svc._score_stock({"pct_chg": 3.0, "pe": 15.0, "pb": 1.5, "amount": 2e8})
    assert 0 <= score <= 100
    assert set(breakdown.keys()) == {"fundamentals", "valuation", "trend", "sentiment", "liquidity"}


def test_market_calendar_push_points_exist_on_trading_day():
    now_utc = datetime.now(ZoneInfo("UTC"))
    points = market_calendar_service.today_push_times("CN", ref_dt=now_utc)
    if points:
        assert "open_plus_30" in points
        assert "pre_close_30" in points
        assert "post_close_30" in points
