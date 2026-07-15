"""数据获取 - 多数据源级联兜底

本模块负责从多个数据源获取A股数据，按优先级依次尝试：
  AKShare(主力) → Tushare Pro(备用) → Baostock(最后兜底)
所有数据源统一输出「前复权」价格，保证回测一致性。

优先级策略:
  日线数据:   AKShare(前复权,免费) → Tushare(未复权+复权因子) → Baostock(前复权,免费)
  股票列表:   Tushare(含行业) → AKShare(无行业) → Baostock(无行业)
  指数成分股: AKShare → Baostock → Tushare
"""
import time          # 休眠等待，用于API限速控制
import random        # 随机数，用于请求间隔抖动(避免集中触发限流)
import logging       # 日志记录
import requests      # HTTP请求库（备用）
import io            # 字节流处理（备用）
import pandas as pd  # 数据处理核心库

# 从配置模块加载 Tushare Token（用于访问 Tushare Pro API）
from core.config import TUSHARE_TOKEN

# ============================================================
# AKShare 重试配置
#   - 免费API有频率限制，批量请求时容易触发限流
#   - 通过指数退避重试 + 随机休眠来应对
# ============================================================
AKSHARE_MAX_RETRIES = 3        # 单只股票请求最大重试次数
AKSHARE_BASE_DELAY = 2.0       # 重试基础等待秒数(每次翻倍: 2s→4s→8s)
AKSHARE_BATCH_SLEEP = 0.5      # 每只股票首次请求前的最小休眠间隔

# 初始化日志记录器（以当前模块名命名）
logger = logging.getLogger(__name__)

# ============================================================
# 日线数据统一列定义（所有数据源输出格式一致）
#   所有获取日线的函数（AKShare/Tushare/Baostock）
#   最终输出的列顺序和名称必须与此一致
# ============================================================
DAILY_COLUMNS = [
    "ts_code", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "pct_chg", "turnover", "adj_factor",
]

# ============================================================
# AKShare 熔断机制
#   当连续失败达到阈值时，临时跳过AKShare一段时间
#   防止API故障时无意义的重试浪费大量时间
# ============================================================
_akshare_consecutive_failures = 0   # 连续失败计数
_akshare_circuit_open_until = 0.0   # 熔断解锁时间戳（time.time）
AKSHARE_CIRCUIT_BREAK_THRESHOLD = 20   # 连续失败次数阈值，达到后熔断
AKSHARE_CIRCUIT_BREAK_SECONDS = 300    # 熔断持续时间（秒）


def _is_akshare_circuit_open() -> bool:
    """检查 AKShare 熔断是否已打开（为True则跳过AKShare）"""
    global _akshare_circuit_open_until
    if _akshare_circuit_open_until > time.time():
        return True
    return False


def _akshare_success():
    """AKShare 请求成功后：重置连续失败计数"""
    global _akshare_consecutive_failures
    _akshare_consecutive_failures = 0


def _akshare_failure():
    """AKShare 请求失败后：累计计数，达到阈值则打开熔断"""
    global _akshare_consecutive_failures, _akshare_circuit_open_until
    _akshare_consecutive_failures += 1
    if _akshare_consecutive_failures >= AKSHARE_CIRCUIT_BREAK_THRESHOLD:
        _akshare_circuit_open_until = time.time() + AKSHARE_CIRCUIT_BREAK_SECONDS
        logger.warning(
            f"AKShare 连续 {_akshare_consecutive_failures} 次失败，"
            f"熔断 {AKSHARE_CIRCUIT_BREAK_SECONDS}s"
        )


# ============================================================
# Tushare Pro API 初始化与限频管理
# ============================================================

_pro = None  # Tushare Pro API 实例（全局单例，延迟初始化）


def _get_pro():
    """延迟初始化 Tushare Pro API（首次调用时创建，后续复用）"""
    global _pro                                    # 声明使用全局变量
    if _pro is None:                               # 未初始化时才创建
        if not TUSHARE_TOKEN or TUSHARE_TOKEN == "your_token_here":  # Token为空或占位符则报错
            raise ValueError("TUSHARE_TOKEN未配置")
        import tushare as ts                       # 延迟导入(避免未配置时影响其他功能)
        ts.set_token(TUSHARE_TOKEN)                # 设置 Token
        _pro = ts.pro_api()                        # 创建 API 实例
    return _pro                                    # 返回缓存的实例


def _rate_limit():
    """Tushare 免费用户限频：最多 1 次/秒，每次调用前休眠

    休眠时长由 TUSHARE_RATE_LIMIT 配置（默认 0.35s），
    付费用户可调小此值以加快数据获取。
    """
    from core.config import TUSHARE_RATE_LIMIT
    time.sleep(TUSHARE_RATE_LIMIT)


# ============================================================
# 股票列表获取（含多源级联）
#   fetch_stock_list() 是外部入口
#   _fetch_stock_list_akshare()   / _fetch_stock_list_baostock() 是内部实现
# ============================================================

def fetch_stock_list() -> pd.DataFrame:
    """获取A股股票列表，多源级联兜底

    优先级: Tushare(含行业) → AKShare(无行业) → Baostock(无行业)
    统一返回列: ts_code, symbol, name, market, list_date, industry, is_st, delist_date
    """
    # ---------- 方案1: Tushare ----------
    # 优势: 含行业分类、上市日期等详细信息
    try:
        pro = _get_pro()                           # 获取 Tushare API 实例
        _rate_limit()                               # 限频控制
        df = pro.stock_basic(                       # 调用 Tushare 股票列表接口
            exchange="",                            # 全部交易所
            list_status="L",                        # L=上市, D=退市, P=暂停
            fields="ts_code,symbol,name,market,list_date,industry"  # 需要的字段
        )
        if df is not None and not df.empty:         # 有数据则处理并返回
            df["is_st"] = df["name"].str.contains(r"ST|\*ST", na=False).astype(int)  # 标记ST股票
            df["delist_date"] = ""                  # 上市股票无退市日期
            logger.info(f"股票列表: Tushare ✓ ({len(df)}只)")
            return df
    except Exception as e:                          # 失败则降级到下一个方案
        logger.warning(f"Tushare获取股票列表失败: {e}")

    # ---------- 方案2: AKShare ----------
    # 优势: 免费无需Token，但无行业信息
    df = _fetch_stock_list_akshare()
    if not df.empty:
        return df

    # ---------- 方案3: Baostock ----------
    # 兜底方案，完全免费无限频
    df = _fetch_stock_list_baostock()
    if not df.empty:
        return df

    # 所有方案均失败
    logger.error("所有数据源获取股票列表均失败")
    return pd.DataFrame()  # 返回空DataFrame


def _fetch_stock_list_akshare() -> pd.DataFrame:
    """AKShare获取A股股票列表（无需Token，推荐方案）"""
    try:
        import akshare as ak                         # 延迟导入
        df = ak.stock_info_a_code_name()             # 从AKShare获取股票代码和名称
        df = df.rename(columns={"code": "symbol", "name": "name"})  # 列名统一
        # 根据代码前缀判断所属市场: 6/9开头→上海, 其余→深圳
        df["market"] = df["symbol"].apply(
            lambda x: "SH" if x.startswith(("6", "9")) else "SZ"
        )
        df["ts_code"] = df["symbol"] + "." + df["market"]  # 拼接标准代码格式: 000001.SZ
        df["list_date"] = ""                          # AKShare不提供上市日期
        df["industry"] = ""                           # AKShare不提供行业信息
        df["is_st"] = df["name"].str.contains(r"ST|\*ST", na=False).astype(int)  # 标记ST
        df["delist_date"] = ""                        # 无退市日期
        logger.info(f"股票列表: AKShare ✓ ({len(df)}只)")
        # 按标准列顺序返回
        return df[["ts_code", "symbol", "name", "market", "list_date",
                    "industry", "is_st", "delist_date"]]
    except Exception as e:
        logger.warning(f"AKShare获取股票列表失败: {e}")
        return pd.DataFrame()


def _fetch_stock_list_baostock() -> pd.DataFrame:
    """Baostock获取A股股票列表（最后兜底方案，复用已缓存的登录连接）"""
    try:
        import baostock as bs                         # 延迟导入
        _ensure_bs_login()                            # 复用连接，避免每次login/logout
        rs = bs.query_stock_basic()                   # 查询股票基本信息
        rows = []
        # 逐行读取查询结果（Baostock使用游标方式）
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:                                  # 无数据
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)    # 转DataFrame
        df = df[df["type"] == "1"]                    # type=1 表示股票（非指数）
        df = df[df["status"] == "1"]                  # status=1 表示上市
        df["symbol"] = df["code"].str.split(".").str[1]   # 从code提取symbol: "sz.000001"→"000001"
        df["market"] = df["code"].str.split(".").str[0].str.upper()  # 提取市场并转大写
        df["ts_code"] = df["symbol"] + "." + df["market"]  # 拼接标准代码
        df = df.rename(columns={"code_name": "name"}) # 列名统一
        df["list_date"] = df.get("ipoDate", "")        # 上市日期
        df["industry"] = ""                            # Baostock不提供行业
        df["is_st"] = df["name"].str.contains(r"ST|\*ST", na=False).astype(int)
        df["delist_date"] = ""
        logger.info(f"股票列表: Baostock ✓ ({len(df)}只)")
        return df[["ts_code", "symbol", "name", "market", "list_date",
                    "industry", "is_st", "delist_date"]]
    except Exception as e:
        logger.warning(f"Baostock获取股票列表失败: {e}")
        return pd.DataFrame()


# ============================================================
# 日线数据获取核心（含三源级联）
#   fetch_daily()                    — 外部统一入口
#   _fetch_daily_akshare()           — 主力数据源
#   _fetch_daily_tushare()           — 备用数据源
#   _fetch_daily_baostock()          — 最后兜底
# ============================================================

def fetch_daily(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取单只股票日线数据，多源级联兜底

    参数:
        ts_code:    股票代码，如 "000001.SZ" 或 "600519.SH"
        start_date: 起始日期 "YYYYMMDD"
        end_date:   结束日期 "YYYYMMDD"

    返回DataFrame列:
        DAILY_COLUMNS — ts_code, trade_date, open, high, low, close,
                        volume, amount, pct_chg, turnover, adj_factor

    优先级:
        AKShare(前复权) → Tushare(未复权+复权因子) → Baostock(前复权)

    熔断保护:
        - 当 AKShare 连续失败 ≥20 次时自动熔断 300 秒
        - 避免 API 故障时对所有股票无意义重试
    """
    # ---------- 方案1: AKShare（主力数据源）----------
    # 优势: 免费、直接返回前复权、数据更新及时
    if not _is_akshare_circuit_open():                   # 熔断未打开才尝试AKShare
        df = _fetch_daily_akshare(ts_code, start_date, end_date)
        if not df.empty:
            _akshare_success()                           # 成功：重置失败计数
            return df
        _akshare_failure()                               # 失败：累计计数
    else:
        logger.debug(f"{ts_code} AKShare 已熔断，跳过")

    # ---------- 方案2: Tushare（备用数据源）----------
    # 优势: 数据质量高，但需要Token且有频率限制
    # 注意: Tushare返回未复权数据，需要额外获取复权因子手动计算前复权
    df = _fetch_daily_tushare(ts_code, start_date, end_date)
    if not df.empty:
        return df

    # ---------- 方案3: Baostock（最后兜底）----------
    # 优势: 完全免费，无限频，但数据更新可能慢半天
    df = _fetch_daily_baostock(ts_code, start_date, end_date)
    if not df.empty:
        return df

    # 所有数据源均失败
    logger.debug(f"{ts_code} 所有数据源均失败")
    return pd.DataFrame()


def _fetch_daily_akshare(ts_code: str, start_date: str,
                         end_date: str) -> pd.DataFrame:
    """AKShare获取日线数据（前复权）- 主力数据源

    AKShare是免费的Python量化库，数据来自东方财富，质量好、更新快。
    内置重试机制 + 指数退避，应对免费API限流。

    重试策略:
      第1次: 等待0.5s
      第2次: 等待2s + 随机0~1s
      第3次: 等待4s + 随机0~1s
    """
    import akshare as ak                          # 延迟导入（避免未安装时影响其他功能）
    symbol = ts_code.split(".")[0]                # 从 ts_code 提取纯数字代码: "000001.SZ"→"000001"

    # 尝试请求，最多 AKSHARE_MAX_RETRIES 次
    for attempt in range(1, AKSHARE_MAX_RETRIES + 1):
        try:
            # ---------- 请求前休眠（限速控制）----------
            if attempt > 1:
                # 重试时使用指数退避: 基延迟 × 2^(attempt-2) + 随机0~1s
                delay = AKSHARE_BASE_DELAY * (2 ** (attempt - 2)) + random.uniform(0, 1)
                logger.debug(f"{ts_code} AKShare 第{attempt}次重试，等待{delay:.1f}s")
                time.sleep(delay)
            else:
                # 首次请求只需最小间隔
                time.sleep(AKSHARE_BATCH_SLEEP)

            # ---------- 调用 AKShare API ----------
            # stock_zh_a_hist: 获取A股历史行情，数据源为东方财富
            df = ak.stock_zh_a_hist(
                symbol=symbol,                     # 股票代码（纯数字）
                period="daily",                    # 日线数据
                start_date=start_date,             # 起始日期 YYYYMMDD
                end_date=end_date,                 # 结束日期 YYYYMMDD
                adjust="qfq"                       # qfq=前复权, hfq=后复权, None=不复权
            )

            # ---------- 返回值为空则重试 ----------
            if df is None or df.empty:
                if attempt < AKSHARE_MAX_RETRIES:
                    continue                       # 还有重试次数，继续尝试
                return pd.DataFrame()              # 已用完重试次数，返回空

            # ---------- 列名翻译：中文 → 英文 ----------
            df = df.rename(columns={
                "日期": "trade_date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
                "成交额": "amount", "涨跌幅": "pct_chg", "换手率": "turnover",
            })

            # ---------- 补充系统字段 ----------
            df["ts_code"] = ts_code                                  # 标记股票代码
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")  # 统一日期格式
            df["adj_factor"] = 1.0                                   # 已前复权，复权因子=1

            logger.info(f"{ts_code} AKShare ✓ ({len(df)}条)")
            # 按统一列序输出、按日期升序排列
            return df[DAILY_COLUMNS].sort_values("trade_date").reset_index(drop=True)

        except Exception as e:
            # 捕获异常（网络超时、API错误等），打日志后重试
            logger.debug(f"{ts_code} AKShare第{attempt}次失败: {e}")
            if attempt < AKSHARE_MAX_RETRIES:
                continue                             # 还有重试次数，继续
            return pd.DataFrame()                    # 无重试次数了，返回空

    return pd.DataFrame()  # 所有重试均失败


def _fetch_daily_tushare(ts_code: str, start_date: str,
                         end_date: str) -> pd.DataFrame:
    """Tushare获取日线数据（未复权）+ 复权因子计算前复权

    处理流程:
      1. 获取未复权日线数据
      2. 获取对应区间的复权因子
      3. 用复权因子计算前复权价格
        前复权价 = 未复权价 × (当日复权因子 / 最新复权因子)

    注意：
      - Tushare免费用户限频 1次/秒
      - 如果拿不到复权因子则放弃此源（价格必须统一为前复权）
    """
    try:
        pro = _get_pro()                             # 获取 Tushare API 实例
        _rate_limit()                                 # 限频 0.35s
        # ---------- 第一步：拉取未复权日线 ----------
        df = pro.daily(
            ts_code=ts_code,                         # 股票代码
            start_date=start_date,                   # 起始日期
            end_date=end_date,                       # 结束日期
            fields="ts_code,trade_date,open,high,low,close,vol,amount,pct_chg"  # 需要的字段
        )
        if df is None or df.empty:
            return pd.DataFrame()                    # 无数据则跳过

        df = df.rename(columns={"vol": "volume"})    # 列名统一: vol → volume

        # ---------- 第二步：获取复权因子 ----------
        # 注意：免费用户 1 分钟只能调 1 次 adj_factor 接口
        try:
            adj_df = pro.adj_factor(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date
            )
            if adj_df is not None and not adj_df.empty:
                adj_map = dict(zip(adj_df["trade_date"], adj_df["adj_factor"]))  # 日期→因子映射
                df["adj_factor"] = df["trade_date"].map(adj_map).fillna(1.0)     # 填充到日线
            else:
                df["adj_factor"] = 1.0                # 无复权因子则默认1
        except Exception:
            # 限频导致拿不到复权因子 → 放弃此数据源
            # （宁可降级到 Baostock，也不用未复权价格）
            logger.debug(f"{ts_code} Tushare复权因子获取失败，跳过该源")
            return pd.DataFrame()

        df["turnover"] = 0.0                          # Tushare不直接提供换手率，置0

        # ---------- 第三步：计算前复权价格 ----------
        # 公式：前复权 = 未复权 × (当日复权因子 / 最新复权因子)
        latest_adj = df["adj_factor"].iloc[-1] if not df.empty else 1.0  # 取最新复权因子
        if latest_adj > 0:
            for col in ["open", "high", "low", "close"]:
                df[col] = (df[col] * df["adj_factor"] / latest_adj).round(2)  # 计算前复权并保留2位小数
            df["adj_factor"] = 1.0                    # 已前复权，复权因子置1

        logger.info(f"{ts_code} Tushare ✓ ({len(df)}条)")
        return df[DAILY_COLUMNS].sort_values("trade_date").reset_index(drop=True)

    except Exception as e:
        logger.debug(f"{ts_code} Tushare失败: {e}")
        return pd.DataFrame()


# ============================================================
# Baostock 连接池管理
#   - Baostock 的 login() 是全局状态，只需登录一次
#   - 复用连接避免每次请求都 login/logout，大幅提升效率
# ============================================================

_bs_logged_in = False  # 全局登录状态标记


def _ensure_bs_login():
    """确保 Baostock 已登录（复用连接，避免每次 login/logout）"""
    global _bs_logged_in                               # 声明使用全局状态
    if not _bs_logged_in:                              # 未登录时才执行登录
        import baostock as bs                          # 延迟导入
        lg = bs.login()                                # 登录 Baostock
        if lg.error_code == "0":                       # 登录成功
            _bs_logged_in = True
            logger.info("Baostock 登录成功")
        else:
            raise ConnectionError(f"Baostock 登录失败: {lg.error_msg}")  # 登录失败直接抛异常
    return _bs_logged_in                                # 返回登录状态


def _fetch_daily_baostock(ts_code: str, start_date: str,
                          end_date: str) -> pd.DataFrame:
    """Baostock获取日线数据（前复权）- 最后兜底

    使用场景：
      - AKShare 和 Tushare 都失败时降级到此方案
      - 完全免费、无限频，数据来源为证券宝
      - 复用已缓存的登录连接（见 _ensure_bs_login）

    注意：
      - 数据更新可能比 AKShare 慢半天（T+1）
      - Baostock 的 adjustflag=2 返回前复权
    """
    try:
        import baostock as bs                          # 延迟导入
        _ensure_bs_login()                             # 确保已登录（复用连接）

        # ---------- 转换代码格式 ----------
        # AKShare/Tushare 格式: "000001.SZ"
        # Baostock 格式: "sz.000001"
        symbol = ts_code.split(".")[0]                 # 提取数字部分: "000001"
        market = ts_code.split(".")[1].lower()         # 提取市场并转小写: "sz"
        bs_code = f"{market}.{symbol}"                 # 拼接 Baostock 格式: "sz.000001"

        # ---------- 日期格式转换 ----------
        # "20250101" → "2025-01-01"（Baostock 要求的格式）
        start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

        # ---------- 调用 Baostock API ----------
        rs = bs.query_history_k_data_plus(
            bs_code,                                   # Baostock 格式代码
            "date,open,high,low,close,volume,amount,turn,pctChg",  # 需要的字段
            start_date=start,                          # 起始日期
            end_date=end,                              # 结束日期
            frequency="d",                             # d=日线, w=周, m=月
            adjustflag="2"                             # 2=前复权, 1=后复权, 3=不复权
        )

        # ---------- 逐行读取查询结果 ----------
        rows = []
        while rs.error_code == "0" and rs.next():      # 游标遍历
            rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()                      # 无数据则返回空

        # ---------- 数据清洗 ----------
        df = pd.DataFrame(rows, columns=rs.fields)     # DataFrame化
        for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")  # 字符串→数值（无效值变NaN）
        df = df.dropna(subset=["close"])               # 去除收盘价为空的记录
        df = df[df["close"] > 0]                       # 去除收盘价≤0的记录

        # ---------- 价格精度标准化 ----------
        # Baostock 可能返回多位小数，统一 rounding 到2位
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].round(2)

        # ---------- 列名统一 ----------
        df["ts_code"] = ts_code                        # 标记股票代码
        df["trade_date"] = df["date"].str.replace("-", "")  # 日期格式统一: "2025-01-01"→"20250101"
        df = df.rename(columns={"turn": "turnover", "pctChg": "pct_chg"})  # 列名统一
        df["adj_factor"] = 1.0                         # 已前复权，复权因子=1

        logger.info(f"{ts_code} Baostock ✓ ({len(df)}条)")
        return df[DAILY_COLUMNS].sort_values("trade_date").reset_index(drop=True)

    except Exception as e:
        logger.debug(f"{ts_code} Baostock失败: {e}")
        return pd.DataFrame()


# ============================================================
# 指数成分股获取（多源级联）
#   用于确定沪深300/中证500等指数的成分股名单
#   优先级: AKShare → Baostock → Tushare
# ============================================================

def fetch_index_components(index_code: str = "399300.SZ") -> list:
    """获取指数成分股列表，多源级联兜底

    参数:
        index_code: 指数代码，默认 "399300.SZ"（沪深300）
                   支持: 399300.SZ(沪深300), 000905.SH(中证500), 399006.SZ(创业板指)

    返回:
        ts_code 列表，如 ["000001.SZ", "600519.SH", ...]
    """
    # 方案1: AKShare（推荐，数据来自中证指数官网）
    codes = _fetch_index_components_akshare(index_code)
    if codes:
        return codes

    # 方案2: Baostock（免费无限频）
    codes = _fetch_index_components_baostock(index_code)
    if codes:
        return codes

    # 方案3: Tushare（需Token，最后兜底）
    try:
        pro = _get_pro()
        _rate_limit()
        df = pro.index_weight(index_code=index_code)  # 调用Tushare指数权重接口
        if df is not None and not df.empty:
            logger.info(f"指数成分股: Tushare ✓")
            return df["con_code"].unique().tolist()    # 返回成分股代码列表
    except Exception as e:
        logger.warning(f"Tushare获取指数成分股失败: {e}")

    return []  # 所有方案均失败


def _fetch_index_components_akshare(index_code: str) -> list:
    """AKShare获取指数成分股（数据来自中证指数官网 csindex.com.cn）"""
    try:
        import akshare as ak
        # ---------- 代码映射 ----------
        # AKShare 的 index_stock_cons_csindex 接受中证指数格式
        code_map = {
            "399300.SZ": "000300",   # 沪深300
            "000300.SH": "000300",   # 沪深300（沪市代码也映射到000300）
            "000905.SH": "000905",   # 中证500
            "399006.SZ": "399006",   # 创业板指
        }
        idx_code = code_map.get(index_code, "000300")  # 默认沪深300

        # ---------- 调用 AKShare ----------
        df = ak.index_stock_cons_csindex(symbol=idx_code)
        if df is not None and not df.empty:
            # AKShare 返回的列不固定，需要自动识别哪一列是6位股票代码
            code_col = None
            max_unique = 0
            for col in df.columns:
                vals = df[col].astype(str)
                if vals.str.match(r"^\d{6}$").all():          # 该列所有值都是6位数字
                    n_unique = vals.nunique()                  # 统计唯一值数量
                    if n_unique > max_unique:                  # 取唯一值最多的列（排除辅助列）
                        max_unique = n_unique
                        code_col = col

            if code_col:
                codes = df[code_col].astype(str).str.zfill(6).tolist()  # 前补零到6位
                # 添加市场后缀: 6/9开头→.SH, 其余→.SZ
                result = [f"{c}.SH" if c.startswith(("6", "9")) else f"{c}.SZ"
                          for c in codes]
                logger.info(f"指数成分股: AKShare ✓ ({len(result)}只)")
                return result
    except Exception as e:
        logger.debug(f"AKShare获取指数成分股失败: {e}")
    return []


def _fetch_index_components_baostock(index_code: str) -> list:
    """Baostock获取指数成分股（复用已缓存的登录连接）"""
    try:
        import baostock as bs
        _ensure_bs_login()                                   # 复用已缓存的登录

        # ---------- 代码映射 ----------
        code_map = {
            "399300.SZ": "sh.000300",   # 沪深300
            "000300.SH": "sh.000300",   # 沪深300
            "000905.SH": "sh.000905",   # 中证500
            "399006.SZ": "sz.399006",   # 创业板指
        }
        bs_code = code_map.get(index_code, "sh.000300")       # 默认沪深300

        # Baostock 的查询需要指定日期
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

        # 根据指数代码选择对应的查询接口
        rs = bs.query_hs300_stocks(date=today)                # 沪深300
        if "500" in bs_code:
            rs = bs.query_zz500_stocks(date=today)            # 中证500

        # 游标遍历查询结果
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if rows:
            df = pd.DataFrame(rows, columns=rs.fields)
            codes = df["code"].tolist()                       # 格式: "sz.000001"
            # 转换为标准格式: "sz.000001" → "000001.SZ"
            result = [f"{c.split('.')[1]}.{c.split('.')[0].upper()}"
                      for c in codes]
            logger.info(f"指数成分股: Baostock ✓ ({len(result)}只)")
            return result
    except Exception as e:
        logger.debug(f"Baostock获取指数成分股失败: {e}")
    return []


# ============================================================
# 数据时效检查
#   用于 Dashboard 首页展示数据新鲜度状态
# ============================================================

def check_data_freshness() -> dict:
    """检查数据库中行情数据的最新日期和统计信息

    返回:
        {
            "latest_date":   最新交易日 "YYYYMMDD",
            "active_stocks": 有数据的股票数量,
            "total_rows":    日线数据总条数,
        }
    """
    from data.storage import get_conn
    with get_conn() as conn:                                 # 获取数据库连接
        cur = conn.execute("SELECT MAX(trade_date) FROM daily_price")
        max_date = cur.fetchone()[0]                         # 最新交易日

        cur = conn.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price")
        active_stocks = cur.fetchone()[0]                    # 有数据的股票数

        cur = conn.execute("SELECT COUNT(*) FROM daily_price")
        total_rows = cur.fetchone()[0]                       # 总记录数

    return {
        "latest_date": max_date,
        "active_stocks": active_stocks,
        "total_rows": total_rows,
    }


# ============================================================
# 大盘指数数据管理
#   三大指数（上证/深证/创业板）的日线数据，存入 index_daily 表
# ============================================================

# 支持的指数列表（key=ts_code格式, value=中文名）
INDEX_CODES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
}


def fetch_index_daily(index_code: str, start_date: str = "",
                      end_date: str = "") -> pd.DataFrame:
    """获取大盘指数日线数据（从 AKShare 实时拉取）

    参数:
        index_code: 指数代码，如 "000001.SH"
        start_date: 起始日期 "YYYYMMDD"（空=全部）
        end_date:   结束日期 "YYYYMMDD"（空=全部）

    返回:
        DataFrame 列: ts_code, trade_date, open, high, low, close, volume, pct_chg
    """
    try:
        import akshare as ak
        # ---------- 代码格式转换 ----------
        # "000001.SH" → "sh000001"（AKShare 格式）
        parts = index_code.split(".")
        market = parts[1].lower()                            # "SH" → "sh"
        symbol = parts[0]                                    # "000001"
        ak_code = f"{market}{symbol}"                        # "sh000001"

        # ---------- 调用 AKShare 指数接口 ----------
        df = ak.stock_zh_index_daily(symbol=ak_code)
        if df is None or df.empty:
            return pd.DataFrame()

        # ---------- 列名翻译：中文→英文 ----------
        df = df.rename(columns={
            "date": "trade_date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume",
        })
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")  # 统一日期格式
        df["ts_code"] = index_code                           # 标记指数代码
        df["pct_chg"] = df["close"].pct_change() * 100       # 计算涨跌幅（百分比）

        # 按标准列序整理
        cols = ["ts_code", "trade_date", "open", "high", "low", "close",
                "volume", "pct_chg"]
        df = df[cols].sort_values("trade_date").reset_index(drop=True)

        # 按日期范围过滤
        if start_date:
            df = df[df["trade_date"] >= start_date]
        if end_date:
            df = df[df["trade_date"] <= end_date]

        logger.info(f"指数 {index_code} AKShare ✓ ({len(df)}条)")
        return df
    except Exception as e:
        logger.warning(f"获取指数 {index_code} 失败: {e}")
        return pd.DataFrame()


def update_all_indices(start_date: str = "", end_date: str = "") -> int:
    """更新所有大盘指数数据到数据库（供增量更新和初始化调用）

    参数:
        start_date: 起始日期 "YYYYMMDD"
        end_date:   结束日期 "YYYYMMDD"（默认今天）

    返回:
        写入数据库的总记录数
    """
    from data.storage import save_index_daily

    if not end_date:
        from datetime import datetime
        end_date = datetime.now().strftime("%Y%m%d")         # 默认到今天

    total = 0
    for code in INDEX_CODES:                                 # 遍历三大指数
        df = fetch_index_daily(code, start_date, end_date)   # 拉取数据
        if not df.empty:
            save_index_daily(df)                              # 写入 index_daily 表
            total += len(df)                                  # 累计记录数
    logger.info(f"指数数据更新完成，共 {total} 条")
    return total
