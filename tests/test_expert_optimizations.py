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
