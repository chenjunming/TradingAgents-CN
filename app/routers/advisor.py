from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.core.database import get_mongo_db
from app.core.response import ok
from app.models.advisor_models import DispatchNowRequest, GenerateRequest
from app.routers.auth_db import get_current_user
from app.services.a_share_sqlite_service import get_a_share_sqlite_service
from app.services.advisor_service import advisor_service
from app.services.market_push_schedule_service import market_push_schedule_service
from app.services.portfolio_service import portfolio_service


router = APIRouter(prefix="/api/advisor", tags=["advisor"])


@router.post("/daily-picks/generate")
async def generate_daily_picks(
    request: GenerateRequest,
    current_user: dict = Depends(get_current_user),
):
    picks = await advisor_service.generate_daily_picks(
        user_id=current_user["id"],
        market=request.market,
        trade_date=request.date,
    )
    return ok([p.model_dump() for p in picks], "选股建议生成完成")


@router.post("/rebalance/generate")
async def generate_rebalance(
    request: GenerateRequest,
    current_user: dict = Depends(get_current_user),
):
    actions = await advisor_service.generate_rebalance(
        user_id=current_user["id"],
        market=request.market,
        trade_date=request.date,
    )
    return ok([a.model_dump() for a in actions], "调仓建议生成完成")


@router.get("/daily-brief")
async def daily_brief(
    date: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    brief = await advisor_service.get_daily_brief(current_user["id"], trade_date=date)
    return ok(brief.model_dump(), "获取摘要成功")


@router.get("/positions/latest")
async def unified_positions(current_user: dict = Depends(get_current_user)):
    data = await portfolio_service.get_unified_positions(current_user["id"])
    return ok([p.model_dump() for p in data], "获取持仓成功")


@router.post("/a-share/import-csv")
async def import_a_share_csv(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".csv":
        raise HTTPException(status_code=400, detail="仅支持CSV文件")

    tmp_path = Path("/tmp") / f"a_share_positions_{datetime.utcnow().timestamp()}.csv"
    content = await file.read()
    tmp_path.write_bytes(content)
    try:
        result = get_a_share_sqlite_service().import_positions_csv(str(tmp_path))
        return ok(result.model_dump(), "A股持仓导入完成")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@router.get("/a-share/positions")
async def list_a_share_positions(current_user: dict = Depends(get_current_user)):
    data = get_a_share_sqlite_service().list_positions()
    return ok([p.model_dump() for p in data], "获取A股持仓成功")


@router.post("/push/dispatch-now")
async def dispatch_now(
    request: DispatchNowRequest,
    current_user: dict = Depends(get_current_user),
):
    stats = await market_push_schedule_service.dispatch_one(
        user_id=current_user["id"],
        market=request.market,
        push_type=request.push_type,
        force=request.force,
    )
    return ok(stats, "手动调度完成")


@router.get("/push/events")
async def push_events(
    date: Optional[str] = Query(default=None),
    market: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    db = get_mongo_db()
    q = {"user_id": current_user["id"]}
    if date:
        q["trading_date"] = date
    if market:
        q["market"] = market.upper()

    cursor = db.market_push_events.find(q).sort("updated_at", -1).limit(200)
    rows = []
    async for item in cursor:
        item["_id"] = str(item.get("_id"))
        rows.append(item)
    return ok(rows)


@router.get("/push/next")
async def next_push(current_user: dict = Depends(get_current_user)):
    rows = await market_push_schedule_service.next_push_times()
    return ok(rows)
