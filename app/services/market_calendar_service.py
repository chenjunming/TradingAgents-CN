from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


MARKET_TO_CALENDAR = {
    "CN": "XSHG",
    "HK": "XHKG",
    "US": "XNYS",
}

MARKET_TO_TZ = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}


@dataclass
class MarketSessionTimes:
    market: str
    trading_date: str
    is_session: bool
    market_open_local: datetime | None
    market_close_local: datetime | None


class MarketCalendarService:
    @staticmethod
    def _get_calendar(market: str):
        import exchange_calendars as xcals
        return xcals.get_calendar(MARKET_TO_CALENDAR[market])

    def get_session_times(self, market: str, ref_dt: datetime | None = None) -> MarketSessionTimes:
        market = market.upper()
        tz_name = MARKET_TO_TZ[market]
        tz = ZoneInfo(tz_name)
        now_local = ref_dt.astimezone(tz) if ref_dt else datetime.now(tz)

        try:
            cal = self._get_calendar(market)
            if not cal.is_session(now_local.date()):
                return MarketSessionTimes(market=market, trading_date=now_local.date().isoformat(), is_session=False, market_open_local=None, market_close_local=None)
            session_label = cal.date_to_session(now_local.date(), direction="none")
            open_utc = cal.session_open(session_label)
            close_utc = cal.session_close(session_label)
            open_local = open_utc.tz_convert(tz_name).to_pydatetime()
            close_local = close_utc.tz_convert(tz_name).to_pydatetime()
        except Exception:
            # fallback: 无交易所日历库时，至少保证工作日推送可用
            if now_local.weekday() > 4:
                return MarketSessionTimes(market=market, trading_date=now_local.date().isoformat(), is_session=False, market_open_local=None, market_close_local=None)
            if market == "CN":
                open_local = now_local.replace(hour=9, minute=30, second=0, microsecond=0)
                close_local = now_local.replace(hour=15, minute=0, second=0, microsecond=0)
            elif market == "HK":
                open_local = now_local.replace(hour=9, minute=30, second=0, microsecond=0)
                close_local = now_local.replace(hour=16, minute=0, second=0, microsecond=0)
            else:
                open_local = now_local.replace(hour=9, minute=30, second=0, microsecond=0)
                close_local = now_local.replace(hour=16, minute=0, second=0, microsecond=0)

        return MarketSessionTimes(
            market=market,
            trading_date=now_local.date().isoformat(),
            is_session=True,
            market_open_local=open_local,
            market_close_local=close_local,
        )

    def is_trading_day(self, market: str, target_date: date | None = None) -> bool:
        market = market.upper()
        d = target_date or datetime.now(ZoneInfo(MARKET_TO_TZ[market])).date()
        try:
            cal = self._get_calendar(market)
            return cal.is_session(d)
        except Exception:
            return d.weekday() < 5

    def today_push_times(self, market: str, ref_dt: datetime | None = None) -> dict[str, datetime]:
        session = self.get_session_times(market, ref_dt=ref_dt)
        if not session.is_session or not session.market_open_local or not session.market_close_local:
            return {}
        return {
            "open_plus_30": session.market_open_local + timedelta(minutes=30),
            "pre_close_30": session.market_close_local - timedelta(minutes=30),
            "post_close_30": session.market_close_local + timedelta(minutes=30),
        }


market_calendar_service = MarketCalendarService()
