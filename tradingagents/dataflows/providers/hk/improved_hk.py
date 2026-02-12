#!/usr/bin/env python3
"""
改进的港股数据获取工具
解决API速率限制和数据获取问题
"""

import time
import json
import os
import pandas as pd
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from tradingagents.config.runtime_settings import get_int
# 导入统一日志系统
from tradingagents.utils.logging_init import get_logger
logger = get_logger("default")

# 新增：使用统一的数据目录配置
try:
    from utils.data_config import get_cache_dir
except Exception:
    # 回退：在项目根目录下的 data/cache/hk
    def get_cache_dir(subdir: Optional[str] = None, create: bool = True):
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data', 'cache')
        if subdir:
            base = os.path.join(base, subdir)
        if create:
            os.makedirs(base, exist_ok=True)
        return base


class ImprovedHKStockProvider:
    """改进的港股数据提供器"""
    
    def __init__(self):
        # 将缓存文件写入到统一的数据缓存目录下，避免污染项目根目录
        hk_cache_dir = get_cache_dir('hk')
        if hasattr(hk_cache_dir, 'joinpath'):  # Path
            self.cache_file = str(hk_cache_dir.joinpath('hk_stock_cache.json'))
        else:  # str
            self.cache_file = os.path.join(hk_cache_dir, 'hk_stock_cache.json')

        self.cache_ttl = get_int("TA_HK_CACHE_TTL_SECONDS", "ta_hk_cache_ttl_seconds", 3600 * 24)
        self.rate_limit_wait = get_int("TA_HK_RATE_LIMIT_WAIT_SECONDS", "ta_hk_rate_limit_wait_seconds", 5)
        self.last_request_time = 0

        # 内置港股名称映射（避免API调用）
        self.hk_stock_names = {
            # 腾讯系
            '0700.HK': '腾讯控股', '0700': '腾讯控股', '00700': '腾讯控股',
            
            # 电信运营商
            '0941.HK': '中国移动', '0941': '中国移动', '00941': '中国移动',
            '0762.HK': '中国联通', '0762': '中国联通', '00762': '中国联通',
            '0728.HK': '中国电信', '0728': '中国电信', '00728': '中国电信',
            
            # 银行
            '0939.HK': '建设银行', '0939': '建设银行', '00939': '建设银行',
            '1398.HK': '工商银行', '1398': '工商银行', '01398': '工商银行',
            '3988.HK': '中国银行', '3988': '中国银行', '03988': '中国银行',
            '0005.HK': '汇丰控股', '0005': '汇丰控股', '00005': '汇丰控股',
            
            # 保险
            '1299.HK': '友邦保险', '1299': '友邦保险', '01299': '友邦保险',
            '2318.HK': '中国平安', '2318': '中国平安', '02318': '中国平安',
            '2628.HK': '中国人寿', '2628': '中国人寿', '02628': '中国人寿',
            
            # 石油化工
            '0857.HK': '中国石油', '0857': '中国石油', '00857': '中国石油',
            '0386.HK': '中国石化', '0386': '中国石化', '00386': '中国石化',
            
            # 地产
            '1109.HK': '华润置地', '1109': '华润置地', '01109': '华润置地',
            '1997.HK': '九龙仓置业', '1997': '九龙仓置业', '01997': '九龙仓置业',
            
            # 科技
            '9988.HK': '阿里巴巴', '9988': '阿里巴巴', '09988': '阿里巴巴',
            '3690.HK': '美团', '3690': '美团', '03690': '美团',
            '1024.HK': '快手', '1024': '快手', '01024': '快手',
            '9618.HK': '京东集团', '9618': '京东集团', '09618': '京东集团',
            
            # 消费
            '1876.HK': '百威亚太', '1876': '百威亚太', '01876': '百威亚太',
            '0291.HK': '华润啤酒', '0291': '华润啤酒', '00291': '华润啤酒',
            
            # 医药
            '1093.HK': '石药集团', '1093': '石药集团', '01093': '石药集团',
            '0867.HK': '康师傅', '0867': '康师傅', '00867': '康师傅',
            
            # 汽车
            '2238.HK': '广汽集团', '2238': '广汽集团', '02238': '广汽集团',
            '1211.HK': '比亚迪', '1211': '比亚迪', '01211': '比亚迪',
            
            # 航空
            '0753.HK': '中国国航', '0753': '中国国航', '00753': '中国国航',
            '0670.HK': '中国东航', '0670': '中国东航', '00670': '中国东航',
            
            # 钢铁
            '0347.HK': '鞍钢股份', '0347': '鞍钢股份', '00347': '鞍钢股份',
            
            # 电力
            '0902.HK': '华能国际', '0902': '华能国际', '00902': '华能国际',
            '0991.HK': '大唐发电', '0991': '大唐发电', '00991': '大唐发电'
        }
        
        self._load_cache()
    
    def _load_cache(self):
        """加载缓存"""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.cache = json.load(f)
            else:
                self.cache = {}
        except Exception as e:
            logger.debug(f"📊 [港股缓存] 加载缓存失败: {e}")
            self.cache = {}
    
    def _save_cache(self):
        """保存缓存"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"📊 [港股缓存] 保存缓存失败: {e}")
    
    def _is_cache_valid(self, key: str) -> bool:
        """检查缓存是否有效"""
        if key not in self.cache:
            return False

        cache_time = self.cache[key].get('timestamp', 0)
        return (time.time() - cache_time) < self.cache_ttl

    def _rate_limit(self):
        """速率限制：确保两次请求之间有足够的间隔"""
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time

        if time_since_last_request < self.rate_limit_wait:
            wait_time = self.rate_limit_wait - time_since_last_request
            logger.debug(f"⏱️ [速率限制] 等待 {wait_time:.2f} 秒")
            time.sleep(wait_time)

        self.last_request_time = time.time()

    def _normalize_hk_symbol(self, symbol: str) -> str:
        """标准化港股代码"""
        # 移除.HK后缀
        clean_symbol = symbol.replace('.HK', '').replace('.hk', '')
        
        # 补齐到5位数字
        if len(clean_symbol) == 4:
            clean_symbol = '0' + clean_symbol
        elif len(clean_symbol) == 3:
            clean_symbol = '00' + clean_symbol
        elif len(clean_symbol) == 2:
            clean_symbol = '000' + clean_symbol
        elif len(clean_symbol) == 1:
            clean_symbol = '0000' + clean_symbol
        
        return clean_symbol
    
    def get_company_name(self, symbol: str) -> str:
        """
        获取港股公司名称
        
        Args:
            symbol: 港股代码
            
        Returns:
            str: 公司名称
        """
        try:
            # 检查缓存
            cache_key = f"name_{symbol}"
            if self._is_cache_valid(cache_key):
                cached_name = self.cache[cache_key]['data']
                logger.debug(f"📊 [港股缓存] 从缓存获取公司名称: {symbol} -> {cached_name}")
                return cached_name
            
            # 方案1：使用内置映射
            normalized_symbol = self._normalize_hk_symbol(symbol)
            
            # 尝试多种格式匹配
            for format_symbol in [symbol, normalized_symbol, f"{normalized_symbol}.HK"]:
                if format_symbol in self.hk_stock_names:
                    company_name = self.hk_stock_names[format_symbol]
                    
                    # 缓存结果
                    self.cache[cache_key] = {
                        'data': company_name,
                        'timestamp': time.time(),
                        'source': 'builtin_mapping'
                    }
                    self._save_cache()
                    
                    logger.debug(f"📊 [港股映射] 获取公司名称: {symbol} -> {company_name}")
                    return company_name
            
            # 方案2：优先尝试AKShare API获取（有速率限制保护）
            try:
                # 速率限制保护
                current_time = time.time()
                if current_time - self.last_request_time < self.rate_limit_wait:
                    wait_time = self.rate_limit_wait - (current_time - self.last_request_time)
                    logger.debug(f"📊 [港股API] 速率限制保护，等待 {wait_time:.1f} 秒")
                    time.sleep(wait_time)

                self.last_request_time = time.time()

                # 优先尝试AKShare获取
                try:
                    # 直接使用 akshare 库获取，避免循环调用
                    logger.debug(f"📊 [港股API] 优先使用AKShare获取: {symbol}")

                    import akshare as ak
                    # 标准化代码格式（akshare 需要 5 位数字格式）
                    normalized_symbol = self._normalize_hk_symbol(symbol)

                    # 尝试获取港股实时行情（包含名称）
                    try:
                        # 使用新浪财经接口（更稳定）
                        df = ak.stock_hk_spot()
                        if df is not None and not df.empty:
                            # 查找匹配的股票
                            matched = df[df['代码'] == normalized_symbol]
                            if not matched.empty:
                                # 新浪接口返回的列名是 '中文名称'
                                akshare_name = matched.iloc[0]['中文名称']
                                if akshare_name and not str(akshare_name).startswith('港股'):
                                    # 缓存AKShare结果
                                    self.cache[cache_key] = {
                                        'data': akshare_name,
                                        'timestamp': time.time(),
                                        'source': 'akshare_sina'
                                    }
                                    self._save_cache()

                                    logger.debug(f"📊 [港股AKShare-新浪] 获取公司名称: {symbol} -> {akshare_name}")
                                    return akshare_name
                    except Exception as e:
                        logger.debug(f"📊 [港股AKShare-新浪] 获取实时行情失败: {e}")

                except Exception as e:
                    logger.debug(f"📊 [港股AKShare] AKShare获取失败: {e}")

                # 备用：尝试从统一接口获取（包含Yahoo Finance）
                from tradingagents.dataflows.interface import get_hk_stock_info_unified
                hk_info = get_hk_stock_info_unified(symbol)

                if hk_info and isinstance(hk_info, dict) and 'name' in hk_info:
                    api_name = hk_info['name']
                    if not api_name.startswith('港股'):
                        # 缓存API结果
                        self.cache[cache_key] = {
                            'data': api_name,
                            'timestamp': time.time(),
                            'source': 'unified_api'
                        }
                        self._save_cache()

                        logger.debug(f"📊 [港股统一API] 获取公司名称: {symbol} -> {api_name}")
                        return api_name

            except Exception as e:
                logger.debug(f"📊 [港股API] API获取失败: {e}")
            
            # 方案3：生成友好的默认名称
            clean_symbol = self._normalize_hk_symbol(symbol)
            default_name = f"港股{clean_symbol}"
            
            # 缓存默认结果（较短的TTL）
            self.cache[cache_key] = {
                'data': default_name,
                'timestamp': time.time() - self.cache_ttl + 3600,  # 1小时后过期
                'source': 'default'
            }
            self._save_cache()
            
            logger.debug(f"📊 [港股默认] 使用默认名称: {symbol} -> {default_name}")
            return default_name
            
        except Exception as e:
            logger.error(f"❌ [港股] 获取公司名称失败: {e}")
            clean_symbol = self._normalize_hk_symbol(symbol)
            return f"港股{clean_symbol}"
    
    def get_financial_indicators(self, symbol: str) -> Dict[str, Any]:
        """
        获取港股财务指标

        使用 AKShare 的 stock_financial_hk_analysis_indicator_em 接口
        获取主要财务指标，包括 EPS、BPS、ROE、ROA 等

        Args:
            symbol: 港股代码

        Returns:
            Dict: 财务指标数据
        """
        try:
            import akshare as ak

            # 标准化代码
            normalized_symbol = self._normalize_hk_symbol(symbol)

            # 检查缓存
            cache_key = f"financial_{normalized_symbol}"
            if self._is_cache_valid(cache_key):
                logger.debug(f"📊 [港股财务指标] 使用缓存: {normalized_symbol}")
                return self.cache[cache_key]['data']

            # 速率限制
            self._rate_limit()

            logger.info(f"📊 [港股财务指标] 获取财务指标: {normalized_symbol}")

            # 调用 AKShare 接口
            df = ak.stock_financial_hk_analysis_indicator_em(symbol=normalized_symbol)

            if df is None or df.empty:
                logger.warning(f"⚠️ [港股财务指标] 未获取到数据: {normalized_symbol}")
                return {}

            # 获取最新一期数据
            latest = df.iloc[0]

            # 提取关键指标
            indicators = {
                # 基本信息
                'report_date': str(latest.get('REPORT_DATE', '')),
                'fiscal_year': str(latest.get('FISCAL_YEAR', '')),

                # 每股指标
                'eps_basic': float(latest.get('BASIC_EPS', 0)) if pd.notna(latest.get('BASIC_EPS')) else None,
                'eps_diluted': float(latest.get('DILUTED_EPS', 0)) if pd.notna(latest.get('DILUTED_EPS')) else None,
                'eps_ttm': float(latest.get('EPS_TTM', 0)) if pd.notna(latest.get('EPS_TTM')) else None,
                'bps': float(latest.get('BPS', 0)) if pd.notna(latest.get('BPS')) else None,
                'per_netcash_operate': float(latest.get('PER_NETCASH_OPERATE', 0)) if pd.notna(latest.get('PER_NETCASH_OPERATE')) else None,

                # 盈利能力指标
                'roe_avg': float(latest.get('ROE_AVG', 0)) if pd.notna(latest.get('ROE_AVG')) else None,
                'roe_yearly': float(latest.get('ROE_YEARLY', 0)) if pd.notna(latest.get('ROE_YEARLY')) else None,
                'roa': float(latest.get('ROA', 0)) if pd.notna(latest.get('ROA')) else None,
                'roic_yearly': float(latest.get('ROIC_YEARLY', 0)) if pd.notna(latest.get('ROIC_YEARLY')) else None,
                'net_profit_ratio': float(latest.get('NET_PROFIT_RATIO', 0)) if pd.notna(latest.get('NET_PROFIT_RATIO')) else None,
                'gross_profit_ratio': float(latest.get('GROSS_PROFIT_RATIO', 0)) if pd.notna(latest.get('GROSS_PROFIT_RATIO')) else None,

                # 营收指标
                'operate_income': float(latest.get('OPERATE_INCOME', 0)) if pd.notna(latest.get('OPERATE_INCOME')) else None,
                'operate_income_yoy': float(latest.get('OPERATE_INCOME_YOY', 0)) if pd.notna(latest.get('OPERATE_INCOME_YOY')) else None,
                'operate_income_qoq': float(latest.get('OPERATE_INCOME_QOQ', 0)) if pd.notna(latest.get('OPERATE_INCOME_QOQ')) else None,
                'gross_profit': float(latest.get('GROSS_PROFIT', 0)) if pd.notna(latest.get('GROSS_PROFIT')) else None,
                'gross_profit_yoy': float(latest.get('GROSS_PROFIT_YOY', 0)) if pd.notna(latest.get('GROSS_PROFIT_YOY')) else None,
                'holder_profit': float(latest.get('HOLDER_PROFIT', 0)) if pd.notna(latest.get('HOLDER_PROFIT')) else None,
                'holder_profit_yoy': float(latest.get('HOLDER_PROFIT_YOY', 0)) if pd.notna(latest.get('HOLDER_PROFIT_YOY')) else None,

                # 偿债能力指标
                'debt_asset_ratio': float(latest.get('DEBT_ASSET_RATIO', 0)) if pd.notna(latest.get('DEBT_ASSET_RATIO')) else None,
                'current_ratio': float(latest.get('CURRENT_RATIO', 0)) if pd.notna(latest.get('CURRENT_RATIO')) else None,

                # 现金流指标
                'ocf_sales': float(latest.get('OCF_SALES', 0)) if pd.notna(latest.get('OCF_SALES')) else None,

                # 数据源
                'source': 'akshare_eastmoney',
                'data_count': len(df)
            }

            # 缓存数据
            self.cache[cache_key] = {
                'data': indicators,
                'timestamp': time.time()
            }
            self._save_cache()

            logger.info(f"✅ [港股财务指标] 成功获取: {normalized_symbol}, 报告期: {indicators['report_date']}")
            return indicators

        except Exception as e:
            logger.error(f"❌ [港股财务指标] 获取失败: {symbol} - {e}")
            return {}

    def get_stock_info(self, symbol: str) -> Dict[str, Any]:
        """
        获取港股基本信息

        Args:
            symbol: 港股代码

        Returns:
            Dict: 港股信息
        """
        try:
            company_name = self.get_company_name(symbol)

            return {
                'symbol': symbol,
                'name': company_name,
                'currency': 'HKD',
                'exchange': 'HKG',
                'market': '港股',
                'source': 'improved_hk_provider'
            }
            
        except Exception as e:
            logger.error(f"❌ [港股] 获取股票信息失败: {e}")
            clean_symbol = self._normalize_hk_symbol(symbol)
            return {
                'symbol': symbol,
                'name': f'港股{clean_symbol}',
                'currency': 'HKD',
                'exchange': 'HKG',
                'market': '港股',
                'source': 'error',
                'error': str(e)
            }


# 全局实例
_improved_hk_provider = None

def get_improved_hk_provider() -> ImprovedHKStockProvider:
    """获取改进的港股提供器实例"""
    global _improved_hk_provider
    if _improved_hk_provider is None:
        _improved_hk_provider = ImprovedHKStockProvider()
    return _improved_hk_provider


def get_hk_company_name_improved(symbol: str) -> str:
    """
    获取港股公司名称的改进版本
    
    Args:
        symbol: 港股代码
        
    Returns:
        str: 公司名称
    """
    provider = get_improved_hk_provider()
    return provider.get_company_name(symbol)


def get_hk_stock_info_improved(symbol: str) -> Dict[str, Any]:
    """
    获取港股信息的改进版本

    Args:
        symbol: 港股代码

    Returns:
        Dict: 港股信息
    """
    provider = get_improved_hk_provider()
    return provider.get_stock_info(symbol)


def get_hk_financial_indicators(symbol: str) -> Dict[str, Any]:
    """
    获取港股财务指标

    Args:
        symbol: 港股代码

    Returns:
        Dict: 财务指标数据，包括：
            - eps_basic: 基本每股收益
            - eps_ttm: 滚动每股收益
            - bps: 每股净资产
            - roe_avg: 平均净资产收益率
            - roa: 总资产收益率
            - operate_income: 营业收入
            - operate_income_yoy: 营业收入同比增长率
            - debt_asset_ratio: 资产负债率
            等
    """
    provider = get_improved_hk_provider()
    return provider.get_financial_indicators(symbol)


def _get_hk_realtime_quote_akshare_no_cache(symbol: str) -> Dict[str, Any]:
    """
    从 AKShare 直接读取港股实时快照（不使用任何本地缓存）。

    Args:
        symbol: 港股代码

    Returns:
        Dict: 实时行情字段，失败返回空字典
    """
    try:
        import akshare as ak

        provider = get_improved_hk_provider()
        normalized_symbol = provider._normalize_hk_symbol(symbol)

        # 直接拉取实时快照，不走本模块缓存
        df = ak.stock_hk_spot()
        if df is None or df.empty:
            return {}

        matched = df[df['代码'] == normalized_symbol]
        if matched.empty:
            return {}

        row = matched.iloc[0]

        def safe_float(value):
            try:
                if value is None or value == '' or (isinstance(value, float) and value != value):
                    return None
                return float(value)
            except Exception:
                return None

        def safe_int(value):
            try:
                if value is None or value == '' or (isinstance(value, float) and value != value):
                    return None
                return int(value)
            except Exception:
                return None

        return {
            'price': safe_float(row.get('最新价')),
            'open': safe_float(row.get('今开')),
            'high': safe_float(row.get('最高')),
            'low': safe_float(row.get('最低')),
            'pre_close': safe_float(row.get('昨收')),
            'volume': safe_int(row.get('成交量')),
            'change_percent': safe_float(row.get('涨跌幅')),
            'source': 'akshare_spot_no_cache',
            'snapshot_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        logger.warning(f"⚠️ [AKShare-实时无缓存] 获取失败: {symbol} - {e}")
        return {}


def _read_longport_credentials() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """读取 LongPort 凭证：环境变量优先，数据库配置兜底。"""
    app_key = os.getenv("LONGPORT_APP_KEY")
    app_secret = os.getenv("LONGPORT_APP_SECRET")
    access_token = os.getenv("LONGPORT_ACCESS_TOKEN")

    if app_key and app_secret and access_token:
        return app_key, app_secret, access_token

    try:
        from app.core.database import get_mongo_db_sync
        db = get_mongo_db_sync()
        config_data = db.system_configs.find_one({"is_active": True}, sort=[("version", -1)])
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
        logger.debug(f"📊 [LongPort] 读取数据库配置失败: {e}")

    return app_key, app_secret, access_token


def _to_longport_hk_symbol(symbol: str) -> str:
    provider = get_improved_hk_provider()
    return f"{provider._normalize_hk_symbol(symbol)}.HK"


def get_hk_stock_info_longport(symbol: str) -> Dict[str, Any]:
    """
    使用 LongPort 获取港股基础信息与实时价格。
    """
    try:
        from longport.openapi import Config, QuoteContext
    except Exception as e:
        return {
            'symbol': symbol,
            'name': f'港股{symbol}',
            'currency': 'HKD',
            'exchange': 'HKG',
            'source': 'longport',
            'error': f'LongPort SDK不可用: {e}',
        }

    app_key, app_secret, access_token = _read_longport_credentials()
    if not (app_key and app_secret and access_token):
        return {
            'symbol': symbol,
            'name': f'港股{symbol}',
            'currency': 'HKD',
            'exchange': 'HKG',
            'source': 'longport',
            'error': 'LongPort凭证未配置',
        }

    os.environ["LONGPORT_APP_KEY"] = app_key
    os.environ["LONGPORT_APP_SECRET"] = app_secret
    os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

    lp_symbol = _to_longport_hk_symbol(symbol)
    cfg = Config.from_env()
    ctx = QuoteContext(cfg)

    def safe_float(value):
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    static_item = None
    quote_item = None

    try:
        static_list = ctx.static_info([lp_symbol]) or []
        if static_list:
            static_item = static_list[0]
    except Exception:
        pass

    try:
        quote_list = ctx.quote([lp_symbol]) or []
        if quote_list:
            quote_item = quote_list[0]
    except Exception:
        pass

    name = None
    if static_item is not None:
        name = (
            getattr(static_item, "name_hk", None)
            or getattr(static_item, "name_cn", None)
            or getattr(static_item, "name_en", None)
        )

    return {
        'symbol': symbol,
        'name': name or f'港股{lp_symbol}',
        'price': safe_float(getattr(quote_item, "last_done", None)) if quote_item else None,
        'open': safe_float(getattr(quote_item, "open", None)) if quote_item else None,
        'high': safe_float(getattr(quote_item, "high", None)) if quote_item else None,
        'low': safe_float(getattr(quote_item, "low", None)) if quote_item else None,
        'volume': safe_float(getattr(quote_item, "volume", None)) if quote_item else None,
        'change_percent': (
            ((safe_float(getattr(quote_item, "last_done", None)) / safe_float(getattr(quote_item, "prev_close", None)) - 1) * 100)
            if quote_item and safe_float(getattr(quote_item, "last_done", None)) is not None and safe_float(getattr(quote_item, "prev_close", None)) not in (None, 0)
            else None
        ),
        'currency': 'HKD',
        'exchange': 'HKG',
        'market': '港股',
        'source': 'longport',
    }


def get_hk_stock_data_longport(symbol: str, start_date: str = None, end_date: str = None) -> str:
    """
    使用 LongPort 获取港股行情（实时价 + 日线K线），并计算技术指标。
    """
    try:
        from longport.openapi import Config, QuoteContext, Period, AdjustType
    except Exception as e:
        return f"❌ LongPort SDK不可用: {e}"

    app_key, app_secret, access_token = _read_longport_credentials()
    if not (app_key and app_secret and access_token):
        return "❌ LongPort凭证未配置（LONGPORT_APP_KEY/LONGPORT_APP_SECRET/LONGPORT_ACCESS_TOKEN）"

    os.environ["LONGPORT_APP_KEY"] = app_key
    os.environ["LONGPORT_APP_SECRET"] = app_secret
    os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

    try:
        lp_symbol = _to_longport_hk_symbol(symbol)

        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')
        if not start_date:
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

        cfg = Config.from_env()
        ctx = QuoteContext(cfg)

        # 1) 实时行情
        quote_list = ctx.quote([lp_symbol]) or []
        quote_item = quote_list[0] if quote_list else None

        # 2) 历史日线
        start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_date, "%Y-%m-%d").date()

        candles = ctx.history_candlesticks_by_date(
            lp_symbol,
            Period.Day,
            AdjustType.ForwardAdjust,
            start=start_d,
            end=end_d,
        ) or []

        if not candles:
            return f"❌ LongPort未返回{symbol}在指定区间的日线数据"

        def safe_float(value):
            try:
                if value is None:
                    return None
                return float(value)
            except Exception:
                return None

        def to_dt(value):
            try:
                return pd.to_datetime(value)
            except Exception:
                try:
                    iv = int(value)
                    return pd.to_datetime(iv, unit="s")
                except Exception:
                    return pd.NaT

        rows = []
        for c in candles:
            rows.append({
                'date': to_dt(getattr(c, 'timestamp', None)),
                'open': safe_float(getattr(c, 'open', None)),
                'high': safe_float(getattr(c, 'high', None)),
                'low': safe_float(getattr(c, 'low', None)),
                'close': safe_float(getattr(c, 'close', None)),
                'volume': safe_float(getattr(c, 'volume', None)),
                'turnover': safe_float(getattr(c, 'turnover', None)),
            })

        df = pd.DataFrame(rows).dropna(subset=['date', 'close']).sort_values('date').reset_index(drop=True)
        if df.empty:
            return f"❌ LongPort返回的{symbol}日线数据为空"

        df['pre_close'] = df['close'].shift(1)
        df['change'] = df['close'] - df['pre_close']
        df['pct_change'] = (df['change'] / df['pre_close'] * 100).round(2)

        from tradingagents.tools.analysis.indicators import add_all_indicators
        df = add_all_indicators(df, close_col='close', high_col='high', low_col='low')

        latest = df.iloc[-1]

        realtime_price = safe_float(getattr(quote_item, 'last_done', None)) if quote_item else None
        realtime_pre_close = safe_float(getattr(quote_item, 'prev_close', None)) if quote_item else None
        realtime_open = safe_float(getattr(quote_item, 'open', None)) if quote_item else None
        realtime_high = safe_float(getattr(quote_item, 'high', None)) if quote_item else None
        realtime_low = safe_float(getattr(quote_item, 'low', None)) if quote_item else None
        realtime_volume = safe_float(getattr(quote_item, 'volume', None)) if quote_item else None
        snapshot_ts = str(getattr(quote_item, 'timestamp', 'N/A')) if quote_item else "N/A"

        current_price = realtime_price if realtime_price is not None else float(latest['close'])
        display_pre_close = realtime_pre_close if realtime_pre_close is not None else (float(latest['pre_close']) if pd.notna(latest['pre_close']) else None)
        display_change = (current_price - display_pre_close) if (display_pre_close not in (None, 0)) else (float(latest['change']) if pd.notna(latest['change']) else None)
        display_pct_change = ((display_change / display_pre_close) * 100) if (display_pre_close not in (None, 0) and display_change is not None) else (float(latest['pct_change']) if pd.notna(latest['pct_change']) else None)

        display_open = realtime_open if realtime_open is not None else float(latest['open'])
        display_high = realtime_high if realtime_high is not None else float(latest['high'])
        display_low = realtime_low if realtime_low is not None else float(latest['low'])
        display_volume = realtime_volume if realtime_volume is not None else float(latest['volume'])

        # 静态信息（用于名称/EPS/BPS）
        static_name = None
        eps_ttm = None
        bps = None
        try:
            static_list = ctx.static_info([lp_symbol]) or []
            if static_list:
                s = static_list[0]
                static_name = (
                    getattr(s, "name_hk", None)
                    or getattr(s, "name_cn", None)
                    or getattr(s, "name_en", None)
                )
                eps_ttm = safe_float(getattr(s, "eps_ttm", None))
                bps = safe_float(getattr(s, "bps", None))
        except Exception:
            pass

        pe_ratio = (current_price / eps_ttm) if (eps_ttm and eps_ttm > 0) else None
        pb_ratio = (current_price / bps) if (bps and bps > 0) else None

        def fmt_hk(v):
            return f"HK${float(v):.2f}" if v is not None else "N/A"

        def fmt_pct(v):
            return f"{float(v):.2f}%" if v is not None else "N/A"

        result = f"""## 港股历史数据 ({symbol})
**数据源**: LongPort OpenAPI
**LongPort代码**: {lp_symbol}
**日期范围**: {start_date} ~ {end_date}
**数据条数**: {len(df)} 条

### 最新价格信息
- 价格来源: LongPort 实时行情
- 实时快照时间: {snapshot_ts}
- 最新价: {fmt_hk(current_price)}
- 昨收: {fmt_hk(display_pre_close)}
- 涨跌额: {fmt_hk(display_change)}
- 涨跌幅: {fmt_pct(display_pct_change)}
- 今开: {fmt_hk(display_open)}
- 最高: {fmt_hk(display_high)}
- 最低: {fmt_hk(display_low)}
- 成交量: {int(display_volume):,d}

### 技术指标（最新值）
**移动平均线**:
- MA5: HK${latest['ma5']:.2f}
- MA10: HK${latest['ma10']:.2f}
- MA20: HK${latest['ma20']:.2f}
- MA60: HK${latest['ma60']:.2f}

**MACD指标**:
- DIF: {latest['macd_dif']:.2f}
- DEA: {latest['macd_dea']:.2f}
- MACD: {latest['macd']:.2f}

**RSI指标**:
- RSI(14): {latest['rsi']:.2f}

**布林带**:
- 上轨: HK${latest['boll_upper']:.2f}
- 中轨: HK${latest['boll_mid']:.2f}
- 下轨: HK${latest['boll_lower']:.2f}

### 估值快照（LongPort静态信息）
- 公司名称: {static_name or f'港股{lp_symbol}'}
- EPS_TTM: {eps_ttm if eps_ttm is not None else 'N/A'}
- BPS: {bps if bps is not None else 'N/A'}
- PE(估算): {f'{pe_ratio:.2f}' if pe_ratio is not None else 'N/A'}
- PB(估算): {f'{pb_ratio:.2f}' if pb_ratio is not None else 'N/A'}

### 最近10个交易日价格
{df[['date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_change', 'volume']].tail(10).to_string(index=False)}
"""
        logger.info(f"✅ [LongPort] 港股数据获取成功: {symbol} ({len(df)}条)")
        return result
    except Exception as e:
        logger.error(f"❌ [LongPort] 港股数据获取失败: {symbol} - {e}")
        return f"❌ LongPort获取港股{symbol}数据失败: {e}"


def _to_longport_symbol(symbol: str) -> str:
    """将 A/HK/US 代码转换为 LongPort 格式。"""
    from tradingagents.utils.stock_utils import StockUtils, StockMarket

    s = str(symbol or "").strip().upper()
    market = StockUtils.identify_stock_market(s)

    if market == StockMarket.HONG_KONG:
        return _to_longport_hk_symbol(s)

    if market == StockMarket.CHINA_A:
        code = "".join(ch for ch in s if ch.isdigit()).zfill(6)
        if code.startswith(("60", "68", "90")):
            return f"{code}.SH"
        return f"{code}.SZ"

    if market == StockMarket.US:
        base = s.replace(".US", "")
        return f"{base}.US"

    return s


def get_stock_data_longport_unified(symbol: str, start_date: str = None, end_date: str = None) -> str:
    """
    通用 LongPort 三市场行情接口（A/HK/US）：
    - 实时行情：quote
    - 历史日线：history_candlesticks_by_date
    - 技术指标：MA/MACD/RSI/BOLL
    """
    try:
        from longport.openapi import Config, QuoteContext, Period, AdjustType
    except Exception as e:
        return f"❌ LongPort SDK不可用: {e}"

    app_key, app_secret, access_token = _read_longport_credentials()
    if not (app_key and app_secret and access_token):
        return "❌ LongPort凭证未配置（LONGPORT_APP_KEY/LONGPORT_APP_SECRET/LONGPORT_ACCESS_TOKEN）"

    os.environ["LONGPORT_APP_KEY"] = app_key
    os.environ["LONGPORT_APP_SECRET"] = app_secret
    os.environ["LONGPORT_ACCESS_TOKEN"] = access_token

    try:
        from tradingagents.utils.stock_utils import StockUtils, StockMarket

        raw_symbol = str(symbol or "").strip()
        lp_symbol = _to_longport_symbol(raw_symbol)
        market = StockUtils.identify_stock_market(raw_symbol)

        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')
        if not start_date:
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

        cfg = Config.from_env()
        ctx = QuoteContext(cfg)

        def safe_float(value):
            try:
                if value is None:
                    return None
                return float(value)
            except Exception:
                return None

        def to_dt(value):
            try:
                return pd.to_datetime(value)
            except Exception:
                try:
                    return pd.to_datetime(int(value), unit="s")
                except Exception:
                    return pd.NaT

        # 实时
        quote_list = ctx.quote([lp_symbol]) or []
        quote_item = quote_list[0] if quote_list else None

        # 日线
        start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_date, "%Y-%m-%d").date()

        try:
            candles = ctx.history_candlesticks_by_date(
                lp_symbol,
                Period.Day,
                AdjustType.ForwardAdjust,
                start=start_d,
                end=end_d,
            ) or []
        except Exception:
            candles = ctx.history_candlesticks_by_date(
                lp_symbol,
                Period.Day,
                AdjustType.NoAdjust,
                start=start_d,
                end=end_d,
            ) or []

        if not candles:
            return f"❌ LongPort未返回{raw_symbol}在指定区间的日线数据"

        rows = []
        for c in candles:
            rows.append({
                "date": to_dt(getattr(c, "timestamp", None)),
                "open": safe_float(getattr(c, "open", None)),
                "high": safe_float(getattr(c, "high", None)),
                "low": safe_float(getattr(c, "low", None)),
                "close": safe_float(getattr(c, "close", None)),
                "volume": safe_float(getattr(c, "volume", None)),
                "turnover": safe_float(getattr(c, "turnover", None)),
            })

        df = pd.DataFrame(rows).dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
        if df.empty:
            return f"❌ LongPort返回的{raw_symbol}日线数据为空"

        df["pre_close"] = df["close"].shift(1)
        df["change"] = df["close"] - df["pre_close"]
        df["pct_change"] = (df["change"] / df["pre_close"] * 100).round(2)

        from tradingagents.tools.analysis.indicators import add_all_indicators
        df = add_all_indicators(df, close_col="close", high_col="high", low_col="low")
        latest = df.iloc[-1]

        realtime_price = safe_float(getattr(quote_item, "last_done", None)) if quote_item else None
        realtime_pre_close = safe_float(getattr(quote_item, "prev_close", None)) if quote_item else None
        realtime_open = safe_float(getattr(quote_item, "open", None)) if quote_item else None
        realtime_high = safe_float(getattr(quote_item, "high", None)) if quote_item else None
        realtime_low = safe_float(getattr(quote_item, "low", None)) if quote_item else None
        realtime_volume = safe_float(getattr(quote_item, "volume", None)) if quote_item else None
        snapshot_ts = str(getattr(quote_item, "timestamp", "N/A")) if quote_item else "N/A"

        current_price = realtime_price if realtime_price is not None else float(latest["close"])
        display_pre_close = realtime_pre_close if realtime_pre_close is not None else (float(latest["pre_close"]) if pd.notna(latest["pre_close"]) else None)
        display_change = (current_price - display_pre_close) if (display_pre_close not in (None, 0)) else (float(latest["change"]) if pd.notna(latest["change"]) else None)
        display_pct_change = ((display_change / display_pre_close) * 100) if (display_pre_close not in (None, 0) and display_change is not None) else (float(latest["pct_change"]) if pd.notna(latest["pct_change"]) else None)
        display_open = realtime_open if realtime_open is not None else float(latest["open"])
        display_high = realtime_high if realtime_high is not None else float(latest["high"])
        display_low = realtime_low if realtime_low is not None else float(latest["low"])
        display_volume = realtime_volume if realtime_volume is not None else float(latest["volume"])

        market_label = "未知市场"
        currency_symbol = ""
        if market == StockMarket.CHINA_A:
            market_label = "A股"
            currency_symbol = "¥"
        elif market == StockMarket.HONG_KONG:
            market_label = "港股"
            currency_symbol = "HK$"
        elif market == StockMarket.US:
            market_label = "美股"
            currency_symbol = "$"

        def fmt_price(v):
            return f"{currency_symbol}{float(v):.2f}" if v is not None else "N/A"

        def fmt_pct(v):
            return f"{float(v):.2f}%" if v is not None else "N/A"

        result = f"""## {market_label}历史数据 ({raw_symbol})
**数据源**: LongPort OpenAPI
**LongPort代码**: {lp_symbol}
**日期范围**: {start_date} ~ {end_date}
**数据条数**: {len(df)} 条

### 最新价格信息
- 价格来源: LongPort 实时行情
- 实时快照时间: {snapshot_ts}
- 最新价: {fmt_price(current_price)}
- 昨收: {fmt_price(display_pre_close)}
- 涨跌额: {fmt_price(display_change)}
- 涨跌幅: {fmt_pct(display_pct_change)}
- 今开: {fmt_price(display_open)}
- 最高: {fmt_price(display_high)}
- 最低: {fmt_price(display_low)}
- 成交量: {int(display_volume):,d}

### 技术指标（最新值）
**移动平均线**:
- MA5: {currency_symbol}{latest['ma5']:.2f}
- MA10: {currency_symbol}{latest['ma10']:.2f}
- MA20: {currency_symbol}{latest['ma20']:.2f}
- MA60: {currency_symbol}{latest['ma60']:.2f}

**MACD指标**:
- DIF: {latest['macd_dif']:.2f}
- DEA: {latest['macd_dea']:.2f}
- MACD: {latest['macd']:.2f}

**RSI指标**:
- RSI(14): {latest['rsi']:.2f}

**布林带**:
- 上轨: {currency_symbol}{latest['boll_upper']:.2f}
- 中轨: {currency_symbol}{latest['boll_mid']:.2f}
- 下轨: {currency_symbol}{latest['boll_lower']:.2f}
"""
        logger.info(f"✅ [LongPort-统一] {market_label}数据获取成功: {raw_symbol} ({len(df)}条)")
        return result
    except Exception as e:
        logger.error(f"❌ [LongPort-统一] 数据获取失败: {symbol} - {e}")
        return f"❌ LongPort获取{symbol}数据失败: {e}"


# 兼容性函数：为了兼容旧的 akshare_utils 导入
def get_hk_stock_data_akshare(symbol: str, start_date: str = None, end_date: str = None):
    """
    兼容性函数：使用 AKShare 新浪财经接口获取港股历史数据

    Args:
        symbol: 港股代码
        start_date: 开始日期
        end_date: 结束日期

    Returns:
        港股数据（格式化字符串）
    """
    try:
        import akshare as ak
        from datetime import datetime, timedelta

        # 标准化代码
        provider = get_improved_hk_provider()
        normalized_symbol = provider._normalize_hk_symbol(symbol)

        # 设置默认日期
        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')
        if not start_date:
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

        logger.info(f"🔄 [AKShare-新浪] 获取港股历史数据: {symbol} ({start_date} ~ {end_date})")

        # 使用新浪财经接口获取历史数据
        df = ak.stock_hk_daily(symbol=normalized_symbol, adjust="qfq")

        if df is None or df.empty:
            logger.warning(f"⚠️ [AKShare-新浪] 返回空数据: {symbol}")
            return f"❌ 无法获取港股{symbol}的历史数据"

        # 过滤日期范围
        df['date'] = pd.to_datetime(df['date'])
        mask = (df['date'] >= start_date) & (df['date'] <= end_date)
        df = df.loc[mask]

        if df.empty:
            logger.warning(f"⚠️ [AKShare-新浪] 日期范围内无数据: {symbol}")
            return f"❌ 港股{symbol}在指定日期范围内无数据"

        # 🔥 添加 pre_close 字段（从前一天的 close 获取）
        # AKShare 不返回 pre_close 字段，需要手动计算
        df['pre_close'] = df['close'].shift(1)

        # 计算涨跌额和涨跌幅
        df['change'] = df['close'] - df['pre_close']
        df['pct_change'] = (df['change'] / df['pre_close'] * 100).round(2)

        # 🔥 使用统一的技术指标计算函数
        from tradingagents.tools.analysis.indicators import add_all_indicators
        df = add_all_indicators(df, close_col='close', high_col='high', low_col='low')

        # 🔥 获取财务指标并计算 PE、PB
        financial_indicators = provider.get_financial_indicators(symbol)

        # 格式化输出（包含价格数据和技术指标）
        latest = df.iloc[-1]
        daily_close = float(latest['close'])

        # 当前价格/涨跌幅：强制直连实时快照（不使用缓存）
        realtime_quote = _get_hk_realtime_quote_akshare_no_cache(symbol)
        use_realtime = bool(realtime_quote and realtime_quote.get('price') is not None)

        current_price = float(realtime_quote['price']) if use_realtime else daily_close
        display_open = float(realtime_quote.get('open')) if use_realtime and realtime_quote.get('open') is not None else float(latest['open'])
        display_high = float(realtime_quote.get('high')) if use_realtime and realtime_quote.get('high') is not None else float(latest['high'])
        display_low = float(realtime_quote.get('low')) if use_realtime and realtime_quote.get('low') is not None else float(latest['low'])
        display_volume = int(realtime_quote.get('volume')) if use_realtime and realtime_quote.get('volume') is not None else int(latest['volume'])

        if use_realtime and realtime_quote.get('pre_close') is not None:
            display_pre_close = float(realtime_quote['pre_close'])
            display_change = current_price - display_pre_close
            display_pct_change = (display_change / display_pre_close * 100) if display_pre_close else None
        else:
            display_pre_close = float(latest['pre_close']) if pd.notna(latest['pre_close']) else None
            display_change = float(latest['change']) if pd.notna(latest['change']) else None
            display_pct_change = float(latest['pct_change']) if pd.notna(latest['pct_change']) else None

        if use_realtime and realtime_quote.get('change_percent') is not None:
            display_pct_change = float(realtime_quote['change_percent'])

        price_source_text = "AKShare 实时快照（无缓存）" if use_realtime else "AKShare 日线收盘（实时失败回退）"
        snapshot_time = realtime_quote.get('snapshot_time') if use_realtime else "N/A"

        def fmt_hk(value: Any) -> str:
            if value is None:
                return "N/A"
            try:
                return f"HK${float(value):.2f}"
            except Exception:
                return "N/A"

        def fmt_pct(value: Any) -> str:
            if value is None:
                return "N/A"
            try:
                return f"{float(value):.2f}%"
            except Exception:
                return "N/A"

        def fmt_int(value: Any) -> str:
            if value is None:
                return "N/A"
            try:
                return f"{int(value):,d}"
            except Exception:
                return "N/A"

        # 计算 PE、PB
        pe_ratio = None
        pb_ratio = None
        financial_section = ""

        if financial_indicators:
            eps_ttm = financial_indicators.get('eps_ttm')
            bps = financial_indicators.get('bps')

            if eps_ttm and eps_ttm > 0:
                pe_ratio = current_price / eps_ttm

            if bps and bps > 0:
                pb_ratio = current_price / bps

            # 构建财务指标部分（处理 None 值）
            def format_value(value, format_str=".2f", suffix="", default="N/A"):
                """格式化数值，处理 None 情况"""
                if value is None:
                    return default
                try:
                    return f"{value:{format_str}}{suffix}"
                except:
                    return default

            financial_section = f"""
### 财务指标（最新报告期：{financial_indicators.get('report_date', 'N/A')}）
**估值指标**:
- PE (市盈率): {f'{pe_ratio:.2f}' if pe_ratio else 'N/A'} (当前价 / EPS_TTM)
- PB (市净率): {f'{pb_ratio:.2f}' if pb_ratio else 'N/A'} (当前价 / BPS)

**每股指标**:
- 基本每股收益 (EPS): HK${format_value(financial_indicators.get('eps_basic'))}
- 滚动每股收益 (EPS_TTM): HK${format_value(financial_indicators.get('eps_ttm'))}
- 每股净资产 (BPS): HK${format_value(financial_indicators.get('bps'))}
- 每股经营现金流: HK${format_value(financial_indicators.get('per_netcash_operate'))}

**盈利能力**:
- 净资产收益率 (ROE): {format_value(financial_indicators.get('roe_avg'), suffix='%')}
- 总资产收益率 (ROA): {format_value(financial_indicators.get('roa'), suffix='%')}
- 净利率: {format_value(financial_indicators.get('net_profit_ratio'), suffix='%')}
- 毛利率: {format_value(financial_indicators.get('gross_profit_ratio'), suffix='%')}

**营收情况**:
- 营业收入: {format_value(financial_indicators.get('operate_income') / 1e8 if financial_indicators.get('operate_income') else None, suffix=' 亿港元')}
- 营收同比增长: {format_value(financial_indicators.get('operate_income_yoy'), suffix='%')}
- 归母净利润: {format_value(financial_indicators.get('holder_profit') / 1e8 if financial_indicators.get('holder_profit') else None, suffix=' 亿港元')}
- 净利润同比增长: {format_value(financial_indicators.get('holder_profit_yoy'), suffix='%')}

**偿债能力**:
- 资产负债率: {format_value(financial_indicators.get('debt_asset_ratio'), suffix='%')}
- 流动比率: {format_value(financial_indicators.get('current_ratio'))}
"""

        result = f"""## 港股历史数据 ({symbol})
**数据源**: AKShare (新浪财经)
**日期范围**: {start_date} ~ {end_date}
**数据条数**: {len(df)} 条

### 最新价格信息
- 价格来源: {price_source_text}
- 实时快照时间: {snapshot_time}
- 最新价: {fmt_hk(current_price)}
- 昨收: {fmt_hk(display_pre_close)}
- 涨跌额: {fmt_hk(display_change)}
- 涨跌幅: {fmt_pct(display_pct_change)}
- 今开: {fmt_hk(display_open)}
- 最高: {fmt_hk(display_high)}
- 最低: {fmt_hk(display_low)}
- 成交量: {fmt_int(display_volume)}

### 技术指标（最新值）
**移动平均线**:
- MA5: HK${latest['ma5']:.2f}
- MA10: HK${latest['ma10']:.2f}
- MA20: HK${latest['ma20']:.2f}
- MA60: HK${latest['ma60']:.2f}

**MACD指标**:
- DIF: {latest['macd_dif']:.2f}
- DEA: {latest['macd_dea']:.2f}
- MACD: {latest['macd']:.2f}

**RSI指标**:
- RSI(14): {latest['rsi']:.2f}

**布林带**:
- 上轨: HK${latest['boll_upper']:.2f}
- 中轨: HK${latest['boll_mid']:.2f}
- 下轨: HK${latest['boll_lower']:.2f}
{financial_section}
### 最近10个交易日价格
{df[['date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_change', 'volume']].tail(10).to_string(index=False)}

### 数据统计
- 最高价: HK${df['high'].max():.2f}
- 最低价: HK${df['low'].min():.2f}
- 平均收盘价: HK${df['close'].mean():.2f}
- 总成交量: {df['volume'].sum():,.0f}
"""

        logger.info(f"✅ [AKShare-新浪] 港股历史数据获取成功: {symbol} ({len(df)}条)")
        return result

    except Exception as e:
        logger.error(f"❌ [AKShare-新浪] 港股历史数据获取失败: {symbol} - {e}")
        return f"❌ 港股{symbol}历史数据获取失败: {str(e)}"


# 🔥 全局缓存：缓存 AKShare 的所有港股数据
_akshare_hk_spot_cache = {
    'data': None,
    'timestamp': None,
    'ttl': 600  # 缓存 10 分钟（参考美股实时行情缓存时长）
}

# 🔥 线程锁：防止多个线程同时调用 AKShare API
import threading
_akshare_hk_spot_lock = threading.Lock()


def get_hk_stock_info_akshare(symbol: str) -> Dict[str, Any]:
    """
    兼容性函数：直接使用 akshare 获取港股信息（避免循环调用）
    🔥 使用全局缓存 + 线程锁，避免重复调用 ak.stock_hk_spot()

    Args:
        symbol: 港股代码

    Returns:
        Dict: 港股信息
    """
    try:
        import akshare as ak
        from datetime import datetime

        # 标准化代码
        provider = get_improved_hk_provider()
        normalized_symbol = provider._normalize_hk_symbol(symbol)

        # 尝试从 akshare 获取实时行情
        try:
            # 🔥 使用互斥锁保护 AKShare API 调用（防止并发导致被封禁）
            # 策略：
            # 1. 尝试获取锁（最多等待 60 秒）
            # 2. 获取锁后，先检查缓存是否已被其他线程更新
            # 3. 如果缓存有效，直接使用；否则调用 API

            thread_id = threading.current_thread().name
            logger.info(f"🔒 [AKShare锁-{thread_id}] 尝试获取锁...")

            # 尝试获取锁，最多等待 60 秒
            lock_acquired = _akshare_hk_spot_lock.acquire(timeout=60)

            if not lock_acquired:
                # 超时，返回错误
                logger.error(f"⏰ [AKShare锁-{thread_id}] 获取锁超时（60秒），放弃")
                raise Exception("AKShare API 调用超时（其他线程占用）")

            try:
                logger.info(f"✅ [AKShare锁-{thread_id}] 已获取锁")

                # 获取锁后，检查缓存是否已被其他线程更新
                now = datetime.now()
                cache = _akshare_hk_spot_cache

                if cache['data'] is not None and cache['timestamp'] is not None:
                    elapsed = (now - cache['timestamp']).total_seconds()
                    if elapsed <= cache['ttl']:
                        # 缓存有效（可能是其他线程刚更新的）
                        logger.info(f"⚡ [AKShare缓存-{thread_id}] 使用缓存数据（{elapsed:.1f}秒前，可能由其他线程更新）")
                        df = cache['data']
                    else:
                        # 缓存过期，需要调用 API
                        logger.info(f"🔄 [AKShare缓存-{thread_id}] 缓存过期（{elapsed:.1f}秒前），调用 API 刷新")
                        df = ak.stock_hk_spot()
                        cache['data'] = df
                        cache['timestamp'] = now
                        logger.info(f"✅ [AKShare缓存-{thread_id}] 已缓存 {len(df)} 只港股数据")
                else:
                    # 缓存为空，首次调用
                    logger.info(f"🔄 [AKShare缓存-{thread_id}] 首次获取港股数据")
                    df = ak.stock_hk_spot()
                    cache['data'] = df
                    cache['timestamp'] = now
                    logger.info(f"✅ [AKShare缓存-{thread_id}] 已缓存 {len(df)} 只港股数据")

            finally:
                # 释放锁
                _akshare_hk_spot_lock.release()
                logger.info(f"🔓 [AKShare锁-{thread_id}] 已释放锁")

            # 从缓存的数据中查找目标股票
            if df is not None and not df.empty:
                matched = df[df['代码'] == normalized_symbol]
                if not matched.empty:
                    row = matched.iloc[0]

                    # 辅助函数：安全转换数值
                    def safe_float(value):
                        try:
                            if value is None or value == '' or (isinstance(value, float) and value != value):  # NaN check
                                return None
                            return float(value)
                        except:
                            return None

                    def safe_int(value):
                        try:
                            if value is None or value == '' or (isinstance(value, float) and value != value):  # NaN check
                                return None
                            return int(value)
                        except:
                            return None

                    return {
                        'symbol': symbol,
                        'name': row['中文名称'],  # 新浪接口的列名
                        'price': safe_float(row.get('最新价')),
                        'open': safe_float(row.get('今开')),
                        'high': safe_float(row.get('最高')),
                        'low': safe_float(row.get('最低')),
                        'volume': safe_int(row.get('成交量')),
                        'change_percent': safe_float(row.get('涨跌幅')),
                        'currency': 'HKD',
                        'exchange': 'HKG',
                        'market': '港股',
                        'source': 'akshare_sina'
                    }
        except Exception as e:
            logger.debug(f"📊 [港股AKShare-新浪] 获取失败: {e}")

        # 如果失败，返回基本信息
        return {
            'symbol': symbol,
            'name': f'港股{normalized_symbol}',
            'currency': 'HKD',
            'exchange': 'HKG',
            'market': '港股',
            'source': 'akshare_fallback'
        }

    except Exception as e:
        logger.error(f"❌ [港股AKShare-新浪] 获取信息失败: {e}")
        return {
            'symbol': symbol,
            'name': f'港股{symbol}',
            'currency': 'HKD',
            'exchange': 'HKG',
            'market': '港股',
            'source': 'error',
            'error': str(e)
        }
