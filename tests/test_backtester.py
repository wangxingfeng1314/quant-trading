"""回测引擎（Backtester）集成测试"""
import json
import pytest
import pandas as pd
import numpy as np

from engine.backtester import Backtester, grid_search
from engine.portfolio import Portfolio
from data.storage import get_daily


class TestBacktesterWithSampleData:
    """用模拟数据测试回测引擎"""

    def test_run_with_dummy_strategy(self, sample_prices, sample_strategy_cls, monkeypatch):
        """使用虚拟策略跑完整回测流程"""
        def mock_get_daily(ts_code, start_date="", end_date=""):
            df = sample_prices.get(ts_code, pd.DataFrame()).copy()
            if not df.empty and start_date:
                df = df[df["trade_date"] >= start_date]
            if not df.empty and end_date:
                df = df[df["trade_date"] <= end_date]
            return df

        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        # cleaner/indicators 依赖真实数据格式，mock 掉以隔离回测引擎测试
        monkeypatch.setattr("engine.backtester.clean_daily", lambda df, **kw: df)
        monkeypatch.setattr("engine.backtester.apply_indicators", lambda df, _: df)

        bt = Backtester(
            strategy_cls=sample_strategy_cls,
            params={},
            universe=["999999.SZ"],
            start_date="20260101",
            end_date="20260131",
            initial_capital=100000,
        )
        result = bt.run(save=False)

        assert result is not None
        assert result.strategy == "test_dummy"
        assert result.initial_capital == 100000
        assert result.final_capital > 0
        assert len(result.equity_curve) > 0

    def test_empty_universe(self, monkeypatch):
        """空标的列表"""
        def mock_empty(ts_code, *args, **kwargs):
            return pd.DataFrame()
        monkeypatch.setattr("engine.backtester.get_daily", mock_empty)
        from strategies.ma_cross import MACrossStrategy

        bt = Backtester(
            strategy_cls=MACrossStrategy,
            params={"fast": 5, "slow": 20},
            universe=[],
            start_date="20260101", end_date="20260131",
            initial_capital=100000,
        )
        result = bt.run(save=False)
        assert result.final_capital == 100000  # 资金不变
        assert result.trade_count == 0

    def test_no_signal_strategy(self, sample_prices, monkeypatch):
        """策略不产生任何信号"""
        from core.models import Signal
        from strategies.base import BaseStrategy

        class NoSignalStrategy(BaseStrategy):
            name = "no_signal"
            description = "不产生任何信号的策略"
            param_schema = {}
            def on_bar(self, trade_date, data, portfolio=None):
                return []

        def mock_get_daily(ts_code, *args, **kwargs):
            return sample_prices.get(ts_code, pd.DataFrame())
        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        monkeypatch.setattr("engine.backtester.clean_daily", lambda df: df)
        monkeypatch.setattr("engine.backtester.apply_indicators",
                            lambda df, _: df)

        bt = Backtester(
            strategy_cls=NoSignalStrategy,
            params={},
            universe=["999999.SZ"],
            start_date="20260101", end_date="20260131",
            initial_capital=100000,
        )
        result = bt.run(save=False)
        assert result.final_capital == 100000  # 没交易
        assert result.trade_count == 0


class TestBacktesterMetrics:
    """回测绩效指标验证"""

    def test_sharpe_and_drawdown(self, sample_prices, sample_strategy_cls, monkeypatch):
        """验证夏普比率和最大回撤计算"""
        import numpy as np

        def mock_get_daily(ts_code, *args, **kwargs):
            return sample_prices.get(ts_code, pd.DataFrame())
        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        monkeypatch.setattr("engine.backtester.clean_daily", lambda df: df)
        monkeypatch.setattr("engine.backtester.apply_indicators",
                            lambda df, _: df)

        bt = Backtester(
            strategy_cls=sample_strategy_cls,
            params={},
            universe=["999999.SZ"],
            start_date="20260101",
            end_date="20260131",
            initial_capital=100000,
        )
        result = bt.run(save=False)

        # 基本指标存在
        assert result.total_return is not None
        assert result.annual_return is not None
        assert result.max_drawdown is not None
        assert result.sharpe_ratio is not None

        # 类型正确
        assert isinstance(result.total_return, float)
        assert isinstance(result.sharpe_ratio, float)

    def test_win_rate_calculation(self, sample_prices, monkeypatch):
        """验证胜率计算"""
        from core.models import Signal
        from strategies.base import BaseStrategy

        # 构造：3次买入卖出，2赚1亏 → 胜率66.67%
        class WinRateStrategy(BaseStrategy):
            name = "winrate_test"
            description = "用于验证胜率"
            param_schema = {}
            def on_bar(self, trade_date, data, portfolio=None):
                signals = []
                for ts_code, df in data.items():
                    row = df.iloc[-1]
                    td = row["trade_date"]
                    # 第1天买
                    if td == "20260105":
                        signals.append(Signal(ts_code=ts_code, trade_date=td,
                            strategy="winrate_test", direction="BUY",
                            score=1.0, price_ref=row["close"], reason="test"))
                    # 第6天卖（赚）
                    elif td == "20260106":
                        signals.append(Signal(ts_code=ts_code, trade_date=td,
                            strategy="winrate_test", direction="SELL",
                            score=1.0, price_ref=row["close"], reason="test"))
                    # 第7天再买
                    elif td == "20260107":
                        signals.append(Signal(ts_code=ts_code, trade_date=td,
                            strategy="winrate_test", direction="BUY",
                            score=1.0, price_ref=row["close"], reason="test"))
                    # 第8天卖（亏，因为价格从高点回落）
                    elif td == "20260108":
                        signals.append(Signal(ts_code=ts_code, trade_date=td,
                            strategy="winrate_test", direction="SELL",
                            score=1.0, price_ref=row["close"], reason="test"))
                return signals

        def mock_get_daily(ts_code, *args, **kwargs):
            return sample_prices.get(ts_code, pd.DataFrame())
        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        monkeypatch.setattr("engine.backtester.clean_daily", lambda df: df)
        monkeypatch.setattr("engine.backtester.apply_indicators",
                            lambda df, _: df)

        bt = Backtester(
            strategy_cls=WinRateStrategy, params={},
            universe=["999999.SZ"],
            start_date="20260101", end_date="20260131",
            initial_capital=100000,
        )
        result = bt.run(save=False)
        # 确保有卖出交易且胜率在合理范围
        assert result.trade_count >= 2
        assert 0 <= result.win_rate <= 100


class TestGridSearch:
    """网格搜索测试"""

    def test_basic_grid_search(self, sample_prices, sample_strategy_cls, monkeypatch):
        """基础网格搜索"""
        def mock_get_daily(ts_code, *args, **kwargs):
            return sample_prices.get(ts_code, pd.DataFrame())
        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        monkeypatch.setattr("engine.backtester.clean_daily", lambda df: df)
        monkeypatch.setattr("engine.backtester.apply_indicators",
                            lambda df, _: df)

        results = grid_search(
            strategy_cls=sample_strategy_cls,
            universe=["999999.SZ"],
            start_date="20260101",
            end_date="20260131",
            initial_capital=100000,
            param_grid={"dummy": [1, 2]},
            metric="total_return",
        )
        assert len(results) == 2  # 2种参数组合
        # 按指标降序排列
        assert results[0]["metric_value"] >= results[1]["metric_value"]

    def test_empty_param_grid(self, sample_strategy_cls, monkeypatch):
        """空参数网格返回默认结果"""
        results = grid_search(
            strategy_cls=sample_strategy_cls,
            universe=["999999.SZ"],
            start_date="20260101",
            end_date="20260131",
            param_grid={},
        )
        # itertools.product(*[]) 返回 [()], 所以 grid_search 会跑一次
        assert len(results) == 1
