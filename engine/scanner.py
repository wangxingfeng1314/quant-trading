"""信号扫描器 - 全市场扫描，输出今日买卖信号"""
import logging
import concurrent.futures
from typing import List, Callable
from datetime import datetime

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
)

logger = logging.getLogger(__name__)


def _scan_single_stock(ts_code: str, end_date: str, strategy_instances: list) -> list:
    """扫描单只股票的所有策略信号（可作为并行任务单元）

    Args:
        ts_code: 股票代码
        end_date: 扫描截止日期
        strategy_instances: 已初始化的策略实例列表

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

    df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma"])
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
                 parallel: bool = True) -> List[Signal]:
    """扫描全市场信号

    Args:
        universe: 股票列表，None则扫描数据库中所有有数据的股票
        strategy_names: 要运行的策略名列表，None则运行所有策略
        end_date: 扫描日期，默认今天
        save: 是否保存到数据库
        progress_callback: 进度回调函数(completed, total)
        parallel: 是否使用并行扫描（默认True，少量股票时可选False）

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

    # ---------- 缓存检查：当天已扫描过的直接返回 ----------
    cached = get_signals(trade_date=end_date)
    if not cached.empty:
        cached = cached[cached["ts_code"].isin(universe)]
        cached = cached[cached["strategy"].isin(strategy_names)]
        cached_stocks = cached["ts_code"].nunique()
        if cached_stocks >= len(universe) * SCANNER_CACHE_THRESHOLD:
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
        else:
            logger.info(f"部分缓存: {cached_stocks}/{len(universe)} 只股票, 补扫剩余部分")

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
                executor.submit(_scan_single_stock, ts_code, end_date, strategy_instances): ts_code
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
            sigs = _scan_single_stock(ts_code, end_date, strategy_instances)
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
