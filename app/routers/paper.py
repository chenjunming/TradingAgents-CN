from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, Any, List, Tuple
from datetime import datetime
import logging
import re
import os
import asyncio

from app.routers.auth_db import get_current_user
from app.core.database import get_mongo_db
from app.core.response import ok

router = APIRouter(prefix="/paper", tags=["paper"])
logger = logging.getLogger("webapi")


# 每个市场的初始资金配置
INITIAL_CASH_BY_MARKET = {
    "CNY": 1_000_000.0,   # A股：100万人民币
    "HKD": 1_000_000.0,   # 港股：100万港币
    "USD": 100_000.0      # 美股：10万美元
}


class PlaceOrderRequest(BaseModel):
    code: str = Field(..., description="股票代码（支持A股/港股/美股）")
    side: Literal["buy", "sell"]
    quantity: int = Field(..., gt=0)
    market: Optional[str] = Field(None, description="市场类型 (CN/HK/US)，不传则自动识别")
    # 可选：关联的分析ID，便于从分析页面一键下单后追踪
    analysis_id: Optional[str] = None


class LongportSyncRequest(BaseModel):
    symbols: Optional[List[str]] = Field(
        default=None,
        description="可选：指定要同步的标的（Longport 格式，如 700.HK/AAPL.US/600000.SH）",
    )
    replace_existing_longport_positions: bool = Field(
        default=True,
        description="是否替换当前用户已同步的长桥持仓（仅删除 source=longport 的旧持仓）",
    )
    sync_cash: bool = Field(
        default=True,
        description="是否同步账户可用现金到 paper_accounts.cash",
    )


def _detect_market_and_code(code: str) -> Tuple[str, str]:
    """
    检测股票代码的市场类型并标准化代码

    Returns:
        (market, normalized_code): 市场类型和标准化后的代码
            - CN: A股（6位数字）
            - HK: 港股（4-5位数字或带.HK后缀）
            - US: 美股（字母代码）
    """
    code = code.strip().upper()

    # 港股：带 .HK 后缀
    if code.endswith('.HK'):
        return ('HK', code[:-3].zfill(5))

    # 美股：纯字母
    if re.match(r'^[A-Z]+$', code):
        return ('US', code)

    # 港股：4-5位数字
    if re.match(r'^\d{4,5}$', code):
        return ('HK', code.zfill(5))

    # A股：6位数字
    if re.match(r'^\d{6}$', code):
        return ('CN', code)

    # 默认当作A股，补齐6位
    return ('CN', code.zfill(6))


async def _get_or_create_account(user_id: str) -> Dict[str, Any]:
    """获取或创建账户（多货币）"""
    db = get_mongo_db()
    acc = await db["paper_accounts"].find_one({"user_id": user_id})
    if not acc:
        now = datetime.utcnow().isoformat()
        acc = {
            "user_id": user_id,
            # 多货币现金账户
            "cash": {
                "CNY": INITIAL_CASH_BY_MARKET["CNY"],
                "HKD": INITIAL_CASH_BY_MARKET["HKD"],
                "USD": INITIAL_CASH_BY_MARKET["USD"]
            },
            # 多货币已实现盈亏
            "realized_pnl": {
                "CNY": 0.0,
                "HKD": 0.0,
                "USD": 0.0
            },
            # 账户设置
            "settings": {
                "auto_currency_conversion": False,
                "default_market": "CN"
            },
            "created_at": now,
            "updated_at": now,
        }
        await db["paper_accounts"].insert_one(acc)
    else:
        # 兼容旧账户结构：如果 cash 或 realized_pnl 仍为标量，迁移为多货币对象
        updates: Dict[str, Any] = {}
        try:
            cash_val = acc.get("cash")
            if not isinstance(cash_val, dict):
                base_cash = float(cash_val or 0.0)
                updates["cash"] = {"CNY": base_cash, "HKD": 0.0, "USD": 0.0}

            pnl_val = acc.get("realized_pnl")
            if not isinstance(pnl_val, dict):
                base_pnl = float(pnl_val or 0.0)
                updates["realized_pnl"] = {"CNY": base_pnl, "HKD": 0.0, "USD": 0.0}

            if updates:
                updates["updated_at"] = datetime.utcnow().isoformat()
                await db["paper_accounts"].update_one({"user_id": user_id}, {"$set": updates})
                # 重新读取迁移后的账户
                acc = await db["paper_accounts"].find_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"❌ 账户结构迁移失败 user_id={user_id}: {e}")
    return acc


async def _get_market_rules(market: str) -> Optional[Dict[str, Any]]:
    """获取市场规则配置"""
    db = get_mongo_db()
    rules_doc = await db["paper_market_rules"].find_one({"market": market})
    if rules_doc:
        return rules_doc.get("rules", {})
    return None


def _calculate_commission(market: str, side: str, amount: float, rules: Dict[str, Any]) -> float:
    """计算手续费"""
    if not rules or "commission" not in rules:
        return 0.0

    commission_config = rules["commission"]
    commission = 0.0

    # 佣金
    comm_rate = commission_config.get("rate", 0.0)
    comm_min = commission_config.get("min", 0.0)
    commission += max(amount * comm_rate, comm_min)

    # 印花税（仅卖出）
    if side == "sell" and "stamp_duty_rate" in commission_config:
        commission += amount * commission_config["stamp_duty_rate"]

    # 其他费用（港股）
    if market == "HK":
        if "transaction_levy_rate" in commission_config:
            commission += amount * commission_config["transaction_levy_rate"]
        if "trading_fee_rate" in commission_config:
            commission += amount * commission_config["trading_fee_rate"]
        if "settlement_fee_rate" in commission_config:
            commission += amount * commission_config["settlement_fee_rate"]

    # SEC费用（美股，仅卖出）
    if market == "US" and side == "sell" and "sec_fee_rate" in commission_config:
        commission += amount * commission_config["sec_fee_rate"]

    return round(commission, 2)


async def _get_available_quantity(user_id: str, code: str, market: str) -> int:
    """获取可用数量（考虑T+1限制）"""
    db = get_mongo_db()
    pos = await db["paper_positions"].find_one({"user_id": user_id, "code": code})

    if not pos:
        return 0

    total_qty = pos.get("quantity", 0)

    # A股T+1：今天买入的不能卖出
    if market == "CN":
        # 获取市场规则
        rules = await _get_market_rules(market)
        if rules and rules.get("t_plus", 0) > 0:
            # 查询今天的买入数量
            today = datetime.utcnow().date().isoformat()
            pipeline = [
                {"$match": {
                    "user_id": user_id,
                    "code": code,
                    "side": "buy",
                    "timestamp": {"$gte": today}
                }},
                {"$group": {"_id": None, "total": {"$sum": "$quantity"}}}
            ]
            today_buy = await db["paper_trades"].aggregate(pipeline).to_list(1)
            today_buy_qty = today_buy[0]["total"] if today_buy else 0
            return max(0, total_qty - today_buy_qty)

    # 港股/美股T+0：全部可用
    return total_qty


async def _get_last_price(code: str, market: str) -> Optional[float]:
    """
    获取股票最新价格（支持多市场）

    Args:
        code: 股票代码
        market: 市场类型 (CN/HK/US)

    Returns:
        最新价格，如果获取失败返回 None
    """
    db = get_mongo_db()

    # A股：从数据库获取
    if market == "CN":
        # 1. 尝试从 market_quotes 获取
        q = await db["market_quotes"].find_one(
            {"$or": [{"code": code}, {"symbol": code}]},
            {"_id": 0, "close": 1}
        )
        if q and q.get("close") is not None:
            try:
                price = float(q["close"])
                if price > 0:
                    logger.debug(f"✅ 从 market_quotes 获取价格: {code} = {price}")
                    return price
            except Exception as e:
                logger.warning(f"⚠️ market_quotes 价格转换失败 {code}: {e}")

        # 2. 回退到 stock_basic_info 的 current_price
        basic_info = await db["stock_basic_info"].find_one(
            {"$or": [{"code": code}, {"symbol": code}]},
            {"_id": 0, "current_price": 1}
        )
        if basic_info and basic_info.get("current_price") is not None:
            try:
                price = float(basic_info["current_price"])
                if price > 0:
                    logger.debug(f"✅ 从 stock_basic_info 获取价格: {code} = {price}")
                    return price
            except Exception as e:
                logger.warning(f"⚠️ stock_basic_info 价格转换失败 {code}: {e}")

        logger.error(f"❌ 无法从数据库获取A股价格: {code}")
        return None

    # 港股/美股：使用 ForeignStockService
    elif market in ['HK', 'US']:
        try:
            from app.services.foreign_stock_service import ForeignStockService
            db = get_mongo_db()
            service = ForeignStockService(db=db)

            quote = await service.get_quote(market, code, force_refresh=False)

            if quote:
                # 尝试多个可能的价格字段
                price = quote.get("price") or quote.get("current_price") or quote.get("close")
                if price and float(price) > 0:
                    logger.debug(f"✅ 从 ForeignStockService 获取{market}价格: {code} = {price}")
                    return float(price)
        except Exception as e:
            logger.error(f"❌ 获取{market}股价格失败 {code}: {e}")
            return None

    logger.error(f"❌ 无法获取股票价格: {code} (market={market})")
    return None


def _zfill_code(code: str) -> str:
    s = str(code).strip()
    if len(s) == 6 and s.isdigit():
        return s
    return s.zfill(6)


def _longport_market_to_internal(market_obj: Any, symbol: str) -> str:
    """
    将 Longport 市场枚举映射到系统市场编码：CN/HK/US
    """
    # 优先依据 symbol 后缀判断，避免 SDK 枚举对象差异
    sym = str(symbol or "").upper()
    if sym.endswith(".HK"):
        return "HK"
    if sym.endswith(".US"):
        return "US"
    if sym.endswith(".SH") or sym.endswith(".SZ") or sym.endswith(".BJ"):
        return "CN"

    # 回退依据 market 对象类型名
    name = ""
    try:
        if hasattr(market_obj, "__name__"):
            name = str(getattr(market_obj, "__name__"))
        else:
            name = str(type(market_obj).__name__)
    except Exception:
        name = str(market_obj)
    upper_name = name.upper()
    if "HK" in upper_name:
        return "HK"
    if "US" in upper_name:
        return "US"
    if "CN" in upper_name:
        return "CN"
    return "CN"


def _normalize_longport_symbol(symbol: str, market: str) -> str:
    """
    将 Longport symbol 归一化为本系统持仓 code：
    - CN: 6位数字
    - HK: 5位数字
    - US: 大写字母代码
    """
    sym = str(symbol or "").upper().strip()
    if "." in sym:
        base = sym.split(".", 1)[0]
    else:
        base = sym

    if market == "HK":
        digits = "".join(ch for ch in base if ch.isdigit())
        return digits.zfill(5) if digits else base
    if market == "CN":
        digits = "".join(ch for ch in base if ch.isdigit())
        return digits.zfill(6) if digits else base
    return base.upper()


async def _get_longport_credentials() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    获取 Longport 凭证：
    1) 环境变量
    2) 数据库 system_configs 中 longport 数据源配置
    """
    app_key = os.getenv("LONGPORT_APP_KEY")
    app_secret = os.getenv("LONGPORT_APP_SECRET")
    access_token = os.getenv("LONGPORT_ACCESS_TOKEN")

    if app_key and app_secret and access_token:
        return app_key, app_secret, access_token

    try:
        db = get_mongo_db()
        config_data = await db.system_configs.find_one({"is_active": True}, sort=[("version", -1)])
        if config_data:
            for ds in config_data.get("data_source_configs", []):
                ds_type = str(ds.get("type", "")).lower()
                ds_name = str(ds.get("name", "")).lower()
                if ds_type != "longport" and ds_name != "longport":
                    continue
                cfg = ds.get("config_params", {}) or {}
                app_key = app_key or ds.get("api_key")
                app_secret = app_secret or ds.get("api_secret")
                access_token = access_token or cfg.get("access_token")
                break
    except Exception as e:
        logger.warning(f"⚠️ 读取 Longport 配置失败: {e}")

    return app_key, app_secret, access_token


@router.get("/account", response_model=dict)
async def get_account(current_user: dict = Depends(get_current_user)):
    """获取或创建纸上账户，返回资金与持仓估值汇总（支持多市场）"""
    db = get_mongo_db()
    acc = await _get_or_create_account(current_user["id"])

    # 聚合持仓估值（按货币分类）
    positions = await db["paper_positions"].find({"user_id": current_user["id"]}).to_list(None)

    positions_value_by_currency = {
        "CNY": 0.0,
        "HKD": 0.0,
        "USD": 0.0
    }

    detailed_positions: List[Dict[str, Any]] = []
    for p in positions:
        code = p.get("code")
        market = p.get("market", "CN")
        currency = p.get("currency", "CNY")
        qty = int(p.get("quantity", 0))
        avg_cost = float(p.get("avg_cost", 0.0))
        available_qty = p.get("available_qty", qty)

        # 获取最新价
        last = await _get_last_price(code, market)
        mkt_value = round((last or 0.0) * qty, 2)
        positions_value_by_currency[currency] += mkt_value

        detailed_positions.append({
            "code": code,
            "market": market,
            "currency": currency,
            "quantity": qty,
            "available_qty": available_qty,
            "avg_cost": avg_cost,
            "last_price": last,
            "market_value": mkt_value,
            "unrealized_pnl": None if last is None else round((last - avg_cost) * qty, 2)
        })

    # 计算总资产（按货币分别显示）
    cash = acc.get("cash", {})
    realized_pnl = acc.get("realized_pnl", {})

    # 兼容旧格式（单一现金）
    if not isinstance(cash, dict):
        cash = {"CNY": float(cash), "HKD": 0.0, "USD": 0.0}
    if not isinstance(realized_pnl, dict):
        realized_pnl = {"CNY": float(realized_pnl), "HKD": 0.0, "USD": 0.0}

    summary = {
        "cash": {
            "CNY": round(float(cash.get("CNY", 0.0)), 2),
            "HKD": round(float(cash.get("HKD", 0.0)), 2),
            "USD": round(float(cash.get("USD", 0.0)), 2)
        },
        "realized_pnl": {
            "CNY": round(float(realized_pnl.get("CNY", 0.0)), 2),
            "HKD": round(float(realized_pnl.get("HKD", 0.0)), 2),
            "USD": round(float(realized_pnl.get("USD", 0.0)), 2)
        },
        "positions_value": positions_value_by_currency,
        "equity": {
            "CNY": round(float(cash.get("CNY", 0.0)) + positions_value_by_currency["CNY"], 2),
            "HKD": round(float(cash.get("HKD", 0.0)) + positions_value_by_currency["HKD"], 2),
            "USD": round(float(cash.get("USD", 0.0)) + positions_value_by_currency["USD"], 2)
        },
        "updated_at": acc.get("updated_at"),
    }

    return ok({"account": summary, "positions": detailed_positions})


@router.post("/order", response_model=dict)
async def place_order(payload: PlaceOrderRequest, current_user: dict = Depends(get_current_user)):
    """提交市价单，按最新价即时成交（支持多市场）"""
    db = get_mongo_db()

    # 1. 识别市场类型
    if payload.market:
        market = payload.market.upper()
        normalized_code = payload.code
    else:
        market, normalized_code = _detect_market_and_code(payload.code)

    side = payload.side
    qty = int(payload.quantity)
    analysis_id = getattr(payload, "analysis_id", None)

    # 2. 确定货币
    currency_map = {
        "CN": "CNY",
        "HK": "HKD",
        "US": "USD"
    }
    currency = currency_map.get(market, "CNY")

    # 3. 获取账户
    acc = await _get_or_create_account(current_user["id"])

    # 4. 获取价格
    price = await _get_last_price(normalized_code, market)
    if price is None or price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无法获取股票 {normalized_code} ({market}) 的最新价格"
        )

    # 5. 计算金额
    notional = round(price * qty, 2)

    # 6. 获取市场规则并计算手续费
    rules = await _get_market_rules(market)
    commission = _calculate_commission(market, side, notional, rules) if rules else 0.0
    total_cost = notional + commission

    # 7. 获取持仓
    pos = await db["paper_positions"].find_one({"user_id": current_user["id"], "code": normalized_code})

    now_iso = datetime.utcnow().isoformat()
    realized_pnl_delta = 0.0

    # 8. 执行买卖逻辑
    if side == "buy":
        # 资金检查（使用对应货币的账户）
        cash = acc.get("cash", {})
        if isinstance(cash, dict):
            available_cash = float(cash.get(currency, 0.0))
        else:
            # 兼容旧格式
            available_cash = float(cash) if currency == "CNY" else 0.0

        if available_cash < total_cost:
            raise HTTPException(
                status_code=400,
                detail=f"可用{currency}不足：需要 {total_cost:.2f}，可用 {available_cash:.2f}"
            )

        # 扣除资金（从对应货币账户）
        new_cash = round(available_cash - total_cost, 2)
        await db["paper_accounts"].update_one(
            {"user_id": current_user["id"]},
            {"$set": {f"cash.{currency}": new_cash, "updated_at": now_iso}}
        )

        # 更新/创建持仓：加权平均成本
        if not pos:
            new_pos = {
                "user_id": current_user["id"],
                "code": normalized_code,
                "market": market,
                "currency": currency,
                "quantity": qty,
                "available_qty": qty if market != "CN" else 0,  # A股T+1，今天买入不可用
                "frozen_qty": 0,
                "avg_cost": price,
                "updated_at": now_iso
            }
            await db["paper_positions"].insert_one(new_pos)
        else:
            old_qty = int(pos.get("quantity", 0))
            old_cost = float(pos.get("avg_cost", 0.0))
            new_qty = old_qty + qty
            new_avg = round((old_cost * old_qty + price * qty) / new_qty, 4) if new_qty > 0 else price

            # A股T+1：新买入的不可用
            if market == "CN":
                new_available = pos.get("available_qty", old_qty)  # 保持原有可用数量
            else:
                new_available = new_qty  # 港股/美股T+0，全部可用

            await db["paper_positions"].update_one(
                {"_id": pos["_id"]},
                {"$set": {
                    "quantity": new_qty,
                    "available_qty": new_available,
                    "avg_cost": new_avg,
                    "updated_at": now_iso
                }}
            )

    else:  # sell
        # 检查可用数量（考虑T+1）
        available_qty = await _get_available_quantity(current_user["id"], normalized_code, market)
        if available_qty < qty:
            raise HTTPException(
                status_code=400,
                detail=f"可用持仓不足：需要 {qty}，可用 {available_qty}"
            )

        old_qty = int(pos.get("quantity", 0))
        avg_cost = float(pos.get("avg_cost", 0.0))
        new_qty = old_qty - qty
        pnl = round((price - avg_cost) * qty, 2)
        realized_pnl_delta = pnl

        # 卖出收入（加到对应货币账户，扣除手续费）
        net_proceeds = notional - commission
        await db["paper_accounts"].update_one(
            {"user_id": current_user["id"]},
            {
                "$inc": {
                    f"cash.{currency}": net_proceeds,
                    f"realized_pnl.{currency}": realized_pnl_delta
                },
                "$set": {"updated_at": now_iso}
            }
        )

        # 更新持仓
        if new_qty == 0:
            await db["paper_positions"].delete_one({"_id": pos["_id"]})
        else:
            new_available = max(0, pos.get("available_qty", old_qty) - qty)
            await db["paper_positions"].update_one(
                {"_id": pos["_id"]},
                {"$set": {
                    "quantity": new_qty,
                    "available_qty": new_available,
                    "updated_at": now_iso
                }}
            )

    # 9. 记录订单与成交（即成）
    order_doc = {
        "user_id": current_user["id"],
        "code": normalized_code,
        "market": market,
        "currency": currency,
        "side": side,
        "quantity": qty,
        "price": price,
        "amount": notional,
        "commission": commission,
        "status": "filled",
        "created_at": now_iso,
        "filled_at": now_iso,
    }
    if analysis_id:
        order_doc["analysis_id"] = analysis_id
    await db["paper_orders"].insert_one(order_doc)

    trade_doc = {
        "user_id": current_user["id"],
        "code": normalized_code,
        "market": market,
        "currency": currency,
        "side": side,
        "quantity": qty,
        "price": price,
        "amount": notional,
        "commission": commission,
        "pnl": realized_pnl_delta if side == "sell" else 0.0,
        "timestamp": now_iso,
    }
    if analysis_id:
        trade_doc["analysis_id"] = analysis_id
    await db["paper_trades"].insert_one(trade_doc)

    return ok({"order": {k: v for k, v in order_doc.items() if k != "_id"}})


@router.get("/positions", response_model=dict)
async def list_positions(current_user: dict = Depends(get_current_user)):
    """获取持仓列表（支持多市场）"""
    db = get_mongo_db()
    items = await db["paper_positions"].find({"user_id": current_user["id"]}).to_list(None)
    enriched: List[Dict[str, Any]] = []
    for p in items:
        code = p.get("code")
        market = p.get("market", "CN")
        currency = p.get("currency", "CNY")
        qty = int(p.get("quantity", 0))
        available_qty = p.get("available_qty", qty)
        avg_cost = float(p.get("avg_cost", 0.0))

        last = await _get_last_price(code, market)
        mkt = round((last or 0.0) * qty, 2)
        enriched.append({
            "code": code,
            "market": market,
            "currency": currency,
            "quantity": qty,
            "available_qty": available_qty,
            "avg_cost": avg_cost,
            "last_price": last,
            "market_value": mkt,
            "unrealized_pnl": None if last is None else round((last - avg_cost) * qty, 2)
        })
    return ok({"items": enriched})


@router.get("/orders", response_model=dict)
async def list_orders(limit: int = Query(50, ge=1, le=200), current_user: dict = Depends(get_current_user)):
    db = get_mongo_db()
    cursor = db["paper_orders"].find({"user_id": current_user["id"]}).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(None)
    # 去除 _id
    cleaned = [{k: v for k, v in it.items() if k != "_id"} for it in items]
    return ok({"items": cleaned})


@router.post("/reset", response_model=dict)
async def reset_account(confirm: bool = Query(False), current_user: dict = Depends(get_current_user)):
    """重置账户（支持多货币）"""
    if not confirm:
        raise HTTPException(status_code=400, detail="请设置 confirm=true 以确认重置")
    db = get_mongo_db()
    await db["paper_accounts"].delete_many({"user_id": current_user["id"]})
    await db["paper_positions"].delete_many({"user_id": current_user["id"]})
    await db["paper_orders"].delete_many({"user_id": current_user["id"]})
    await db["paper_trades"].delete_many({"user_id": current_user["id"]})
    # 重新创建账户
    acc = await _get_or_create_account(current_user["id"])
    return ok({"message": "账户已重置", "cash": acc.get("cash", {})})


@router.post("/sync/longport/positions", response_model=dict)
async def sync_longport_positions(
    payload: LongportSyncRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    从 Longport 同步持仓到 paper_positions，用于后续 agent 调仓分析。
    """
    user_id = current_user["id"]
    db = get_mongo_db()

    try:
        from longport.openapi import Config, TradeContext
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Longport SDK 未安装，请先安装依赖：pip install longport",
        )

    app_key, app_secret, access_token = await _get_longport_credentials()
    if not (app_key and app_secret and access_token):
        raise HTTPException(
            status_code=400,
            detail="Longport 凭证不完整，请配置 LONGPORT_APP_KEY/LONGPORT_APP_SECRET/LONGPORT_ACCESS_TOKEN",
        )

    # 给 SDK 提供环境变量（Config.from_env 读取）
    os.environ["LONGPORT_APP_KEY"] = app_key
    os.environ["LONGPORT_APP_SECRET"] = app_secret
    os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

    now_iso = datetime.utcnow().isoformat()

    # 确保账户存在
    await _get_or_create_account(user_id)

    # 拉取长桥持仓与余额
    try:
        ctx = TradeContext(Config.from_env())
        stock_resp = await asyncio.to_thread(ctx.stock_positions, payload.symbols)
        balances = await asyncio.to_thread(ctx.account_balance)
    except Exception as e:
        logger.error(f"❌ Longport 持仓同步失败: {e}")
        raise HTTPException(status_code=502, detail=f"Longport 接口调用失败: {str(e)}")

    channels = getattr(stock_resp, "channels", []) or []
    upsert_count = 0
    skip_count = 0
    synced_keys = set()
    synced_positions: List[Dict[str, Any]] = []
    aggregated: Dict[str, Dict[str, Any]] = {}

    for ch in channels:
        channel_name = str(getattr(ch, "account_channel", "") or "")
        positions = getattr(ch, "positions", []) or []
        for p in positions:
            symbol = str(getattr(p, "symbol", "") or "")
            if not symbol:
                skip_count += 1
                continue

            market = _longport_market_to_internal(getattr(p, "market", None), symbol)
            code = _normalize_longport_symbol(symbol, market)
            if not code:
                skip_count += 1
                continue

            raw_qty = float(getattr(p, "quantity", 0) or 0)
            raw_available = float(getattr(p, "available_quantity", 0) or 0)
            qty = raw_qty
            available_qty = raw_available
            if qty <= 0:
                skip_count += 1
                continue

            key = f"{market}:{code}"
            cost_price = float(getattr(p, "cost_price", 0) or 0)
            currency = str(getattr(p, "currency", "") or ("CNY" if market == "CN" else "HKD" if market == "HK" else "USD"))
            symbol_name = str(getattr(p, "symbol_name", "") or "")
            row = aggregated.get(key)
            if row is None:
                aggregated[key] = {
                    "user_id": user_id,
                    "code": code,
                    "market": market,
                    "currency": currency,
                    "quantity": qty,
                    "available_qty": max(0.0, available_qty),
                    "avg_cost_amount": (cost_price * qty) if cost_price > 0 else 0.0,
                    "avg_cost_qty": qty if cost_price > 0 else 0.0,
                    "symbol_name": symbol_name,
                    "source": "longport",
                    "source_channels": {channel_name} if channel_name else set(),
                    "external_symbol": symbol,
                    "raw_quantity": qty,
                    "raw_available_quantity": max(0.0, available_qty),
                    "updated_at": now_iso,
                }
            else:
                row["quantity"] += qty
                row["available_qty"] += max(0.0, available_qty)
                if cost_price > 0:
                    row["avg_cost_amount"] += cost_price * qty
                    row["avg_cost_qty"] += qty
                row["raw_quantity"] += qty
                row["raw_available_quantity"] += max(0.0, available_qty)
                if channel_name:
                    row["source_channels"].add(channel_name)
                if not row.get("symbol_name") and symbol_name:
                    row["symbol_name"] = symbol_name

    for key, item in aggregated.items():
        qty = float(item.get("quantity") or 0.0)
        available_qty = float(item.get("available_qty") or 0.0)
        available_qty = max(0.0, min(available_qty, qty))
        avg_cost_qty = float(item.get("avg_cost_qty") or 0.0)
        avg_cost_amount = float(item.get("avg_cost_amount") or 0.0)
        avg_cost = (avg_cost_amount / avg_cost_qty) if avg_cost_qty > 0 else 0.0

        doc = {
            "user_id": user_id,
            "code": item["code"],
            "market": item["market"],
            "currency": item["currency"],
            "quantity": qty,
            "available_qty": available_qty,
            "frozen_qty": max(0.0, qty - available_qty),
            "avg_cost": avg_cost,
            "symbol_name": item.get("symbol_name") or "",
            "source": "longport",
            "source_channel": ",".join(sorted(item.get("source_channels") or [])),
            "external_symbol": item.get("external_symbol") or "",
            "raw_quantity": float(item.get("raw_quantity") or 0.0),
            "raw_available_quantity": float(item.get("raw_available_quantity") or 0.0),
            "updated_at": now_iso,
        }

        await db["paper_positions"].update_one(
            {"user_id": user_id, "code": item["code"], "market": item["market"]},
            {"$set": doc},
            upsert=True,
        )
        synced_keys.add(key)
        synced_positions.append(
            {
                "code": item["code"],
                "market": item["market"],
                "quantity": qty,
                "external_symbol": item.get("external_symbol") or "",
            }
        )
        upsert_count += 1

    # 替换旧 longport 持仓（不影响手工/其他来源持仓）
    removed_count = 0
    if payload.replace_existing_longport_positions:
        existing = await db["paper_positions"].find({"user_id": user_id, "source": "longport"}).to_list(None)
        for pos in existing:
            key = f"{pos.get('market', 'CN')}:{pos.get('code', '')}"
            if key not in synced_keys:
                await db["paper_positions"].delete_one({"_id": pos["_id"]})
                removed_count += 1

    # 同步现金（可选）
    cash_updates: Dict[str, float] = {}
    if payload.sync_cash:
        for b in (balances or []):
            # 优先用 cash_infos.available_cash
            cur = str(getattr(b, "currency", "") or "").upper()
            if not cur:
                continue
            available_cash = None
            cash_infos = getattr(b, "cash_infos", []) or []
            for ci in cash_infos:
                ci_cur = str(getattr(ci, "currency", "") or "").upper()
                if ci_cur == cur:
                    available_cash = float(getattr(ci, "available_cash", 0) or 0)
                    break
            if available_cash is None:
                available_cash = float(getattr(b, "total_cash", 0) or 0)
            cash_updates[cur] = round(available_cash, 2)

        if cash_updates:
            set_doc = {f"cash.{k}": v for k, v in cash_updates.items()}
            set_doc["updated_at"] = now_iso
            await db["paper_accounts"].update_one({"user_id": user_id}, {"$set": set_doc}, upsert=True)

    # 记录同步日志（供后续 agent / 审计查询）
    await db["paper_portfolio_sync_logs"].insert_one({
        "user_id": user_id,
        "source": "longport",
        "synced_positions_count": upsert_count,
        "removed_positions_count": removed_count,
        "skipped_positions_count": skip_count,
        "synced_cash": cash_updates,
        "replace_existing_longport_positions": payload.replace_existing_longport_positions,
        "sync_cash": payload.sync_cash,
        "symbols_filter": payload.symbols or [],
        "created_at": now_iso,
    })

    return ok({
        "source": "longport",
        "synced_positions_count": upsert_count,
        "removed_positions_count": removed_count,
        "skipped_positions_count": skip_count,
        "synced_cash": cash_updates,
        "positions_preview": synced_positions[:20],
        "updated_at": now_iso,
    })
