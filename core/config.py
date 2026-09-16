"""全局配置加载

本模块加载 .env 文件中的配置项，并为每个配置提供默认值。
所有其他模块通过 `from core.config import XXX` 获取配置。

设计原则：
  1. 集中管理：所有配置在此统一加载，避免散落在各模块
  2. 默认兜底：每个配置都有合理的默认值，.env 缺失时能正常运行
  3. 路径自动解析：基于项目根目录(PROJECT_ROOT)解析所有相对路径
"""
import os                    # 环境变量读取
from pathlib import Path     # 跨平台路径处理
from dotenv import load_dotenv  # .env 文件加载

# ============================================================
# 项目根目录
#   自动定位：当前文件 (core/config.py) 的父目录的父目录
#   即: E:/wxf/claude/quant-trading/
#   所有相对路径基于此解析
# ============================================================
PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# 加载项目根目录下的 .env 文件（环境变量覆盖）
load_dotenv(PROJECT_ROOT / ".env")

# ============================================================
# 数据库配置
# ============================================================
DB_PATH = PROJECT_ROOT / os.getenv("DB_PATH", "data/quant.db")
# SQLite 数据库文件路径，默认: <PROJECT_ROOT>/data/quant.db

# ============================================================
# Tushare Pro 数据源配置
# ============================================================
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
# Tushare Pro API Token（可选）
# 在 https://tushare.pro 注册获取
# 为空时不使用 Tushare 数据源

TUSHARE_RATE_LIMIT = float(os.getenv("TUSHARE_RATE_LIMIT", "0.35"))
# Tushare 免费用户限频：1 次/秒，每次调用前休眠 0.35s
# 付费用户可调小此值以加快数据获取

# ============================================================
# TickFlow 数据源配置
# ============================================================
TICKFLOW_API_KEY = os.getenv("TICKFLOW_API_KEY", "")
# TickFlow API Key（可选）
# 在 https://tickflow.org 控制台生成
# 为空时不使用 TickFlow 数据源

TICKFLOW_BASE_URL = os.getenv("TICKFLOW_BASE_URL", "https://api.tickflow.org")
# TickFlow 服务地址
# 完整服务: https://api.tickflow.org（需 API Key）
# 免费服务: https://free-api.tickflow.org（无需 Key，仅历史日K）

# ============================================================
# 数据获取配置
# ============================================================
DATA_START_DATE = os.getenv("DATA_START_DATE", "20210701")
# 数据起始日期（YYYYMMDD）
# 首次初始化/增量更新时的最早日期，默认 2021-07-01

# ============================================================
# 回测交易费用参数
#   基于 A 股实际收费标准
# ============================================================
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", "0.00025"))
# 券商佣金费率（默认万2.5）

MIN_COMMISSION = float(os.getenv("MIN_COMMISSION", "5.0"))
# 最低佣金（元），A股默认最低5元

STAMP_TAX_RATE = float(os.getenv("STAMP_TAX_RATE", "0.0005"))
# 印花税率（默认万5，卖出时收取）

TRANSFER_FEE_RATE = float(os.getenv("TRANSFER_FEE_RATE", "0.00001"))
# 过户费率（默认万0.1）

# ============================================================
# 滑点模型（模拟成交价与实时价的偏差）
# ============================================================
SLIPPAGE_RATE = float(os.getenv("SLIPPAGE_RATE", "0.001"))
# 滑点比率（按成交金额的千分之一）
# 买入时价格上浮，卖出时价格下浮

# ============================================================
# 默认回测参数
# ============================================================
DEFAULT_CAPITAL = float(os.getenv("DEFAULT_CAPITAL", "100000"))
# 回测默认初始资金（元），默认 10万

# ============================================================
# 定时调度任务配置
#   SCHEDULER_ENABLED: 是否启用定时更新（默认开启）
#   SCHEDULER_HOUR:    每日执行的小时（默认 17:00，收盘后）
#   SCHEDULER_MINUTE:  执行的分钟（默认 00）
# ============================================================
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"
# 字符串转布尔： "true"/"1"→True, 其他→False

SCHEDULER_HOUR = int(os.getenv("SCHEDULER_HOUR", "17"))
# 定时任务小时（24小时制，默认 17点）

SCHEDULER_MINUTE = int(os.getenv("SCHEDULER_MINUTE", "0"))
# 定时任务分钟（默认 0分）

# ============================================================
# 日志目录管理
# ============================================================
LOG_DIR = PROJECT_ROOT / "logs"
# 日志文件存放目录，默认 <PROJECT_ROOT>/logs/

LOG_DIR.mkdir(exist_ok=True)
# 自动创建日志目录（如果不存在）

# ============================================================
# 扫描器配置（原 scanner.py 硬编码）
# ============================================================
SCANNER_CACHE_THRESHOLD = float(os.getenv("SCANNER_CACHE_THRESHOLD", "0.9"))
# 缓存命中阈值：已扫描股票数达到总量的此比例时跳过全量扫描

SCANNER_MIN_DATA_DAYS = int(os.getenv("SCANNER_MIN_DATA_DAYS", "60"))
# 最少数据天数：少于此天数的股票跳过扫描

SCANNER_PARALLEL_WORKERS = int(os.getenv("SCANNER_PARALLEL_WORKERS", "0"))
# 并行扫描进程数（0=自动检测CPU核心数）

# ============================================================
# 回测引擎配置（原 backtester.py 硬编码）
# ============================================================
BACKTEST_MIN_POSITION_PCT = float(os.getenv("BACKTEST_MIN_POSITION_PCT", "0.05"))
# 最低仓位比例（score=0 时）

BACKTEST_MAX_POSITION_PCT = float(os.getenv("BACKTEST_MAX_POSITION_PCT", "0.20"))
# 最高仓位比例（score=1 时）

BACKTEST_POSITION_STEP = BACKTEST_MAX_POSITION_PCT - BACKTEST_MIN_POSITION_PCT
# 仓位步进 = max - min（score 乘以此值加 min）

# ============================================================
# 数据源熔断配置（原 fetcher.py 硬编码）
# ============================================================
FETCHER_CIRCUIT_THRESHOLD = int(os.getenv("FETCHER_CIRCUIT_THRESHOLD", "20"))
# 连续失败次数阈值，达到后触发熔断

FETCHER_CIRCUIT_COOLDOWN = int(os.getenv("FETCHER_CIRCUIT_COOLDOWN", "300"))
# 熔断冷却时间（秒）

# ============================================================
# 应用安全配置
# ============================================================
APP_AUTH_ENABLED = os.getenv("APP_AUTH_ENABLED", "false").lower() == "true"
# 是否启用 Streamlit 应用登录认证（默认关闭，部署到公网时建议开启）

APP_AUTH_PASSWORD = os.getenv("APP_AUTH_PASSWORD", "")
# 应用登录密码（为空且 AUTH_ENABLED=true 时使用默认密码 "quant123"）

# ============================================================
# 流动性与停牌风控配置（原 scanner.py 扩展）
# ============================================================
SCANNER_MIN_DAILY_AMOUNT = float(os.getenv("SCANNER_MIN_DAILY_AMOUNT", "0"))
# 最少日均成交额过滤（元，默认 0 不强滤，实盘建议 20000000-30000000）

SCANNER_FILTER_SUSPENDED = os.getenv("SCANNER_FILTER_SUSPENDED", "true").lower() == "true"
# 是否过滤最新交易日停牌/无成交量标的

# ============================================================
# 生产级日志分级轮转（Log Rotation）
# ============================================================
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(name: str = "quant", level: int = logging.INFO, log_filename: str = "quant.log") -> logging.Logger:
    """初始化生产级日志轮转记录器

    - 自动挂载 RotatingFileHandler，单文件上限 20MB，保留 5 份历史归档
    - UTF-8 编码，统一输出格式
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复挂载 handler
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    if not file_handlers:
        log_file = LOG_DIR / log_filename
        rfh = RotatingFileHandler(
            str(log_file),
            maxBytes=20 * 1024 * 1024,  # 20MB
            backupCount=5,
            encoding="utf-8",
        )
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
        rfh.setFormatter(formatter)
        rfh.setLevel(level)
        logger.addHandler(rfh)

    return logger

# 默认配置全局日志记录器
default_logger = setup_logging()

