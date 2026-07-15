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
