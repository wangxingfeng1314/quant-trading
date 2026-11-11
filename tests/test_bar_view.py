"""列式行情视图（engine/bar_view.py）与回测快路径的等价性回归

核心断言只有一条：**同一次回测，开不开列式视图，结果必须逐位一致**。
只要这条守住，性能优化就没有污染任何回测结论。
"""
import numpy as np
import pandas as pd
import pytest

from engine.bar_view import BarFrame, ColumnView, FrameStore


# ============================================================
# 构造合成行情（不依赖数据库）
# ============================================================

def _make_raw(n: int = 220, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = []
    d = 20240101
    while len(dates) < n:
        # 跳过周末，仅用于生成连续交易日序列
        weekday = pd.Timestamp(str(d)).weekday()
        if weekday < 5:
            dates.append(str(d))
        d = (pd.Timestamp(str(d)) + pd.Timedelta(days=1)).strftime("%Y%m%d")
        d = int(d)

    close = 10.0 + np.cumsum(rng.normal(0.01, 0.15, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.05, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.08, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.08, n))
    return pd.DataFrame({
        "ts_code": "600000.SH",
        "trade_date": dates,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(500_000, 3_000_000, n).astype(float),
        "amount": close * 1_000_000,
        "pct_chg": pd.Series(close).pct_change().fillna(0.0).to_numpy() * 100,
        "turnover": rng.uniform(0.5, 5.0, n),
        "adj_factor": 1.0,
    })


@pytest.fixture
def pricedf() -> pd.DataFrame:
    """带完整指标列的合成行情"""
    from data.indicators import apply_indicators
    df = _make_raw()
    return apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])


# ============================================================
# BarFrame / ColumnView 语义
# ============================================================

class TestBarFrameSemantics:
    def test_column_tail_matches_dataframe(self, pricedf):
        store = FrameStore(pricedf)
        for stop in (50, 120, len(pricedf)):
            bf = BarFrame(store, 0, stop)
            sl = pricedf.iloc[:stop]
            for col in ("close", "open", "volume", "ma20", "rsi14"):
                assert bf[col].iloc[-1] == sl[col].iloc[-1]
                assert bf[col].iloc[-2] == sl[col].iloc[-2]
                assert bf[col].iloc[-1] == pytest.approx(sl[col].to_numpy()[-1])

    def test_len_and_columns(self, pricedf):
        store = FrameStore(pricedf)
        bf = BarFrame(store, 10, 60)
        assert len(bf) == 50
        assert list(bf.columns) == list(pricedf.columns)
        assert "close" in bf
        assert "not_a_column" not in bf

    def test_negative_slice_matches_pandas(self, pricedf):
        """策略里 df["close"].iloc[-22:] 这类尾部切片必须与 pandas 一致"""
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 150)
        sl = pricedf.iloc[:150]
        got = bf["close"].iloc[-22:]
        want = sl["close"].iloc[-22:]
        assert isinstance(got, pd.Series)
        assert list(got.to_numpy()) == list(want.to_numpy())

    def test_reverse_slice_matches_pandas(self, pricedf):
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 20)
        assert list(bf["close"].iloc[::-1].to_numpy()) == \
               list(pricedf["close"].iloc[:20].iloc[::-1].to_numpy())

    def test_rolling_matches_pandas(self, pricedf):
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 100)
        got = bf["close"].iloc[-22:].rolling(window=20, min_periods=20).mean().iloc[-1]
        want = pricedf["close"].iloc[:100].iloc[-22:].rolling(window=20, min_periods=20).mean().iloc[-1]
        assert got == pytest.approx(want)

    def test_row_slice_returns_barframe(self, pricedf):
        """df.iloc[:-1] 仍应保持快路径，且长度语义正确"""
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 100)
        head = bf.iloc[:-1]
        assert isinstance(head, BarFrame)
        assert len(head) == 99
        assert head["close"].iloc[-1] == pricedf["close"].iloc[98]

    def test_single_row_falls_back_to_series(self, pricedf):
        """df.iloc[-1] 需要整行，退回真实 Series，行为必须与 pandas 一致"""
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 100)
        row = bf.iloc[-1]
        assert isinstance(row, pd.Series)
        assert row["close"] == pricedf.iloc[99]["close"]

    def test_unknown_attribute_falls_back(self, pricedf):
        """未实现的接口自动退回真实 DataFrame —— 这是第三方策略的安全底线"""
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 30)
        assert bf.shape == (30, len(pricedf.columns))
        assert bf.empty is False
        assert list(bf.index) == list(range(30))
        assert list(bf["close"].values) == list(pricedf["close"].iloc[:30].to_numpy())
        assert bf["close"].dtype == pricedf["close"].dtype

    def test_column_view_does_not_copy(self, pricedf):
        """列视图应直接引用底层数组，不做拷贝"""
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 50)
        arr = bf["close"].to_numpy()
        assert np.shares_memory(arr, pricedf["close"].to_numpy())

    def test_out_of_bounds_raises(self, pricedf):
        store = FrameStore(pricedf)
        bf = BarFrame(store, 0, 3)
        with pytest.raises(IndexError):
            _ = bf["close"].iloc[-4]


# ============================================================
# 快慢路径对拍 —— 整个优化的验收线
# ============================================================

class TestFastPathEquivalence:
    """开启列式视图与原生 DataFrame 路径，回测结果必须完全一致"""

    def _run(self, monkeypatch, fast: bool, data: dict):
        import engine.backtester as btmod
        from engine.backtester import Backtester
        from strategies import STRATEGY_REGISTRY

        monkeypatch.setattr(btmod, "BACKTEST_FAST_BAR_VIEW", fast)
        btmod.clear_backtester_cache()
        r = Backtester(
            strategy_cls=STRATEGY_REGISTRY["ma_cross"], params={},
            universe=list(data.keys()),
            start_date="20240201", end_date="20240731",
            initial_capital=100000, execution_mode="next_open",
            preloaded_data=data,
        ).run(save=False)
        return r

    @pytest.mark.parametrize("strategy", ["ma_cross", "kdj_cross",
                                          "bollinger_reversal", "signal_combo"])
    def test_all_builtin_strategies_identical(self, monkeypatch, pricedf, strategy):
        import engine.backtester as btmod
        from engine.backtester import Backtester
        from strategies import STRATEGY_REGISTRY

        data = {"600000.SH": pricedf, "600001.SH": _make_raw(seed=11)}

        results = {}
        for fast in (False, True):
            monkeypatch.setattr(btmod, "BACKTEST_FAST_BAR_VIEW", fast)
            btmod.clear_backtester_cache()
            results[fast] = Backtester(
                strategy_cls=STRATEGY_REGISTRY[strategy], params={},
                universe=list(data.keys()),
                start_date="20240201", end_date="20240731",
                initial_capital=100000, execution_mode="next_open",
                preloaded_data=data,
            ).run(save=False)

        slow, fast = results[False], results[True]
        assert slow.trade_count == fast.trade_count
        assert slow.total_return == pytest.approx(fast.total_return, abs=1e-9)
        assert slow.max_drawdown == pytest.approx(fast.max_drawdown, abs=1e-9)
        assert slow.sharpe_ratio == pytest.approx(fast.sharpe_ratio, abs=1e-9)
        assert [e["equity"] for e in slow.equity_curve] == \
               pytest.approx([e["equity"] for e in fast.equity_curve], abs=1e-9)
        # 逐笔成交明细也必须一致
        assert len(slow.trades) == len(fast.trades)
        for a, b in zip(slow.trades, fast.trades):
            assert (a.ts_code, a.trade_date, a.direction, a.volume) == \
                   (b.ts_code, b.trade_date, b.direction, b.volume)
            assert a.price == pytest.approx(b.price, abs=1e-9)
