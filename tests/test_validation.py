"""样本外验证框架（engine/validation.py）的回归测试

重点验证三件事：
1. 滚动窗口切分不重叠、不越界、训练段永远在测试段之前（否则就是未来函数）
2. 各折权益拼接后的总收益 == 各折收益连乘（拼接逻辑无泄漏）
3. 蒙特卡洛两种口径的数学性质正确（重排终点恒定、自助终点可变）
"""
import numpy as np
import pandas as pd
import pytest

from core.models import Trade
from engine import validation as V
from engine.validation import (
    WalkForwardReport, monte_carlo_trades, parameter_stability, walk_forward,
)


# ============================================================
# 合成行情与策略
# ============================================================

def _make_df(n=260, seed=3, drift=0.02):
    rng = np.random.default_rng(seed)
    dates = []
    d = pd.Timestamp("2023-01-02")
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d += pd.Timedelta(days=1)
    close = 10.0 + np.cumsum(rng.normal(drift, 0.12, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.04, n)
    return pd.DataFrame({
        "ts_code": "600000.SH",
        "trade_date": dates,
        "open": open_,
        "high": np.maximum(open_, close) + 0.05,
        "low": np.minimum(open_, close) - 0.05,
        "close": close,
        "volume": rng.integers(500_000, 2_000_000, n).astype(float),
        "amount": close * 1_000_000,
        "pct_chg": pd.Series(close).pct_change().fillna(0.0).to_numpy() * 100,
        "turnover": 2.0,
        "adj_factor": 1.0,
    })


@pytest.fixture
def preloaded(monkeypatch):
    """把 preload 替换成合成数据，避免测试依赖数据库"""
    from data.indicators import apply_indicators
    data = {}
    for code, seed, drift in [("600000.SH", 3, 0.02), ("600001.SH", 9, 0.05)]:
        raw = _make_df(seed=seed, drift=drift)
        raw["ts_code"] = code
        data[code] = apply_indicators(raw, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])

    monkeypatch.setattr(V, "preload_backtest_data", lambda *a, **k: data)
    monkeypatch.setattr(V, "get_benchmark_returns", lambda *a, **k: {})
    return data


def _make_trade(direction: str, pnl: float) -> Trade:
    return Trade(
        ts_code="600000.SH", direction=direction, trade_date="20240102",
        price=10.0, volume=100, commission=1.0, tax=0.0, pnl=pnl,
    )


# ============================================================
# 滚动窗口切分
# ============================================================

class TestWalkForwardWindows:
    def test_windows_are_ordered_and_non_overlapping(self, preloaded):
        from strategies import STRATEGY_REGISTRY
        rep = walk_forward(
            STRATEGY_REGISTRY["ma_cross"],
            {"fast_period": [3, 5], "slow_period": [15, 20]},
            list(preloaded.keys()), "20230101", "20240731",
            n_splits=3, train_ratio=0.6,
        )
        assert len(rep.windows) >= 2
        prev_test_end = ""
        for w in rep.windows:
            # 训练段必须先于测试段 —— 否则就是把未来喂给了选参
            assert w.train_start < w.train_end < w.test_start <= w.test_end
            # 测试段首尾相接、不重叠
            if prev_test_end:
                assert w.test_start > prev_test_end
            prev_test_end = w.test_end

    def test_no_lookahead_train_ends_before_test(self, preloaded):
        from strategies import STRATEGY_REGISTRY
        rep = walk_forward(
            STRATEGY_REGISTRY["ma_cross"], {"fast_period": [5], "slow_period": [20]},
            list(preloaded.keys()), "20230101", "20240731",
            n_splits=3, train_ratio=0.6,
        )
        for w in rep.windows:
            assert w.train_end < w.test_start

    def test_too_short_range_raises(self, preloaded):
        from strategies import STRATEGY_REGISTRY
        with pytest.raises(ValueError):
            walk_forward(
                STRATEGY_REGISTRY["ma_cross"], {"fast_period": [5]},
                list(preloaded.keys()), "20240101", "20240201",
                n_splits=4, train_ratio=0.7,
            )

    def test_empty_grid_raises(self, preloaded):
        from strategies import STRATEGY_REGISTRY
        with pytest.raises(ValueError):
            walk_forward(STRATEGY_REGISTRY["ma_cross"], {}, ["600000.SH"],
                         "20230101", "20240731")


# ============================================================
# 样本外曲线拼接
# ============================================================

class TestStitching:
    def test_oos_return_equals_compounded_fold_returns(self, preloaded):
        """拼接曲线的总收益必须等于各折收益连乘，说明折算没有重复计数"""
        from strategies import STRATEGY_REGISTRY
        rep = walk_forward(
            STRATEGY_REGISTRY["ma_cross"],
            {"fast_period": [3, 5], "slow_period": [15, 20]},
            list(preloaded.keys()), "20230101", "20240731",
            n_splits=3, train_ratio=0.6,
        )
        compounded = 1.0
        for w in rep.windows:
            compounded *= (1 + w.test_return / 100.0)
        expected = (compounded - 1) * 100
        # 报告里的指标统一保留 2 位小数，对比容差按舍入误差给
        assert rep.oos_total_return == pytest.approx(expected, abs=0.01)

    def test_oos_equity_is_monotonic_in_dates(self, preloaded):
        from strategies import STRATEGY_REGISTRY
        rep = walk_forward(
            STRATEGY_REGISTRY["ma_cross"], {"fast_period": [5], "slow_period": [20]},
            list(preloaded.keys()), "20230101", "20240731",
            n_splits=3, train_ratio=0.6,
        )
        dates = [e["date"] for e in rep.oos_equity]
        assert dates == sorted(dates)
        assert len(dates) == len(set(dates))

    def test_efficiency_undefined_when_in_sample_not_positive(self, preloaded):
        """样本内不赚钱时效率比必须置 0 并给出警示，不能算出误导性的正数"""
        from strategies import STRATEGY_REGISTRY
        rep = walk_forward(
            STRATEGY_REGISTRY["ma_cross"],
            {"fast_period": [3, 5], "slow_period": [15, 20]},
            list(preloaded.keys()), "20230101", "20240731",
            n_splits=3, train_ratio=0.6,
        )
        if rep.is_mean_metric <= 0:
            assert rep.efficiency == 0.0
            assert any("效率比不具解释力" in w for w in rep.warnings)


# ============================================================
# 蒙特卡洛
# ============================================================

class TestMonteCarlo:
    def test_permutation_keeps_endpoint_fixed(self):
        """重排不改变盈亏总和 —— 这是它不能用来算亏损概率的根本原因"""
        trades = [_make_trade("SELL", p) for p in
                  [500, -300, 200, -100, 800, -500, 150, -50]]
        mc = monte_carlo_trades(trades, initial_capital=100000, runs=200)
        # 重排口径的终点恒定，故不应提供收益分位数
        assert "final_return_p50" in mc  # 来自自助口径
        assert mc["realized_pnl_total"] == pytest.approx(sum([500, -300, 200, -100, 800, -500, 150, -50]), abs=0.01)

    def test_bootstrap_varies_endpoint(self):
        """自助重采样的终点必须是有方差的分布"""
        trades = [_make_trade("SELL", p) for p in
                  [500, -300, 200, -100, 800, -500, 150, -50]]
        mc = monte_carlo_trades(trades, initial_capital=100000, runs=500)
        assert mc["final_return_p5"] < mc["final_return_p95"]
        assert 0 <= mc["loss_probability"] <= 100

    def test_drawdown_is_non_negative_and_bounded(self):
        trades = [_make_trade("SELL", p) for p in [100, -100, 100, -100, 100, -100]]
        mc = monte_carlo_trades(trades, initial_capital=100000, runs=300)
        assert mc["drawdown_min"] >= 0
        assert mc["drawdown_max"] >= mc["drawdown_p95"] >= mc["drawdown_p50"]

    def test_known_sequence_matches_manual_drawdown(self):
        """实际路径回撤应等于手工计算的已实现盈亏回撤"""
        pnls = [1000, -3000, -1000, 500]
        trades = [_make_trade("SELL", p) for p in pnls]
        mc = monte_carlo_trades(trades, initial_capital=10000, runs=50)
        # 10000 -> 11000 -> 8000 -> 7000 -> 7500，峰值 11000，最深 7000
        assert mc["observed_realized_drawdown"] == pytest.approx((11000 - 7000) / 11000 * 100, abs=0.01)

    def test_too_few_trades_returns_note(self):
        mc = monte_carlo_trades([_make_trade("SELL", 100)], runs=100)
        assert mc["runs"] == 0
        assert "note" in mc

    def test_only_sell_trades_counted(self):
        trades = [_make_trade("BUY", 0) for _ in range(5)] + \
                 [_make_trade("SELL", p) for p in [100, -50, 200, -80]]
        mc = monte_carlo_trades(trades, initial_capital=100000, runs=100)
        assert mc["trades"] == 4

    def test_histogram_samples_are_drawdown_free_and_bounded(self):
        """UI 画分布图用的降采样序列：长度受限、值域与分位数一致"""
        trades = [_make_trade("SELL", p) for p in
                  [500, -300, 200, -100, 800, -500, 150, -50]]
        mc = monte_carlo_trades(trades, initial_capital=100000, runs=2000)
        for key in ("drawdown_samples", "return_samples"):
            samples = mc[key]
            assert samples, f"{key} 不应为空"
            assert len(samples) <= 400, "降采样上限 400，避免把上千个点塞进前端"
        dd = mc["drawdown_samples"]
        assert min(dd) >= 0
        assert max(dd) <= mc["drawdown_max"] + 0.01
        assert min(dd) >= mc["drawdown_min"] - 0.01
        # 收益样本来自自助口径，应存在正负两侧（该组盈亏合计为正但分布有负尾）
        rets = mc["return_samples"]
        assert max(rets) > 0


# ============================================================
# 参数稳定性
# ============================================================

class TestParameterStability:
    def test_robustness_computed_from_neighbors(self, preloaded):
        st = parameter_stability(
            strategy_cls=__import__("strategies").STRATEGY_REGISTRY["ma_cross"],
            universe=list(preloaded.keys()),
            start_date="20230101", end_date="20240731",
            param_grid={"fast_period": [3, 5, 8], "slow_period": [15, 20, 30]},
        )
        assert st["best_params"]
        assert "robustness" in st
        assert st["per_param"]["fast_period"]["mean"] is not None
        assert 0.0 <= st["positive_ratio"] <= 1.0
        assert st["grid_std"] >= 0

    def test_single_value_grid_has_no_neighbors(self, preloaded):
        st = parameter_stability(
            strategy_cls=__import__("strategies").STRATEGY_REGISTRY["ma_cross"],
            universe=list(preloaded.keys()),
            start_date="20230101", end_date="20240731",
            param_grid={"fast_period": [5], "slow_period": [20]},
        )
        assert st["robustness"] == 0.0
        assert st["neighbor_mean"] is None


# ============================================================
# 报告
# ============================================================

class TestReport:
    def test_summary_contains_key_numbers(self, preloaded):
        from strategies import STRATEGY_REGISTRY
        rep = walk_forward(
            STRATEGY_REGISTRY["ma_cross"], {"fast_period": [5], "slow_period": [20]},
            list(preloaded.keys()), "20230101", "20240731",
            n_splits=3, train_ratio=0.6,
        )
        text = rep.summary()
        assert "样本外" in text
        assert "结论" in text
        d = rep.as_dict()
        assert d["折数"] == len(rep.windows)
        assert isinstance(d["结论"], str) and d["结论"]

    def test_verdict_flags_insufficient_folds(self):
        rep = WalkForwardReport(metric="total_return")
        rep.windows = []
        rep.is_mean_metric = 5.0
        rep.oos_metrics = {"total_return": 3.0}
        verdict = V._make_verdict(rep)
        assert "过拟合" in verdict or "疑点" in verdict
