"""全面测试本轮 8 项工业级优化功能"""
import pytest
import pandas as pd
import logging
from logging.handlers import RotatingFileHandler

from engine.position import Position
from engine.portfolio import Portfolio
from engine.backtester import Backtester
from engine.scanner import check_stock_liquidity
from services.signal_service import SignalService, resolve_signal_conflicts
from core.models import Signal
from core.config import setup_logging


def test_position_adjust_for_split_and_dividend():
    """测试单只股票除权除息处理"""
    pos = Position(ts_code="600519.SH", shares=1000, avg_cost=20.0, available_shares=1000, total_cost=20000.0)

    # 1. 送转股 10 送 10 (split_factor = 2.0)
    cash_div = pos.adjust_for_split(split_factor=2.0)
    assert cash_div == 0.0
    assert pos.shares == 2000
    assert pos.available_shares == 2000
    assert pos.avg_cost == 10.0

    # 2. 现金分红 每股派现 1.0 元
    cash_div = pos.adjust_for_split(dividend_per_share=1.0)
    assert cash_div == 2000.0  # 2000 股 * 1.0
    assert pos.avg_cost == 9.0  # 10.0 - 1.0


def test_portfolio_handle_corporate_action():
    """测试组合层级的除权除息现金入账与持仓调整"""
    portfolio = Portfolio(initial_capital=100000.0)
    trade = portfolio.buy(
        ts_code="000001.SZ", price=10.0, volume=1000,
        trade_date="20240101", slippage=False,
    )
    assert trade is not None
    init_cash = portfolio.cash

    # 10 送 5 (1.5) + 每股分红 0.5 元
    div_received = portfolio.handle_corporate_action("000001.SZ", split_factor=1.5, dividend_per_share=0.5)
    assert div_received == 500.0  # 1000股 * 0.5元
    assert portfolio.cash == init_cash + 500.0

    pos = portfolio.get_position("000001.SZ")
    assert pos.shares == 1500


def test_backtester_equal_weight_budget():
    """测试基于全组合市值的等权买入预算计算"""
    from strategies.base import BaseStrategy
    portfolio = Portfolio(initial_capital=100000.0)
    backtester = Backtester(
        strategy_cls=BaseStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240101",
        end_date="20240110",
        initial_capital=100000.0,
    )

    sig = Signal(ts_code="000001.SZ", trade_date="20240102", strategy="MACD", direction="BUY", score=0.5)
    budget = backtester._calc_buy_budget(portfolio, sig)
    # score=0.5 时，pct 约为 min(0.05) + 0.5 * step(0.15) = 0.125
    expected_budget = portfolio.total_equity({}) * 0.125
    assert abs(budget - expected_budget) < 1.0

    # 当现金不足预期预算时，应自动受限于可用现金
    portfolio2 = Portfolio(initial_capital=100000.0)
    portfolio2.buy("000001.SZ", price=10.0, volume=9500, trade_date="20240101", slippage=False)
    # 持仓市值约 95,000 元，现金剩余约 5,000 元，但理想预算为 12,500 元
    budget_limited = backtester._calc_buy_budget(portfolio2, sig)
    assert budget_limited == portfolio2.cash



def test_signal_arbitration_resonance_and_conflict():
    """测试多策略信号仲裁引擎：同向共振与多空净额化"""
    sig1 = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="MACD", direction="BUY", score=0.6, reason="金叉")
    sig2 = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="RSI", direction="BUY", score=0.7, reason="超卖回升")

    # 1. 同向共振
    results = resolve_signal_conflicts([sig1, sig2])
    assert len(results) == 1
    res = results[0]
    assert res.direction == "BUY"
    assert res.score > 0.7  # 共振加成
    assert "ensemble" in res.strategy
    assert "共振" in res.reason
    assert res.context_snapshot.get("is_ensemble") is True


    # 2. 多空冲突
    sig_sell = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="BOLL", direction="SELL", score=0.65, reason="触轨")
    conflict_results = resolve_signal_conflicts([sig1, sig_sell])
    # 0.6 vs 0.65，净差 0.05 < 0.15 阈值，应中和过滤
    assert len(conflict_results) == 0

    # 3. 显著优势冲突
    sig_strong_buy = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="MA", direction="BUY", score=0.9, reason="多头排列")
    strong_results = resolve_signal_conflicts([sig_strong_buy, sig_sell])
    assert len(strong_results) == 1
    assert strong_results[0].direction == "BUY"
    assert "净胜" in strong_results[0].reason



def test_logging_rotation_configuration():
    """测试生产级日志轮转配置"""
    logger = setup_logging(name="test_quant_log", level=logging.INFO, log_filename="test_quant.log")
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) >= 1
    handler = file_handlers[0]
    assert handler.maxBytes == 20 * 1024 * 1024
    assert handler.backupCount == 5


def test_scanner_liquidity_guard():
    """测试流动性风控与停牌标的过滤"""
    # 正常流动性标的
    normal_df = pd.DataFrame([
        {"trade_date": "20240102", "vol": 100000, "amount": 40000000.0},
        {"trade_date": "20240103", "vol": 120000, "amount": 50000000.0},
        {"trade_date": "20240104", "vol": 110000, "amount": 45000000.0},
        {"trade_date": "20240105", "vol": 130000, "amount": 60000000.0},
    ])
    assert check_stock_liquidity(normal_df, end_date="20240105", min_daily_amount=30000000.0, filter_suspended=True) is True

    # 停牌标的 (vol=0)
    suspended_df = pd.DataFrame([
        {"trade_date": "20240102", "vol": 100000, "amount": 40000000.0},
        {"trade_date": "20240105", "vol": 0, "amount": 0.0},
    ])
    assert check_stock_liquidity(suspended_df, end_date="20240105", min_daily_amount=0.0, filter_suspended=True) is False

    # 微量流动性陷阱标的 (日均不足 3000 万)
    illiquid_df = pd.DataFrame([
        {"trade_date": "20240102", "vol": 1000, "amount": 500000.0},
        {"trade_date": "20240103", "vol": 1000, "amount": 500000.0},
        {"trade_date": "20240104", "vol": 1000, "amount": 500000.0},
        {"trade_date": "20240105", "vol": 1000, "amount": 500000.0},
    ])
    assert check_stock_liquidity(illiquid_df, end_date="20240105", min_daily_amount=30000000.0, filter_suspended=True) is False


def test_signal_combo_edge_trigger_and_overbought_guard():
    """测试 signal_combo 边沿触发（首日共振触发，连涨不重复刷屏）与 RSI>68 超买防追高"""
    from strategies.signal_combo import SignalComboStrategy

    strat = SignalComboStrategy(ma_period=5, buy_threshold=2)
    # 构造 32 天数据：前 31 天震荡弱势(total < 2)，第 32 天首次放量站上均线且 RSI=60 (适中偏强)
    df = pd.DataFrame({
        "trade_date": [f"202601{i+1:02d}" for i in range(32)],
        "open": [10.0] * 32,
        "high": [10.2] * 32,
        "low": [9.8] * 32,
        "close": [9.8] * 31 + [10.5],
        "volume": [1000.0] * 31 + [2000.0],
        "vol_ma5": [1000.0] * 32,
        "ma5": [10.0] * 32,
        "dif": [-0.1] * 31 + [0.2],
        "dea": [0.0] * 31 + [0.1],
        "macd_hist": [-0.1] * 31 + [0.1],
        "rsi14": [45.0] * 31 + [60.0],
        "pct_chg": [0.0] * 31 + [3.5],
    })
    sigs_first = strat.on_bar("20260132", {"000001.SZ": df})
    assert len(sigs_first) == 1
    assert sigs_first[0].direction == "BUY"

    # 若第 31 天和第 32 天都处于高位且 RSI=75 (超买)，绝不发出追高买入信号
    df_overbought = df.copy()
    df_overbought["close"] = [10.5] * 32
    df_overbought["rsi14"] = [75.0] * 32
    sigs_ob = strat.on_bar("20260132", {"000001.SZ": df_overbought})
    assert not any(s.direction == "BUY" for s in sigs_ob)


def test_scanner_conflict_interception_and_resonance(monkeypatch):
    """测试扫描器同股多空冲突拦截（趋势破位时拦截同日买入信号）与多策略同向共振加成"""
    from engine.scanner import _scan_single_stock
    from strategies.base import BaseStrategy

    class DummyBullStrategy1(BaseStrategy):
        name = "ma_bullish"
        def on_bar(self, trade_date, data, portfolio=None):
            return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="BUY", score=0.75, price_ref=10.0, reason="多头1")]

    class DummyBullStrategy2(BaseStrategy):
        name = "macd_cross"
        def on_bar(self, trade_date, data, portfolio=None):
            return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="BUY", score=0.72, price_ref=10.0, reason="多头2")]

    class DummyTrendBreakdownSell(BaseStrategy):
        name = "ma_cross"
        def on_bar(self, trade_date, data, portfolio=None):
            return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="SELL", score=0.70, price_ref=10.0, reason="均线死叉")]

    dates = pd.date_range("2025-01-01", periods=70, freq="B").strftime("%Y%m%d").tolist()
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 70,
        "trade_date": dates,
        "open": [10.0] * 70,
        "high": [10.5] * 70,
        "low": [9.5] * 70,
        "close": [10.2] * 70,
        "volume": [100000.0] * 70,
        "amount": [50000000.0] * 70,
    })
    monkeypatch.setattr("engine.scanner.get_daily", lambda ts, end_date=None: df.copy())

    # 1. 同向双策略看多共振：保留2条BUY且带共振标记
    res_bull = _scan_single_stock("000001.SZ", dates[-1], [DummyBullStrategy1(), DummyBullStrategy2()], min_daily_amount=0.0)
    assert len(res_bull) == 2
    assert all(s.direction == "BUY" and "策略共振" in s.reason for s in res_bull)

    # 2. 存在均线死叉看空冲突：自动拦截买入推荐，只保留卖出信号
    res_conflict = _scan_single_stock("000001.SZ", dates[-1], [DummyBullStrategy1(), DummyTrendBreakdownSell()], min_daily_amount=0.0)
    assert all(s.direction == "SELL" for s in res_conflict)

