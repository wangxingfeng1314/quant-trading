"""回测引擎主循环"""
import json
import logging
from typing import List, Type, Callable
import pandas as pd
import itertools
import concurrent.futures

from engine.portfolio import Portfolio
from data.storage import get_daily, save_backtest_result, save_backtest_trades
from data.indicators import apply_indicators
from data.cleaner import clean_daily
from core.models import BacktestResult
from core.config import BACKTEST_MIN_POSITION_PCT, BACKTEST_POSITION_STEP

logger = logging.getLogger(__name__)


class Backtester:
    """回测引擎

    用法:
        bt = Backtester(strategy_cls, params, universe, start_date, end_date, capital)
        result = bt.run()
    """

    def __init__(self, strategy_cls, params: dict,
                 universe: list, start_date: str, end_date: str,
                 initial_capital: float = 100000,
                 execution_mode: str = "current_close",
                 risk_manager = None,
                 preloaded_data: dict = None,
                 max_active_positions: int = 0,
                 position_sizing: str = "score",
                 risk_per_trade: float = 0.015):
        """
        Args:
            strategy_cls: 策略类（不是实例）
            params: 策略参数字典
            universe: 股票代码列表 ['000001.SZ', '600519.SH', ...]
            start_date: 回测开始日期 'YYYYMMDD'
            end_date: 回测结束日期 'YYYYMMDD'
            initial_capital: 初始资金
            execution_mode: 撮合模式，"current_close"（当日收盘价撮合）或 "next_open"（次日开盘价撮合，更贴近真实实盘）
            risk_manager: 可选的 RiskManager 风控拦截器实例
            preloaded_data: 预先加载并计算好指标的数据字典 {ts_code: DataFrame}，用于网格搜索加速
            max_active_positions: 最大并发持仓股票只数，0 表示不限制
            position_sizing: 仓位分配模式，"score"（按信号评分阶梯分配）或 "atr_risk"（按ATR波动率风险平价定仓）
            risk_per_trade: ATR风险平价模式下单笔交易最大风险占总净值比例（默认 1.5%）
        """
        self.strategy_cls = strategy_cls
        self.params = params
        self.universe = universe
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.execution_mode = execution_mode
        self.risk_manager = risk_manager
        self.preloaded_data = preloaded_data
        self.max_active_positions = max_active_positions
        self.position_sizing = position_sizing
        self.risk_per_trade = risk_per_trade

    def run(self, save: bool = True) -> BacktestResult:
        """执行回测

        Returns:
            BacktestResult对象
        """
        import numpy as np

        # 1. 初始化 ST 标的缓存 (解决 is_st 判定失效问题)
        from data.storage import get_instrument_list
        instruments = get_instrument_list()
        self.st_lookup = {}
        if not instruments.empty:
            for _, row in instruments.iterrows():
                code = row["ts_code"]
                is_st = False
                if "is_st" in row and row["is_st"] == 1:
                    is_st = True
                elif "name" in row and isinstance(row["name"], str) and "ST" in row["name"].upper():
                    is_st = True
                self.st_lookup[code] = is_st

        # 2. 加载并预处理所有股票数据（若已传入 preloaded_data 则免重复 I/O）
        if self.preloaded_data is not None and self.preloaded_data:
            stock_data = {
                ts_code: self.preloaded_data[ts_code]
                for ts_code in self.universe
                if ts_code in self.preloaded_data and not self.preloaded_data[ts_code].empty
            }
        else:
            logger.info(f"加载数据: {len(self.universe)} 只股票, "
                         f"{self.start_date} ~ {self.end_date}")
            stock_data = {}  # {ts_code: DataFrame}
            # 计算 warm-up 起始日期（提前180天加载历史K线，确保MA60/MACD/KDJ等长周期指标在回测首日就已就绪）
            warmup_start = ""
            if self.start_date:
                try:
                    from datetime import datetime, timedelta
                    dt = datetime.strptime(self.start_date, "%Y%m%d")
                    warmup_start = (dt - timedelta(days=180)).strftime("%Y%m%d")
                except Exception:
                    warmup_start = self.start_date

            for ts_code in self.universe:
                df = get_daily(ts_code, warmup_start, self.end_date)
                if df.empty:
                    continue
                df = clean_daily(df)
                if df.empty:
                    continue
                df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
                stock_data[ts_code] = df

        if not stock_data:
            logger.error("没有可用数据")
            return BacktestResult(
                strategy=self.strategy_cls.name, params=json.dumps(self.params),
                start_date=self.start_date, end_date=self.end_date,
                initial_capital=self.initial_capital, final_capital=self.initial_capital,
            )

        logger.info(f"有效股票: {len(stock_data)} 只")

        # 2. 构建所有交易日序列
        all_dates = set()
        for df in stock_data.values():
            all_dates.update(df["trade_date"].tolist())
        trade_dates = sorted(all_dates)

        # 只取 start_date ~ end_date 之间的
        trade_dates = [d for d in trade_dates
                       if self.start_date <= d <= self.end_date]
        logger.info(f"交易日数: {len(trade_dates)}")

        # 3. 初始化策略和组合
        strategy = self.strategy_cls(**self.params)
        portfolio = Portfolio(self.initial_capital)

        # 预先按日期索引数据（加速查找）
        date_index = {}  # {ts_code: {trade_date: row_index}}
        for ts_code, df in stock_data.items():
            date_index[ts_code] = {
                row["trade_date"]: i
                for i, row in df.iterrows()
            }

        pending_signals = []

        # 4. 逐日迭代
        for date in trade_dates:
            # 每日开盘前触发持仓日结（T+1结转）
            portfolio.on_new_day(date)

            # 收集当日价格
            prices = {}
            data_slice = {}  # 传给策略的数据切片

            for ts_code, df in stock_data.items():
                if date in date_index[ts_code]:
                    idx = date_index[ts_code][date]
                    prices[ts_code] = df.iloc[idx]["close"]
                    # 零拷贝优化：传视图而非 copy()，策略内部按需读取
                    data_slice[ts_code] = df.iloc[:idx + 1]

            if not prices:
                continue

            # next_open 模式：次日开盘时撮合前一交易日生成的 pending_signals
            deferred_sells = []
            if self.execution_mode == "next_open" and pending_signals:
                # 横截面排序：卖出优先，买入按 score 降序
                pending_signals.sort(key=lambda s: (0 if s.direction == "SELL" else 1, -s.score))
                for sig in pending_signals:
                    ts_code = sig.ts_code
                    if ts_code not in stock_data or date not in date_index[ts_code]:
                        if sig.direction == "SELL":
                            deferred_sells.append(sig)
                        continue
                    curr_idx = date_index[ts_code][date]
                    df_stock = stock_data[ts_code]
                    open_price = float(df_stock.iloc[curr_idx]["open"])
                    prev_close = float(df_stock.iloc[curr_idx - 1]["close"]) if curr_idx > 0 else 0.0
                    is_st = self.st_lookup.get(ts_code, False)
                    is_cy = ts_code.startswith(("300", "301", "688"))
                    is_bj = ts_code.startswith(("43", "83", "87", "92")) or ts_code.endswith(".BJ")
                    snapshot = self._build_snapshot(sig, df_stock, curr_idx)

                    if sig.direction == "BUY":
                        # 最大持仓只数控制：若非已有持仓且已达上限，则跳过
                        curr_pos = portfolio.get_position(ts_code)
                        if self.max_active_positions > 0 and (curr_pos is None or curr_pos.is_empty):
                            if portfolio.active_position_count >= self.max_active_positions:
                                continue

                        if open_price <= 0:
                            continue
                        budget = self._calc_buy_budget(portfolio, sig, df_stock=df_stock, curr_idx=curr_idx)
                        volume = int(budget / open_price) // 100 * 100
                        if volume > 0:
                            portfolio.buy(
                                ts_code=ts_code,
                                price=open_price,
                                volume=volume,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                            )
                    elif sig.direction == "SELL":
                        pos = portfolio.get_position(ts_code)
                        if pos and not pos.is_empty:
                            sell_vol = pos.available_shares
                            sold_trade = portfolio.sell(
                                ts_code=ts_code,
                                price=open_price,
                                volume=sell_vol,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                            )
                            # 若因一字跌停封死导致未能卖出，顺延至下一交易日继续挂单卖出
                            if sold_trade is None and sell_vol > 0:
                                deferred_sells.append(sig)
                pending_signals = []

            # 策略产生信号
            signals = strategy.on_bar(date, data_slice, portfolio)

            # 风控拦截器检查（止损/跟踪止盈/到期强制平仓）
            if self.risk_manager is not None:
                risk_signals = self.risk_manager.check_risks(date, portfolio, prices)
                if risk_signals:
                    risk_sell_codes = {s.ts_code for s in risk_signals}
                    filtered_signals = [s for s in signals if s.ts_code not in risk_sell_codes]
                    signals = risk_signals + filtered_signals

            # 合并前日跌停未成交顺延的卖出单
            if deferred_sells:
                existing_sell_codes = {s.ts_code for s in signals if s.direction == "SELL"}
                for ds in deferred_sells:
                    if ds.ts_code not in existing_sell_codes:
                        signals = [ds] + [s for s in signals if s.ts_code != ds.ts_code]
                        existing_sell_codes.add(ds.ts_code)

            # 横截面优先级排序：卖出优先（释放资金），买入按 score 降序排列
            signals.sort(key=lambda s: (0 if s.direction == "SELL" else 1, -s.score))

            if self.execution_mode == "next_open":
                pending_signals = signals
            else:
                # current_close 模式：当日收盘价撮合
                for sig in signals:
                    ts_code = sig.ts_code
                    df_stock = stock_data.get(ts_code)
                    curr_idx = date_index[ts_code][date] if (df_stock is not None and date in date_index.get(ts_code, {})) else 0
                    prev_close = float(df_stock.iloc[curr_idx - 1]["close"]) if (df_stock is not None and curr_idx > 0) else 0.0
                    is_st = self.st_lookup.get(ts_code, False)
                    is_cy = ts_code.startswith(("300", "301", "688"))
                    is_bj = ts_code.startswith(("43", "83", "87", "92")) or ts_code.endswith(".BJ")
                    snapshot = self._build_snapshot(sig, df_stock, curr_idx)

                    if sig.direction == "BUY":
                        # 最大持仓只数控制
                        curr_pos = portfolio.get_position(ts_code)
                        if self.max_active_positions > 0 and (curr_pos is None or curr_pos.is_empty):
                            if portfolio.active_position_count >= self.max_active_positions:
                                continue

                        if sig.price_ref <= 0:
                            continue
                        budget = self._calc_buy_budget(portfolio, sig, df_stock=df_stock, curr_idx=curr_idx)
                        volume = int(budget / sig.price_ref) // 100 * 100
                        if volume > 0:
                            portfolio.buy(
                                ts_code=sig.ts_code,
                                price=sig.price_ref,
                                volume=volume,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                            )
                    elif sig.direction == "SELL":
                        pos = portfolio.get_position(sig.ts_code)
                        if pos and not pos.is_empty:
                            sell_vol = pos.available_shares
                            portfolio.sell(
                                ts_code=sig.ts_code,
                                price=sig.price_ref,
                                volume=sell_vol,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                            )

            # 记录权益
            portfolio.record_equity(date, prices)

        # 5. 计算指标
        metrics = portfolio.calc_metrics()

        result = BacktestResult(
            strategy=strategy.name,
            params=json.dumps(self.params, ensure_ascii=False),
            start_date=self.start_date,
            end_date=self.end_date,
            initial_capital=self.initial_capital,
            final_capital=metrics.get("final_capital", self.initial_capital),
            total_return=metrics.get("total_return", 0),
            annual_return=metrics.get("annual_return", 0),
            max_drawdown=metrics.get("max_drawdown", 0),
            sharpe_ratio=metrics.get("sharpe_ratio", 0),
            calmar_ratio=metrics.get("calmar_ratio", 0),
            win_rate=metrics.get("win_rate", 0),
            trade_count=metrics.get("trade_count", 0),
            sell_count=metrics.get("sell_count", 0),
            equity_curve=portfolio.equity_curve,
            trades=portfolio.trades,
        )

        # 6. 保存到数据库
        if save and portfolio.trades:
            bt_id = save_backtest_result(result)
            save_backtest_trades(bt_id, portfolio.trades)
            logger.info(f"回测结果已保存, ID={bt_id}")

        logger.info(
            f"回测完成: {strategy.name} | "
            f"总收益 {result.total_return:.2f}% | "
            f"年化 {result.annual_return:.2f}% | "
            f"最大回撤 {result.max_drawdown:.2f}% | "
            f"夏普 {result.sharpe_ratio:.2f} | "
            f"交易 {result.trade_count} 次"
        )

        return result

    def _build_snapshot(self, sig, df_stock, curr_idx) -> dict:
        """构建交易决策与风控执行的上下文快照"""
        snap = {
            "score": getattr(sig, "score", 0.0),
            "reason": getattr(sig, "reason", ""),
            "execution_mode": self.execution_mode,
        }
        if hasattr(sig, "context_snapshot") and sig.context_snapshot:
            snap.update(sig.context_snapshot)
        if df_stock is not None and curr_idx is not None and curr_idx < len(df_stock):
            row = df_stock.iloc[curr_idx]
            for col in ["close", "open", "high", "low", "ma5", "ma20", "ma60", "rsi14", "dif", "dea", "kdj_k", "atr14"]:
                if col in row and pd.notna(row[col]):
                    snap[col] = round(float(row[col]), 2)
        return snap

    def _calc_buy_budget(self, portfolio, sig, df_stock=None, curr_idx=None) -> float:
        """根据组合总资产与持仓限制计算单笔买入预算（支持评分阶梯定仓与 ATR 波动率风险平价定仓）"""
        nav = portfolio.total_value
        max_pct = (1.0 / self.max_active_positions) if self.max_active_positions > 0 else 1.0

        if self.position_sizing == "atr_risk" and df_stock is not None and curr_idx is not None and curr_idx < len(df_stock):
            row = df_stock.iloc[curr_idx]
            atr_val = row.get("atr14")
            ref_price = float(row.get("close", sig.price_ref or 0.0))
            if pd.notna(atr_val) and float(atr_val) > 0 and ref_price > 0:
                # 单股止损距离设为 2 * ATR14（保底 2% 价格波动，防止极低波动股仓位无限膨胀）
                stop_dist_pct = max(2.0 * float(atr_val) / ref_price, 0.02)
                # 风险平价仓位比例 = 单笔最大风险比例 / 止损距离比例，同时结合信号置信度微调
                score_factor = 0.8 + 0.4 * max(0.0, min(float(getattr(sig, "score", 0.7)), 1.0))
                atr_pct = (self.risk_per_trade / stop_dist_pct) * score_factor
                cap_pct = min(max_pct, 0.40) if self.max_active_positions != 1 else 1.0
                target_pct = max(0.05, min(atr_pct, cap_pct))
                ideal_budget = nav * target_pct
                return max(0.0, min(portfolio.cash, ideal_budget))

        if self.max_active_positions > 0:
            target_pct = min(max_pct, 1.0)
        else:
            target_pct = min(BACKTEST_MIN_POSITION_PCT + sig.score * BACKTEST_POSITION_STEP, 1.0)
        # 以组合当前总净值 (NAV) 定额，并受限于可用现金
        ideal_budget = nav * target_pct
        return max(0.0, min(portfolio.cash, ideal_budget))


def preload_backtest_data(universe: list, start_date: str, end_date: str) -> dict:
    """预加载行情并预计算常用指标，用于多组回测与网格搜索共享内存（消除重复 I/O）"""
    data = {}
    warmup_start = ""
    if start_date:
        try:
            from datetime import datetime, timedelta
            dt = datetime.strptime(start_date, "%Y%m%d")
            warmup_start = (dt - timedelta(days=180)).strftime("%Y%m%d")
        except Exception:
            warmup_start = start_date

    for ts_code in universe:
        df = get_daily(ts_code, warmup_start, end_date)
        if df.empty:
            continue
        df = clean_daily(df)
        if df.empty:
            continue
        df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
        data[ts_code] = df
    return data


def _execute_single_grid_combo(args):
    """顶层并行网格搜索执行函数（支持 Windows 跨进程序列化）"""
    if len(args) == 7:
        strategy_cls, params, universe, start_date, end_date, initial_capital, preloaded_data = args
        execution_mode, risk_manager = "next_open", None
    else:
        strategy_cls, params, universe, start_date, end_date, initial_capital, preloaded_data, execution_mode, risk_manager = args
    bt = Backtester(
        strategy_cls=strategy_cls,
        params=params,
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        execution_mode=execution_mode,
        risk_manager=risk_manager,
        preloaded_data=preloaded_data,
    )
    return bt.run(save=False)


def grid_search(strategy_cls, universe: list, start_date: str, end_date: str,
                initial_capital: float = 100000, param_grid: dict = None,
                metric: str = "total_return", progress_callback: Callable = None,
                execution_mode: str = "next_open", risk_manager = None) -> list:
    """参数网格搜索（带数据预热加速，默认对齐 next_open 实盘撮合防过拟合）

    遍历所有参数组合，运行回测并返回按指定指标排序的结果。
    """
    if param_grid is None:
        param_grid = {}

    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    if total == 0:
        return []

    # Tier 1 优化：在网格循环前单次预热加载数据与指标计算
    preloaded = preload_backtest_data(universe, start_date, end_date)

    results = []
    for i, combo in enumerate(combinations):
        params = dict(zip(param_names, combo))

        # 若传入了 risk_manager，为每轮回测克隆独立状态实例，防止 highest_prices 跨轮次污染
        rm_copy = None
        if risk_manager is not None:
            from engine.risk_manager import RiskManager
            rm_copy = RiskManager(
                stop_loss_pct=risk_manager.stop_loss_pct,
                trailing_stop_activation=risk_manager.trailing_stop_activation,
                trailing_stop_callback=risk_manager.trailing_stop_callback,
                max_holding_days=risk_manager.max_holding_days,
            )

        bt = Backtester(
            strategy_cls=strategy_cls,
            params=params,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            execution_mode=execution_mode,
            risk_manager=rm_copy,
            preloaded_data=preloaded,
        )
        result = bt.run(save=False)

        metric_value = getattr(result, metric, 0)
        if metric == "max_drawdown":
            metric_value = -metric_value

        results.append({
            "params": params,
            "result": result,
            "metric_value": metric_value,
        })

        if progress_callback:
            progress_callback(i + 1, total)

    results.sort(key=lambda r: r["metric_value"], reverse=True)
    return results


def grid_search_parallel(strategy_cls, universe: list, start_date: str, end_date: str,
                          initial_capital: float = 100000, param_grid: dict = None,
                          metric: str = "total_return", max_workers: int = None,
                          progress_callback: Callable = None,
                          execution_mode: str = "next_open", risk_manager = None) -> list:
    """并行参数网格搜索（基于 ProcessPoolExecutor + 数据预热加速）

    适用于参数组合较多（>10）的场景。利用多核 CPU 并行执行，并共享预加载数据。
    """
    if param_grid is None:
        param_grid = {}

    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    if total == 0:
        return []

    # Tier 1 优化：主进程一次性预处理，子任务零重复 I/O
    preloaded = preload_backtest_data(universe, start_date, end_date)

    tasks = []
    for combo in combinations:
        params = dict(zip(param_names, combo))
        args = (strategy_cls, params, universe, start_date, end_date, initial_capital, preloaded, execution_mode, risk_manager)
        tasks.append((combo, args))

    results = []
    completed = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_execute_single_grid_combo, args): combo
                   for combo, args in tasks}

        for future in concurrent.futures.as_completed(futures):
            combo = futures[future]
            try:
                result = future.result()
                params = dict(zip(param_names, combo))
                metric_value = getattr(result, metric, 0)
                if metric == "max_drawdown":
                    metric_value = -metric_value

                results.append({
                    "params": params,
                    "result": result,
                    "metric_value": metric_value,
                })
            except Exception as e:
                logger.error(f"并行回测异常 ({combo}): {e}")

            completed += 1
            if progress_callback:
                progress_callback(completed, total)

    results.sort(key=lambda r: r["metric_value"], reverse=True)
    return results
