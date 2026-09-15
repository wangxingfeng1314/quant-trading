"""策略全覆盖单元测试

针对 strategies/ 目录下所有已注册策略进行元数据规范、极端边界容错和信号合规性测试。
"""
import pytest
import pandas as pd
import numpy as np

from strategies import STRATEGY_REGISTRY, get_strategy, list_strategies
from core.models import Signal
from data.indicators import apply_indicators


def test_registry_not_empty():
    """确保策略注册表成功自动发现至少 15 个策略"""
    assert len(STRATEGY_REGISTRY) >= 15


@pytest.mark.parametrize("strat_name, strat_cls", list(STRATEGY_REGISTRY.items()))
def test_strategy_metadata(strat_name, strat_cls):
    """测试策略元数据与参数规格"""
    assert strat_cls.name == strat_name
    assert isinstance(strat_cls.description, str) and len(strat_cls.description) > 0
    assert hasattr(strat_cls, "style")
    assert strat_cls.style in ["短线", "震荡", "中长线", "综合"]
    assert isinstance(strat_cls.param_schema, dict)

    # 验证实例化
    instance = strat_cls()
    assert instance.name == strat_name


@pytest.mark.parametrize("strat_name, strat_cls", list(STRATEGY_REGISTRY.items()))
def test_strategy_empty_and_corrupt_data_tolerance(strat_name, strat_cls):
    """测试策略对空数据、单行数据、缺失指标数据的容错性"""
    strat = strat_cls()

    # 1. 完全空字典
    signals = strat.on_bar("20240105", {}, None)
    assert isinstance(signals, list)

    # 2. 包含空 DataFrame
    signals = strat.on_bar("20240105", {"000001.SZ": pd.DataFrame()}, None)
    assert isinstance(signals, list)

    # 3. 仅有单行数据（历史不足）
    single_row_df = pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": "20240105",
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "vol": 10000.0, "amount": 102000.0
    }])
    signals = strat.on_bar("20240105", {"000001.SZ": single_row_df}, None)
    assert isinstance(signals, list)

    # 4. 全 NaN 列数据
    nan_df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 10,
        "trade_date": [f"202401{i:02d}" for i in range(1, 11)],
        "open": [np.nan] * 10,
        "high": [np.nan] * 10,
        "low": [np.nan] * 10,
        "close": [np.nan] * 10,
        "vol": [np.nan] * 10,
        "amount": [np.nan] * 10,
    })
    signals = strat.on_bar("20240110", {"000001.SZ": nan_df}, None)
    assert isinstance(signals, list)


@pytest.mark.parametrize("strat_name, strat_cls", list(STRATEGY_REGISTRY.items()))
def test_strategy_signal_contract_with_synthetic_data(strat_name, strat_cls):
    """测试在具备完整指标的典型数据下运行，验证返回信号的格式与契约"""
    # 构造 100 天上升金叉行情的模拟数据
    dates = pd.date_range("2024-01-01", periods=100, freq="B").strftime("%Y%m%d").tolist()
    np.random.seed(42)
    base_price = 10.0 + np.cumsum(np.random.normal(0.05, 0.2, 100))
    base_price = np.maximum(base_price, 1.0)

    v_series = np.random.uniform(50000, 200000, 100)
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 100,
        "trade_date": dates,
        "open": base_price * 0.99,
        "high": base_price * 1.02,
        "low": base_price * 0.98,
        "close": base_price,
        "volume": v_series,
        "vol": v_series,
        "amount": base_price * v_series,
    })

    # 计算指标
    df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])

    strat = strat_cls()
    # 依次在第 50 天和第 99 天触发测试
    for test_idx in [50, 99]:
        test_date = dates[test_idx]
        data_slice = {"000001.SZ": df.iloc[:test_idx + 1]}
        signals = strat.on_bar(test_date, data_slice, None)

        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)
            assert sig.ts_code == "000001.SZ"
            assert sig.direction in ["BUY", "SELL"]
            assert 0.0 <= sig.score <= 1.0
            assert sig.price_ref > 0
            assert isinstance(sig.reason, str) and len(sig.reason) > 0
