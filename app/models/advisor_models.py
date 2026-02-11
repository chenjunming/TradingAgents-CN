from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field


MarketCode = Literal["CN", "HK", "US"]
PushType = Literal["open_plus_30", "pre_close_30", "post_close_30"]
ActionType = Literal["increase", "reduce", "hold", "watch"]


class UnifiedPosition(BaseModel):
    symbol: str
    name: str = ""
    market: MarketCode
    quantity: float = 0.0
    available_quantity: Optional[float] = None
    avg_cost: Optional[float] = None
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    currency: Optional[str] = None
    source: str = "unknown"
    stale_data: bool = False


class CandidateStock(BaseModel):
    symbol: str
    name: str = ""
    market: MarketCode
    score_total: float
    score_breakdown: Dict[str, float] = Field(default_factory=dict)
    action: ActionType
    reason: str
    risk: str
    invalid_condition: str
    target_weight: float = 0.0


class RebalanceAction(BaseModel):
    symbol: str
    name: str = ""
    market: MarketCode
    action: ActionType
    current_weight: float
    target_weight: float
    delta_weight: float
    reason: str
    risk: str
    invalid_condition: str
    priority: int = 1


class DailyBriefResponse(BaseModel):
    date: str
    user_id: str
    summary: str
    positions: List[UnifiedPosition] = Field(default_factory=list)
    picks: List[CandidateStock] = Field(default_factory=list)
    rebalance: List[RebalanceAction] = Field(default_factory=list)
    risk_alerts: List[str] = Field(default_factory=list)
    generated_at: datetime


class MarketPushEvent(BaseModel):
    event_id: str
    user_id: str
    market: MarketCode
    trading_date: str
    push_type: PushType
    scheduled_at: datetime
    sent_at: Optional[datetime] = None
    status: Literal["sent", "skipped", "failed"]
    reason: Optional[str] = None


class FeishuCommandRequest(BaseModel):
    command: str
    args: List[str] = Field(default_factory=list)
    user_id: Optional[str] = None
    chat_id: Optional[str] = None


class FeishuCommandResponse(BaseModel):
    message_type: str = "text"
    fallback_text: str
    card_payload: Optional[Dict[str, Any]] = None


class GenerateRequest(BaseModel):
    market: Optional[MarketCode] = None
    date: Optional[str] = None
    force_refresh: bool = False


class DispatchNowRequest(BaseModel):
    market: MarketCode
    push_type: PushType
    force: bool = False


class ASharePositionUpsert(BaseModel):
    symbol: str
    name: str = ""
    quantity: float
    avg_cost: Optional[float] = None
    market_value: Optional[float] = None


class AShareImportResult(BaseModel):
    total_rows: int
    success_rows: int
    failed_rows: int
    errors: List[str] = Field(default_factory=list)
