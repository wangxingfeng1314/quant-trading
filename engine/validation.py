"""样本外验证 —— 判断策略是「真有优势」还是「参数拟合了历史噪声」

## 为什么需要这个模块

网格搜索给出的最优参数，是在**同一段历史**上挑出来的最优解。那组参数在这段
历史上的收益、胜率、回撤，本质上都是**样本内**成绩：任何足够多的参数组合里，
总会有一组恰好贴合这段行情的噪声。把它当成"预期表现"展示，期望值系统性偏高。

本模块提供三道检验：

1. **滚动样本外（walk-forward）** —— 把历史切成若干「训练段 + 紧随其后的测试段」，
   在训练段挑参数、在**从未参与选参**的测试段验证，把各测试段的权益首尾拼接成
   一条完整的样本外曲线。这条曲线上的收益/回撤/夏普，才是可以拿去和外行解释的数字。

2. **参数稳定性** —— 最优参数是不是"孤峰"。如果±1 档的邻居参数立刻亏损，
   说明最优点只是噪声里的巧合，实盘换个市场状态就失效。

3. **交易序列蒙特卡洛** —— 把已实现盈亏的顺序随机重排数千次，看最大回撤的分布。
   用来回答"我这次回撤 15%，是策略本身的风险，还是只是运气差"。

三者合起来给出一个 verdict：策略是稳健、可疑，还是过拟合。

## 用法

    from engine.validation import walk_forward
    report = walk_forward(
        strategy_cls, param_grid={"fast_period": [3, 5, 8], "slow_period": [15, 20, 30]},
        universe=codes, start_date="20200101", end_date="20241231",
        n_splits=4, train_ratio=0.7,
    )
    print(report.summary())
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from core.config import (
    BACKTEST_WALK_FORWARD_MIN_TRAIN_DAYS,
    BACKTEST_WALK_FORWARD_MIN_TEST_DAYS,
    BACKTEST_MONTE_CARLO_RUNS,
)
from engine.backtester import (
    Backtester, grid_search, preload_backtest_data, get_benchmark_returns,
    _options_kwargs, build_backtest_options,
)
from engine.portfolio import Portfolio

logger = logging.getLogger(__name__)

__all__ = [
    "WalkForwardWindow", "WalkForwardReport", "walk_forward",
    "parameter_stability", "monte_carlo_trades",
]


# ============================================================
# 数据结构
# ============================================================

@dataclass
class WalkForwardWindow:
    """单个「训练段 → 测试段」折的结果"""
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_params: dict
    train_metric: float
    test_metric: float
    test_return: float
    test_max_drawdown: float
    test_trades: int
    test_result: object = None

    def as_dict(self) -> dict:
        return {
            "折": self.index + 1,
            "训练区间": f"{self.train_start}~{self.train_end}",
            "测试区间": f"{self.test_start}~{self.test_end}",
            "最优参数": _fmt_params(self.best_params),
            "训练段指标": round(self.train_metric, 2),
            "测试段指标": round(self.test_metric, 2),
            "测试段收益%": round(self.test_return, 2),
            "测试段回撤%": round(self.test_max_drawdown, 2),
            "测试段交易": self.test_trades,
        }


@dataclass
class WalkForwardReport:
    """样本外验证总报告"""
    metric: str
    windows: list = field(default_factory=list)
    # 拼接后的样本外曲线与指标
    oos_equity: list = field(default_factory=list)      # [{date, equity}]
    oos_metrics: dict = field(default_factory=dict)
    oos_trades: list = field(default_factory=list)
    # 样本内外对比
    is_mean_metric: float = 0.0
    oos_mean_metric: float = 0.0
    efficiency: float = 0.0          # 样本外 / 样本内
    param_stability: dict = field(default_factory=dict)
    monte_carlo: dict = field(default_factory=dict)
    verdict: str = ""
    warnings: list = field(default_factory=list)

    @property
    def oos_total_return(self) -> float:
        return self.oos_metrics.get("total_return", 0.0)

    @property
    def oos_max_drawdown(self) -> float:
        return self.oos_metrics.get("max_drawdown", 0.0)

    @property
    def oos_sharpe(self) -> float:
        return self.oos_metrics.get("sharpe_ratio", 0.0)

    @property
    def oos_annual_return(self) -> float:
        return self.oos_metrics.get("annual_return", 0.0)

    def as_dict(self) -> dict:
        return {
            "验证指标": self.metric,
            "折数": len(self.windows),
            "样本外总收益%": round(self.oos_total_return, 2),
            "样本外年化%": round(self.oos_annual_return, 2),
            "样本外最大回撤%": round(self.oos_max_drawdown, 2),
            "样本外夏普": round(float(self.oos_sharpe), 2),
            "样本内均值": round(float(self.is_mean_metric), 2),
            "样本外均值": round(float(self.oos_mean_metric), 2),
            "样本外效率": round(float(self.efficiency), 2),
            "结论": self.verdict,
        }

    def summary(self) -> str:
        """面向人的一段话摘要（可直接贴到报告里）"""
        if self.is_mean_metric > 1e-9:
            eff = f"效率 {self.efficiency:.0%}"
        else:
            eff = "效率不适用（样本内非正）"
        lines = [
            f"样本外验证（{self.metric}，{len(self.windows)} 折滚动）",
            f"  样本内均值 {self.is_mean_metric:.2f} → 样本外均值 {self.oos_mean_metric:.2f}（{eff}）",
            f"  拼接样本外曲线：总收益 {self.oos_total_return:.2f}% | "
            f"年化 {self.oos_annual_return:.2f}% | "
            f"最大回撤 {self.oos_max_drawdown:.2f}% | 夏普 {self.oos_sharpe:.2f}",
        ]
        if self.param_stability:
            rob = self.param_stability.get("robustness", 0)
            lines.append(
                f"  参数稳定性：邻居/最优 = {rob:.2f}"
                f"（{'稳健' if rob >= 0.7 else '脆弱'}）"
            )
        if self.monte_carlo and self.monte_carlo.get("runs"):
            mc = self.monte_carlo
            lines.append(
                f"  路径风险（重排 {mc['runs']} 次）：实际路径回撤 "
                f"{mc.get('observed_realized_drawdown', 0):.2f}% | "
                f"中位 {mc.get('drawdown_p50', 0):.2f}% | "
                f"95% 分位 {mc.get('drawdown_p95', 0):.2f}% | "
                f"最差 {mc.get('drawdown_max', 0):.2f}%"
            )
            lines.append(
                f"  收益不确定性（自助重采样）：已实现盈亏合计 {mc.get('realized_pnl_total', 0):,.0f} 元 | "
                f"收益 90% 区间 {mc.get('final_return_p5', 0):.2f}% ~ {mc.get('final_return_p95', 0):.2f}% | "
                f"亏损概率 {mc.get('loss_probability', 0):.1f}%"
            )
            lines.append(
                f"  组合盯市回撤 {mc.get('observed_drawdown', 0):.2f}% | "
                f"重排中有 {mc.get('worse_than_observed_pct', 0):.0f}% 的路径比实际更深"
            )
        lines.append(f"  结论：{self.verdict}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


def _fmt_params(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _metric_of(result, metric: str) -> float:
    """按与 grid_search 一致的符号口径取指标值

    grid_search 对 max_drawdown 会取负号（越大越好），样本外对比必须用同一口径，
    否则「样本内 vs 样本外」会变成一正一负，效率计算完全失真。
    """
    value = float(getattr(result, metric, 0) or 0)
    return -value if metric == "max_drawdown" else value


# ============================================================
# 滚动样本外验证
# ============================================================

def _collect_trade_dates(preloaded: dict, start_date: str, end_date: str) -> list:
    """取区间内所有出现过的交易日（与 Backtester 的 trade_dates 口径一致）"""
    dates = set()
    for df in preloaded.values():
        dates.update(df["trade_date"].tolist())
    return sorted(d for d in dates if start_date <= d <= end_date)


def _slice_window(dates: list, start: str, end: str) -> list:
    return [d for d in dates if start <= d <= end]


def walk_forward(strategy_cls, param_grid: dict, universe: list,
                 start_date: str, end_date: str,
                 initial_capital: float = 100000,
                 n_splits: int = 4,
                 train_ratio: float = 0.7,
                 metric: str = "total_return",
                 execution_mode: str = "next_open",
                 risk_manager=None,
                 train_days: Optional[int] = None,
                 test_days: Optional[int] = None,
                 min_train_days: Optional[int] = None,
                 min_test_days: Optional[int] = None,
                 progress_callback: Callable = None,
                 backtest_options: dict = None) -> WalkForwardReport:
    """滚动样本外验证（walk-forward）

    把 [start_date, end_date] 切成 n_splits 个「训练段 + 测试段」：
    每个折在训练段上用网格搜索挑参数，然后在紧随其后的、**从未参与选参**的
    测试段上运行，最后把各测试段的权益首尾拼接成一条完整的样本外曲线。

    Args:
        strategy_cls: 策略类
        param_grid: 参数网格 {参数名: [候选值, ...]}
        universe: 标的列表
        start_date / end_date: 完整回测区间 'YYYYMMDD'
        initial_capital: 初始资金
        n_splits: 折数（测试段互不重叠且首尾相接）
        train_ratio: 训练段占整体的比例。4 折 + 0.7 意味着前 70% 用于首折训练，
            之后每折训练窗滚动前进一个测试窗宽度
        metric: 选参用的指标（total_return / sharpe_ratio / calmar_ratio 等）
        execution_mode: 撮合模式，建议保持 next_open 贴近实盘
        risk_manager: 可选风控实例（每折会克隆独立状态）
        train_days / test_days: 直接指定窗口交易日数，给定时覆盖 n_splits/train_ratio
        min_train_days / min_test_days: 窗口最小交易日数，默认取配置
        progress_callback: 进度回调 callback(done, total)
        backtest_options: 由 ``build_backtest_options()`` 打包的引擎配置。训练段选参
            与测试段验证必须使用与单次回测相同的仓位/持仓/择时规则，否则样本外
            结论与实盘配置不可比。

    Returns:
        WalkForwardReport
    """
    if param_grid is None or not param_grid:
        raise ValueError("walk_forward 需要非空 param_grid，否则无从挑选参数")

    min_train = min_train_days if min_train_days is not None else BACKTEST_WALK_FORWARD_MIN_TRAIN_DAYS
    min_test = min_test_days if min_test_days is not None else BACKTEST_WALK_FORWARD_MIN_TEST_DAYS

    # 一次预加载覆盖整个区间：各折共用，避免反复拉行情与重算指标
    preloaded = preload_backtest_data(universe, start_date, end_date)
    if not preloaded:
        raise ValueError("区间内没有任何可用行情数据")

    dates = _collect_trade_dates(preloaded, start_date, end_date)
    if not dates:
        raise ValueError(f"{start_date}~{end_date} 区间内没有交易日")

    # ---- 计算窗口宽度 ----
    if test_days is not None:
        train = int(train_days) if train_days else int(len(dates) * train_ratio)
        n_splits = max(1, (len(dates) - train) // max(1, int(test_days)))
        test = int(test_days)
    else:
        test = int(len(dates) * (1 - train_ratio) / max(1, n_splits))
        train = int(len(dates) * train_ratio)

    if train < min_train:
        raise ValueError(
            f"训练段仅 {train} 个交易日，低于下限 {min_train}。"
            f"参数在这么短的历史上会被噪声主导，请拉长区间或调低 train_ratio 以外的限制"
        )
    if test < min_test:
        raise ValueError(f"测试段仅 {test} 个交易日，低于下限 {min_test}。请减少折数或拉长区间")
    if train + n_splits * test > len(dates):
        n_splits = max(1, (len(dates) - train) // test)
        if n_splits < 1:
            raise ValueError(
                f"区间长度 {len(dates)} 天不足以切出「{train} 天训练 + {test} 天测试」的一折"
            )

    report = WalkForwardReport(metric=metric)
    total_steps = n_splits * 2  # 每折 = 一次网格搜索 + 一次测试回测

    combined_equity: list = []
    combined_trades: list = []
    running_equity = float(initial_capital)

    for i in range(n_splits):
        tr_start = dates[i * test]
        tr_end = dates[i * test + train - 1]
        te_start = dates[i * test + train]
        te_end = dates[min(i * test + train + test - 1, len(dates) - 1)]

        # ---- 训练段选参 ----
        grid = grid_search(
            strategy_cls=strategy_cls,
            universe=universe,
            start_date=tr_start,
            end_date=tr_end,
            initial_capital=initial_capital,
            param_grid=param_grid,
            metric=metric,
            execution_mode=execution_mode,
            risk_manager=risk_manager,
            preloaded_data=preloaded,
            backtest_options=backtest_options,
        )
        if progress_callback:
            progress_callback(i * 2 + 1, total_steps)

        if not grid:
            report.warnings.append(f"第 {i+1} 折训练段未能得到任何有效参数组合，已跳过")
            continue

        best = grid[0]
        best_params = best["params"]

        # ---- 测试段验证（参数冻结，不再调参）----
        bt = Backtester(
            strategy_cls=strategy_cls,
            params=best_params,
            universe=universe,
            start_date=te_start,
            end_date=te_end,
            initial_capital=initial_capital,
            execution_mode=execution_mode,
            risk_manager=_clone_risk_manager(risk_manager),
            preloaded_data=preloaded,
            **_options_kwargs(backtest_options),
        )
        test_result = bt.run(save=False)
        if progress_callback:
            progress_callback(i * 2 + 2, total_steps)

        report.windows.append(WalkForwardWindow(
            index=i,
            train_start=tr_start, train_end=tr_end,
            test_start=te_start, test_end=te_end,
            best_params=best_params,
            train_metric=float(best["metric_value"]),
            test_metric=_metric_of(test_result, metric),
            test_return=float(test_result.total_return),
            test_max_drawdown=float(test_result.max_drawdown),
            test_trades=int(test_result.trade_count),
            test_result=test_result,
        ))

        # ---- 拼接样本外曲线：把本段收益按比例接到累计净值上 ----
        eqs = [e["equity"] for e in test_result.equity_curve]
        if eqs and eqs[0] > 0:
            for e in test_result.equity_curve:
                combined_equity.append({
                    "date": e["date"],
                    "equity": running_equity * (e["equity"] / eqs[0]),
                })
            running_equity = running_equity * (eqs[-1] / eqs[0])
        combined_trades.extend(test_result.trades)

    if not report.windows:
        report.verdict = "所有折均未产出有效结果，无法评估"
        return report

    # ---- 样本外汇总指标（复用 Portfolio 的绩效口径，保证与单次回测可比）----
    stitched = Portfolio(initial_capital)
    stitched.equity_curve = combined_equity
    stitched.trades = combined_trades
    report.oos_equity = combined_equity
    report.oos_trades = combined_trades
    report.oos_metrics = stitched.calc_metrics(
        benchmark_returns=get_benchmark_returns(start_date, end_date) or None
    )

    report.is_mean_metric = float(np.mean([w.train_metric for w in report.windows]))
    report.oos_mean_metric = float(np.mean([w.test_metric for w in report.windows]))
    if report.is_mean_metric > 1e-9:
        report.efficiency = report.oos_mean_metric / report.is_mean_metric
    else:
        # 样本内本身不赚钱时，"效率"没有意义（负/负会算成正），置 0 并显式说明
        report.efficiency = 0.0
        report.warnings.append(
            f"样本内均值 {report.is_mean_metric:.2f} 非正，效率比不具解释力，请直接看样本外绝对收益"
        )

    # ---- 参数稳定性与蒙特卡洛 ----
    report.param_stability = parameter_stability(
        strategy_cls=strategy_cls,
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        param_grid=param_grid,
        metric=metric,
        execution_mode=execution_mode,
        initial_capital=initial_capital,
        risk_manager=risk_manager,
        preloaded_data=preloaded,
        backtest_options=backtest_options,
    )
    report.monte_carlo = monte_carlo_trades(
        combined_trades,
        initial_capital=initial_capital,
        observed_drawdown=report.oos_max_drawdown,
    )
    report.verdict = _make_verdict(report)
    return report


def _clone_risk_manager(risk_manager):
    """为每个折克隆独立风控实例，防止 highest_prices 等状态跨折污染"""
    if risk_manager is None:
        return None
    # 统一委托给 risk_manager.clone_risk_manager：旧的手工复制漏掉了
    # enable_break_even / break_even_trigger_pct / break_even_buffer_pct，
    # 会让训练段与验证段用不同的风控配置跑，结论不可比。
    from engine.risk_manager import clone_risk_manager
    return clone_risk_manager(risk_manager)


# ============================================================
# 参数稳定性
# ============================================================

def parameter_stability(strategy_cls, universe: list, start_date: str, end_date: str,
                        param_grid: dict, metric: str = "total_return",
                        execution_mode: str = "next_open",
                        initial_capital: float = 100000,
                        risk_manager=None,
                        preloaded_data: dict = None,
                        backtest_options: dict = None) -> dict:
    """检验最优参数是「稳健高原」还是「孤立尖峰」

    做法：在整个区间上跑完整网格，取最优组合，再看与它**只差一档参数**的邻居
    表现如何。真优势应当对参数不敏感；若邻居立刻转亏，最优点大概率是拟合噪声。

    Returns:
        {
          "best_params", "best_metric",
          "neighbor_mean", "robustness",       # 邻居均值 / 最优值，越接近 1 越稳健
          "grid_median", "grid_std",
          "positive_ratio",                    # 全网格中指标为正的组合占比
          "per_param": {参数名: {"best": v, "neighbors": [...], "mean": ...}}
        }
    """
    grid = grid_search(
        strategy_cls=strategy_cls, universe=universe,
        start_date=start_date, end_date=end_date,
        initial_capital=initial_capital, param_grid=param_grid,
        metric=metric, execution_mode=execution_mode,
        risk_manager=risk_manager, preloaded_data=preloaded_data,
        backtest_options=backtest_options,
    )
    if not grid:
        return {}

    best = grid[0]
    best_params = best["params"]
    best_metric = float(best["metric_value"])
    values = [float(r["metric_value"]) for r in grid]

    # 建索引：参数取值元组 -> 指标
    names = list(param_grid.keys())
    lookup = {}
    for r in grid:
        lookup[tuple(r["params"][n] for n in names)] = float(r["metric_value"])

    neighbor_metrics = []
    per_param = {}
    for n in names:
        options = list(param_grid[n])
        try:
            pos = options.index(best_params[n])
        except ValueError:
            continue
        local = []
        for delta in (-1, 1):
            j = pos + delta
            if j < 0 or j >= len(options):
                continue
            key = tuple(options[j] if m == n else best_params[m] for m in names)
            if key in lookup:
                local.append(lookup[key])
                neighbor_metrics.append(lookup[key])
        per_param[n] = {
            "best": best_params[n],
            "neighbors": [round(v, 2) for v in local],
            "mean": round(float(np.mean(local)), 2) if local else None,
        }

    robustness = 0.0
    if neighbor_metrics:
        if abs(best_metric) > 1e-9:
            robustness = float(np.mean(neighbor_metrics) / best_metric)
        else:
            robustness = 0.0
        # 最优为正而邻居均值为负时，robustness 会是负数，语义上等价于「脆弱」，保留原值便于识别
    else:
        logger.info("网格过小，无法构造邻居（每个参数至少需要 2 个候选值）")

    return {
        "best_params": best_params,
        "best_metric": round(best_metric, 2),
        "neighbor_mean": round(float(np.mean(neighbor_metrics)), 2) if neighbor_metrics else None,
        "robustness": round(robustness, 3),
        "grid_median": round(float(np.median(values)), 2),
        "grid_std": round(float(np.std(values)), 2),
        "positive_ratio": round(sum(1 for v in values if v > 0) / len(values), 3),
        "per_param": per_param,
    }


# ============================================================
# 交易序列蒙特卡洛
# ============================================================

def monte_carlo_trades(trades: list, initial_capital: float = 100000,
                       observed_drawdown: float = 0.0,
                       runs: Optional[int] = None, seed: int = 42) -> dict:
    """交易序列蒙特卡洛：分别回答「路径风险」和「收益不确定性」两个问题

    **重排（permutation）** —— 把已实现盈亏顺序随机打乱。求和不变，所以终点固定，
    只有路径在变。它回答："同样的交易，换个发生顺序，回撤可能多深？"
    如果实际路径的回撤明显浅于重排分布，说明这次是运气好，实盘要按 95% 分位准备。

    **自助重采样（bootstrap）** —— 有放回地重抽同样笔数的交易。这相当于问：
    "如果市场给我的是另一批统计特征相同、但具体结果不同的交易，我会赚还是亏？"
    收益的分布与亏损概率取自这里。（重排的终点是固定的，拿它算亏损概率没有意义。）

    **口径说明**：两条曲线都由已实现盈亏累加而来，与组合逐日盯市的权益曲线不是同一条
    基准 —— 后者还含持仓浮盈浮亏，因此已实现盈亏总和可能明显小于组合总收益。
    为保证可比，"实际路径"的回撤在本函数内按已实现盈亏重算
    （``observed_realized_drawdown``）；传入的 ``observed_drawdown`` 仅作参考。

    Args:
        trades: Trade 列表（只统计 SELL 的已实现盈亏）
        initial_capital: 初始资金，用于折算百分比回撤
        observed_drawdown: 组合盯市最大回撤（%），仅作参考展示
        runs: 每种模拟的次数，默认取配置
        seed: 随机种子，保证结果可复现
    """
    runs = runs or BACKTEST_MONTE_CARLO_RUNS
    pnls = np.array([float(getattr(t, "pnl", 0.0)) for t in trades
                     if getattr(t, "direction", "") == "SELL"], dtype=float)
    if pnls.size < 3:
        return {"runs": 0, "note": "已实现交易不足 3 笔，蒙特卡洛无统计意义"}

    rng = np.random.default_rng(seed)
    base = float(initial_capital)
    if base <= 0:
        return {"runs": 0, "note": "初始资金无效"}

    def _path_drawdown(seq: np.ndarray) -> float:
        equity = base + np.cumsum(seq)
        peak = np.maximum.accumulate(np.maximum(equity, base))
        dd = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)
        return float(dd.max()) if dd.size else 0.0

    observed_realized = _path_drawdown(pnls)

    # ---- 1) 重排：只看路径风险，终点恒定 ----
    perm_dd = np.empty(runs, dtype=float)
    for i in range(runs):
        perm_dd[i] = _path_drawdown(rng.permutation(pnls))

    # ---- 2) 自助重采样：交易笔数与结果都在变，反映收益不确定性 ----
    boot_ret = np.empty(runs, dtype=float)
    boot_dd = np.empty(runs, dtype=float)
    n = pnls.size
    for i in range(runs):
        sample = pnls[rng.integers(0, n, n)]
        boot_ret[i] = sample.sum() / base
        boot_dd[i] = _path_drawdown(sample)

    worse = float(np.mean(perm_dd > observed_realized) * 100)

    def _sample(arr: np.ndarray, limit: int = 400) -> list:
        """降采样到最多 limit 个点，供 UI 画分布直方图（不参与任何判断）"""
        if arr.size <= limit:
            return [round(float(v), 2) for v in arr]
        step = max(1, arr.size // limit)
        return [round(float(v), 2) for v in arr[::step][:limit]]

    return {
        "runs": runs,
        "trades": int(n),
        "realized_pnl_total": round(float(pnls.sum()), 2),
        "observed_realized_drawdown": round(observed_realized, 2),
        "observed_drawdown": round(float(observed_drawdown), 2),
        # 重排口径：路径风险
        "drawdown_p50": round(float(np.percentile(perm_dd, 50)), 2),
        "drawdown_p95": round(float(np.percentile(perm_dd, 95)), 2),
        "drawdown_max": round(float(perm_dd.max()), 2),
        "drawdown_min": round(float(perm_dd.min()), 2),
        "worse_than_observed_pct": round(worse, 1),
        "drawdown_samples": _sample(perm_dd),
        # 自助口径：收益不确定性
        "final_return_p5": round(float(np.percentile(boot_ret, 5)) * 100, 2),
        "final_return_p50": round(float(np.percentile(boot_ret, 50)) * 100, 2),
        "final_return_p95": round(float(np.percentile(boot_ret, 95)) * 100, 2),
        "loss_probability": round(float(np.mean(boot_ret < 0) * 100), 1),
        "return_samples": _sample(boot_ret * 100),
    }


# ============================================================
# 结论判定
# ============================================================

def _make_verdict(report: WalkForwardReport) -> str:
    """把三项检验综合成一句可执行的判断"""
    problems = []
    positives = []

    if report.oos_total_return <= 0:
        problems.append(f"样本外总收益为负（{report.oos_total_return:.2f}%）")

    if report.is_mean_metric <= 1e-9:
        problems.append(
            f"训练段均值仅 {report.is_mean_metric:.2f}，策略连样本内都不赚钱，"
            f"最优参数缺乏选择依据"
        )

    # 效率比只在样本内为正时有解释力（负/负会算成正，属于误导）
    if report.is_mean_metric > 1e-9:
        if report.efficiency < 0.3:
            problems.append(f"样本外效率仅 {report.efficiency:.0%}，收益在样本外大幅衰减")
        elif report.efficiency >= 0.7:
            positives.append(f"样本外效率 {report.efficiency:.0%}，衰减有限")

    rob = report.param_stability.get("robustness")
    if rob is not None:
        if rob < 0.5:
            problems.append(f"参数稳定性 {rob:.2f}，最优参数是孤立尖峰")
        elif rob >= 0.7:
            positives.append(f"参数稳定性 {rob:.2f}")

    mc = report.monte_carlo or {}
    if mc.get("worse_than_observed_pct", 0) > 50:
        problems.append(
            f"实际交易顺序下的回撤 {mc.get('observed_realized_drawdown', 0):.1f}% 偏浅，"
            f"重排分布 95% 分位达 {mc.get('drawdown_p95', 0):.1f}%，"
            f"实盘很可能遭遇更深的回撤"
        )
    elif mc.get("runs"):
        positives.append("实际回撤未显著优于随机排列，风险已充分体现")

    if mc.get("loss_probability", 0) > 40:
        problems.append(
            f"自助重采样下亏损概率 {mc['loss_probability']:.0f}%，收益不具备确定性"
        )

    if len(report.windows) < 3:
        problems.append(f"仅 {len(report.windows)} 折，统计上不足以支撑结论")

    if len(problems) >= 2:
        return "过拟合风险高 — " + "；".join(problems)
    if problems:
        return "存在疑点 — " + "；".join(problems)
    if positives:
        return "样本外表现稳健 — " + "；".join(positives)
    return "证据不足，建议拉长区间后复验"
