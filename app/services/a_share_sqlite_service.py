from __future__ import annotations

import csv
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import settings
from app.models.advisor_models import AShareImportResult, UnifiedPosition


class AShareSqliteService:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.A_SHARE_SQLITE_PATH
        self._lock = threading.Lock()
        self._ensure_parent_dir()
        self.init_db()

    def _ensure_parent_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS a_positions (
                        symbol TEXT PRIMARY KEY,
                        name TEXT DEFAULT '',
                        quantity REAL NOT NULL,
                        avg_cost REAL,
                        market_value REAL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS a_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        price REAL NOT NULL,
                        traded_at TEXT NOT NULL,
                        notes TEXT DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS advisor_picks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT DEFAULT '',
                        market TEXT NOT NULL,
                        score_total REAL NOT NULL,
                        action TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        risk TEXT NOT NULL,
                        invalid_condition TEXT NOT NULL,
                        target_weight REAL NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS advisor_rebalance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT DEFAULT '',
                        market TEXT NOT NULL,
                        action TEXT NOT NULL,
                        current_weight REAL NOT NULL,
                        target_weight REAL NOT NULL,
                        delta_weight REAL NOT NULL,
                        reason TEXT NOT NULL,
                        risk TEXT NOT NULL,
                        invalid_condition TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS advisor_evidence (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        market TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS daily_brief (
                        trade_date TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def list_positions(self) -> List[UnifiedPosition]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT symbol, name, quantity, avg_cost, market_value FROM a_positions ORDER BY symbol"
                ).fetchall()
            finally:
                conn.close()

        positions: List[UnifiedPosition] = []
        for row in rows:
            positions.append(
                UnifiedPosition(
                    symbol=row["symbol"],
                    name=row["name"] or "",
                    market="CN",
                    quantity=float(row["quantity"] or 0),
                    avg_cost=float(row["avg_cost"]) if row["avg_cost"] is not None else None,
                    market_value=float(row["market_value"]) if row["market_value"] is not None else None,
                    source="sqlite_a_share",
                )
            )
        return positions

    def upsert_position(
        self,
        symbol: str,
        name: str,
        quantity: float,
        avg_cost: Optional[float],
        market_value: Optional[float],
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO a_positions(symbol, name, quantity, avg_cost, market_value, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name,
                        quantity=excluded.quantity,
                        avg_cost=excluded.avg_cost,
                        market_value=excluded.market_value,
                        updated_at=excluded.updated_at
                    """,
                    (symbol, name, quantity, avg_cost, market_value, now),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_position(self, symbol: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    "DELETE FROM a_positions WHERE symbol=?",
                    (symbol.zfill(6),),
                )
                conn.commit()
                return result.rowcount > 0
            finally:
                conn.close()

    def import_positions_csv(self, csv_path: str) -> AShareImportResult:
        required = {"symbol", "name", "quantity", "cost_price", "market_value"}
        total_rows = 0
        success_rows = 0
        errors: List[str] = []

        if not os.path.exists(csv_path):
            return AShareImportResult(
                total_rows=0,
                success_rows=0,
                failed_rows=1,
                errors=[f"CSV文件不存在: {csv_path}"],
            )

        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return AShareImportResult(
                    total_rows=0,
                    success_rows=0,
                    failed_rows=1,
                    errors=["CSV缺少表头"],
                )
            missing = required - set(reader.fieldnames)
            if missing:
                return AShareImportResult(
                    total_rows=0,
                    success_rows=0,
                    failed_rows=1,
                    errors=[f"CSV缺少字段: {','.join(sorted(missing))}"],
                )

            for idx, row in enumerate(reader, start=2):
                total_rows += 1
                try:
                    symbol = str(row.get("symbol", "")).strip()
                    if not symbol:
                        raise ValueError("symbol为空")
                    symbol = symbol.zfill(6)
                    name = str(row.get("name", "")).strip()
                    quantity = float(row.get("quantity") or 0)
                    cost_price = row.get("cost_price")
                    market_value = row.get("market_value")
                    avg_cost = float(cost_price) if cost_price not in (None, "") else None
                    mv = float(market_value) if market_value not in (None, "") else None
                    self.upsert_position(symbol, name, quantity, avg_cost, mv)
                    success_rows += 1
                except Exception as exc:
                    errors.append(f"第{idx}行失败: {exc}")

        return AShareImportResult(
            total_rows=total_rows,
            success_rows=success_rows,
            failed_rows=max(total_rows - success_rows, 0),
            errors=errors,
        )


_a_share_sqlite_service: Optional[AShareSqliteService] = None


def get_a_share_sqlite_service() -> AShareSqliteService:
    global _a_share_sqlite_service
    if _a_share_sqlite_service is None:
        _a_share_sqlite_service = AShareSqliteService()
    return _a_share_sqlite_service
