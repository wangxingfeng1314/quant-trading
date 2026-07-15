"""信号扫描器 - 全市场扫描，输出今日买卖信号"""
import logging
from typing import List, Callable
from datetime import datetime

from data.storage import get_daily, get_stock_list, save_signal, get_signals
from data.indicators import apply_indicators
from data.cleaner import clean_daily
from strategies import STRATEGY_REGISTRY
from core.models import Signal

logger = logging.getLogger(__name__)


def scan_signals(universe: list = None, strategy_names: list = None,
                 end_date: str = "", save: bool = True,
                 progress_callback: Callable = None) -> List[Signal]:
    """扫描全市场信号

    Args:
        universe: 股票列表，None则扫描数据库中所有有数据的股票
        strategy_names: 要运行的策略名列表，None则运行所有策略
        end_date: 扫描日期，默认今天
        save: 是否保存到数据库

    Returns:
        Signal列表，按score降序排列
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    if strategy_names is None:
        strategy_names = list(STRATEGY_REGISTRY.keys())

    # 获取股票列表
    if universe is None:
        stock_df = get_stock_list()
        if stock_df.empty:
            logger.error("无股票数据")
            return []
        universe = stock_df["ts_code"].tolist()

    # 预过滤：只扫描有数据且数据量足够的股票
    from data.storage import get_conn
    with get_conn() as conn:
        valid = set()
        cur = conn.execute(
            "SELECT ts_code, COUNT(*) as cnt FROM daily_price "
            "GROUP BY ts_code HAVING cnt >= 60"
        )
        for row in cur.fetchall():
            valid.add(row[0])
    universe = [c for c in universe if c in valid]

    logger.info(f"扫描 {len(universe)} 只股票(预过滤), "
                f"策略: {strategy_names}, 日期: {end_date}")

    all_signals = []

    # 初始化策略
    strategies = []
    for name in strategy_names:
        if name in STRATEGY_REGISTRY:
            strategies.append(STRATEGY_REGISTRY[name]())

    # 逐只股票扫描
    for i, ts_code in enumerate(universe):
        df = get_daily(ts_code)
        if df.empty or len(df) < 60:
            continue

        df = clean_daily(df)
        if df.empty:
            continue

        # 只取到end_date的数据
        df = df[df["trade_date"] <= end_date]
        if df.empty:
            continue

        df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma"])

        # 构造data_dict
        data_dict = {ts_code: df}

        # 运行每个策略
        for strategy in strategies:
            try:
                signals = strategy.on_bar(end_date, data_dict)
                all_signals.extend(signals)
            except Exception as e:
                logger.debug(f"{ts_code} {strategy.name} 扫描异常: {e}")

        if (i + 1) % 100 == 0:
            logger.info(f"已扫描 {i + 1}/{len(universe)}")

        if progress_callback:
            progress_callback(i + 1, len(universe))

    # 按score降序排列
    all_signals.sort(key=lambda s: s.score, reverse=True)

    # 保存
    if save and all_signals:
        for sig in all_signals:
            save_signal(sig)
        logger.info(f"已保存 {len(all_signals)} 条信号")

    logger.info(f"扫描完成，共 {len(all_signals)} 条信号")
    return all_signals
