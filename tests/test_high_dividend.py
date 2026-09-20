"""500亿+大市值高股息价值趋势策略单元测试"""
import pytest
import pandas as pd
import numpy as np

from strategies.high_dividend_value import HighDividendValueStrategy
from core.models import Signal
from engine.portfolio import Portfolio
from engine.backtester import Backtester


def _create_mock_df(n=80, base_price=10.0, trend=0.01):
    """创建模拟日线数据"""
    dates = [f"202601{i+1:02d}" if i < 30 else f"202602{i-29:02d}" for i in range(n)]
    prices = [base_price * (1 + trend * i) for i in range(n)]

    df = pd.DataFrame({
        "trade_date": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [100000.0] * n,
        "amount": [1000000.0] * n,
        "ma10": prices,
        "ma60": [p * 0.95 for p in prices],  # 保持在 MA60 上方
        "rsi14": [55.0] * n,
    })
    return df


class TestHighDividendValueStrategy:
    """高股息策略单元测试"""

    def test_init_and_schema(self):
        strat = HighDividendValueStrategy()
        assert strat.name == "high_dividend_value"
        assert strat.style == "中长线"
        assert "fast_ma" in strat.param_schema
        assert "slow_ma" in strat.param_schema
        assert "bias_entry_max" in strat.param_schema
        assert "bias_exit_pct" in strat.param_schema

    def test_data_too_short(self):
        """数据不足65日跳过"""
        strat = HighDividendValueStrategy(slow_ma=60)
        df_short = _create_mock_df(n=50)
        sigs = strat.on_bar("20260220", {"601088.SH": df_short})
        assert sigs == []

    def test_zero_volume_skipped(self):
        """停牌成交量为0跳过"""
        strat = HighDividendValueStrategy()
        df = _create_mock_df(n=80)
        df.loc[df.index[-1], "volume"] = 0
        sigs = strat.on_bar("20260220", {"601088.SH": df})
        assert sigs == []

    def test_golden_cross_buy(self):
        """测试快线上穿慢线金叉买入"""
        strat = HighDividendValueStrategy(fast_ma=10, slow_ma=60)
        df = _create_mock_df(n=80)

        # 构造前一日快线 <= 慢线，今日快线 > 慢线
        df.loc[df.index[-2], "ma10"] = 10.0
        df.loc[df.index[-2], "ma60"] = 10.1
        df.loc[df.index[-1], "ma10"] = 10.2
        df.loc[df.index[-1], "ma60"] = 10.1
        df.loc[df.index[-1], "close"] = 10.3
        df.loc[df.index[-1], "rsi14"] = 55.0

        sigs = strat.on_bar("20260220", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert sigs[0].score >= 0.8
        assert "大市值高股息" in sigs[0].reason

    def test_overheat_exit_sell(self):
        """测试股价短期严重偏离MA60过热止盈"""
        strat = HighDividendValueStrategy(bias_exit_pct=22.0)
        df = _create_mock_df(n=80)

        # 构造价格大幅高于 MA60 (偏离 30%)
        df.loc[df.index[-1], "ma60"] = 10.0
        df.loc[df.index[-1], "close"] = 13.0  # +30% > 22%

        sigs = strat.on_bar("20260220", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "SELL"
        assert "过热止盈" in sigs[0].reason

    def test_trend_breakdown_sell(self):
        """测试跌破长期均线防守止损"""
        strat = HighDividendValueStrategy(fast_ma=10, slow_ma=60)
        df = _create_mock_df(n=80)

        # 构造快慢线死叉且收盘跌破基准线
        df.loc[df.index[-2], "ma10"] = 10.1
        df.loc[df.index[-2], "ma60"] = 10.0
        df.loc[df.index[-1], "ma10"] = 9.8
        df.loc[df.index[-1], "ma60"] = 9.9
        df.loc[df.index[-1], "close"] = 9.6  # 跌破 60 日线

        sigs = strat.on_bar("20260220", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "SELL"
        assert "破位" in sigs[0].reason
