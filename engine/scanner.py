"""信号扫描器 - 全市场扫描，输出今日买卖信号"""
import logging
import concurrent.futures
from typing import List, Callable
from datetime import datetime
import pandas as pd


from data.storage import (
    get_daily, get_instrument_list, save_signal, save_signals_batch,
    get_signals, get_conn,
)
from data.indicators import apply_indicators
from data.cleaner import clean_daily
from strategies import STRATEGY_REGISTRY
from core.models import Signal
from core.config import (
    SCANNER_CACHE_THRESHOLD, SCANNER_MIN_DATA_DAYS, SCANNER_PARALLEL_WORKERS,
    SCANNER_MIN_DAILY_AMOUNT, SCANNER_FILTER_SUSPENDED,
)

logger = logging.getLogger(__name__)


def check_stock_liquidity(
    df: "pd.DataFrame",
    end_date: str = "",
    min_daily_amount: float = 0.0,
    filter_suspended: bool = True,
) -> bool:
    """检查标的是否满足流动性要求及非停牌状态

    Args:
        df: 清洗并截断至 end_date 的日线数据（升序排列）
        end_date: 扫描目标日期
        min_daily_amount: 近 5 日最低日均成交额（元，0表示不限制）
        filter_suspended: 是否过滤停牌/成交量为0标的
    """
    if df is None or df.empty:
        return False

    latest = df.iloc[-1]

    # 1. 停牌过滤
    if filter_suspended:
        # 最后一根 bar 的成交量或成交额为 0 视为停牌 (兼容 volume 与 vol 字段)
        vol_val = latest.get("volume") if ("volume" in latest and pd.notna(latest.get("volume"))) else latest.get("vol", 0)
        amt_val = latest.get("amount", 0)
        if float(vol_val or 0) <= 0 or float(amt_val or 0) <= 0:
            return False
        # 检查最新交易日与 end_date 跨度，若最新交易日落后超过30天视为长期停牌
        if end_date:
            try:
                dt_latest = datetime.strptime(str(latest.get("trade_date", "")), "%Y%m%d")
                dt_end = datetime.strptime(str(end_date), "%Y%m%d")
                if (dt_end - dt_latest).days > 30:
                    return False
            except Exception:
                pass

    # 2. 流动性过滤（近5日日均成交额）
    if min_daily_amount > 0 and len(df) >= 1:
        check_window = min(5, len(df))
        avg_amt = df["amount"].tail(check_window).mean()
        if avg_amt < min_daily_amount:
            return False

    return True


def _scan_single_stock(
    ts_code: str,
    end_date: str,
    strategy_instances: list,
    min_daily_amount: float = None,
    filter_suspended: bool = None,
) -> list:
    """扫描单只股票的所有策略信号（可作为并行任务单元）

    Args:
        ts_code: 股票代码
        end_date: 扫描截止日期
        strategy_instances: 已初始化的策略实例列表
        min_daily_amount: 最小日均成交额（元）
        filter_suspended: 是否过滤停牌标的

    Returns:
        该股票产生的 Signal 列表
    """
    df = get_daily(ts_code)
    if df.empty or len(df) < SCANNER_MIN_DATA_DAYS:
        return []

    df = clean_daily(df)
    if df.empty:
        return []

    # 只取到 end_date 的数据
    df = df[df["trade_date"] <= end_date]
    if df.empty:
        return []

    # 流动性与停牌守卫检查
    amt_threshold = min_daily_amount if min_daily_amount is not None else SCANNER_MIN_DAILY_AMOUNT
    flt_susp = filter_suspended if filter_suspended is not None else SCANNER_FILTER_SUSPENDED
    if not check_stock_liquidity(df, end_date=end_date, min_daily_amount=amt_threshold, filter_suspended=flt_susp):
        return []

    df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
    data_dict = {ts_code: df}

    signals = []
    for strategy in strategy_instances:
        try:
            sigs = strategy.on_bar(end_date, data_dict)
            signals.extend(sigs)
        except Exception as e:
            logger.warning(f"{ts_code} {strategy.name} 扫描异常: {e}")

    return signals


def scan_signals(universe: list = None, strategy_names: list = None,
                 end_date: str = "", save: bool = True,
                 progress_callback: Callable = None,
                 parallel: bool = True,
                  min_daily_amount: float = None,
                  filter_suspended: bool = None,
                  force_refresh: bool = False) -> List[Signal]:

    """扫描全市场信号

    Args:
        universe: 股票列表，None则扫描数据库中所有有数据的股票
        strategy_names: 要运行的策略名列表，None则运行所有策略
        end_date: 扫描日期，默认今天
        save: 是否保存到数据库
        progress_callback: 进度回调函数(completed, total)
        parallel: 是否使用并行扫描（默认True，少量股票时可选False）
        min_daily_amount: 最小日均成交额
        filter_suspended: 是否过滤停牌
        force_refresh: 是否强制全量重扫（忽略缓存）

    Returns:
        Signal列表，按score降序排列
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    if strategy_names is None:
        strategy_names = list(STRATEGY_REGISTRY.keys())

    # 获取标的列表（股票 + ETF）
    if universe is None:
        stock_df = get_instrument_list()
        if stock_df.empty:
            logger.error("无标的数据")
            return []
        universe = stock_df["ts_code"].tolist()

    # ---------- 缓存检查：针对小规模自选股，当天已扫描过的直接返回 ----------
    if not force_refresh and len(universe) <= 20:
        cached = get_signals(trade_date=end_date)
        if not cached.empty:
            cached = cached[cached["ts_code"].isin(universe)]
            cached = cached[cached["strategy"].isin(strategy_names)]
            if not cached.empty and set(cached["ts_code"]) == set(universe):
                logger.info(f"缓存命中: {len(cached)} 条信号 (日期={end_date}), 跳过全量扫描")
                signals_list = []
                for _, row in cached.iterrows():
                    signals_list.append(Signal(
                        ts_code=row["ts_code"], trade_date=row["trade_date"],
                        strategy=row["strategy"], direction=row["direction"],
                        score=float(row.get("score", 0)),
                        reason=row.get("reason", ""),
                        price_ref=float(row.get("price_ref", 0)),
                    ))
                signals_list.sort(key=lambda s: s.score, reverse=True)
                return signals_list

    # 预过滤：只扫描有数据且数据量足够的股票
    with get_conn() as conn:
        valid = set()
        cur = conn.execute(
            f"SELECT ts_code, COUNT(*) as cnt FROM daily_price "
            f"GROUP BY ts_code HAVING cnt >= {SCANNER_MIN_DATA_DAYS}"
        )
        for row in cur.fetchall():
            valid.add(row[0])
    universe = [c for c in universe if c in valid]

    logger.info(f"扫描 {len(universe)} 只股票(预过滤), "
                f"策略: {strategy_names}, 日期: {end_date}")

    # 初始化策略实例
    strategy_instances = []
    for name in strategy_names:
        if name in STRATEGY_REGISTRY:
            strategy_instances.append(STRATEGY_REGISTRY[name]())

    all_signals = []

    # 并行扫描（>= 50 只股票时启用并行）
    use_parallel = parallel and len(universe) >= 50

    if use_parallel:
        max_workers = SCANNER_PARALLEL_WORKERS or None  # None = 自动检测 CPU 核心数
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _scan_single_stock, ts_code, end_date, strategy_instances,
                    min_daily_amount, filter_suspended,
                ): ts_code
                for ts_code in universe
            }
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                ts_code = futures[future]
                try:
                    sigs = future.result()
                    all_signals.extend(sigs)
                except Exception as e:
                    logger.warning(f"{ts_code} 扫描异常: {e}")

                completed += 1
                if completed % 100 == 0:
                    logger.info(f"已扫描 {completed}/{len(universe)}")
                if progress_callback:
                    progress_callback(completed, len(universe))
    else:
        # 串行扫描（少量股票时更高效，无进程开销）
        for i, ts_code in enumerate(universe):
            sigs = _scan_single_stock(
                ts_code, end_date, strategy_instances,
                min_daily_amount, filter_suspended,
            )
            all_signals.extend(sigs)

            if (i + 1) % 100 == 0:
                logger.info(f"已扫描 {i + 1}/{len(universe)}")
            if progress_callback:
                progress_callback(i + 1, len(universe))

    # 按 score 降序排列
    all_signals.sort(key=lambda s: s.score, reverse=True)

    # 批量保存（一次 executemany 替代逐条 INSERT）
    if save and all_signals:
        save_signals_batch(all_signals)
        logger.info(f"已批量保存 {len(all_signals)} 条信号")

    logger.info(f"扫描完成，共 {len(all_signals)} 条信号")
    return all_signals
