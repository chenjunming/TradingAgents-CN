from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.core.response import ok
from app.routers.auth_db import get_current_user
from app.services.a_share_sqlite_service import get_a_share_sqlite_service

router = APIRouter(prefix="/api/a-share", tags=["a-share-sqlite"])


@router.post("/import/csv")
async def import_csv(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if Path(file.filename or "").suffix.lower() != ".csv":
        raise HTTPException(status_code=400, detail="仅支持CSV")

    tmp = Path("/tmp") / f"a_share_{Path(file.filename or 'upload.csv').name}"
    tmp.write_bytes(await file.read())
    try:
        result = get_a_share_sqlite_service().import_positions_csv(str(tmp))
        return ok(result.model_dump(), "导入完成")
    finally:
        if tmp.exists():
            tmp.unlink()


@router.get("/positions")
async def list_positions(user: dict = Depends(get_current_user)):
    positions = get_a_share_sqlite_service().list_positions()
    return ok([x.model_dump() for x in positions], "获取成功")
