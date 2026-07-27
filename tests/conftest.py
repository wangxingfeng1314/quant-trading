"""pytest 共享夹具"""
import sys
from pathlib import Path
import pandas as pd
import pytest

# 确保项目根目录在 sys.path 中（方便测试代码 import）
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def sample_prices() -> dict:
    """模拟 30 个交易日的价格数据（用于回测引擎测试）

    构造一条先涨后跌的价格序列：
      第1-10天: 从 100 涨到 150（上升趋势）
      第11-20天: 从 150 跌到 100（下降趋势）
      第21-30天: 从 100 涨到 130（反弹）
    """
    import numpy as np
    prices = []
    for i in range(30):
        if i < 10:
            p = 100 + i * 5              # 100 → 150
        elif i < 20:
            p = 150 - (i - 10) * 5       # 150 → 100
        else:
            p = 100 + (i - 20) * 3       # 100 → 130
        prices.append(round(p, 2))

    dates = [f"202601{i+1:02d}" for i in range(9)] + \
            [f"202601{i+10:02d}" for i in range(21)]

    df = pd.DataFrame({
        "ts_code": "999999.SZ",
        "trade_date": dates,
        "open": prices,
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": [1000000 + i * 10000 for i in range(30)],
        "amount": [p * 10000 for p in prices],
        "pct_chg": [0.0] + [((prices[i] / prices[i-1]) - 1) * 100 for i in range(1, 30)],
        "turnover": [2.0 + i * 0.1 for i in range(30)],
        "adj_factor": 1.0,
    })
    return {"999999.SZ": df}


@pytest.fixture
def sample_strategy_cls():
    """一个简单的测试策略：第5天买入，第15天卖出"""
    from core.models import Signal
    from strategies.base import BaseStrategy

    class DummyStrategy(BaseStrategy):
        name = "test_dummy"
        description = "用于测试的虚拟策略：第5天买入，第15天卖出"
        param_schema = {}

        def __init__(self, **kwargs):
            super().__init__()

        def on_bar(self, trade_date, data, portfolio=None):
            signals = []
            for ts_code, df in data.items():
                row = df.iloc[-1]
                # 第5天全仓买入（按价格约算）
                if row["trade_date"] == "20260105":
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=row["trade_date"],
                        strategy="test_dummy", direction="BUY",
                        score=1.0, price_ref=row["close"],
                        reason="模拟买入信号",
                    ))
                # 第15天全部卖出
                elif row["trade_date"] == "20260115":
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=row["trade_date"],
                        strategy="test_dummy", direction="SELL",
                        score=1.0, price_ref=row["close"],
                        reason="模拟卖出信号",
                    ))
            return signals

    return DummyStrategy


@pytest.fixture
def sample_ma_strategy_cls():
    """一个双均线交叉策略的简化版本，用于测试回测引擎"""
    from core.models import Signal
    from strategies.base import BaseStrategy

    class SimpleMACross(BaseStrategy):
        name = "simple_ma_cross"
        description = "简化版双均线交叉"
        param_schema = {"fast": 5, "slow": 20}

        def __init__(self, fast=5, slow=20):
            self.fast = fast
            self.slow = slow

        def on_bar(self, trade_date, data, portfolio=None):
            signals = []
            for ts_code, df in data.items():
                if len(df) < self.slow:
                    continue
                # 简单判断：收盘价上穿 MA5 买入，下穿卖出
                ma = df["close"].rolling(self.slow).mean()
                if len(ma) < 2:
                    continue
                row = df.iloc[-1]
                prev_row = df.iloc[-2]
                prev_ma = ma.iloc[-2]
                cur_ma = ma.iloc[-1]

                if pd.isna(prev_ma) or pd.isna(cur_ma):
                    continue

                # 上穿：前值低于MA，当前高于MA → BUY
                if prev_row["close"] <= prev_ma and row["close"] > cur_ma:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=row["trade_date"],
                        strategy="simple_ma_cross", direction="BUY",
                        score=0.8, price_ref=row["close"],
                        reason="上穿均线",
                    ))
            return signals

    return SimpleMACross
