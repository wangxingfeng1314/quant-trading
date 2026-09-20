"""500亿+大市值纯股息率估值策略单元测试"""
import pytest
import pandas as pd
import numpy as np

from strategies.high_dividend_value import HighDividendValueStrategy
from core.models import Signal
from engine.portfolio import Portfolio


def _create_mock_df(prices, volume=100000.0, div_col=None):
    """创建模拟日线数据"""
    n = len(prices)
    dates = [f"202601{i+1:02d}" if i < 30 else f"202602{i-29:02d}" for i in range(n)]

    data = {
        "trade_date": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [volume] * n,
        "amount": [p * volume for p in prices],
    }
    if div_col is not None:
        data["dividend_yield"] = div_col
    return pd.DataFrame(data)


class TestHighDividendValueStrategy:
    """高股息纯估值策略单元测试"""

    def test_init_and_schema(self):
        strat = HighDividendValueStrategy()
        assert strat.name == "high_dividend_value"
        assert strat.style == "中长线"
        assert "buy_div_yield" in strat.param_schema
        assert "sell_div_yield" in strat.param_schema
        assert "custom_dps" in strat.param_schema
        assert strat.buy_div_yield == 5.0
        assert strat.sell_div_yield == 3.5

    def test_zero_volume_skipped(self):
        """停牌成交量为0跳过"""
        strat = HighDividendValueStrategy()
        df = _create_mock_df([10.0, 10.0], volume=0)
        sigs = strat.on_bar("20260220", {"601088.SH": df})
        assert sigs == []

    def test_buy_signal_above_5_pct(self):
        """测试股息率 >= 5.0% 触发买入 (中国神华 2024 年派息 2.26 元，价格 40 元 -> 股息率 5.65% >= 5%)"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5)
        # 价格 40 元，2024年分红 2.26 元 -> 股息率 = 2.26 / 40 * 100 = 5.65%
        df = _create_mock_df([41.0, 40.0])
        sigs = strat.on_bar("20240520", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert sigs[0].score >= 0.6
        assert "股息率达 5.65%" in sigs[0].reason

    def test_sell_signal_below_3_5_pct(self):
        """测试股息率 <= 3.5% 触发卖出 (价格上涨到 70 元 -> 股息率 3.23% <= 3.5%)"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5)
        # 价格 70 元，2024年分红 2.26 元 -> 股息率 = 2.26 / 70 * 100 = 3.23%
        df = _create_mock_df([68.0, 70.0])
        sigs = strat.on_bar("20240920", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "SELL"
        assert "股息率降至 3.23%" in sigs[0].reason

    def test_hold_between_thresholds(self):
        """测试股息率在 3.5% ~ 5.0% 区间时不产生买卖信号，耐心持有"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5)
        # 价格 50 元，2024年分红 2.26 元 -> 股息率 = 4.52% (介于 3.5% 和 5.0% 之间)
        df = _create_mock_df([50.0, 50.0])
        sigs = strat.on_bar("20240620", {"601088.SH": df})
        assert sigs == []

    def test_custom_dps(self):
        """测试自定义每股分红 custom_dps"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, custom_dps=1.0)
        # 未在内置库里的股票，通过 custom_dps=1.0，价格 18 元 -> 股息率 5.56% >= 5% 触发买入
        df = _create_mock_df([18.0, 18.0])
        sigs = strat.on_bar("20260220", {"000001.SZ": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert "5.56%" in sigs[0].reason

    def test_direct_dividend_yield_column(self):
        """测试直接从行情 DataFrame 读取 dividend_yield 列"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5)
        df = _create_mock_df([10.0, 10.0], div_col=[4.0, 6.2])
        sigs = strat.on_bar("20260220", {"999999.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert "6.20%" in sigs[0].reason

