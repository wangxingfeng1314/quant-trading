"""500亿+大市值高股息顺势增强策略单元测试"""
import pytest
import pandas as pd
import numpy as np

from strategies.high_dividend_value import HighDividendValueStrategy
from core.models import Signal
from engine.portfolio import Portfolio


def _create_mock_df(prices, volume=100000.0, div_col=None, ma20_val=None, ma60_val=None):
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
    if ma20_val is not None:
        data["ma20"] = [ma20_val] * n
    if ma60_val is not None:
        data["ma60"] = [ma60_val] * n
    return pd.DataFrame(data)


class TestHighDividendValueStrategy:
    """高股息顺势增强策略单元测试"""

    def test_init_and_schema(self):
        strat = HighDividendValueStrategy()
        assert strat.name == "high_dividend_value"
        assert strat.style == "中长线"
        assert "buy_div_yield" in strat.param_schema
        assert "sell_div_yield" in strat.param_schema
        assert "ma_entry_filter" in strat.param_schema
        assert "trend_ma_period" in strat.param_schema
        assert "profit_target_pct" in strat.param_schema
        assert "bias_take_profit" in strat.param_schema
        assert "custom_dps" in strat.param_schema
        assert strat.buy_div_yield == 5.0
        assert strat.sell_div_yield == 3.5
        assert strat.ma_entry_filter == 1
        assert strat.trend_ma_period == 20
        assert strat.profit_target_pct == 0.0

    def test_zero_volume_skipped(self):
        """停牌成交量为0跳过"""
        strat = HighDividendValueStrategy()
        df = _create_mock_df([10.0, 10.0], volume=0)
        sigs = strat.on_bar("20260220", {"601088.SH": df})
        assert sigs == []

    def test_buy_signal_above_5_pct_pure_mode(self):
        """测试纯股息模式下股息率 >= 5.0% 触发买入 (ma_entry_filter=0)"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, ma_entry_filter=0)
        # 价格 40 元，2024年分红 2.26 元 -> 股息率 = 2.26 / 40 * 100 = 5.65%
        df = _create_mock_df([41.0, 40.0])
        sigs = strat.on_bar("20240520", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert sigs[0].score >= 0.6
        assert "股息率达 5.65%" in sigs[0].reason

    def test_trend_filter_suppresses_buy_when_below_ma(self):
        """测试开启顺势增强 (ma_entry_filter=1) 时，若价格跌破MA则不买入防左侧被套"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, ma_entry_filter=1, trend_ma_period=20)
        # 价格 40 元 (股息率 5.65%)，但 MA20=42.0 (现价 < MA20)
        df = _create_mock_df([41.0, 40.0], ma20_val=42.0)
        sigs = strat.on_bar("20240520", {"601088.SH": df})
        assert sigs == []

    def test_trend_filter_triggers_buy_when_above_ma(self):
        """测试开启顺势增强 (ma_entry_filter=1) 时，价格站上MA顺势确认买入"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, ma_entry_filter=1, trend_ma_period=20)
        # 价格 40 元 (股息率 5.65%)，MA20=38.0 (现价 40 >= 38)
        df = _create_mock_df([41.0, 40.0], ma20_val=38.0)
        sigs = strat.on_bar("20240520", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert "站上MA20" in sigs[0].reason

    def test_oversold_filter_triggers_buy_when_below_ma(self):
        """测试超跌抄底模式 (ma_entry_filter=-1) 时，价格低于MA120才买入"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, ma_entry_filter=-1, trend_ma_period=120)
        # 价格 40 元 (股息率 5.65%)，MA120=45.0 (现价 40 < 45) -> 触发超跌买入
        df = _create_mock_df([41.0, 40.0], ma60_val=45.0)  # df 无 ma120 列时自动动态均值计算
        sigs = strat.on_bar("20240520", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert "超跌高股息买入" in sigs[0].reason

    def test_profit_target_sell(self):
        """测试固定波段盈利达到 profit_target_pct 触发卖出"""
        strat = HighDividendValueStrategy(profit_target_pct=8.0)
        portfolio = Portfolio(initial_capital=100000)
        # 买入 40 元，含佣金成本约 40.05 元，当前价格 44 元 -> 实际盈利约 9.86% >= 8.0%
        portfolio.buy(ts_code="601088.SH", trade_date="20240520", price=40.0, volume=1000)
        df = _create_mock_df([43.0, 44.0])
        sigs = strat.on_bar("20240620", {"601088.SH": df}, portfolio=portfolio)
        assert len(sigs) == 1
        assert sigs[0].direction == "SELL"
        assert "波段止盈卖出" in sigs[0].reason
        assert "9.86%" in sigs[0].reason

    def test_sell_signal_below_3_5_pct(self):
        """测试股息率 <= 3.5% 触发估值止盈卖出 (价格上涨到 70 元 -> 股息率 3.23% <= 3.5%)"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5)
        df = _create_mock_df([68.0, 70.0])
        sigs = strat.on_bar("20240920", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "SELL"
        assert "股息率降至 3.23%" in sigs[0].reason

    def test_bias_overheat_take_profit(self):
        """测试短期价格严重偏离均线 (>25%) 触发估值过热止盈"""
        strat = HighDividendValueStrategy(bias_take_profit=25.0, trend_ma_period=20)
        # 价格 50 元，MA20 为 38 元 -> 偏离度 = (50/38 - 1)*100 = 31.6% >= 25%
        df = _create_mock_df([48.0, 50.0], ma20_val=38.0)
        sigs = strat.on_bar("20240920", {"601088.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "SELL"
        assert "过热止盈" in sigs[0].reason
        assert "偏离MA20" in sigs[0].reason

    def test_hold_between_thresholds(self):
        """测试股息率在 3.5% ~ 5.0% 区间且未严重过热时不产生买卖信号，耐心持有"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, bias_take_profit=25.0)
        # 价格 50 元，分红 2.26 元 -> 股息率 = 4.52%, MA20=48 (偏离仅 4.2%)
        df = _create_mock_df([50.0, 50.0], ma20_val=48.0)
        sigs = strat.on_bar("20240620", {"601088.SH": df})
        assert sigs == []

    def test_custom_dps(self):
        """测试自定义每股分红 custom_dps"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, ma_entry_filter=0, custom_dps=1.0)
        df = _create_mock_df([18.0, 18.0])
        sigs = strat.on_bar("20260220", {"000001.SZ": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert "5.56%" in sigs[0].reason

    def test_direct_dividend_yield_column(self):
        """测试直接从行情 DataFrame 读取 dividend_yield 列"""
        strat = HighDividendValueStrategy(buy_div_yield=5.0, sell_div_yield=3.5, ma_entry_filter=0)
        df = _create_mock_df([10.0, 10.0], div_col=[4.0, 6.2])
        sigs = strat.on_bar("20260220", {"999999.SH": df})
        assert len(sigs) == 1
        assert sigs[0].direction == "BUY"
        assert "6.20%" in sigs[0].reason

    def test_backward_compatibility_use_trend_filter(self):
        """测试向后兼容旧参数 use_trend_filter"""
        strat = HighDividendValueStrategy(use_trend_filter=0)
        assert strat.ma_entry_filter == 0
        strat2 = HighDividendValueStrategy(use_trend_filter=1)
        assert strat2.ma_entry_filter == 1


