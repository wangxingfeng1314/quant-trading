"""Tier 1 ~ Tier 4 深度架构与工程优化的专项测试

涵盖：
  - Tier 1: 数据预热 (Preloaded Data) 与网格搜索加速
  - Tier 2: 除权除息断层检测 (check_split_dividend_anomaly) 与全量覆写保护
  - Tier 3: 交易决策与风控上下文快照 (context_snapshot) 捕获与持久化
  - Tier 4: 横截面置信度排序与最大活跃持仓上限 (max_active_positions)
"""
import pytest
import pandas as pd
import numpy as np

from core.models import Signal, Trade
from engine.portfolio import Portfolio
from engine.backtester import Backtester, grid_search, preload_backtest_data
from engine.risk_manager import RiskManager
from data.storage import (
    save_daily, clear_daily, get_daily, check_split_dividend_anomaly,
    save_backtest_result, save_backtest_trades, get_conn, init_db
)
from strategies.base import BaseStrategy


# -------------------------------------------------------------
# 测试辅助策略
# -------------------------------------------------------------
class MultiStockDummyStrategy(BaseStrategy):
    name = "multi_stock_dummy"
    description = "为不同股票赋予不同置信度"

    def __init__(self, scores: dict = None):
        self.scores = scores or {"000001.SZ": 0.9, "000002.SZ": 0.5, "600519.SH": 0.3}

    def on_bar(self, trade_date: str, data: dict, portfolio=None):
        signals = []
        for ts_code, df in data.items():
            if portfolio and portfolio.positions.get(ts_code) and not portfolio.positions[ts_code].is_empty:
                continue
            row = df.iloc[-1]
            score = self.scores.get(ts_code, 0.5)
            signals.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy=self.name,
                direction="BUY",
                score=score,
                price_ref=row["close"],
                reason=f"测试买入评分={score}"
            ))
        return signals


# -------------------------------------------------------------
# Tier 1 测试：数据预热机制与结果一致性
# -------------------------------------------------------------
def test_preloaded_data_consistency(monkeypatch):
    """验证传入 preloaded_data 与普通回测生成的指标和交易结果完全一致"""
    dates = ["20240102", "20240103", "20240104", "20240105"]
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 4,
        "trade_date": dates,
        "open": [10.0, 10.2, 10.5, 10.8],
        "high": [10.3, 10.6, 10.9, 11.0],
        "low": [9.9, 10.1, 10.4, 10.7],
        "close": [10.1, 10.4, 10.7, 10.9],
        "volume": [100000.0] * 4,
        "vol": [100000.0] * 4,
        "amount": [1000000.0] * 4,
    })

    monkeypatch.setattr("engine.backtester.get_daily", lambda ts_code, s, e: df.copy())
    monkeypatch.setattr("engine.backtester.save_backtest_result", lambda res: 1)
    monkeypatch.setattr("engine.backtester.save_backtest_trades", lambda tid, trades: None)

    # 1. 预加载数据
    preloaded = preload_backtest_data(["000001.SZ"], "20240102", "20240105")
    assert "000001.SZ" in preloaded
    assert "ma5" in preloaded["000001.SZ"].columns

    # 2. 使用 preloaded_data 运行
    bt_fast = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240105",
        preloaded_data=preloaded
    )
    res_fast = bt_fast.run(save=False)

    # 3. 普通运行
    bt_normal = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240105",
    )
    res_normal = bt_normal.run(save=False)

    assert res_fast.total_return == res_normal.total_return
    assert len(res_fast.trades) == len(res_normal.trades)


# -------------------------------------------------------------
# Tier 2 测试：除权除息跳空断层检测
# -------------------------------------------------------------
def test_check_split_dividend_anomaly():
    """验证除权除息检测逻辑：平稳行情不触发，断崖跳空准确触发"""
    test_code = "999999.SZ"
    clear_daily(test_code)

    # 先存入基准历史数据（收盘价 20.0）
    hist_df = pd.DataFrame([{
        "ts_code": test_code, "trade_date": "20240531",
        "open": 20.0, "high": 20.5, "low": 19.8, "close": 20.0,
        "volume": 10000.0, "amount": 200000.0, "pct_chg": 0.0, "adj_factor": 1.0
    }])
    save_daily(hist_df)

    # 1. 正常行情：今天 20.2 元 (+1.0%) -> 不应触发断层
    normal_incoming = pd.DataFrame([{
        "ts_code": test_code, "trade_date": "20240603",
        "open": 20.1, "high": 20.4, "low": 20.0, "close": 20.2,
        "volume": 10000.0, "amount": 202000.0, "pct_chg": 1.0, "adj_factor": 1.0
    }])
    assert not check_split_dividend_anomaly(test_code, normal_incoming)

    # 2. 发生除权（10送10），增量前复权收盘价变为 10.1 元，pct_chg 为 +1.0%
    # 推导的前收为 10.0，但库里是 20.0 -> 差异达 100% -> 必须触发异常
    split_incoming = pd.DataFrame([{
        "ts_code": test_code, "trade_date": "20240603",
        "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.1,
        "volume": 20000.0, "amount": 202000.0, "pct_chg": 1.0, "adj_factor": 1.0
    }])
    assert check_split_dividend_anomaly(test_code, split_incoming)

    # 清理测试数据
    clear_daily(test_code)
    assert get_daily(test_code).empty


# -------------------------------------------------------------
# Tier 3 测试：决策与风控上下文快照
# -------------------------------------------------------------
def test_decision_snapshot_and_db_storage():
    """验证交易决策快照被捕获并能成功持久化到数据库"""
    init_db()  # 触发可能存在的迁移
    dates = ["20240102", "20240103"]
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 2,
        "trade_date": dates,
        "open": [10.0, 10.5],
        "high": [10.2, 10.8],
        "low": [9.8, 10.3],
        "close": [10.1, 10.6],
        "volume": [100000.0] * 2,
        "vol": [100000.0] * 2,
        "amount": [1000000.0] * 2,
    })

    preloaded = {"000001.SZ": df.copy()}
    # 回测产生买入 Trade
    bt = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240103",
        preloaded_data=preloaded
    )
    res = bt.run(save=False)
    assert len(res.trades) > 0
    trade = res.trades[0]
    # 验证快照被正确挂载
    assert trade.context_snapshot is not None
    assert "score" in trade.context_snapshot
    assert "close" in trade.context_snapshot
    assert trade.context_snapshot["close"] == 10.1

    # 验证存入数据库
    bt_id = save_backtest_result(res)
    save_backtest_trades(bt_id, res.trades)
    with get_conn() as conn:
        row = conn.execute("SELECT context_snapshot FROM backtest_trade WHERE backtest_id = ?", (bt_id,)).fetchone()
        assert row is not None
        assert "score" in row[0]


def test_risk_manager_snapshot():
    """验证 RiskManager 触发风控时捕获详细参数快照"""
    rm = RiskManager(stop_loss_pct=-5.0)
    p = Portfolio(initial_capital=100000)
    pos = pos = p.buy(ts_code="000001.SZ", price=10.0, volume=1000, trade_date="20240102")

    signals = rm.check_risks("20240103", p, {"000001.SZ": 9.40})
    assert len(signals) == 1
    sig = signals[0]
    assert sig.context_snapshot is not None
    assert sig.context_snapshot["risk_type"] == "stop_loss"
    assert sig.context_snapshot["pnl_pct"] <= -5.0
    assert sig.context_snapshot["current_price"] == 9.40


# -------------------------------------------------------------
# Tier 4 测试：横截面排序与持仓上限控制
# -------------------------------------------------------------
def test_cross_sectional_ranking_and_max_positions(monkeypatch):
    """验证横截面排序与最大持仓上限：同日多个信号优先买入高评分标的，满额后拦截后续标的"""
    dates = ["20240102", "20240103"]
    # 构造 3 只股票数据
    codes = ["000001.SZ", "000002.SZ", "600519.SH"]
    preloaded = {}
    for c in codes:
        preloaded[c] = pd.DataFrame({
            "ts_code": [c] * 2,
            "trade_date": dates,
            "open": [10.0, 10.2],
            "high": [10.5, 10.6],
            "low": [9.8, 10.0],
            "close": [10.1, 10.4],
            "volume": [100000.0] * 2,
            "vol": [100000.0] * 2,
            "amount": [1000000.0] * 2,
        })

    # 设置评分: 000001(0.9) > 000002(0.6) > 600519(0.3)
    # 最大持仓上限限制为 2 只
    bt = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={"scores": {"000001.SZ": 0.9, "000002.SZ": 0.6, "600519.SH": 0.3}},
        universe=codes,
        start_date="20240102",
        end_date="20240103",
        initial_capital=100000,
        preloaded_data=preloaded,
        max_active_positions=2  # 限制最多同时持有 2 只
    )
    res = bt.run(save=False)

    bought_codes = [t.ts_code for t in res.trades if t.direction == "BUY"]
    # 断言：必须成功买入 000001.SZ (0.9) 和 000002.SZ (0.6)，而 600519.SH (0.3) 必须被拦截！
    assert len(bought_codes) == 2
    assert "000001.SZ" in bought_codes
    assert "000002.SZ" in bought_codes
    assert "600519.SH" not in bought_codes
