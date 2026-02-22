from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import os
import warnings

# Legacy env var aliases (deprecated): map API_HOST/PORT/DEBUG -> HOST/PORT/DEBUG
_LEGACY_ENV_ALIASES = {
    "API_HOST": "HOST",
    "API_PORT": "PORT",
    "API_DEBUG": "DEBUG",
}
for _legacy, _new in _LEGACY_ENV_ALIASES.items():
    if _new not in os.environ and _legacy in os.environ:
        os.environ[_new] = os.environ[_legacy]
        warnings.warn(
            f"Environment variable {_legacy} is deprecated; use {_new} instead.",
            DeprecationWarning,
            stacklevel=2,
        )

class Settings(BaseSettings):
    # 基础配置
    DEBUG: bool = Field(default=True)
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)
    ALLOWED_ORIGINS: List[str] = Field(default_factory=lambda: ["*"])
    ALLOWED_HOSTS: List[str] = Field(default_factory=lambda: ["*"])

    # MongoDB配置
    MONGODB_HOST: str = Field(default="localhost")
    MONGODB_PORT: int = Field(default=27017)
    MONGODB_USERNAME: str = Field(default="")
    MONGODB_PASSWORD: str = Field(default="")
    MONGODB_DATABASE: str = Field(default="tradingagents")
    MONGODB_AUTH_SOURCE: str = Field(default="admin")
    MONGO_MAX_CONNECTIONS: int = Field(default=100)
    MONGO_MIN_CONNECTIONS: int = Field(default=10)
    # MongoDB超时参数（毫秒）- 用于处理大量历史数据
    MONGO_CONNECT_TIMEOUT_MS: int = Field(default=30000)  # 连接超时：30秒（原为10秒）
    MONGO_SOCKET_TIMEOUT_MS: int = Field(default=60000)   # 套接字超时：60秒（原为20秒）
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = Field(default=5000)  # 服务器选择超时：5秒

    @property
    def MONGO_URI(self) -> str:
        """构建MongoDB URI"""
        if self.MONGODB_USERNAME and self.MONGODB_PASSWORD:
            return f"mongodb://{self.MONGODB_USERNAME}:{self.MONGODB_PASSWORD}@{self.MONGODB_HOST}:{self.MONGODB_PORT}/{self.MONGODB_DATABASE}?authSource={self.MONGODB_AUTH_SOURCE}"
        else:
            return f"mongodb://{self.MONGODB_HOST}:{self.MONGODB_PORT}/{self.MONGODB_DATABASE}"

    @property
    def MONGO_DB(self) -> str:
        """获取数据库名称"""
        return self.MONGODB_DATABASE

    # Redis配置
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_PASSWORD: str = Field(default="")
    REDIS_DB: int = Field(default=0)
    REDIS_MAX_CONNECTIONS: int = Field(default=20)
    REDIS_RETRY_ON_TIMEOUT: bool = Field(default=True)

    @property
    def REDIS_URL(self) -> str:
        """构建Redis URL"""
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        else:
            return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # JWT配置
    JWT_SECRET: str = Field(default="change-me-in-production")
    JWT_ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=30)

    # 队列配置
    QUEUE_MAX_SIZE: int = Field(default=10000)
    QUEUE_VISIBILITY_TIMEOUT: int = Field(default=300)  # 5分钟
    QUEUE_MAX_RETRIES: int = Field(default=3)
    WORKER_HEARTBEAT_INTERVAL: int = Field(default=30)  # 30秒


    # 队列轮询/清理间隔（秒）
    QUEUE_POLL_INTERVAL_SECONDS: float = Field(default=1.0)
    QUEUE_CLEANUP_INTERVAL_SECONDS: float = Field(default=60.0)

    # 并发控制
    DEFAULT_USER_CONCURRENT_LIMIT: int = Field(default=3)
    GLOBAL_CONCURRENT_LIMIT: int = Field(default=50)
    DEFAULT_DAILY_QUOTA: int = Field(default=1000)

    # 速率限制
    RATE_LIMIT_ENABLED: bool = Field(default=True)
    DEFAULT_RATE_LIMIT: int = Field(default=100)  # 每分钟请求数

    # 日志配置
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FORMAT: str = Field(default="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    LOG_FILE: str = Field(default="logs/tradingagents.log")

    # 代理配置
    # 用于配置需要绕过代理的域名（国内数据源）
    # 多个域名用逗号分隔
    # ⚠️ Windows 不支持通配符 *，必须使用完整域名
    # 详细说明: docs/proxy_configuration.md
    HTTP_PROXY: str = Field(default="")
    HTTPS_PROXY: str = Field(default="")
    NO_PROXY: str = Field(
        default="localhost,127.0.0.1,eastmoney.com,push2.eastmoney.com,82.push2.eastmoney.com,82.push2delay.eastmoney.com,gtimg.cn,sinaimg.cn,api.tushare.pro,baostock.com"
    )

    # 文件上传配置
    MAX_UPLOAD_SIZE: int = Field(default=10 * 1024 * 1024)  # 10MB
    UPLOAD_DIR: str = Field(default="uploads")

    # 缓存配置
    CACHE_TTL: int = Field(default=3600)  # 1小时
    SCREENING_CACHE_TTL: int = Field(default=1800)  # 30分钟

    # 安全配置
    BCRYPT_ROUNDS: int = Field(default=12)
    SESSION_EXPIRE_HOURS: int = Field(default=24)
    CSRF_SECRET: str = Field(default="change-me-csrf-secret")

    # 外部服务配置
    STOCK_DATA_API_URL: str = Field(default="")
    STOCK_DATA_API_KEY: str = Field(default="")

    # SSE 配置
    SSE_POLL_TIMEOUT_SECONDS: float = Field(default=1.0)
    SSE_HEARTBEAT_INTERVAL_SECONDS: int = Field(default=10)
    SSE_TASK_MAX_IDLE_SECONDS: int = Field(default=300)
    SSE_BATCH_POLL_INTERVAL_SECONDS: float = Field(default=2.0)
    SSE_BATCH_MAX_IDLE_SECONDS: int = Field(default=600)


    # 监控配置
    METRICS_ENABLED: bool = Field(default=True)
    HEALTH_CHECK_INTERVAL: int = Field(default=60)  # 60秒


    # 配置真相来源（方案A）：file|db|hybrid
    # - file：以文件/env 为准（推荐，生产缺省）
    # - db：以数据库为准（仅兼容旧版，不推荐）
    # - hybrid：文件/env 优先，DB 作为兜底
    CONFIG_SOT: str = Field(default="file")


    # 基础信息同步任务配置（可配置调度）
    SYNC_STOCK_BASICS_ENABLED: bool = Field(default=True)
    # 是否在应用启动时立即执行一次基础信息同步（默认关闭，避免每次启动批量更新）
    SYNC_STOCK_BASICS_RUN_ON_STARTUP: bool = Field(default=False)
    # 判定“基础信息已足够”的最小记录数；达到该数量时，启动即同步会跳过
    SYNC_STOCK_BASICS_INIT_MIN_COUNT: int = Field(default=4000, ge=1)
    # 优先使用 CRON 表达式，例如 "30 6 * * *" 表示每日 06:30
    SYNC_STOCK_BASICS_CRON: str = Field(default="")
    # 若未提供 CRON，则使用简单时间字符串 "HH:MM"（24小时制）
    SYNC_STOCK_BASICS_TIME: str = Field(default="06:30")
    # 时区
    TIMEZONE: str = Field(default="Asia/Shanghai")

    # ==================== 个人投研助手配置 ====================
    # A股本地SQLite
    A_SHARE_SQLITE_PATH: str = Field(default="./data/local_investment.db")

    # LongPort凭证（港美持仓）
    LONGPORT_APP_KEY: str = Field(default="")
    LONGPORT_APP_SECRET: str = Field(default="")
    LONGPORT_ACCESS_TOKEN: str = Field(default="")

    # 飞书机器人配置
    FEISHU_BOT_MODE: str = Field(default="stream", description="飞书接入模式: stream/webhook")
    FEISHU_BOT_APP_ID: str = Field(default="")
    FEISHU_BOT_APP_SECRET: str = Field(default="")
    FEISHU_BOT_VERIFICATION_TOKEN: str = Field(default="")
    FEISHU_BOT_ENCRYPT_KEY: str = Field(default="")
    FEISHU_BOT_DEFAULT_CHAT_ID: str = Field(default="")
    FEISHU_BOT_API_BASE: str = Field(default="https://open.feishu.cn")
    # 信号推送目标（单用户场景建议直接配置）
    # receive_id_type: chat_id | open_id | user_id | union_id
    FEISHU_SIGNAL_RECEIVE_ID_TYPE: str = Field(default="open_id")
    FEISHU_SIGNAL_RECEIVE_ID: str = Field(default="")
    FEISHU_ANALYSIS_PROGRESS_POLL_SECONDS: int = Field(default=6, ge=2, le=60)
    FEISHU_ANALYSIS_PROGRESS_MIN_PUSH_INTERVAL_SECONDS: int = Field(default=15, ge=3, le=300)
    FEISHU_ANALYSIS_PROGRESS_MIN_INCREMENT: int = Field(default=10, ge=1, le=50)
    FEISHU_ANALYSIS_PROGRESS_HEARTBEAT_SECONDS: int = Field(default=60, ge=10, le=600)
    FEISHU_ANALYSIS_PROGRESS_MAX_TRACK_MINUTES: int = Field(default=120, ge=10, le=1440)

    # 分市场推送配置
    MARKET_PUSH_ENABLED: bool = Field(default=True)
    MARKET_PUSH_SCAN_INTERVAL_SECONDS: int = Field(default=60, ge=10, le=3600)
    MARKET_PUSH_WINDOW_SECONDS: int = Field(default=90, ge=10, le=600)
    MARKET_PUSH_MARKETS: str = Field(default="CN,HK,US")
    MARKET_PUSH_EXPOSURE_ONLY: bool = Field(default=True)
    SIGNALS_ENABLED: bool = Field(default=True)
    SIGNALS_RESPECT_MARKET_HOURS: bool = Field(default=True)
    ANALYSIS_ZOMBIE_CLEANUP_ENABLED: bool = Field(default=True)
    ANALYSIS_ZOMBIE_CLEANUP_RUN_ON_STARTUP: bool = Field(default=True)
    ANALYSIS_ZOMBIE_CLEANUP_INTERVAL_MINUTES: int = Field(default=30, ge=5, le=1440)
    ANALYSIS_ZOMBIE_MAX_RUNNING_HOURS: int = Field(default=2, ge=1, le=168)
    # 分析反馈长期评估（用于持续复盘，不固定 30 天）
    ANALYSIS_FEEDBACK_ENABLED: bool = Field(default=True)
    ANALYSIS_FEEDBACK_RUN_ON_STARTUP: bool = Field(default=True)
    ANALYSIS_FEEDBACK_SCAN_INTERVAL_MINUTES: int = Field(default=60, ge=1, le=1440)
    ANALYSIS_FEEDBACK_INTERVAL_DAYS: int = Field(default=7, ge=1, le=365)
    ANALYSIS_FEEDBACK_MAX_TRACKING_DAYS: int = Field(default=730, ge=7, le=3650)
    ANALYSIS_FEEDBACK_MAX_EVALUATIONS: int = Field(default=200, ge=1, le=10000)
    ANALYSIS_FEEDBACK_HOLD_TOLERANCE_PCT: float = Field(default=3.0, ge=0.0, le=100.0)
    ANALYSIS_FEEDBACK_MIN_EFFECTIVE_CHANGE_PCT: float = Field(default=0.2, ge=0.0, le=100.0)
    ANALYSIS_FEEDBACK_STOP_GAIN_PCT: float = Field(default=15.0, ge=0.0, le=500.0)
    ANALYSIS_FEEDBACK_STOP_LOSS_PCT: float = Field(default=8.0, ge=0.0, le=500.0)
    ANALYSIS_FEEDBACK_TIME_TAKE_PROFIT_DAYS: int = Field(default=30, ge=1, le=3650)
    ANALYSIS_FEEDBACK_MAX_HOLDING_DAYS: int = Field(default=180, ge=1, le=3650)
    ANALYSIS_FEEDBACK_MEMORY_WRITE_ENABLED: bool = Field(default=True)
    ANALYSIS_FEEDBACK_MEMORY_MIN_DAYS_BETWEEN_WRITES: int = Field(default=14, ge=0, le=3650)
    ANALYSIS_FEEDBACK_MEMORY_MIN_RETURN_DELTA_PCT: float = Field(default=1.0, ge=0.0, le=100.0)
    ANALYSIS_FEEDBACK_BATCH_SIZE: int = Field(default=50, ge=1, le=1000)

    # 实时行情入库任务
    QUOTES_INGEST_ENABLED: bool = Field(default=True)
    QUOTES_INGEST_INTERVAL_SECONDS: int = Field(
        default=360,
        description="实时行情采集间隔（秒）。默认360秒（6分钟），免费用户建议>=300秒，付费用户可设置5-60秒"
    )
    # 休市期/启动兜底补数（填充上一笔快照）
    QUOTES_BACKFILL_ON_STARTUP: bool = Field(default=True)
    QUOTES_BACKFILL_ON_OFFHOURS: bool = Field(default=True)

    # 实时行情接口轮换配置
    QUOTES_ROTATION_ENABLED: bool = Field(
        default=True,
        description="启用接口轮换机制（Tushare → AKShare东方财富 → AKShare新浪财经）"
    )
    QUOTES_TUSHARE_HOURLY_LIMIT: int = Field(
        default=2,
        description="Tushare rt_k接口每小时调用次数限制（免费用户2次，付费用户可设置更高）"
    )
    QUOTES_AUTO_DETECT_TUSHARE_PERMISSION: bool = Field(
        default=True,
        description="自动检测Tushare rt_k接口权限，付费用户自动切换到高频模式（5秒）"
    )

    # Tushare基础配置
    TUSHARE_TOKEN: str = Field(default="", description="Tushare API Token")
    TUSHARE_HTTP_URL: str = Field(default="", description="Tushare Pro HTTP URL（可选，自定义网关/加速地址）")
    TUSHARE_ENABLED: bool = Field(default=True, description="启用Tushare数据源")
    TUSHARE_TIER: str = Field(default="standard", description="Tushare积分等级 (free/basic/standard/premium/vip)")
    TUSHARE_RATE_LIMIT_SAFETY_MARGIN: float = Field(default=0.8, ge=0.1, le=1.0, description="速率限制安全边际")

    # Tushare统一数据同步配置
    TUSHARE_UNIFIED_ENABLED: bool = Field(default=True)
    TUSHARE_BASIC_INFO_SYNC_ENABLED: bool = Field(default=True)
    TUSHARE_BASIC_INFO_SYNC_CRON: str = Field(default="0 2 * * *")  # 每日凌晨2点
    TUSHARE_QUOTES_SYNC_ENABLED: bool = Field(default=True)
    TUSHARE_QUOTES_SYNC_CRON: str = Field(default="*/5 9-15 * * 1-5")  # 交易时间每5分钟
    TUSHARE_HISTORICAL_SYNC_ENABLED: bool = Field(default=True)
    TUSHARE_HISTORICAL_SYNC_CRON: str = Field(default="0 16 * * 1-5")  # 工作日16点
    TUSHARE_FINANCIAL_SYNC_ENABLED: bool = Field(default=True)
    TUSHARE_FINANCIAL_SYNC_CRON: str = Field(default="0 3 * * 0")  # 周日凌晨3点
    TUSHARE_STATUS_CHECK_ENABLED: bool = Field(default=True)
    TUSHARE_STATUS_CHECK_CRON: str = Field(default="0 * * * *")  # 每小时

    # Tushare数据初始化配置
    TUSHARE_INIT_HISTORICAL_DAYS: int = Field(default=365, ge=1, le=3650, description="初始化历史数据天数")
    TUSHARE_INIT_BATCH_SIZE: int = Field(default=100, ge=10, le=1000, description="初始化批处理大小")
    TUSHARE_INIT_AUTO_START: bool = Field(default=False, description="应用启动时自动检查并初始化数据")

    # AKShare统一数据同步配置
    AKSHARE_UNIFIED_ENABLED: bool = Field(default=True, description="启用AKShare统一数据同步")
    AKSHARE_BASIC_INFO_SYNC_ENABLED: bool = Field(default=True, description="启用基础信息同步")
    AKSHARE_BASIC_INFO_SYNC_CRON: str = Field(default="0 3 * * *", description="基础信息同步CRON表达式")  # 每日凌晨3点
    AKSHARE_QUOTES_SYNC_ENABLED: bool = Field(default=True, description="启用行情同步")
    AKSHARE_QUOTES_SYNC_CRON: str = Field(default="*/30 9-15 * * 1-5", description="行情同步CRON表达式")  # 交易时间每30分钟（避免频率限制）
    AKSHARE_HISTORICAL_SYNC_ENABLED: bool = Field(default=True, description="启用历史数据同步")
    AKSHARE_HISTORICAL_SYNC_CRON: str = Field(default="0 17 * * 1-5", description="历史数据同步CRON表达式")  # 工作日17点
    AKSHARE_FINANCIAL_SYNC_ENABLED: bool = Field(default=True, description="启用财务数据同步")
    AKSHARE_FINANCIAL_SYNC_CRON: str = Field(default="0 4 * * 0", description="财务数据同步CRON表达式")  # 周日凌晨4点
    AKSHARE_STATUS_CHECK_ENABLED: bool = Field(default=True, description="启用状态检查")
    AKSHARE_STATUS_CHECK_CRON: str = Field(default="30 * * * *", description="状态检查CRON表达式")  # 每小时30分

    # AKShare数据初始化配置
    AKSHARE_INIT_HISTORICAL_DAYS: int = Field(default=365, ge=1, le=3650, description="初始化历史数据天数")
    AKSHARE_INIT_BATCH_SIZE: int = Field(default=100, ge=10, le=1000, description="初始化批处理大小")
    AKSHARE_INIT_AUTO_START: bool = Field(default=False, description="应用启动时自动检查并初始化数据")

    # ==================== 分析师数据获取配置 ====================

    # 市场分析师数据范围配置
    # 默认60天：可覆盖MA60等所有常用技术指标（MA5/10/20/60, MACD, RSI, BOLL）
    MARKET_ANALYST_LOOKBACK_DAYS: int = Field(default=60, ge=5, le=365, description="市场分析回溯天数（用于技术分析）")

    # ==================== BaoStock统一数据同步配置 ====================

    # BaoStock统一数据同步总开关
    BAOSTOCK_UNIFIED_ENABLED: bool = Field(default=True, description="启用BaoStock统一数据同步")

    # BaoStock数据同步任务配置
    BAOSTOCK_BASIC_INFO_SYNC_ENABLED: bool = Field(default=True, description="启用基础信息同步")
    BAOSTOCK_BASIC_INFO_SYNC_CRON: str = Field(default="0 4 * * *", description="基础信息同步CRON表达式")  # 每日凌晨4点
    BAOSTOCK_DAILY_QUOTES_SYNC_ENABLED: bool = Field(default=True, description="启用日K线同步（注意：BaoStock不支持实时行情）")
    BAOSTOCK_DAILY_QUOTES_SYNC_CRON: str = Field(default="0 16 * * 1-5", description="日K线同步CRON表达式")  # 工作日收盘后16:00
    BAOSTOCK_HISTORICAL_SYNC_ENABLED: bool = Field(default=True, description="启用历史数据同步")
    BAOSTOCK_HISTORICAL_SYNC_CRON: str = Field(default="0 18 * * 1-5", description="历史数据同步CRON表达式")  # 工作日18点
    BAOSTOCK_STATUS_CHECK_ENABLED: bool = Field(default=True, description="启用状态检查")
    BAOSTOCK_STATUS_CHECK_CRON: str = Field(default="45 * * * *", description="状态检查CRON表达式")  # 每小时45分

    # BaoStock数据初始化配置
    BAOSTOCK_INIT_HISTORICAL_DAYS: int = Field(default=365, ge=1, le=3650, description="初始化历史数据天数")
    BAOSTOCK_INIT_BATCH_SIZE: int = Field(default=50, ge=10, le=500, description="初始化批处理大小")
    BAOSTOCK_INIT_AUTO_START: bool = Field(default=False, description="应用启动时自动检查并初始化数据")

    # 数据目录配置
    TRADINGAGENTS_DATA_DIR: str = Field(default="./data")

    @property
    def log_dir(self) -> str:
        """获取日志目录"""
        return os.path.dirname(self.LOG_FILE)

    # ==================== 港股数据配置 ====================

    # 港股数据源配置（按需获取 + 定期增量同步）
    HK_DATA_CACHE_HOURS: int = Field(default=24, ge=1, le=168, description="港股数据缓存时长（小时）")
    HK_DEFAULT_DATA_SOURCE: str = Field(default="yfinance", description="港股默认数据源（yfinance/akshare）")
    HK_UNIVERSE_MIN_COUNT: int = Field(default=1500, ge=100, le=10000, description="港股全量股票池最小规模阈值")

    # 港股全量股票池同步
    HK_UNIVERSE_SYNC_ENABLED: bool = Field(default=True, description="启用港股股票池全量同步")
    HK_UNIVERSE_SYNC_CRON: str = Field(default="5 6 * * *", description="港股股票池全量同步CRON表达式")
    HK_UNIVERSE_RUN_ON_STARTUP: bool = Field(default=True, description="应用启动时是否先执行一次港股股票池同步")

    # 港股基础信息增量同步
    HK_BASIC_INFO_SYNC_ENABLED: bool = Field(default=True, description="启用港股基础信息增量同步")
    HK_BASIC_INFO_SYNC_CRON: str = Field(default="*/20 * * * *", description="港股基础信息增量同步CRON表达式")
    HK_BASIC_INFO_INCREMENTAL_BATCH_SIZE: int = Field(default=300, ge=50, le=5000, description="港股基础信息增量批次大小")
    HK_BASIC_INFO_SYNC_CONCURRENCY: int = Field(default=4, ge=1, le=64, description="港股基础信息同步并发数")

    # 港股行情增量同步
    HK_QUOTES_SYNC_ENABLED: bool = Field(default=True, description="启用港股行情增量同步")
    HK_QUOTES_SYNC_CRON: str = Field(default="*/10 * * * *", description="港股行情增量同步CRON表达式")
    HK_QUOTES_INCREMENTAL_BATCH_SIZE: int = Field(default=300, ge=50, le=5000, description="港股行情增量批次大小")
    HK_QUOTES_SYNC_CONCURRENCY: int = Field(default=6, ge=1, le=64, description="港股行情同步并发数")

    # 港股同步状态巡检
    HK_STATUS_CHECK_ENABLED: bool = Field(default=True, description="启用港股同步状态检查")
    HK_STATUS_CHECK_CRON: str = Field(default="10 * * * *", description="港股同步状态检查CRON表达式")

    # ==================== 美股数据配置 ====================

    # 美股数据源配置（按需获取 + 定期增量同步）
    US_DATA_CACHE_HOURS: int = Field(default=24, ge=1, le=168, description="美股数据缓存时长（小时）")
    US_DEFAULT_DATA_SOURCE: str = Field(default="yfinance", description="美股默认数据源（yfinance/finnhub）")
    US_UNIVERSE_MIN_COUNT: int = Field(default=3000, ge=100, le=50000, description="美股全量股票池最小规模阈值")

    # 美股全量股票池同步（优先 Finnhub，回退 SEC/NasdaqTrader）
    US_UNIVERSE_SYNC_ENABLED: bool = Field(default=True, description="启用美股股票池全量同步")
    US_UNIVERSE_SYNC_CRON: str = Field(default="10 6 * * *", description="美股股票池全量同步CRON表达式")
    US_UNIVERSE_RUN_ON_STARTUP: bool = Field(default=True, description="应用启动时是否先执行一次美股股票池同步")

    # 美股基础信息增量同步（yfinance）
    US_BASIC_INFO_SYNC_ENABLED: bool = Field(default=True, description="启用美股基础信息增量同步")
    US_BASIC_INFO_SYNC_CRON: str = Field(default="*/15 * * * *", description="美股基础信息增量同步CRON表达式")
    US_BASIC_INFO_INCREMENTAL_BATCH_SIZE: int = Field(default=300, ge=50, le=5000, description="美股基础信息增量批次大小")
    US_BASIC_INFO_SYNC_CONCURRENCY: int = Field(default=6, ge=1, le=64, description="美股基础信息同步并发数")

    # 美股行情增量同步（yfinance）
    US_QUOTES_SYNC_ENABLED: bool = Field(default=True, description="启用美股行情增量同步")
    US_QUOTES_SYNC_CRON: str = Field(default="*/15 * * * *", description="美股行情增量同步CRON表达式")
    US_QUOTES_INCREMENTAL_BATCH_SIZE: int = Field(default=300, ge=50, le=5000, description="美股行情增量批次大小")
    US_QUOTES_SYNC_CONCURRENCY: int = Field(default=8, ge=1, le=64, description="美股行情同步并发数")

    # 美股同步状态巡检
    US_STATUS_CHECK_ENABLED: bool = Field(default=True, description="启用美股同步状态检查")
    US_STATUS_CHECK_CRON: str = Field(default="15 * * * *", description="美股同步状态检查CRON表达式")

    # ===== 新闻数据同步服务配置 =====
    NEWS_SYNC_ENABLED: bool = Field(default=True)
    NEWS_SYNC_CRON: str = Field(default="0 */2 * * *")  # 每2小时
    NEWS_SYNC_HOURS_BACK: int = Field(default=24)
    NEWS_SYNC_MAX_PER_SOURCE: int = Field(default=50)

    @property
    def is_production(self) -> bool:
        """是否为生产环境"""
        return not self.DEBUG

    # Ignore any extra environment variables present in .env or process env
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# 自动将代理配置设置到环境变量
# 这样 requests 库可以直接读取 os.environ['NO_PROXY']
if settings.HTTP_PROXY:
    os.environ['HTTP_PROXY'] = settings.HTTP_PROXY
if settings.HTTPS_PROXY:
    os.environ['HTTPS_PROXY'] = settings.HTTPS_PROXY
if settings.NO_PROXY:
    os.environ['NO_PROXY'] = settings.NO_PROXY


def get_settings() -> Settings:
    """获取配置实例"""
    return settings
