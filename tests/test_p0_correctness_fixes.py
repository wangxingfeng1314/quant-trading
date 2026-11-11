"""P0 级回测正确性缺陷的回归测试

覆盖四个已验证缺陷：
  1. 停牌日持仓市值被按 0 计（虚假回撤）
  2. 保本单判定使用固定 15 元绝对阈值（与资金规模脱钩）
  3. 总收益率分母误用「首日收盘后权益」而非初始资金
  4. Alpha/Beta 用尾部截断对齐基准，交易日缺日即错位

以及前复权口径下不得重复叠加现金分红的约束。
"""
import numpy as np
import pandas as pd
import pytest

from core.models import Trade
from engine.portfolio import Portfolio


# ============================================================
# 缺陷 1：停牌日持仓市值归零
# ============================================================

class TestSuspendedHoldingValuation:
    """停牌日必须沿用最近一次已知收盘价，而不是把市值按 0 计"""

    def test_market_value_carries_forward_last_price(self):
        p = Portfolio(100000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000, trade_date="20260701", slippage=False)

        # 首日有行情，记录最新价
        assert p.market_value({"000001.SZ": 10.0}) == 10000.0

        # 次日停牌：prices 中没有该标的，市值应保持上一日的 10000 而不是 0
        assert p.market_value({}) == 10000.0

        # 再停牌一天，仍然保持（且不因空 prices 而清零）
        assert p.market_value({}) == 10000.0

        # 复牌后按新价更新
        assert p.market_value({"000001.SZ": 12.0}) == 12000.0

    def test_total_equity_stable_across_suspension(self):
        p = Portfolio(100000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000, trade_date="20260701", slippage=False)
        eq_before = p.total_equity({"000001.SZ": 10.0})
        eq_suspended = p.total_equity({})
        assert abs(eq_suspended - eq_before) < 1e-6

    def test_never_priced_position_falls_back_to_cost(self):
        """从未拿到任何行情的持仓，用成本兜底，绝不按 0 计价"""
        p = Portfolio(100000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000, trade_date="20260701", slippage=False)
        mv = p.market_value({})
        # avg_cost 含买入手续费，故略高于 10000，但绝不能为 0
        assert mv == pytest.approx(10000.0, abs=20.0)
        assert mv > 0

    def test_no_fake_drawdown_when_one_of_two_holdings_suspended(self, monkeypatch):
        """两只持仓中有一只停牌，回撤不应被虚假放大"""
        dates_all = [f"202601{d:02d}" for d in range(1, 21)]
        dates_suspended = [d for d in dates_all if d != "20260110"]

        def make_df(code, price, dates):
            return pd.DataFrame({
                "ts_code": code,
                "trade_date": dates,
                "open": [price] * len(dates),
                "high": [price] * len(dates),
                "low": [price] * len(dates),
                "close": [price] * len(dates),
                "volume": [1_000_000] * len(dates),
                "amount": [price * 1_000_000] * len(dates),
            })

        frames = {
            "999001.SZ": make_df("999001.SZ", 10.0, dates_suspended),  # 停牌一天
            "999002.SZ": make_df("999002.SZ", 20.0, dates_all),        # 全程有行情
        }

        def mock_get_daily(ts_code, start_date="", end_date=""):
            return frames.get(ts_code, pd.DataFrame()).copy()

        monkeypatch.setattr("engine.backtester.get_daily", mock_get_daily)
        monkeypatch.setattr("engine.backtester.clean_daily", lambda df, **kw: df)
        monkeypatch.setattr("engine.backtester.apply_indicators", lambda df, _: df)

        from core.models import Signal
        from strategies.base import BaseStrategy

        class BuyOnceHold(BaseStrategy):
            name = "buy_once_hold"
            description = "首日买入 999001 后持有不动"
            param_schema = {}

            def on_bar(self, trade_date, data, portfolio=None):
                if trade_date != "20260101":
                    return []
                # 仅买 999001（会在 0110 停牌的那只）
                return [Signal(ts_code="999001.SZ", trade_date=trade_date,
                               strategy=self.name, direction="BUY",
                               score=1.0, price_ref=10.0, reason="建仓")]

        from engine.backtester import Backtester
        bt = Backtester(
            strategy_cls=BuyOnceHold, params={},
            universe=["999001.SZ", "999002.SZ"],
            start_date="20260101", end_date="20260120",
            initial_capital=100000.0,
            execution_mode="current_close",
        )
        result = bt.run(save=False)

        assert result.trade_count >= 1, "应当成功建仓"
        # 价格全程不变，最大回撤应接近 0；旧实现会出现约 10% 的假回撤
        assert result.max_drawdown < 1.0, (
            f"停牌日不应产生虚假回撤，实际最大回撤 {result.max_drawdown}%"
        )
        equities = [e["equity"] for e in result.equity_curve]
        # 建仓后的净值应全程持平（允许手续费造成的首日小幅下移）
        assert max(equities) - min(equities) < 500


# ============================================================
# 缺陷 2：保本单判定阈值必须与成交金额挂钩
# ============================================================

class TestBreakEvenThreshold:
    """保本判定必须按「盈亏 / 成交额」比例，而非固定元数"""

    @staticmethod
    def _portfolio_with_sells(sells):
        p = Portfolio(1_000_000)
        p.equity_curve = [
            {"date": "20260101", "equity": 1_000_000, "cash": 1_000_000, "market_value": 0},
            {"date": "20260102", "equity": 1_000_000, "cash": 1_000_000, "market_value": 0},
        ]
        for price, volume, pnl in sells:
            p.trades.append(Trade(
                ts_code="000001.SZ", direction="SELL", trade_date="20260102",
                price=price, volume=volume, pnl=pnl,
            ))
        return p

    def test_same_pnl_amount_judged_by_notional(self):
        """同为 200 元盈亏：占成交额 0.04% 算保本，占 2% 不算"""
        # 成交额 500,000 → 200/500000 = 0.04% → 保本
        big = self._portfolio_with_sells([(100.0, 5000, 200.0)])
        assert big.calc_metrics()["break_even_count"] == 1

        # 成交额 10,000 → 200/10000 = 2% → 正常盈利，不是保本
        small = self._portfolio_with_sells([(10.0, 1000, 200.0)])
        assert small.calc_metrics()["break_even_count"] == 0

    def test_threshold_is_not_a_fixed_absolute_amount(self):
        """旧实现 abs(pnl) <= 15.0 会把 1000 万资金的 12 元盈亏判成保本 —— 新实现不会"""
        # 成交额 12,000,000，盈亏 12 元 = 0.0001% → 实为保本（比例判据下仍算保本）
        p = self._portfolio_with_sells([(120.0, 100000, 12.0)])
        # 成交额 12,000，盈亏 12 元 = 0.1% → 恰在阈值上，算保本
        q = self._portfolio_with_sells([(12.0, 1000, 12.0)])
        # 成交额 1,200，盈亏 12 元 = 1% → 明显是盈利单，不是保本
        r = self._portfolio_with_sells([(12.0, 100, 12.0)])
        assert p.calc_metrics()["break_even_count"] == 1
        assert q.calc_metrics()["break_even_count"] == 1
        assert r.calc_metrics()["break_even_count"] == 0

    def test_risk_manager_break_even_stop_still_counted(self):
        """风控模块标记的保本平仓仍应计入，不受比例判据影响"""
        p = Portfolio(1_000_000)
        p.equity_curve = [
            {"date": "20260101", "equity": 1_000_000, "cash": 1_000_000, "market_value": 0},
            {"date": "20260102", "equity": 1_000_000, "cash": 1_000_000, "market_value": 0},
        ]
        p.trades.append(Trade(
            ts_code="000001.SZ", direction="SELL", trade_date="20260102",
            price=10.0, volume=1000, pnl=800.0,  # 8% 盈利，但风控标记为保本平仓
            context_snapshot={"risk_type": "break_even_stop"},
        ))
        assert p.calc_metrics()["break_even_count"] == 1


# ============================================================
# 缺陷 3：总收益率分母
# ============================================================

class TestReturnBaseline:
    """总收益率必须以初始资金为分母"""

    def test_total_return_uses_initial_capital_not_first_equity(self):
        p = Portfolio(100000, )
        # 首日因手续费权益已跌到 99800；期末回到 100000
        p.equity_curve = [
            {"date": "20260101", "equity": 99800, "cash": 99800, "market_value": 0},
            {"date": "20260102", "equity": 100000, "cash": 100000, "market_value": 0},
        ]
        metrics = p.calc_metrics()
        # 相对初始资金 100000 → 0%；旧实现相对 99800 → +0.20%
        assert metrics["total_return"] == pytest.approx(0.0, abs=0.01)

    def test_total_return_after_loss(self):
        p = Portfolio(100000)
        p.equity_curve = [
            {"date": "20260101", "equity": 99500, "cash": 99500, "market_value": 0},
            {"date": "20260102", "equity": 90000, "cash": 90000, "market_value": 0},
        ]
        assert p.calc_metrics()["total_return"] == pytest.approx(-10.0, abs=0.01)


# ============================================================
# 缺陷 4：Alpha/Beta 日期对齐
# ============================================================

class TestBenchmarkAlignment:
    """基准收益必须按日期精确 join，不能用尾部截断"""

    @staticmethod
    def _curve(dates, equities):
        return [{"date": d, "equity": e, "cash": e, "market_value": 0}
                for d, e in zip(dates, equities)]

    def test_beta_is_one_when_strategy_tracks_benchmark_with_gaps(self):
        """组合收益与基准完全一致（但组合缺了中间某日），Beta 应为 1"""
        dates = ["20260105", "20260106", "20260107", "20260109"]  # 缺 0108
        equities = [100000, 101000, 99990, 100990]
        p = Portfolio(100000)
        p.equity_curve = self._curve(dates, equities)

        # 按日期给出基准收益，仅对组合存在的日期比对
        rets = [equities[i + 1] / equities[i] - 1 for i in range(len(equities) - 1)]
        bench = {dates[i + 1]: r for i, r in enumerate(rets)}

        metrics = p.calc_metrics(benchmark_returns=bench)
        assert metrics["beta"] == pytest.approx(1.0, abs=1e-6)
        assert metrics["alpha"] == pytest.approx(0.0, abs=1e-6)

    def test_misaligned_benchmark_does_not_produce_beta(self):
        """基准日期完全对不上时，应优雅退化为 0，而不是算出一个假 Beta"""
        p = Portfolio(100000)
        p.equity_curve = self._curve(
            ["20260105", "20260106", "20260107"], [100000, 101000, 100000]
        )
        bench = {"20250101": 0.01, "20250102": -0.02, "20250103": 0.03}
        metrics = p.calc_metrics(benchmark_returns=bench)
        assert metrics["beta"] == 0.0
        assert metrics["alpha"] == 0.0

    def test_list_style_benchmark_still_supported(self):
        """旧式序列入参保持向后兼容（尾部等长截断）"""
        p = Portfolio(100000)
        p.equity_curve = self._curve(
            ["20260105", "20260106", "20260107"], [100000, 101000, 100000]
        )
        metrics = p.calc_metrics(benchmark_returns=[0.01, -0.00990099])
        assert isinstance(metrics["beta"], float)
        assert metrics["beta"] != 0.0

    def test_beta_estimator_not_inflated_by_ddof_mismatch(self):
        """Beta 不得被 n/(n-1) 放大

        np.cov 默认 ddof=1、np.var 默认 ddof=0，两者混用会让 Beta 系统性偏大。
        组合收益与基准完全一致时，Beta 必须精确等于 1。
        """
        dates = ["20260101", "20260102", "20260103", "20260104", "20260105"]
        equities = [100000, 100500, 100200, 100800, 100400]
        p = Portfolio(100000)
        p.equity_curve = self._curve(dates, equities)
        rets = [equities[i + 1] / equities[i] - 1 for i in range(4)]
        bench = {dates[i + 1]: rets[i] for i in range(4)}

        metrics = p.calc_metrics(benchmark_returns=bench)
        # 旧实现会得到 4/3 ≈ 1.33
        assert metrics["beta"] == pytest.approx(1.0, abs=1e-9)


# ============================================================
# 前复权口径：不得重复叠加现金分红
# ============================================================

class TestAdjustedPriceNoDoubleCounting:
    def test_backtester_never_applies_corporate_actions(self, monkeypatch):
        """回测主循环不得调用 handle_corporate_action / apply_split_or_dividend

        行情为前复权序列，分红送转已内含于价格；再叠加会虚增收益。
        """
        calls = []

        def boom(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("前复权数据下不应调用除权除息处理（会重复计入分红）")

        monkeypatch.setattr(Portfolio, "handle_corporate_action", boom)
        monkeypatch.setattr(Portfolio, "apply_split_or_dividend", boom)

        dates = [f"202601{d:02d}" for d in range(1, 11)]
        df = pd.DataFrame({
            "ts_code": "999001.SZ", "trade_date": dates,
            "open": [10.0] * 10, "high": [10.1] * 10,
            "low": [9.9] * 10, "close": [10.0] * 10,
            "volume": [1_000_000] * 10, "amount": [1e7] * 10,
        })
        monkeypatch.setattr("engine.backtester.get_daily",
                            lambda ts_code, s="", e="": df.copy())
        monkeypatch.setattr("engine.backtester.clean_daily", lambda d, **kw: d)
        monkeypatch.setattr("engine.backtester.apply_indicators", lambda d, _: d)

        from core.models import Signal
        from strategies.base import BaseStrategy
        from engine.backtester import Backtester

        class BuyHold(BaseStrategy):
            name = "buy_hold"
            description = "首日买入持有"
            param_schema = {}

            def on_bar(self, trade_date, data, portfolio=None):
                if trade_date != "20260101":
                    return []
                return [Signal(ts_code="999001.SZ", trade_date=trade_date,
                               strategy=self.name, direction="BUY",
                               score=1.0, price_ref=10.0, reason="建仓")]

        bt = Backtester(strategy_cls=BuyHold, params={}, universe=["999001.SZ"],
                        start_date="20260101", end_date="20260110",
                        initial_capital=100000.0, execution_mode="current_close")
        result = bt.run(save=False)
        assert calls == []
        # 价格不变 → 收益仅应体现交易成本，绝不应额外多出分红收益
        assert result.total_return <= 0.0
