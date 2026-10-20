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
    try:
        df = get_daily(ts_code, end_date=end_date)
    except TypeError:
        df = get_daily(ts_code)
    if df.empty:
        return []

    df = clean_daily(df)
    if df.empty:
        return []

    # 只取到 end_date 的数据
    if end_date:
        df = df[df["trade_date"] <= end_date]

    # 截断后必须满足最小有效数据天数，防止历史扫描时因前序K线过短产生虚假指标或计算异常
    if df.empty or len(df) < SCANNER_MIN_DATA_DAYS:
        return []

    # 流动性与停牌守卫检查
    amt_threshold = min_daily_amount if min_daily_amount is not None else SCANNER_MIN_DAILY_AMOUNT
    flt_susp = filter_suspended if filter_suspended is not None else SCANNER_FILTER_SUSPENDED
    if not check_stock_liquidity(df, end_date=end_date, min_daily_amount=amt_threshold, filter_suspended=flt_susp):
        return []

    df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
    data_dict = {ts_code: df}

    # 使用行情K线实际交易日触发策略，避免周末或假日扫描导致信号日期失真
    bar_date = str(df["trade_date"].iloc[-1])
    raw_signals = []
    for strategy in strategy_instances:
        try:
            sigs = strategy.on_bar(bar_date, data_dict)
            raw_signals.extend(sigs)
        except Exception as e:
            logger.warning(f"{ts_code} {strategy.name} 扫描异常: {e}")

    if not raw_signals:
        return []

    # ---------- 策略可信度权重校准与同股多空冲突消解 ----------
    from services.signal_service import STRATEGY_CREDIBILITY_WEIGHTS
    for sig in raw_signals:
        weight = STRATEGY_CREDIBILITY_WEIGHTS.get(sig.strategy, 1.0)
        sig.score = round(min(max(sig.score * weight, 0.10), 1.0), 2)

    if len(strategy_instances) <= 1:
        return raw_signals

    buys = [s for s in raw_signals if s.direction == "BUY"]
    sells = [s for s in raw_signals if s.direction == "SELL"]

    # 趋势破位类策略集合：一旦触发卖出，说明短期/中期趋势已坏，同日严禁推荐买入
    trend_breakdown_strategies = {
        "ma_bullish", "ma_cross", "ma60_breakout", "ma_pullback",
        "volume_price_breakout", "turtle", "donchian_breakout",
    }

    if buys and sells:
        has_trend_breakdown = any(s.strategy in trend_breakdown_strategies for s in sells)
        max_buy = max(s.score for s in buys)
        max_sell = max(s.score for s in sells)
        # 若存在趋势破位卖出，或看空信号数 >= 看多信号数，或最高看空分接近/超过看多分 → 拦截买入信号，杜绝左右互搏
        if has_trend_breakdown or len(sells) >= len(buys) or max_sell >= max_buy - 0.05:
            sell_str = ",".join(s.strategy for s in sells)
            logger.debug(f"{ts_code} ({bar_date}) 存在看空冲突 [{sell_str}]，已自动拦截同日买入信号")
            return sells
        else:
            # 买方显著占优，但仍提示存在局部看空分歧并适度扣分
            sell_str = ",".join(s.strategy for s in sells)
            for b in buys:
                b.score = round(max(b.score - 0.12, 0.10), 2)
                b.reason = f"{b.reason} [⚠️含分歧:{sell_str}]"
            return buys + sells

    # 纯多头多策略同日共振加分（>=2个独立策略同时看多且0看空）
    if len(buys) >= 2 and not sells:
        resonance_bonus = min(0.06 * (len(buys) - 1), 0.18)
        strat_list_str = "+".join(s.strategy for s in buys)
        for b in buys:
            b.score = round(min(b.score + resonance_bonus, 0.98), 2)
            if "策略共振" not in b.reason:
                b.reason = f"[🔥{len(buys)}策略共振:{strat_list_str}] {b.reason}"

    return raw_signals


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

    # 大盘环境感知调权（空头环境自动压降激进追涨信号，多头环境小幅鼓励顺势突破）
    if all_signals:
        try:
            from services.signal_service import get_market_regime
            regime_info = get_market_regime(end_date)
            regime = regime_info.get("regime", "RANGE")
            aggressive_strats = {"turtle", "donchian_breakout", "volume_price_breakout", "signal_combo", "kdj_cross"}
            trend_strats = {"ma_bullish", "ma60_breakout", "ma_cross", "volume_price_breakout", "macd_cross"}
            for sig in all_signals:
                if sig.direction == "BUY":
                    if regime == "BEAR" and sig.strategy in aggressive_strats:
                        sig.score = round(max(sig.score - 0.08, 0.10), 2)
                    elif regime == "BULL" and sig.strategy in trend_strats:
                        sig.score = round(min(sig.score + 0.03, 0.99), 2)
        except Exception as e:
            logger.debug(f"大盘环境调权跳过: {e}")

    # 按 score 降序排列
    all_signals.sort(key=lambda s: s.score, reverse=True)

    # 批量保存（先清理本次扫描标集合在对应交易日的旧信号，防止过期的伪信号残留）
    if save and all_signals:
        try:
            bar_dates = sorted({s.trade_date for s in all_signals if s.trade_date})
            if bar_dates and universe and strategy_names:
                with get_conn() as conn:
                    for b_dt in bar_dates:
                        u_ph = ",".join("?" * len(universe))
                        s_ph = ",".join("?" * len(strategy_names))
                        conn.execute(
                            f"DELETE FROM signal WHERE trade_date = ? AND ts_code IN ({u_ph}) AND strategy IN ({s_ph})",
                            [b_dt] + list(universe) + list(strategy_names),
                        )
        except Exception as e:
            logger.debug(f"清理同日旧信号跳过: {e}")
        save_signals_batch(all_signals)
        logger.info(f"已批量保存 {len(all_signals)} 条信号")

    logger.info(f"扫描完成，共 {len(all_signals)} 条信号")
    return all_signals
