from __future__ import annotations

import asyncio
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.response import ok
from app.models.analysis import AnalysisParameters, SingleAnalysisRequest
from app.routers.auth_db import get_current_user
from app.services.simple_analysis_service import get_simple_analysis_service
from app.services.signals.store import SignalStore

router = APIRouter(prefix="/api/signals", tags=["signals"])
store = SignalStore()


class MuteRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=365)


class DeliveryTargetUpsertRequest(BaseModel):
    system_user_id: Optional[str] = Field(default=None, description="系统用户ID；不传默认当前用户")
    receive_id_type: Literal["chat_id", "open_id", "user_id", "union_id"] = "chat_id"
    receive_id: str = Field(min_length=1)
    enabled: bool = True


def _market_to_market_type(market: str) -> str:
    mk = str(market or "").upper()
    if mk == "HK":
        return "港股"
    if mk == "US":
        return "美股"
    return "A股"


def _normalize_symbol_for_market(ticker: str, market: str) -> str:
    mk = str(market or "").upper().strip()
    raw = str(ticker or "").strip().upper()
    if not raw:
        return ""

    if mk == "HK":
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return f"{digits.zfill(5)}.HK"
        return raw

    if mk == "US":
        return re.sub(r"\.US$", "", raw, flags=re.IGNORECASE)

    # CN / fallback
    if re.fullmatch(r"\d{1,6}", raw):
        return raw.zfill(6)
    if re.fullmatch(r"\d{6}\.(SH|SZ)", raw):
        return raw.split(".", 1)[0]
    return raw


async def _start_analysis_for_event(event: dict) -> dict:
    user_id = str(event.get("user_id") or "default")
    ticker = str(event.get("ticker") or "").upper()
    market = str(event.get("market") or "CN").upper()
    analysis_symbol = _normalize_symbol_for_market(ticker=ticker, market=market)
    if not ticker:
        raise ValueError("event ticker is empty")

    req = SingleAnalysisRequest(
        symbol=analysis_symbol,
        stock_code=analysis_symbol,
        parameters=AnalysisParameters(
            market_type=_market_to_market_type(market),
            research_depth="标准",
        ),
    )
    service = get_simple_analysis_service()
    created = await service.create_analysis_task(user_id, req)
    task_id = str(created.get("task_id") or "")
    if not task_id:
        raise RuntimeError("create_analysis_task did not return task_id")

    asyncio.create_task(service.execute_analysis_background(task_id, user_id, req))
    await store.set_event_analysis_task(
        user_id=user_id,
        event_id=str(event.get("event_id") or ""),
        task_id=task_id,
        status="pending",
    )
    return {
        "event_id": event.get("event_id"),
        "task_id": task_id,
        "status": "started",
        "ticker": analysis_symbol,
        "market": market,
    }


@router.get("")
async def list_signals(
    ticker: Optional[str] = Query(default=None),
    level: Optional[str] = Query(default=None),
    rule_id: Optional[str] = Query(default=None),
    acked: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
):
    await store.ensure_indexes()
    data = await store.list_events(
        user_id=user["id"],
        ticker=ticker,
        level=level,
        rule_id=rule_id,
        acked=acked,
        limit=limit,
        offset=offset,
    )
    return ok(data=data, message="ok")


@router.post("/{event_id}/ack")
async def ack_signal(event_id: str, user: dict = Depends(get_current_user)):
    await store.ensure_indexes()
    success = await store.ack_event(user_id=user["id"], event_id=event_id)
    if not success:
        raise HTTPException(status_code=404, detail="signal event not found")
    return ok(message="acked")


@router.post("/{event_id}/mute")
async def mute_signal(event_id: str, payload: MuteRequest, user: dict = Depends(get_current_user)):
    await store.ensure_indexes()
    success = await store.mute_from_event(user_id=user["id"], event_id=event_id, days=payload.days)
    if not success:
        raise HTTPException(status_code=404, detail="signal event not found")
    return ok(message=f"muted for {payload.days} days")


@router.post("/{event_id}/analysis/run")
async def run_signal_analysis(event_id: str, user: dict = Depends(get_current_user)):
    await store.ensure_indexes()
    event = await store.get_event(user_id=user["id"], event_id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="signal event not found")
    data = await _start_analysis_for_event(event)
    return ok(data=data, message="analysis started")


@router.get("/{event_id}/analysis/run")
async def run_signal_analysis_by_token(event_id: str, token: str = Query(default="")):
    await store.ensure_indexes()
    event = await store.get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="signal event not found")

    expected = str((event.get("action_tokens") or {}).get("analysis") or "")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="invalid token")

    data = await _start_analysis_for_event(event)
    return ok(data=data, message="analysis started")


@router.get("/ruleset/current")
async def get_current_ruleset(user: dict = Depends(get_current_user)):
    await store.ensure_indexes()
    doc = await store.current_ruleset()
    return ok(data=doc or {}, message="ok")


@router.post("/delivery-target")
async def upsert_delivery_target(payload: DeliveryTargetUpsertRequest, user: dict = Depends(get_current_user)):
    await store.ensure_indexes()
    target_user_id = str(payload.system_user_id or user["id"]).strip()
    if not target_user_id:
        raise HTTPException(status_code=400, detail="system_user_id is required")
    if target_user_id != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="only admin can update other users delivery target")

    data = await store.upsert_delivery_target(
        system_user_id=target_user_id,
        receive_id_type=payload.receive_id_type,
        receive_id=payload.receive_id,
        enabled=payload.enabled,
    )
    return ok(data=data, message="delivery target updated")


@router.get("/delivery-target")
async def get_delivery_target(
    system_user_id: Optional[str] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    await store.ensure_indexes()
    target_user_id = str(system_user_id or user["id"]).strip()
    if not target_user_id:
        raise HTTPException(status_code=400, detail="system_user_id is required")
    if target_user_id != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="only admin can query other users delivery target")

    doc = await store.get_delivery_target(target_user_id)
    return ok(data=doc or {}, message="ok")
