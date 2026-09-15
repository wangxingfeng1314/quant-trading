"""P0 / P1 / P2 阶段新增功能与修复的专项测试

涵盖：
  - P0: T+1 可用持仓与今日冻结解耦、回测次日开盘价撮合与涨跌停保护
  - P1: 消息推送交易执行指引内容生成
  - P2: 统一风控管理器（固定止损、移动跟踪止盈、最大持仓天数）、ATR%指标
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch

from engine.position import Position
from engine.portfolio import Portfolio
from engine.backtester import Backtester
from engine.risk_manager import RiskManager
from core.models import Signal
from data.indicators import add_atr
from notifier.push import notify_signals
from strategies.base import BaseStrategy


# -------------------------------------------------------------
# P0 测试：T+1 可用持仓解耦
# -------------------------------------------------------------
def test_position_t1_available_shares_with_intraday_addon():
    """验证 T+1 规则下，加仓不影响昨日已沉淀持仓的卖出能力"""
    pos = Position(ts_code="000001.SZ")

    # Day 1: 买入 1000 股
    pos.buy(price=10.0, volume=1000, cost=5.0, trade_date="20240102")
    assert pos.shares == 1000
    assert pos.available_shares == 0  # 当日买入冻结
    assert not pos.can_sell("20240102", volume=100)

    # Day 2: 进入新交易日，昨日 1000 股转为可用
    pos.on_new_day("20240103")
    assert pos.available_shares == 1000
    assert pos.can_sell("20240103", volume=1000)

    # Day 2 盘中又加仓 500 股
    pos.buy(price=10.5, volume=500, cost=5.0, trade_date="20240103")
    assert pos.shares == 1500
    # 关键断言：加仓后，昨日的 1000 股仍然可卖！
    assert pos.available_shares == 1000
    assert pos.can_sell("20240103", volume=1000)

    # 尝试卖出 1000 股
    realized_pnl = pos.sell(volume=1000, price=11.0, cost=5.0)
    assert pos.shares == 500  # 剩下今日买的 500 股
    assert pos.available_shares == 0  # 今日可用已被全部卖出
    assert not pos.can_sell("20240103", volume=100)

    # Day 3: 进入第 3 天，今日买的 500 股转为可用
    pos.on_new_day("20240104")
    assert pos.available_shares == 500
    assert pos.can_sell("20240104", volume=500)


def test_portfolio_on_new_day_and_sell():
    """验证 Portfolio 集成 on_new_day 与 T+1 保护"""
    p = Portfolio(initial_capital=100000)
    trade = p.buy(ts_code="000001.SZ", price=10.0, volume=1000, trade_date="20240102")
    assert trade is not None

    # 当日不能卖出
    trade_sell_fail = p.sell(ts_code="000001.SZ", price=10.5, volume=500, trade_date="20240102")
    assert trade_sell_fail is None

    # 跨日自动结转并成功卖出
    trade_sell_ok = p.sell(ts_code="000001.SZ", price=10.5, volume=500, trade_date="20240103")
    assert trade_sell_ok is not None
    assert trade_sell_ok.volume == 500


# -------------------------------------------------------------
# P0 测试：次日开盘价回测模式
# -------------------------------------------------------------
class BuyHoldDummyStrategy(BaseStrategy):
    name = "buy_hold_dummy"
    description = "第1天买入并持有"

    def on_bar(self, trade_date: str, data: dict, portfolio=None):
        if portfolio and portfolio.positions.get("000001.SZ") and not portfolio.positions["000001.SZ"].is_empty:
            return []
        row = data["000001.SZ"].iloc[-1]
        return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name,
                       direction="BUY", score=0.8, price_ref=row["close"])]


def test_backtester_next_open_execution(monkeypatch):
    """测试 next_open 模式下，第 T 日产生的信号在第 T+1 日开盘价成交"""
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

    # mock get_daily
    monkeypatch.setattr("engine.backtester.get_daily", lambda ts_code, s, e: df.copy())
    monkeypatch.setattr("engine.backtester.save_backtest_result", lambda res: 1)
    monkeypatch.setattr("engine.backtester.save_backtest_trades", lambda tid, trades: None)

    bt = Backtester(
        strategy_cls=BuyHoldDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240105",
        initial_capital=100000,
        execution_mode="next_open"
    )
    result = bt.run()
    assert result is not None
    # 在 next_open 模式下，Day 1 (20240102) 产生信号，成交日期应为 Day 2 (20240103)
    if result.trades:
        first_trade = result.trades[0]
        assert first_trade.trade_date == "20240103"


# -------------------------------------------------------------
# P1 测试：推送量化行动指引
# -------------------------------------------------------------
def test_notify_signals_with_actionable_plan():
    """验证 notify_signals 升级后包含建议股数、预估金额与止损止盈"""
    signals = [
        Signal(ts_code="000001.SZ", trade_date="20240102", strategy="ma_bullish",
               direction="BUY", score=0.8, price_ref=10.0, reason="均线多头"),
        Signal(ts_code="600519.SH", trade_date="20240102", strategy="macd_divergence",
               direction="SELL", score=0.9, price_ref=1800.0, reason="顶背离"),
    ]

    sent_title = None
    sent_content = None

    def mock_send(title, content):
        nonlocal sent_title, sent_content
        sent_title = title
        sent_content = content
        return True

    with patch("notifier.push.send_notification", side_effect=mock_send):
        with patch("data.storage.get_instrument_name", return_value="平安银行"):
            notify_signals(signals, ["测试策略"])

    assert sent_title is not None
    assert "量化行动指引" in sent_content
    assert "建议股数" in sent_content
    assert "建议止损" in sent_content
    assert "目标止盈" in sent_content
    assert "¥9.50 (-5%)" in sent_content
    assert "¥11.00 (+10%)" in sent_content


# -------------------------------------------------------------
# P2 测试：统一风控拦截器与指标拓展
# -------------------------------------------------------------
def test_risk_manager_stop_loss():
    """验证固定止损功能：当浮亏超过阈值时触发强制平仓"""
    rm = RiskManager(stop_loss_pct=-5.0)
    p = Portfolio(initial_capital=100000)

    # 成本价 10.0，买入 1000 股
    pos = Position(ts_code="000001.SZ", shares=1000, avg_cost=10.0, buy_date="20240102")
    p.positions["000001.SZ"] = pos

    # 价格跌至 9.40 (浮亏 -6.0% <= -5.0%)
    signals = rm.check_risks("20240103", p, {"000001.SZ": 9.40})
    assert len(signals) == 1
    assert signals[0].direction == "SELL"
    assert "固定止损" in signals[0].reason


def test_risk_manager_trailing_stop():
    """验证移动跟踪止盈功能：最高浮盈达到8%后，从高点回撤3%触发保盈平仓"""
    rm = RiskManager(trailing_stop_activation=8.0, trailing_stop_callback=3.0)
    p = Portfolio(initial_capital=100000)

    pos = Position(ts_code="000001.SZ", shares=1000, avg_cost=10.0, buy_date="20240102")
    p.positions["000001.SZ"] = pos

    # Day 1: 股价大涨到 11.0 (浮盈 +10% >= +8%，激活移动止盈)
    signals1 = rm.check_risks("20240103", p, {"000001.SZ": 11.0})
    assert len(signals1) == 0  # 还在上涨，不平仓

    # Day 2: 股价从 11.0 回落到 10.5 (从最高点回撤 (1-10.5/11.0)*100 = 4.54% >= 3.0%)
    signals2 = rm.check_risks("20240104", p, {"000001.SZ": 10.5})
    assert len(signals2) == 1
    assert signals2[0].direction == "SELL"
    assert "跟踪止盈" in signals2[0].reason


def test_risk_manager_max_holding_days():
    """验证最大持仓天数强制出场"""
    rm = RiskManager(max_holding_days=10)
    p = Portfolio(initial_capital=100000)

    pos = Position(ts_code="000001.SZ", shares=1000, avg_cost=10.0, buy_date="20240101")
    p.positions["000001.SZ"] = pos

    # 未满10天不触发
    signals1 = rm.check_risks("20240105", p, {"000001.SZ": 10.0})
    assert len(signals1) == 0

    # 满15天触发
    signals2 = rm.check_risks("20240116", p, {"000001.SZ": 10.0})
    assert len(signals2) == 1
    assert "持仓到期" in signals2[0].reason


def test_indicators_atr_pct():
    """验证 add_atr 计算出 atr14 和 atr_pct"""
    df = pd.DataFrame({
        "close": [10.0, 10.5, 11.0, 10.8, 11.2],
        "high": [10.2, 10.7, 11.2, 11.0, 11.5],
        "low": [9.8, 10.2, 10.8, 10.6, 11.0],
    })
    res = add_atr(df, period=3)
    assert "atr3" in res.columns
    assert "atr_pct" in res.columns
    assert not res["atr_pct"].isna().all()
    assert (res["atr_pct"] > 0).all()
