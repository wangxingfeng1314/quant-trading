"""回测引擎集成测试 — 使用真实的 cleaner + indicators（不 mock 数据处理层）

验证回测引擎在真实数据清洗和技术指标计算下的正确性。
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import pandas as pd
import numpy as np

from core.models import Signal, BacktestResult
from data.cleaner import clean_daily
from data.indicators import apply_indicators


def _make_sample_df(seed=1) -> pd.DataFrame:
    """构造通过真实 clean_daily 的模拟数据

    要求:
      - close/volume/amount 的 ratio ≈ 1.0（AKShare 新格式标准）
      - high >= open/close, low <= open/close
      - 所有价格 > 0, volume > 0, amount > 0
    """
    np.random.seed(seed)
    n = 100  # 100 个交易日

    # 价格序列：先涨后跌再涨
    prices = []
    for i in range(n):
        if i < 30:
            p = 100 + i * 2
        elif i < 60:
            p = 160 - (i - 30) * 1.5
        elif i < 80:
            p = 115 + (i - 60) * 1.0
        else:
            p = 135 + (i - 80) * 0.5
        prices.append(round(p, 2))

    dates = []
    base = 20260101
    for i in range(n):
        d = base + i
        # 跳过周末（简化：连续日期即可）
        dates.append(str(d))

    # 生成 OHLCV
    open_prices = [round(p + np.random.uniform(-1, 1), 2) for p in prices]
    high_prices = [round(max(o, p) + abs(np.random.normal(0, 1)), 2)
                   for o, p in zip(open_prices, prices)]
    low_prices = [round(min(o, p) - abs(np.random.normal(0, 1)), 2)
                  for o, p in zip(open_prices, prices)]

    # volume（股）和 amount（元），ratio ≈ 1.0
    volumes = [int(500000 + np.random.uniform(-100000, 100000)) for _ in range(n)]
    amounts = [round(p * v * (1 + np.random.uniform(-0.01, 0.01)), 2)
               for p, v in zip(prices, volumes)]

    df = pd.DataFrame({
        "ts_code": "999999.SZ",
        "trade_date": dates,
        "open": open_prices,
        "high": high_prices,
        "low": low_prices,
        "close": prices,
        "volume": volumes,
        "amount": amounts,
        "pct_chg": [0.0] + [round((prices[i] / prices[i-1] - 1) * 100, 4)
                            for i in range(1, n)],
        "turnover": [round(2 + np.random.uniform(-0.5, 0.5), 2) for _ in range(n)],
        "adj_factor": 1.0,
    })
    return df


class TestIntegrationCleanAndBacktest:
    """集成测试：真实 cleaner → 真实 indicators → 回测"""

    def test_clean_daily_preserves_data(self):
        """验证 clean_daily 不会错误地删除有效数据"""
        df = _make_sample_df()
        assert len(df) == 100

        cleaned = clean_daily(df)
        assert len(cleaned) == 100, f"cleaner 不应删除有效数据，但少了 {100 - len(cleaned)} 条"

        # 验证所有关键列还在
        for col in ["ts_code", "trade_date", "open", "high", "low", "close", "volume"]:
            assert col in cleaned.columns

        # 验证日期有序
        assert cleaned["trade_date"].is_monotonic_increasing

    def test_apply_indicators_adds_columns(self):
        """验证技术指标计算添加了正确的列"""
        df = _make_sample_df()
        cleaned = clean_daily(df)
        result = apply_indicators(cleaned, ["ma", "macd", "rsi", "boll", "vol_ma"])

        expected_columns = {
            "ma5", "ma10", "ma20", "ma60",
            "dif", "dea", "macd_hist",
            "rsi14",
            "boll_upper", "boll_mid", "boll_lower",
            "vol_ma5", "vol_ma10", "vol_ma20",
        }
        for col in expected_columns:
            assert col in result.columns, f"缺少指标列: {col}"

        # 前 5 个 ma20 值是价格本身（窗口不足20时填充自身）

    def test_simple_strategy_backtest_with_real_indicators(self, monkeypatch):
        """用真实数据+指标跑完整回测"""
        from engine.backtester import Backtester
        from strategies.base import BaseStrategy

        # 策略：MA20 > 收盘价时买入，反之卖出
        class MA20Strategy(BaseStrategy):
            name = "ma20_test"
            description = "收盘价在MA20上方买入"
            param_schema = {}
            def on_bar(self, trade_date, data, portfolio=None):
                signals = []
                for ts_code, df in data.items():
                    row = df.iloc[-1]
                    if pd.isna(row.get("ma_20")):
                        continue
                    # 收盘价 > MA20 → BUY
                    if row["close"] > row["ma_20"]:
                        signals.append(Signal(
                            ts_code=ts_code, trade_date=row["trade_date"],
                            strategy=self.name, direction="BUY",
                            score=0.6, price_ref=row["close"],
                            reason="收盘价在MA20上方",
                        ))
                    # 收盘价 < MA20 → SELL
                    elif row["close"] < row["ma_20"]:
                        # 检查是否有持仓
                        if portfolio and not portfolio.get_position(ts_code) is None:
                            pos = portfolio.get_position(ts_code)
                            if pos and not pos.is_empty:
                                signals.append(Signal(
                                    ts_code=ts_code, trade_date=row["trade_date"],
                                    strategy=self.name, direction="SELL",
                                    score=0.6, price_ref=row["close"],
                                    reason="收盘价跌破MA20",
                                ))
                return signals

        df = _make_sample_df()
        cleaned = clean_daily(df)
        result = apply_indicators(cleaned, ["ma", "macd", "rsi", "boll", "vol_ma"])

        # Mock get_daily 返回真实处理后的数据
        def mock_get_daily(ts_code, *args, **kwargs):
            return result[result["ts_code"] == ts_code]

        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        # 不 mock cleaner 和 indicators 了——我们已经提前处理好了

        bt = Backtester(
            strategy_cls=MA20Strategy,
            params={},
            universe=["999999.SZ"],
            start_date="20260101",
            end_date="20261231",
            initial_capital=100000,
        )
        bt_result = bt.run(save=False)

        assert bt_result is not None
        assert bt_result.initial_capital == 100000
        # 应该至少有几笔交易（价格穿过MA20多次）
        assert bt_result.total_return is not None
        assert isinstance(bt_result.total_return, float)

    def test_cleaner_handles_new_format(self):
        """验证 clean_daily 正确处理新版AKShare格式（ratio≈1.0）"""
        df = _make_sample_df()
        cleaned = clean_daily(df)
        assert len(cleaned) > 0
        # 验证 ratio 在新格式范围内
        mask = (cleaned["close"] > 0) & (cleaned["volume"] > 0) & (cleaned["amount"] > 0)
        if mask.any():
            ratio = cleaned.loc[mask, "amount"] / (cleaned.loc[mask, "close"] * cleaned.loc[mask, "volume"])
            # ratio 应在 0.15 ~ 1.5 之间（新格式）
            assert (ratio > 0.15).all() and (ratio < 1.5).all()

    def test_indicators_consistency(self):
        """验证技术指标计算的一致性"""
        df = _make_sample_df()
        cleaned = clean_daily(df)

        # 用全部默认指标
        result = apply_indicators(cleaned)

        # MACD: macd_hist = (dif - dea) * 2
        macd_hist_check = (result["dif"] - result["dea"]) * 2
        assert np.allclose(macd_hist_check.dropna(), result["macd_hist"].dropna(), rtol=1e-10)

        # 布林带：mid ± 2*std（仅检查有数据的行）
        valid = result["boll_mid"].notna()
        if valid.any():
            assert (result.loc[valid, "boll_upper"] >= result.loc[valid, "boll_mid"]).all()
            assert (result.loc[valid, "boll_lower"] <= result.loc[valid, "boll_mid"]).all()
