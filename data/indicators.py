"""技术指标计算 - 纯函数，输入DataFrame返回加了列的DataFrame

所有函数保持纯函数风格：不修改输入的原始 DataFrame，每次返回新的副本。
支持的指标：
  - MA:   移动均线 (5/10/20/60)
  - EMA:  指数移动均线 (12/26)
  - MACD: 指数平滑异同移动平均线
  - RSI:  相对强弱指标 (Wilder平滑)
  - BOLL: 布林带 (20日, 2倍标准差)
  - KDJ:  随机指标 (9/3/3)
  - VOL_MA: 成交量均线 (5/10/20)
  - ATR:  平均真实波幅 (14日)
"""
import pandas as pd     # 数据处理
import numpy as np      # 数值计算


def add_ma(df: pd.DataFrame, periods: list = None) -> pd.DataFrame:
    """添加移动均线 (MA)

    参数:
        periods: 均线周期列表，默认 [5, 10, 20, 60]
    添加列:
        ma5, ma10, ma20, ma60 — 对应周期的简单移动均线
    """
    if periods is None:
        periods = [5, 10, 20, 60]
    df = df.copy()
    for p in periods:
        # rolling(window=p) 计算过去p个收盘价的平均值
        # min_periods=1 确保起始数据不足时也能计算（前p-1行用已有数据）
        df[f"ma{p}"] = df["close"].rolling(window=p, min_periods=1).mean()
    return df


def add_ema(df: pd.DataFrame, periods: list = None) -> pd.DataFrame:
    """添加指数移动均线 (EMA)

    参数:
        periods: EMA周期列表，默认 [12, 26]
    添加列:
        ema12, ema26 — 对应周期的指数移动均线
    """
    if periods is None:
        periods = [12, 26]
    df = df.copy()
    for p in periods:
        # ewm(span=p) 使用指数加权，adjust=False 使用递归公式
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def add_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26,
             signal: int = 9) -> pd.DataFrame:
    """添加MACD指标

    经典MACD公式:
      dif     = EMA(fast) - EMA(slow)         — 快慢线差值
      dea     = EMA(dif, signal)               — 信号线
      macd_hist = (dif - dea) * 2              — 柱状图

    添加列: dif, dea, macd_hist
    """
    df = df.copy()
    # 计算快慢EMA
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["dif"] = ema_fast - ema_slow               # DIF线
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()  # DEA信号线
    df["macd_hist"] = (df["dif"] - df["dea"]) * 2 # MACD柱状图（加倍放大）
    return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """添加RSI指标（Wilder平滑法）

    使用 Wilder's 平滑（alpha=1/period），与传统 RSI 公式一致。
    相比标准EMA，Wilder平滑给予近期数据更均衡的权重。

    参数:
        period: 计算周期，默认 14
    添加列: rsi14
    """
    df = df.copy()
    delta = df["close"].diff()                    # 每日价格变化
    gain = delta.clip(lower=0)                    # 涨幅（正值）
    loss = (-delta).clip(lower=0)                 # 跌幅（正值）
    # Wilder平滑: alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)   # 相对强弱比（避免除0）
    df[f"rsi{period}"] = 100 - (100 / (1 + rs))   # RSI公式
    return df


def add_bollinger(df: pd.DataFrame, period: int = 20,
                  std_dev: float = 2.0) -> pd.DataFrame:
    """添加布林带 (Bollinger Bands)

    公式:
      boll_mid   = MA(close, period)
      boll_upper = boll_mid + std_dev × σ(close)
      boll_lower = boll_mid - std_dev × σ(close)

    参数:
        period:  计算周期，默认 20
        std_dev: 标准差倍数，默认 2.0
    添加列: boll_mid, boll_upper, boll_lower
    """
    df = df.copy()
    # min_periods=period: 确保前period-1行不计算（数据不足时无意义）
    df["boll_mid"] = df["close"].rolling(window=period, min_periods=period).mean()
    std = df["close"].rolling(window=period, min_periods=period).std()
    df["boll_upper"] = df["boll_mid"] + std_dev * std
    df["boll_lower"] = df["boll_mid"] - std_dev * std
    return df


def add_kdj(df: pd.DataFrame, n: int = 9, m1: int = 3,
            m2: int = 3) -> pd.DataFrame:
    """添加KDJ随机指标

    公式:
      RSV = (close - low_n) / (high_n - low_n) × 100
      K   = EMA(RSV, m1)
      D   = EMA(K, m2)
      J   = 3K - 2D

    参数:
        n:   RSV计算周期，默认 9
        m1: K值平滑周期，默认 3
        m2: D值平滑周期，默认 3
    添加列: kdj_k, kdj_d, kdj_j
    """
    df = df.copy()
    low_n = df["low"].rolling(window=n, min_periods=1).min()    # N日内最低价
    high_n = df["high"].rolling(window=n, min_periods=1).max()  # N日内最高价
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100  # RSV值
    df["kdj_k"] = rsv.ewm(alpha=1 / m1, adjust=False).mean()   # K值
    df["kdj_d"] = df["kdj_k"].ewm(alpha=1 / m2, adjust=False).mean()  # D值
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]            # J值
    return df


def add_volume_ma(df: pd.DataFrame, periods: list = None) -> pd.DataFrame:
    """添加成交量均线

    参数:
        periods: 均线周期列表，默认 [5, 10, 20]
    添加列: vol_ma5, vol_ma10, vol_ma20
    """
    if periods is None:
        periods = [5, 10, 20]
    df = df.copy()
    for p in periods:
        df[f"vol_ma{p}"] = df["volume"].rolling(window=p, min_periods=1).mean()
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """添加ATR (Average True Range) 平均真实波幅

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = MA(TR, period)

    参数:
        period: 计算周期，默认 14
    添加列: atr14
    """
    df = df.copy()
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)             # 前一日收盘价
    tr = pd.concat([
        high - low,                                # 当日振幅
        (high - prev_close).abs(),                 # 最高到前收的绝对偏差
        (low - prev_close).abs()                   # 最低到前收的绝对偏差
    ], axis=1).max(axis=1)                         # 取三者的最大值
    df[f"atr{period}"] = tr.rolling(window=period, min_periods=1).mean()
    return df


def apply_indicators(df: pd.DataFrame, indicators: list = None) -> pd.DataFrame:
    """批量添加多个技术指标

    一次性计算多个指标，避免重复遍历DataFrame。
    常用的组合已预定义：
      - None（默认）: ma, macd, rsi, boll, vol_ma
      - 也可传入自定义列表，如 ["ma", "rsi"]

    参数:
        df: 含 OHLCV 列的原始行情 DataFrame
        indicators: 要计算的指标名列表，None=全部默认指标

    返回:
        添加了指标列的新 DataFrame（不修改输入数据）
    """
    # 所有支持的指标及其对应的计算函数
    all_indicators = {
        "ma": lambda d: add_ma(d),
        "macd": lambda d: add_macd(d),
        "rsi": lambda d: add_rsi(d),
        "boll": lambda d: add_bollinger(d),
        "kdj": lambda d: add_kdj(d),
        "vol_ma": lambda d: add_volume_ma(d),
        "atr": lambda d: add_atr(d),
    }

    # 默认指标组合：覆盖最常用的技术分析指标
    if indicators is None:
        indicators = ["ma", "macd", "rsi", "boll", "vol_ma"]

    # 依次计算每个指标
    for name in indicators:
        if name in all_indicators:
            df = all_indicators[name](df)  # 每个指标都是纯函数，返回新DataFrame

    return df
