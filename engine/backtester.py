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

logger = logging.getLogger(__name__)


class Backtester:
    """回测引擎

    用法:
        bt = Backtester(strategy_cls, params, universe, start_date, end_date, capital)
        result = bt.run()
    """

    def __init__(self, strategy_cls, params: dict,
                 universe: list, start_date: str, end_date: str,
                 initial_capital: float = 100000):
        """
        Args:
            strategy_cls: 策略类（不是实例）
            params: 策略参数字典
            universe: 股票代码列表 ['000001.SZ', '600519.SH', ...]
            start_date: 回测开始日期 'YYYYMMDD'
            end_date: 回测结束日期 'YYYYMMDD'
            initial_capital: 初始资金
        """
        self.strategy_cls = strategy_cls
        self.params = params
        self.universe = universe
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital

    def run(self, save: bool = True) -> BacktestResult:
        """执行回测

        Returns:
            BacktestResult对象
        """
        import numpy as np

        # 1. 加载并预处理所有股票数据
        logger.info(f"加载数据: {len(self.universe)} 只股票, "
                     f"{self.start_date} ~ {self.end_date}")

        stock_data = {}  # {ts_code: DataFrame}
        for ts_code in self.universe:
            df = get_daily(ts_code, self.start_date, self.end_date)
            if df.empty:
                continue
            df = clean_daily(df)
            if df.empty:
                continue
            df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma"])
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

        # 4. 逐日迭代
        for date in trade_dates:
            # 收集当日价格
            prices = {}
            data_slice = {}  # 传给策略的数据切片

            for ts_code, df in stock_data.items():
                if date in date_index[ts_code]:
                    idx = date_index[ts_code][date]
                    prices[ts_code] = df.loc[idx, "close"]
                    # 数据切片：从开始到当日（含），防止未来数据泄露
                    data_slice[ts_code] = df.loc[:idx].copy()

            if not prices:
                continue

            # 记录权益
            portfolio.record_equity(date, prices)

            # 策略产生信号
            signals = strategy.on_bar(date, data_slice, portfolio)

            # 执行信号 - 按评分分配仓位
            for sig in signals:
                if sig.direction == "BUY":
                    # 评分决定仓位比例：score=0.5 → 5%, score=1.0 → 20%
                    position_pct = 0.05 + sig.score * 0.15  # 5%~20%
                    budget = portfolio.cash * position_pct
                    volume = int(budget / max(sig.price_ref, 1)) // 100 * 100
                    if volume > 0:
                        portfolio.buy(
                            ts_code=sig.ts_code,
                            price=sig.price_ref,
                            volume=volume,
                            trade_date=date,
                        )
                elif sig.direction == "SELL":
                    pos = portfolio.get_position(sig.ts_code)
                    if pos and not pos.is_empty:
                        portfolio.sell(
                            ts_code=sig.ts_code,
                            price=sig.price_ref,
                            volume=pos.shares,
                            trade_date=date,
                        )

        # 5. 最终权益记录
        if trade_dates:
            last_prices = {}
            for ts_code, df in stock_data.items():
                last_row = df.iloc[-1]
                if last_row["trade_date"] <= self.end_date:
                    last_prices[ts_code] = last_row["close"]
            portfolio.record_equity(trade_dates[-1], last_prices)

        # 6. 计算指标
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
            equity_curve=portfolio.equity_curve,
            trades=portfolio.trades,
        )

        # 7. 保存到数据库
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


def grid_search(strategy_cls, universe: list, start_date: str, end_date: str,
                initial_capital: float = 100000, param_grid: dict = None,
                metric: str = "total_return", progress_callback: Callable = None) -> list:
    """参数网格搜索

    遍历所有参数组合，运行回测并返回按指定指标排序的结果。

    Args:
        strategy_cls: 策略类
        universe: 股票列表
        start_date: 开始日期
        end_date: 结束日期
        initial_capital: 初始资金
        param_grid: {参数名: [取值列表], ...}
        metric: 排序指标，如 'total_return', 'sharpe_ratio', 'max_drawdown'
        progress_callback: 进度回调函数(completed, total)

    Returns:
        [{'params': {...}, 'result': BacktestResult, 'metric_value': float}, ...]
    """
    if param_grid is None:
        param_grid = {}

    # 生成所有参数组合
    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    if total == 0:
        return []

    results = []
    for i, combo in enumerate(combinations):
        params = dict(zip(param_names, combo))

        bt = Backtester(
            strategy_cls=strategy_cls,
            params=params,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        )
        result = bt.run(save=False)

        # 提取排序指标值
        metric_value = getattr(result, metric, 0)
        if metric == "max_drawdown":
            metric_value = -metric_value  # 回撤越小越好（负数转正数排序）

        results.append({
            "params": params,
            "result": result,
            "metric_value": metric_value,
        })

        if progress_callback:
            progress_callback(i + 1, total)

    # 按指标降序排列
    results.sort(key=lambda r: r["metric_value"], reverse=True)
    return results


def grid_search_parallel(strategy_cls, universe: list, start_date: str, end_date: str,
                          initial_capital: float = 100000, param_grid: dict = None,
                          metric: str = "total_return", max_workers: int = None,
                          progress_callback: Callable = None) -> list:
    """并行参数网格搜索（基于 ProcessPoolExecutor）

    与 grid_search() 功能相同，但利用多核 CPU 并行执行回测。
    适用于参数组合较多（>10）的场景。

    Args:
        同 grid_search()
        max_workers: 并行进程数（默认 = CPU 核心数）

    Returns:
        同 grid_search()
    """
    if param_grid is None:
        param_grid = {}

    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    if total == 0:
        return []

    # 对单个参数组合执行一次回测（作为并行任务单元）
    def _run_single(combo):
        params = dict(zip(param_names, combo))
        bt = Backtester(
            strategy_cls=strategy_cls,
            params=params,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
        )
        return bt.run(save=False)

    results = []
    completed = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_single, combo): combo
                   for combo in combinations}

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
