from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.response import ok
from app.routers.auth_db import get_current_user
from app.services.portfolio_service import portfolio_service

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class LongportBindRequest(BaseModel):
    app_key: str
    app_secret: str
    access_token: str


@router.get("/positions/latest")
async def positions_latest(current_user: dict = Depends(get_current_user)):
    data = await portfolio_service.get_unified_positions(current_user["id"])
    return ok([p.model_dump() for p in data], "获取统一持仓成功")


@router.get("/markets/exposure")
async def market_exposure(current_user: dict = Depends(get_current_user)):
    data = sorted(await portfolio_service.get_exposure_markets(current_user["id"]))
    return ok(data, "获取市场暴露成功")


@router.post("/longport/bind")
async def bind_longport_credentials(
    request: LongportBindRequest,
    current_user: dict = Depends(get_current_user),
):
    await portfolio_service.bind_longport_credentials(
        user_id=current_user["id"],
        app_key=request.app_key,
        app_secret=request.app_secret,
        access_token=request.access_token,
    )
    return ok(message="LongPort凭证绑定成功")
