"""v0.4.7 深度审查修复与优化回归测试

覆盖：
1. current_close 模式下顺延卖单在停牌日禁止虚假成交（必须继续顺延到复牌日）；
2. 参与率上限导致卖出部分成交时，剩余份额自动顺延续卖；
3. 策略推荐器样本外充足性校验必须看已平仓卖单数 (sells >= OOS_MIN_TRADES)，而非买卖总笔数；
4. CircuitBreaker 冷却期过期后必须清零历史失败计数，给予完整恢复探测窗口；
5. scheduler.scan_and_notify 扫描或推送抛异常时禁止写幂等标记锁死当日重试，notify_signals 返回布尔值；
6. validate_capital 拒绝 NaN / Inf / bool；
7. chinese_date_picker / chinese_date_input 对 2 月 30 日 / 小月 31 日自动钳位防非法日历；
8. _wilder_mean 前段存在孤立 NaN 空洞时自动从首个连续有效窗口起算，且 apply_indicators 不修改原 DataFrame。
"""
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


def test_current_close_deferred_sell_skips_suspended_day(monkeypatch):
    """current_close 模式下，跌停顺延的卖单遇到次日停牌必须继续顺延，绝不能按昨价虚假成交。"""
    import engine.backtester as btmod
    from core.models import Signal
    from strategies.base import BaseStrategy

    monkeypatch.setattr(btmod, "get_benchmark_returns", lambda *a, **kw: {})
    monkeypatch.setattr(btmod, "get_st_lookup", lambda *a, **kw: {})

    # 交易日序列：Day0 买入，Day1 涨10%并一字跌停卖单拒单（触发顺延），
    # Day2 股票 A 停牌（只有基准股 B 有行情），Day3 股票 A 复牌成交。
    df_a = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 3,
        "trade_date": ["20240102", "20240103", "20240105"],  # 缺 20240104（停牌）
        "open": [10.0, 9.0, 9.5],
        "high": [10.0, 9.0, 9.6],  # 20240103 一字跌停（high == lower = 9.00）
        "low": [10.0, 9.0, 9.4],
        "close": [10.0, 9.0, 9.5],
        "volume": [1_000_000.0] * 3,
        "amount": [10_000_000.0] * 3,
    })
    df_b = pd.DataFrame({
        "ts_code": ["000002.SZ"] * 4,
        "trade_date": ["20240102", "20240103", "20240104", "20240105"],
        "open": [20.0] * 4,
        "high": [20.0] * 4,
        "low": [20.0] * 4,
        "close": [20.0] * 4,
        "volume": [1_000_000.0] * 4,
        "amount": [20_000_000.0] * 4,
    })

    class DeferSellTestStrategy(BaseStrategy):
        name = "test_defer_suspend"
        desc = "test"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio):
            if trade_date == "20240102":
                return [Signal("000001.SZ", trade_date, self.name, "BUY", 0.9, "buy", 10.0)]
            if trade_date == "20240103":
                return [Signal("000001.SZ", trade_date, self.name, "SELL", 0.9, "sell", 9.0)]
            return []

    bt = btmod.Backtester(
        strategy_cls=DeferSellTestStrategy,
        params={},
        universe=["000001.SZ", "000002.SZ"],
        start_date="20240102",
        end_date="20240105",
        initial_capital=100000.0,
        execution_mode="current_close",
        preloaded_data={"000001.SZ": df_a, "000002.SZ": df_b},
    )
    res = bt.run(save=False)
    sells = [t for t in res.trades if t.direction == "SELL"]
    assert len(sells) == 1
    assert sells[0].trade_date == "20240105", "停牌日 20240104 不允许成交，应顺延至复牌日 20240105"


def test_partial_sell_due_to_participation_cap_defers_remainder(monkeypatch):
    """当参与率上限导致卖出部分成交时，剩余可用份额应继续顺延到下一交易日卖出。"""
    import engine.backtester as btmod
    import engine.commission as comm
    from core.models import Signal
    from strategies.base import BaseStrategy

    monkeypatch.setattr(btmod, "get_benchmark_returns", lambda *a, **kw: {})
    monkeypatch.setattr(btmod, "get_st_lookup", lambda *a, **kw: {})
    monkeypatch.setattr(comm, "MAX_PARTICIPATION_RATE", 0.1)

    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 4,
        "trade_date": ["20240102", "20240103", "20240104", "20240105"],
        "open": [10.0, 10.0, 10.0, 10.0],
        "high": [10.2, 10.2, 10.2, 10.2],
        "low": [9.8, 9.8, 9.8, 9.8],
        "close": [10.0, 10.0, 10.0, 10.0],
        # Day0 放量买入 9900 股；Day1/Day2/Day3 缩量到每天只允许卖 5000 股（50000 * 0.1）
        "volume": [100_000.0, 50_000.0, 50_000.0, 50_000.0],
        "amount": [1_000_000.0, 500_000.0, 500_000.0, 500_000.0],
    })

    class PartialSellStrategy(BaseStrategy):
        name = "test_partial_sell"
        desc = "test"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio):
            if trade_date == "20240102":
                return [Signal("000001.SZ", trade_date, self.name, "BUY", 0.9, "buy", 10.0)]
            if trade_date == "20240103":
                return [Signal("000001.SZ", trade_date, self.name, "SELL", 0.9, "sell", 10.0)]
            return []

    bt = btmod.Backtester(
        strategy_cls=PartialSellStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240105",
        initial_capital=100000.0,
        execution_mode="current_close",
        preloaded_data={"000001.SZ": df},
        max_active_positions=1,
    )
    res = bt.run(save=False)
    buys = [t for t in res.trades if t.direction == "BUY"]
    sells = [t for t in res.trades if t.direction == "SELL"]
    assert len(buys) == 1
    assert len(sells) >= 2, "分批受限卖出应在后续交易日持续顺延出清剩余份额"
    assert sum(t.volume for t in sells) == buys[0].volume


def test_recommender_requires_closed_sells_for_oos_badge(monkeypatch):
    """样本外只出现 3 次买入但 0~2 次平仓时，胜率无统计意义，不得展示样本外胜率/收益标签。"""
    import services.strategy_recommender as sr

    dates = pd.bdate_range("2023-01-02", periods=160).strftime("%Y%m%d").tolist()
    df = pd.DataFrame({"trade_date": dates, "close": np.linspace(10, 20, len(dates))})
    monkeypatch.setattr(sr, "get_daily", lambda *a, **kw: df)
    monkeypatch.setattr(sr, "preload_backtest_data", lambda *a, **kw: {})

    train_start = dates[0]

    class FewSellsBacktester:
        def __init__(self, strategy_cls, params, universe, start_date, end_date, **kwargs):
            self.name = strategy_cls.name
            self.start_date = start_date

        def run(self, save=False):
            if self.start_date == train_start:
                return SimpleNamespace(total_return=20.0, win_rate=70.0, trade_count=10,
                                       sell_count=5, max_drawdown=5.0, sharpe_ratio=1.2)
            # 样本外：total trades = 4 (如3买1卖)，但仅 1 笔平仓 -> 不足以给胜率标签
            return SimpleNamespace(total_return=15.0, win_rate=100.0, trade_count=4,
                                   sell_count=1, max_drawdown=2.0, sharpe_ratio=1.5)

    monkeypatch.setattr(sr, "Backtester", FewSellsBacktester)
    out = sr.evaluate_stock_strategy("000001.SZ", start_date=dates[0], end_date=dates[-1])
    assert "样本外交易不足" in out["tag"]
    assert "100.0%" not in out["tag"]


def test_circuit_breaker_resets_failures_after_cooldown(monkeypatch):
    """熔断冷却期到期后应重置失败计数，而不是下一次单次失败就立刻二次熔断。"""
    import data.fetcher_circuit as fc

    now = [1000.0]
    monkeypatch.setattr(fc.time, "time", lambda: now[0])

    cb = fc.CircuitBreaker("test_src", threshold=3, cooldown=60)
    cb.failure()
    cb.failure()
    assert not cb.is_open()
    cb.failure()
    assert cb.is_open()

    # 冷却期结束
    now[0] += 61.0
    assert not cb.is_open(), "冷却到期后熔断器应允许探测"
    # 探测失败一次不应直接再次熔断（需要重新累计到 threshold=3）
    cb.failure()
    assert not cb.is_open(), "单次探测失败不应直接重新触发完整冷却"
    cb.failure()
    assert not cb.is_open()
    cb.failure()
    assert cb.is_open()


def test_scan_and_notify_does_not_mark_sent_on_scan_exception(monkeypatch):
    """信号扫描异常时，不应写入 notify_log 幂等锁，否则当天后续补跑/手动重试会被永久跳过。"""
    import scheduler

    sent = []
    monkeypatch.setattr("data.storage.notification_already_sent", lambda key: key in sent)
    monkeypatch.setattr("data.storage.mark_notification_sent", lambda key: sent.append(key))
    monkeypatch.setattr("data.storage.get_watchlist", lambda: pd.DataFrame([{"ts_code": "000001.SZ"}]))
    monkeypatch.setattr("engine.scanner.scan_signals", lambda **kw: (_ for _ in ()).throw(RuntimeError("network error")))
    monkeypatch.setattr("notifier.push.notify_position_summary", lambda: False)

    scheduler.scan_and_notify()
    assert len(sent) == 0, "扫描失败时绝不能锁定当日幂等键"


def test_validate_capital_rejects_nan_inf_bool():
    from core.exceptions import ValidationError
    from services.validators import validate_capital

    for bad in (float("nan"), float("inf"), float("-inf"), True, False):
        with pytest.raises(ValidationError):
            validate_capital(bad)

    assert math.isclose(validate_capital(100000), 100000.0)


def test_wilder_mean_recovers_after_leading_internal_nan():
    """前段存在孤立 NaN 空洞时，_wilder_mean 仍能定位首个连续 period 窗口并正确递推。"""
    from data.indicators import _wilder_mean, apply_indicators

    # 第 2 位插入 NaN，随后是连续 20 个有效值 1.0
    vals = [1.0, 1.0, np.nan] + [2.0] * 20
    s = pd.Series(vals)
    res = _wilder_mean(s, period=5)
    # 前 3+5-1 = 7 位（index 0..6）无连续5日窗口，index 7 开始应当有值 2.0
    assert res.iloc[:7].isna().all()
    assert np.isclose(res.iloc[7], 2.0)
    assert np.isclose(res.iloc[-1], 2.0)

    raw = pd.DataFrame({
        "trade_date": [f"202401{i:02d}" for i in range(1, 26)],
        "open": np.linspace(10, 12, 25),
        "high": np.linspace(10.5, 12.5, 25),
        "low": np.linspace(9.5, 11.5, 25),
        "close": np.linspace(10, 12, 25),
        "volume": np.full(25, 100000.0),
    })
    raw_cols = list(raw.columns)
    out = apply_indicators(raw)
    assert list(raw.columns) == raw_cols, "apply_indicators 必须保持纯函数，不污染输入列"
    assert {"ma20", "rsi14", "atr14", "kdj_k"}.issubset(out.columns)


def test_chinese_date_picker_clamps_invalid_month_day(monkeypatch):
    """2 月 30 日 / 小月 31 日必须按当月实际天数钳位，避免 downstream strptime 崩溃。"""
    import app.st_utils as su

    class _DummyCol:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(su.st, "markdown", lambda *a, **kw: None)
    monkeypatch.setattr(su.st, "columns", lambda n: [_DummyCol() for _ in range(n)])
    # 返回 2023 年 2 月 30 日（平年 2 月只有 28 天）
    vals = iter([2023, 2, 30])
    monkeypatch.setattr(su.st, "number_input", lambda *a, **kw: next(vals))
    picked = su.chinese_date_picker("测试日期", key="t_dp")
    assert picked == "20230228"

    # 闰年 2024 年 2 月 30 日应钳位到 20240229
    vals_leap = iter([2024, 2, 30])
    monkeypatch.setattr(su.st, "number_input", lambda *a, **kw: next(vals_leap))
    assert su.chinese_date_picker("测试闰年", key="t_dp2") == "20240229"

