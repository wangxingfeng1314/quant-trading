"""信号扫描器 - 全市场扫描，输出今日买卖信号"""
import logging
import concurrent.futures
from typing import List, Callable
from datetime import datetime, timedelta
import pandas as pd


from data.storage import (
    get_daily, get_instrument_list, save_signal, save_signals_batch,
    get_signals, get_conn, get_watchlist,
)
from data.indicators import apply_indicators
from data.cleaner import clean_daily
from strategies import STRATEGY_REGISTRY
from core.models import Signal
from core.config import (
    SCANNER_CACHE_THRESHOLD, SCANNER_MIN_DATA_DAYS, SCANNER_PARALLEL_WORKERS,
    SCANNER_MIN_DAILY_AMOUNT, SCANNER_FILTER_SUSPENDED,
    SCANNER_MIN_BUY_SCORE, SCANNER_WHIPSAW_COOLDOWN_DAYS,
    SCANNER_SELL_DEDUP_DAYS, SCANNER_BEAR_BLOCK_TREND_BUY,
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


def _filter_by_watchlist_strategy(signals: list) -> list:
    """自选股按各自适配策略过滤（防错配策略与样本外不适配标的乱推）。

    规则：
      - 若标的不在自选股推荐映射表中（或暂无有效推荐策略且未被熔断），原样放行；
      - 若被样本外熔断为「⛔ 暂不适配·观望」：放行 SELL 保护已有持仓，拦截单策略 BUY（>=2共振仍放行）；
      - 若在映射表中：仅保留命中该股适配策略（附 ``[⭐适配]`` 标记）的信号，
        或当日同向有 ``>= 2`` 个独立策略同时触发的强共振信号。
    """
    if not signals:
        return signals
    try:
        wl = get_watchlist()
    except Exception as e:
        logger.debug(f"读取自选股适配策略跳过: {e}")
        return signals
    if wl is None or wl.empty or "recommended_strategy" not in wl.columns:
        return signals

    from services.strategy_recommender import (
        parse_recommended_strategy_key, is_no_trade_recommendation,
    )

    rec_map = {}
    no_trade_codes = set()
    for _, row in wl.iterrows():
        code_str = str(row["ts_code"])
        raw_tag = row.get("recommended_strategy", "")
        if is_no_trade_recommendation(raw_tag):
            no_trade_codes.add(code_str)
            continue
        key = parse_recommended_strategy_key(raw_tag)
        if key:
            rec_map[code_str] = key
    if not rec_map and not no_trade_codes:
        return signals

    grouped = {}
    for s in signals:
        grouped.setdefault((s.ts_code, s.direction), []).append(s)

    kept = []
    for (code, direction), grp in grouped.items():
        distinct_strats = {s.strategy for s in grp}
        if len(distinct_strats) >= 2:
            kept.extend(grp)
            continue

        if code in no_trade_codes:
            if direction == "SELL":
                kept.extend(grp)
            else:
                logger.info(
                    f"熔断拦截不适配标的买入: {code} ({','.join(distinct_strats)}) "
                    f"已标记为「暂不适配·观望」"
                )
            continue

        rec_key = rec_map.get(code)
        if not rec_key:
            kept.extend(grp)
            continue

        for s in grp:
            if s.strategy == rec_key:
                if "[⭐适配]" not in (s.reason or "") and "策略共振" not in (s.reason or ""):
                    s.reason = f"[⭐适配] {s.reason}"
                kept.append(s)
            else:
                logger.info(
                    f"过滤非适配策略信号: {code} {s.direction} {s.strategy} "
                    f"(该股适配策略为 {rec_key})"
                )
    return kept


def _scan_cross_sectional(
    universe: list,
    end_date: str,
    cross_strats: list,
    min_daily_amount: float = None,
    filter_suspended: bool = None,
) -> list:
    """运行横截面轮动类策略（需要同时观察全部候选标的面板进行强弱排名）。"""
    if not universe or not cross_strats:
        return []
    amt_threshold = min_daily_amount if min_daily_amount is not None else SCANNER_MIN_DAILY_AMOUNT
    flt_susp = filter_suspended if filter_suspended is not None else SCANNER_FILTER_SUSPENDED

    panel_data = {}
    for ts_code in universe:
        try:
            try:
                df = get_daily(ts_code, end_date=end_date)
            except TypeError:
                df = get_daily(ts_code)
            if df is None or df.empty:
                continue
            df = clean_daily(df)
            if df.empty:
                continue
            if end_date:
                df = df[df["trade_date"] <= end_date]
            if len(df) < SCANNER_MIN_DATA_DAYS:
                continue
            if not check_stock_liquidity(
                df, end_date=end_date, min_daily_amount=amt_threshold, filter_suspended=flt_susp
            ):
                continue
            panel_data[ts_code] = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
        except Exception as e:
            logger.debug(f"横截面数据预加载跳过 {ts_code}: {e}")

    if not panel_data:
        return []

    latest_dt = max(str(d["trade_date"].iloc[-1]) for d in panel_data.values())
    if len(panel_data) > 1:
        panel_data = {
            c: d for c, d in panel_data.items() if str(d["trade_date"].iloc[-1]) == latest_dt
        }
    if not panel_data:
        return []

    from services.signal_service import STRATEGY_CREDIBILITY_WEIGHTS
    out_signals = []
    for strat in cross_strats:
        try:
            sigs = strat.on_bar(latest_dt, panel_data, None)
            for s in sigs:
                weight = STRATEGY_CREDIBILITY_WEIGHTS.get(s.strategy, 1.0)
                s.score = round(min(max(s.score * weight, 0.10), 1.0), 2)
                out_signals.append(s)
        except Exception as e:
            logger.warning(f"横截面策略 {strat.name} 扫描异常: {e}")
    return out_signals


def _min_guard_date(prior_dates: list, sig_date: str, n_days: int) -> str:
    """按精确交易日序列计算冷却边界日期，历史不足时按日历天数回退。"""
    if n_days <= 0:
        return "99991231"
    if len(prior_dates) >= n_days:
        return prior_dates[n_days - 1]
    try:
        dt = datetime.strptime(sig_date, "%Y%m%d")
        cal_days = max(n_days * 2 + 1, 7 if n_days >= 3 else 5)
        return (dt - timedelta(days=cal_days)).strftime("%Y%m%d")
    except ValueError:
        return "00000000"


def _apply_cross_day_whipsaw_guard(
    signals: list,
    cooldown_days: int = None,
    sell_dedup_days: int = None,
) -> list:
    """跨日反手冷却与滞后重复卖出过滤（防昨日刚卖、今日反弹即高价追回）。

    规则：
      1. 防低卖高买：若同标的在过去 ``cooldown_days`` 个交易日内产生过 SELL 信号
         (参考卖出价 ``prev_price``)，则拦截当日单策略追高 BUY 信号
         （除非多策略共振 ``>= 2``，或股价相对前期卖出价深跌超 5% 形成更低吸点）。
      2. 防高位卖完再补刀：对超买/超卖短线策略（kdj_cross / bollinger_reversal），
         若过去 ``sell_dedup_days`` 个交易日内已在更高价格（``prev_price > price_ref * 1.01``）
         提示过 SELL 且今日评分未显著增强（``score <= prev_score + 0.05``），抑制重复 SELL。
    """
    if not signals:
        return signals
    if cooldown_days is None:
        cooldown_days = SCANNER_WHIPSAW_COOLDOWN_DAYS
    if sell_dedup_days is None:
        sell_dedup_days = SCANNER_SELL_DEDUP_DAYS
    cooldown_days = max(0, int(cooldown_days))
    sell_dedup_days = max(0, int(sell_dedup_days))
    if cooldown_days <= 0 and sell_dedup_days <= 0:
        return signals

    # 统计当日同股同向多策略 BUY 共振数量
    buy_strats_by_code = {}
    for s in signals:
        if s.direction == "BUY":
            buy_strats_by_code.setdefault(s.ts_code, set()).add(s.strategy)

    lookback = max(cooldown_days, sell_dedup_days)
    kept = []
    try:
        with get_conn() as conn:
            date_cache = {}
            sell_cache = {}
            for sig in signals:
                code = sig.ts_code
                sig_date = str(sig.trade_date or "").strip()
                if not sig_date:
                    kept.append(sig)
                    continue

                cache_key = (code, sig_date)
                if cache_key not in date_cache:
                    rows = conn.execute(
                        "SELECT trade_date FROM daily_price "
                        "WHERE ts_code = ? AND trade_date < ? "
                        "ORDER BY trade_date DESC LIMIT ?",
                        (code, sig_date, lookback),
                    ).fetchall()
                    prior_dates = [str(r[0]) for r in rows]

                    min_buy_dt = _min_guard_date(prior_dates, sig_date, cooldown_days)
                    min_sell_dt = _min_guard_date(prior_dates, sig_date, sell_dedup_days)
                    date_cache[cache_key] = (min_buy_dt, min_sell_dt)

                    earliest_q = min(min_buy_dt, min_sell_dt)
                    sell_rows = conn.execute(
                        "SELECT trade_date, strategy, score, price_ref FROM signal "
                        "WHERE ts_code = ? AND direction = 'SELL' "
                        "AND trade_date >= ? AND trade_date < ? "
                        "ORDER BY trade_date DESC, id DESC",
                        (code, earliest_q, sig_date),
                    ).fetchall()
                    sell_cache[cache_key] = sell_rows

                min_buy_dt, min_sell_dt = date_cache[cache_key]
                prev_sells = sell_cache[cache_key]

                if sig.direction == "BUY" and cooldown_days > 0:
                    recent_buys_block = [r for r in prev_sells if str(r[0]) >= min_buy_dt]
                    if recent_buys_block and len(buy_strats_by_code.get(code, set())) < 2:
                        prev_date, prev_strat, _prev_score, prev_price = recent_buys_block[0]
                        prev_px = float(prev_price or 0.0)
                        curr_px = float(sig.price_ref or 0.0)
                        if prev_px <= 0 or curr_px <= 0 or curr_px >= prev_px * 0.95:
                            logger.info(
                                f"反手冷却拦截低卖高买: {code} ({sig_date}) {sig.strategy} "
                                f"BUY ¥{curr_px:.2f}（{prev_date} {prev_strat} 刚SELL于 ¥{prev_px:.2f}）"
                            )
                            continue

                if (
                    sig.direction == "SELL"
                    and sell_dedup_days > 0
                    and sig.strategy in {"kdj_cross", "bollinger_reversal"}
                ):
                    recent_sells = [r for r in prev_sells if str(r[0]) >= min_sell_dt]
                    valid_prev = [r for r in recent_sells if float(r[3] or 0.0) > 0]
                    if valid_prev:
                        max_prev_px = max(float(r[3]) for r in valid_prev)
                        max_prev_score = max(float(r[2] or 0.0) for r in valid_prev)
                        curr_px = float(sig.price_ref or 0.0)
                        if curr_px > 0 and max_prev_px > curr_px * 1.01 and sig.score <= max_prev_score + 0.05:
                            logger.info(
                                f"抑制滞后重复卖出: {code} ({sig_date}) {sig.strategy} "
                                f"SELL ¥{curr_px:.2f} (score={sig.score:.2f}，"
                                f"近{sell_dedup_days}日内已于高位 ¥{max_prev_px:.2f} 提示卖出)"
                            )
                            continue

                kept.append(sig)
    except Exception as e:
        logger.debug(f"跨日防洗盘检查降级放行: {e}")
        return signals

    return kept


def scan_signals(universe: list = None, strategy_names: list = None,
                 end_date: str = "", save: bool = True,
                 progress_callback: Callable = None,
                 parallel: bool = True,
                  min_daily_amount: float = None,
                  filter_suspended: bool = None,
                  force_refresh: bool = False,
                  respect_market_regime: bool = None,
                  respect_watchlist_strategy: bool = False,
                  anti_whipsaw: bool = True) -> List[Signal]:

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
        respect_market_regime: 空头防守期是否与回测一致，硬拦截趋势追涨买入（None=读取配置）
        respect_watchlist_strategy: 自选股是否按各自适配策略过滤（多策略共振仍放行）
        anti_whipsaw: 是否启用跨日反手冷却与滞后重复卖出抑制

    Returns:
        Signal列表，按score降序排列
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if respect_market_regime is None:
        respect_market_regime = SCANNER_BEAR_BLOCK_TREND_BUY

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
    if not force_refresh and len(universe) <= 20 and not respect_watchlist_strategy:
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

    # 预过滤：只扫描有数据且数据量足够的股票，同时记录各自截至 end_date 的最新交易日
    db_latest_map = {}
    with get_conn() as conn:
        valid = set()
        cur = conn.execute(
            f"SELECT ts_code, COUNT(*) as cnt FROM daily_price "
            f"GROUP BY ts_code HAVING cnt >= {SCANNER_MIN_DATA_DAYS}"
        )
        for row in cur.fetchall():
            valid.add(row[0])
        if universe:
            for i in range(0, len(universe), 500):
                chunk = universe[i:i + 500]
                ph = ",".join("?" * len(chunk))
                for code, max_dt in conn.execute(
                    f"SELECT ts_code, MAX(trade_date) FROM daily_price "
                    f"WHERE ts_code IN ({ph}) AND trade_date <= ? GROUP BY ts_code",
                    list(chunk) + [end_date],
                ).fetchall():
                    if max_dt:
                        db_latest_map[str(code)] = str(max_dt)
    universe = [c for c in universe if c in valid]

    logger.info(f"扫描 {len(universe)} 只股票(预过滤), "
                f"策略: {strategy_names}, 日期: {end_date}")

    # 初始化策略实例（区分单股逐个扫描与横截面多标的一体化排名扫描）
    strategy_instances = []
    for name in strategy_names:
        if name in STRATEGY_REGISTRY:
            strategy_instances.append(STRATEGY_REGISTRY[name]())

    single_strats = [s for s in strategy_instances if not getattr(s, "is_cross_sectional", False)]
    cross_strats = [s for s in strategy_instances if getattr(s, "is_cross_sectional", False)]

    all_signals = []

    # 并行扫描（>= 50 只股票时启用并行）
    use_parallel = parallel and len(universe) >= 50

    if single_strats:
        if use_parallel:
            max_workers = SCANNER_PARALLEL_WORKERS or None  # None = 自动检测 CPU 核心数
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _scan_single_stock, ts_code, end_date, single_strats,
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
                    ts_code, end_date, single_strats,
                    min_daily_amount, filter_suspended,
                )
                all_signals.extend(sigs)

                if (i + 1) % 100 == 0:
                    logger.info(f"已扫描 {i + 1}/{len(universe)}")
                if progress_callback:
                    progress_callback(i + 1, len(universe))

    if cross_strats:
        cross_sigs = _scan_cross_sectional(
            universe, end_date, cross_strats,
            min_daily_amount=min_daily_amount,
            filter_suspended=filter_suspended,
        )
        all_signals.extend(cross_sigs)
        if progress_callback and not single_strats:
            progress_callback(len(universe), len(universe))

    # 1. 过滤滞后交易日过期信号（例如单只股票数据源延迟停在昨日，避免把昨日信号当作今日重推）
    sig_dates = {s.trade_date for s in all_signals if s.trade_date}
    db_dates = {db_latest_map[c] for c in universe if c in db_latest_map}
    latest_bar_date = max(sig_dates | db_dates) if (sig_dates or db_dates) else ""
    if len(universe) > 1 and latest_bar_date and all_signals:
        fresh_signals = []
        for s in all_signals:
            if s.trade_date and s.trade_date < latest_bar_date:
                logger.info(
                    f"跳过行情滞后标的的过期信号: {s.ts_code} ({s.trade_date} < {latest_bar_date})"
                )
                continue
            fresh_signals.append(s)
        all_signals = fresh_signals

    # 2. 大盘环境风控（与回测引擎对齐：allow_bear_buy=False 的趋势追涨策略在空头期硬拦截；
    #    若大盘平稳但创业板指处于空头，同步拦截高波/成长类趋势买单防二八分化陷阱）
    if all_signals:
        try:
            from services.signal_service import get_market_regime
            regime_info = get_market_regime(end_date)
            regime = regime_info.get("regime", "RANGE")
            growth_regime = "RANGE"
            if respect_market_regime and regime != "BEAR" and any(s.direction == "BUY" for s in all_signals):
                try:
                    growth_info = get_market_regime(end_date, index_code="399006.SZ")
                    growth_regime = growth_info.get("regime", "RANGE")
                except TypeError:
                    growth_regime = "RANGE"

            bear_blocked_buy_strats = {
                "ma_cross", "signal_combo", "ma_bullish", "ma60_breakout",
                "volume_price_breakout", "turtle", "donchian_breakout",
            }
            oversold_strats = {"kdj_cross", "bollinger_reversal", "momentum_rotation"}
            trend_strats = {"ma_bullish", "ma60_breakout", "ma_cross", "volume_price_breakout", "macd_cross"}
            after_regime = []
            for sig in all_signals:
                strat_cls = STRATEGY_REGISTRY.get(sig.strategy)
                allow_bear = getattr(strat_cls, "allow_bear_buy", None)
                is_trend_bear_block = (
                    (allow_bear is False)
                    or (allow_bear is None and sig.strategy in bear_blocked_buy_strats)
                )
                if sig.direction == "BUY" and regime == "BEAR":
                    if respect_market_regime and is_trend_bear_block:
                        logger.info(
                            f"大盘空头防守拦截趋势买单: {sig.ts_code} {sig.strategy} "
                            f"(score={sig.score:.2f})"
                        )
                        continue
                    if allow_bear is True or sig.strategy in oversold_strats:
                        sig.score = round(max(sig.score - 0.05, 0.10), 2)
                    elif not respect_market_regime and is_trend_bear_block:
                        sig.score = round(max(sig.score - 0.08, 0.10), 2)
                elif sig.direction == "BUY" and respect_market_regime and is_trend_bear_block and growth_regime == "BEAR":
                    is_growth_sym = sig.ts_code.startswith(("300", "301", "688"))
                    if not is_growth_sym:
                        try:
                            from services.valuation_service import _estimate_causal_atr_and_close
                            atr_pct, _ = _estimate_causal_atr_and_close(sig.ts_code, get_daily)
                            is_growth_sym = atr_pct >= 2.2
                        except Exception:
                            is_growth_sym = False
                    if is_growth_sym:
                        logger.info(
                            f"创业板空头分化拦截成长/高波趋势买单: {sig.ts_code} {sig.strategy} "
                            f"(score={sig.score:.2f})"
                        )
                        continue
                elif sig.direction == "BUY" and regime == "BULL" and sig.strategy in trend_strats:
                    sig.score = round(min(sig.score + 0.03, 0.99), 2)
                after_regime.append(sig)
            all_signals = after_regime
        except Exception as e:
            logger.debug(f"大盘环境调权跳过: {e}")

    # 3. 自选股个股适配策略过滤（含「⛔ 暂不适配·观望」熔断）
    if respect_watchlist_strategy and all_signals:
        all_signals = _filter_by_watchlist_strategy(all_signals)

    # 4. 跨日反手冷却 & 滞后重复卖出抑制
    if anti_whipsaw and all_signals:
        all_signals = _apply_cross_day_whipsaw_guard(all_signals)

    # 5. 轻量估值与财报窗口守卫（仅当本地 stock_valuation 表已有缓存数据时激活）
    if all_signals and any(s.direction == "BUY" for s in all_signals):
        try:
            from data.storage import get_valuation_map
            from services.valuation_service import evaluate_valuation_guard
            buy_codes = list({s.ts_code for s in all_signals if s.direction == "BUY"})
            val_map = get_valuation_map(buy_codes)
            if val_map:
                for sig in all_signals:
                    if sig.direction != "BUY" or sig.ts_code not in val_map:
                        continue
                    vg = evaluate_valuation_guard(
                        sig.ts_code, val_map[sig.ts_code], ref_date=sig.trade_date
                    )
                    if vg["score_adj"] != 0.0:
                        sig.score = round(min(max(sig.score + vg["score_adj"], 0.10), 0.99), 2)
                    badge = vg.get("badge", "")
                    if not vg["allow_left_buy"] and badge:
                        sig.reason = f"[{badge}警示] {sig.reason}"
                    elif "💎" in badge:
                        sig.reason = f"[💎低估高息] {sig.reason}"
                    elif vg.get("earnings_warning"):
                        sig.reason = f"[📅财报窗口] {sig.reason}"
        except Exception as e:
            logger.debug(f"估值守卫检查跳过: {e}")

    # 按 score 降序排列
    all_signals.sort(key=lambda s: s.score, reverse=True)

    # 批量保存：仅按实际保留信号的 (trade_date, ts_code) 与最新交易日当日在库标的精准对口清理，
    # 绝不拿滞后股票的旧日期去误删其余股票昨天的历史信号。
    if save:
        try:
            clean_pairs = {(s.trade_date, s.ts_code) for s in all_signals if s.trade_date and s.ts_code}
            if latest_bar_date:
                for code in universe:
                    if db_latest_map.get(code) == latest_bar_date:
                        clean_pairs.add((latest_bar_date, code))
            if clean_pairs and strategy_names:
                with get_conn() as conn:
                    s_ph = ",".join("?" * len(strategy_names))
                    for b_dt, code in sorted(clean_pairs):
                        conn.execute(
                            f"DELETE FROM signal WHERE trade_date = ? AND ts_code = ? AND strategy IN ({s_ph})",
                            [b_dt, code] + list(strategy_names),
                        )
        except Exception as e:
            logger.debug(f"清理同日旧信号跳过: {e}")
        if all_signals:
            save_signals_batch(all_signals)
            logger.info(f"已批量保存 {len(all_signals)} 条信号")

    logger.info(f"扫描完成，共 {len(all_signals)} 条信号")
    return all_signals
