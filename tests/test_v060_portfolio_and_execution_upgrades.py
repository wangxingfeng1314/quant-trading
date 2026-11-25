"""v0.6.0 专项回归测试：有条件僵持止损、贝叶斯推荐收缩、双指数择时、组别隔离、实盘复盘账本与组合级风控"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from core.models import Signal
from strategies.base import BaseStrategy


def _make_ohlcv(
    ts_code: str,
    n_days: int = 80,
    start_price: float = 10.0,
    pct_returns: np.ndarray = None,
    start_date: str = "20260101",
    open_gap_idx: int = None,
    open_gap_ratio: float = 0.0,
    spread: float = 0.015,
) -> pd.DataFrame:
    base_dt = datetime.strptime(start_date, "%Y%m%d")
    dates = [(base_dt + timedelta(days=i)).strftime("%Y%m%d") for i in range(n_days)]
    if pct_returns is None:
        pct_returns = np.full(n_days, 0.002)
    closes = [start_price]
    for r in pct_returns[1:]:
        closes.append(round(closes[-1] * (1.0 + float(r)), 4))
    opens = [closes[0]] + [round(closes[i - 1], 4) for i in range(1, n_days)]
    if open_gap_idx is not None and 1 <= open_gap_idx < n_days:
        opens[open_gap_idx] = round(closes[open_gap_idx - 1] * (1.0 + open_gap_ratio), 4)
        closes[open_gap_idx] = round(opens[open_gap_idx] * 1.005, 4)
    highs = [round(max(o, c) * (1.0 + spread), 4) for o, c in zip(opens, closes)]
    lows = [round(min(o, c) * (1.0 - spread), 4) for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "ts_code": ts_code,
        "trade_date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1_000_000.0] * n_days,
        "amount": [10_000_000.0] * n_days,
        "pct_chg": [0.0] + [round((closes[i] / closes[i - 1] - 1.0) * 100.0, 2) for i in range(1, n_days)],
        "turnover": [2.5] * n_days,
        "adj_factor": [1.0] * n_days,
    })


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    import data.storage as st_mod

    db_file = tmp_path / "v060_test.db"
    monkeypatch.setattr(st_mod, "DB_PATH", db_file)
    if getattr(st_mod._thread_local, "conn", None) is not None:
        st_mod._thread_local.conn.close()
        st_mod._thread_local.conn = None
    st_mod.init_db()
    yield st_mod
    if getattr(st_mod._thread_local, "conn", None) is not None:
        st_mod._thread_local.conn.close()
        st_mod._thread_local.conn = None


# =====================================================================
# P0-1: 有条件僵持时间止损 (max_holding_pnl_cap)
# =====================================================================
def test_conditional_zombie_time_stop_and_diagnosis():
    from engine.risk_manager import RiskManager, clone_risk_manager
    from services.position_service import diagnose_position_action

    rm_kdj = RiskManager.for_strategy("kdj_cross", use_atr_stop=True)
    assert rm_kdj.max_holding_days == 60
    assert rm_kdj.max_holding_pnl_cap == 2.0

    rm_copy = clone_risk_manager(rm_kdj)
    assert rm_copy.max_holding_pnl_cap == 2.0

    # 持仓达 60 天但盈利 +8.5%（高于 2% 门槛，且未触及移动/保本止损）：不应时间止损砍仓
    pos_win = SimpleNamespace(
        is_empty=False, avg_cost=50.0, buy_date="20260925",
        available_shares=1000, shares=1000,
    )
    rm_kdj.highest_prices["601318.SH"] = 55.0
    sigs_win = rm_kdj.check_risks(
        trade_date="20261124",
        portfolio=SimpleNamespace(positions={"601318.SH": pos_win}),
        current_prices={"601318.SH": 54.25},      # +8.5%，最高55回撤仅1.36%
        current_atr_pct={"601318.SH": 3.0},
    )
    assert sigs_win == []

    # 持仓达 60 天且盈利仅 +1.0%（低于 +2.0% 门槛）：触发有条件时间止损
    pos_zombie = SimpleNamespace(
        is_empty=False, avg_cost=50.0, buy_date="20260925",
        available_shares=1000, shares=1000,
    )
    rm_kdj.highest_prices["601318.SH"] = 51.0
    sigs_zombie = rm_kdj.check_risks(
        trade_date="20261124",
        portfolio=SimpleNamespace(positions={"601318.SH": pos_zombie}),
        current_prices={"601318.SH": 50.50},      # +1.0%
        current_atr_pct={"601318.SH": 3.0},
    )
    assert len(sigs_zombie) == 1
    assert sigs_zombie[0].direction == "SELL"
    assert "持仓到期" in sigs_zombie[0].reason or "滞涨" in sigs_zombie[0].reason

    # 持仓诊断同步对齐：持有65天(+8.0%) 不触发时间止损；持有65天(+0.5%) 触发时间止损调仓
    df_win = pd.DataFrame([
        {"trade_date": "20260920", "high": 50.5, "close": 50.0},
        {"trade_date": "20261124", "high": 54.5, "close": 54.0},
    ])
    df_zombie = pd.DataFrame([
        {"trade_date": "20260920", "high": 50.2, "close": 50.0},
        {"trade_date": "20261124", "high": 50.4, "close": 50.25},
    ])
    fake_atr = lambda code, px, score=0.7: {"atr_pct": 2.5, "sl_pct": 6.0, "tp_pct": 14.0}
    empty_sig = lambda **kw: pd.DataFrame()

    diag_hold = diagnose_position_action(
        ts_code="601318.SH",
        buy_price=50.0,
        buy_date="20260920",
        strategy_hint="kdj_cross",
        get_daily_fn=lambda *a, **kw: df_win,
        get_signals_fn=empty_sig,
        calc_atr_fn=fake_atr,
    )
    assert diag_hold["holding_days"] == 65
    assert diag_hold["level"] == "hold"
    assert "时间止损" not in diag_hold["action"]

    diag_zombie = diagnose_position_action(
        ts_code="601318.SH",
        buy_price=50.0,
        buy_date="20260920",
        strategy_hint="kdj_cross",
        get_daily_fn=lambda *a, **kw: df_zombie,
        get_signals_fn=empty_sig,
        calc_atr_fn=fake_atr,
    )
    assert diag_zombie["holding_days"] == 65
    assert diag_zombie["level"] == "warning"
    assert "时间止损" in diag_zombie["action"]


# =====================================================================
# P0-2: 贝叶斯小样本盈亏比收缩 & 样本外弱亏损(<3笔)兜底熔断
# =====================================================================
def test_recommender_bayesian_pf_shrinkage_and_weak_oos_gate(monkeypatch):
    from core.models import BacktestResult
    import services.strategy_recommender as rec_mod

    df = _make_ohlcv("600001.SH", n_days=380, start_date="20250101")
    end_dt = str(df["trade_date"].iloc[-1])
    monkeypatch.setattr(rec_mod, "get_daily", lambda code, *a, **kw: df)
    monkeypatch.setattr(rec_mod, "preload_backtest_data", lambda *a, **kw: {"600001.SH": df})

    def _make_bt_res(sname: str, ret: float, sharpe: float, win: float, pf: float, n_sells: int) -> BacktestResult:
        r = BacktestResult(
            strategy=sname, params="{}", start_date="", end_date="",
            initial_capital=100000.0, final_capital=100000.0 * (1.0 + ret / 100.0),
            total_return=ret, max_drawdown=5.0, sharpe_ratio=sharpe,
            win_rate=win, trade_count=n_sells * 2, sell_count=n_sells,
        )
        r.profit_factor = pf
        return r

    # 候选均包含在 strategy_candidates 中，同属低波反转（相同 style_prior）：
    # - kdj_cross: 训练段仅 2 笔卖单却偶发 PF=3.0 (小样本收缩: 1 + 2*(2/8)=1.5)
    # - bollinger_reversal: 训练段 10 笔卖单且实打实 PF=2.4 (足样本无收缩: eff_pf=2.4)
    class _FakeBT:
        def __init__(self, strategy_cls=None, **kwargs):
            self.sname = getattr(strategy_cls, "name", "")
            self.start_date = kwargs.get("start_date", "")

        def run(self, save=False):
            is_oos = self.start_date > "20250601"
            if self.sname == "kdj_cross":
                return _make_bt_res(
                    "kdj_cross",
                    ret=6.0 if not is_oos else -4.5,
                    sharpe=0.6 if not is_oos else -0.8,
                    win=60.0 if not is_oos else 0.0,
                    pf=3.0 if not is_oos else 0.2,
                    n_sells=2,
                )
            if self.sname == "bollinger_reversal":
                return _make_bt_res(
                    "bollinger_reversal",
                    ret=6.0 if not is_oos else 2.5,
                    sharpe=0.6 if not is_oos else 0.5,
                    win=60.0 if not is_oos else 60.0,
                    pf=2.4 if not is_oos else 1.5,
                    n_sells=10 if not is_oos else 4,
                )
            return _make_bt_res(self.sname, ret=-5.0, sharpe=-0.5, win=30.0, pf=0.6, n_sells=2)

    monkeypatch.setattr(rec_mod, "Backtester", _FakeBT)
    res = rec_mod.evaluate_stock_strategy("600001.SH", name="测试股", industry="机械设备", end_date=end_dt)
    assert res["strategy"] == "bollinger_reversal"
    assert res["oos_passed"] is True

    # 所有策略在样本外均仅有 2 笔(<3)且总收益 <= -3%：触发闭环熔断「暂不适配·观望」
    class _WeakOOSBT(_FakeBT):
        def run(self, save=False):
            is_oos = self.start_date > "20250601"
            if is_oos:
                return _make_bt_res(self.sname, ret=-4.5, sharpe=-0.6, win=0.0, pf=0.3, n_sells=2)
            return super().run(save=False)

    monkeypatch.setattr(rec_mod, "Backtester", _WeakOOSBT)
    res_weak = rec_mod.evaluate_stock_strategy("600001.SH", name="弱势股", industry="机械设备", end_date=end_dt)
    assert res_weak["oos_passed"] is False
    assert rec_mod.is_no_trade_recommendation(res_weak["tag"])


# =====================================================================
# P0-3 & P2-7: 滞后指数回退 + 双指数共振拦截创业板 + 定时扫描隔离
# =====================================================================
def test_dual_regime_fallback_and_scanner_growth_block(isolated_db, monkeypatch):
    from services.signal_service import get_market_regime, get_dual_market_regime
    from engine.scanner import scan_signals

    # 构造 000300 滞后于 000001（000300 只到第80天，000001 到第95天稳健多头），399006 连续单边走熊下跌（RSI14=0）
    df_300 = _make_ohlcv("000300.SH", n_days=80, start_price=3800.0, pct_returns=np.full(80, 0.001), start_date="20251101")
    df_001 = _make_ohlcv("000001.SH", n_days=95, start_price=3000.0, pct_returns=np.full(95, 0.002), start_date="20251101")
    df_cyb = _make_ohlcv("399006.SZ", n_days=95, start_price=2600.0, pct_returns=np.full(95, -0.004), start_date="20251101")

    def fake_get_index_daily(code, *a, **kw):
        if code == "000300.SH":
            return df_300
        if code == "000001.SH":
            return df_001
        if code == "399006.SZ":
            return df_cyb
        return pd.DataFrame()

    monkeypatch.setattr("services.signal_service.get_index_daily", fake_get_index_daily)

    reg = get_market_regime(index_code="000300.SH")
    assert reg["index_code"] == "000001.SH"
    assert reg["regime"] == "BULL"

    dual = get_dual_market_regime()
    assert dual["large_cap"]["index_code"] == "000001.SH"
    assert dual["large_cap"]["regime"] == "BULL"
    assert dual["growth"]["regime"] == "BEAR"
    assert dual["combined_regime"] == "GROWTH_BEAR"

    # 写入隔离库并验证 scanner 在 000001=BULL & 399006=BEAR 时拦截创业板(300750.SZ)、放行主板低波(601318.SH)
    df_main = _make_ohlcv("601318.SH", n_days=80, pct_returns=np.full(80, 0.001), start_date="20251101", spread=0.005)
    df_gem = _make_ohlcv("300750.SZ", n_days=80, pct_returns=np.full(80, 0.001), start_date="20251101", spread=0.025)
    isolated_db.save_daily(df_main)
    isolated_db.save_daily(df_gem)
    end_dt = str(df_main["trade_date"].iloc[-1])

    class _AlwaysBuyTrend(BaseStrategy):
        name = "ma_cross"
        description = "always buy"
        allow_bear_buy = False

        def on_bar(self, trade_date, data, portfolio=None):
            return [
                Signal(
                    ts_code=code, trade_date=trade_date, strategy="ma_cross",
                    direction="BUY", score=0.9, price_ref=float(sdf.iloc[-1]["close"]),
                    reason="test buy",
                )
                for code, sdf in data.items()
            ]

    monkeypatch.setattr("engine.scanner.get_daily", lambda c, *a, **kw: df_gem if c == "300750.SZ" else df_main)
    monkeypatch.setattr("engine.scanner.get_instrument_list", lambda: pd.DataFrame({
        "ts_code": ["601318.SH", "300750.SZ"],
        "name": ["中国平安", "宁德时代"],
        "type": ["stock", "stock"],
    }))

    with patch.dict("engine.scanner.STRATEGY_REGISTRY", {"ma_cross": _AlwaysBuyTrend}):
        sigs = scan_signals(
            universe=["601318.SH", "300750.SZ"],
            strategy_names=["ma_cross"],
            end_date=end_dt,
            save=False,
            parallel=False,
            min_daily_amount=0,
            filter_suspended=False,
            force_refresh=True,
            respect_market_regime=True,
            anti_whipsaw=False,
        )
    bought_codes = {s.ts_code for s in sigs if s.direction == "BUY"}
    assert "601318.SH" in bought_codes
    assert "300750.SZ" not in bought_codes


def test_scheduler_watchlist_group_isolation(monkeypatch):
    import scheduler as sched_mod

    wl_df = pd.DataFrame([
        {"ts_code": "510300.SH", "group_name": "ETF轮动池", "recommended_strategy": ""},
        {"ts_code": "510500.SH", "group_name": "ETF轮动池", "recommended_strategy": ""},
        {"ts_code": "601318.SH", "group_name": "核心蓝筹", "recommended_strategy": "⭐ 中信推荐·KDJ拐点 (+12%)"},
    ])
    calls = []

    def fake_scan(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("data.storage.get_watchlist", lambda: wl_df)
    monkeypatch.setattr("data.storage.notification_already_sent", lambda *a, **kw: False)
    monkeypatch.setattr("data.storage.mark_notification_sent", lambda *a, **kw: None)
    monkeypatch.setattr("engine.scanner.scan_signals", fake_scan)
    monkeypatch.setattr("notifier.push.notify_signals", lambda *a, **kw: True)
    monkeypatch.setattr("notifier.push.notify_position_summary", lambda *a, **kw: False)

    sched_mod.scan_and_notify(force=True)
    assert len(calls) == 2
    regular_call = next(c for c in calls if "601318.SH" in c["universe"])
    rot_call = next(c for c in calls if "510300.SH" in c["universe"])
    assert "momentum_rotation" not in regular_call["strategy_names"]
    assert rot_call["strategy_names"] == ["momentum_rotation"]
    assert set(rot_call["universe"]) == {"510300.SH", "510500.SH"}


# =====================================================================
# P1-4: real_trade_journal v7 + 加仓合并 + FIFO 分批减仓 + 复盘统计
# =====================================================================
def test_real_trade_journal_upsert_reduce_and_summary(isolated_db):
    from services.position_service import summarize_trade_journal

    with isolated_db.get_conn() as conn:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 7
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='real_trade_journal'"
        ).fetchone()
        assert tbl is not None

    # 同股两次买入 -> 自动按加权均价合并为一条持仓 + 2 条 BUY 流水
    pid1 = isolated_db.upsert_position_buy("601318.SH", buy_price=50.0, shares=1000, buy_date="20261101", strategy="kdj_cross")
    pid2 = isolated_db.upsert_position_buy("601318.SH", buy_price=60.0, shares=1000, buy_date="20261105", strategy="kdj_cross")
    assert pid1 == pid2

    pos = isolated_db.get_positions()
    assert len(pos) == 1
    assert int(pos[0]["shares"]) == 2000
    assert abs(float(pos[0]["buy_price"]) - 55.0) < 1e-6

    # 卖出 1000 股 -> 持仓剩余 1000 股 + 1 条 SELL 结算流水（含真实费率扣减）
    res_half = isolated_db.reduce_position(
        "601318.SH", sell_shares=1000, sell_price=65.0,
        sell_date="20261115", strategy="kdj_cross", signal_price=65.0,
    )
    assert res_half["remaining_shares"] == 1000
    assert res_half["holding_days"] == 14
    assert res_half["pnl"] > 9500.0
    assert len(isolated_db.get_positions()) == 1

    # 卖出剩余 1000 股 -> 持仓清空
    res_all = isolated_db.reduce_position(
        "601318.SH", sell_shares=1000, sell_price=60.0,
        sell_date="20261120", strategy="kdj_cross",
    )
    assert res_all["remaining_shares"] == 0
    assert len(isolated_db.get_positions()) == 0

    jdf = isolated_db.get_trade_journal()
    assert len(jdf) == 4  # 2 BUY + 2 SELL

    summary = summarize_trade_journal(jdf)
    assert summary["closed_count"] == 2
    assert summary["buy_count"] == 2
    assert summary["win_rate"] == 100.0
    assert summary["total_pnl"] > 14000.0
    assert len(summary["by_strategy"]) == 1
    assert summary["by_strategy"][0]["平仓笔数"] == 2
    assert "KDJ" in summary["by_strategy"][0]["策略"]


# =====================================================================
# P1-5 & P2-6: 缺口过滤 / 回撤降速器 / 行业上限 / 相关性约束 / 多策略统一路由
# =====================================================================
class _Day2BuyStrategy(BaseStrategy):
    name = "day2_buy"
    description = "Buy on day 2"

    def on_bar(self, trade_date, data, portfolio=None):
        sigs = []
        for code, df in data.items():
            if len(df) == 3 and (not portfolio or not portfolio.positions.get(code) or portfolio.positions[code].is_empty):
                sigs.append(Signal(
                    ts_code=code, trade_date=trade_date, strategy=self.name,
                    direction="BUY", score=0.9, price_ref=float(df.iloc[-1]["close"]),
                    reason="day2",
                ))
        return sigs


def test_backtester_gap_filter_sector_correlation_and_router(monkeypatch):
    from engine.backtester import Backtester, build_backtest_options, BACKTEST_OPTION_KEYS
    from engine.risk_manager import RiskManager

    opts = build_backtest_options(max_open_gap_pct=3.0, drawdown_brake_pct=8.0, max_sector_positions=2, max_correlation=0.75)
    assert set(opts.keys()) == set(BACKTEST_OPTION_KEYS)

    # 1. 高开缺口拦截：第 3 天发出买单，第 4 天(index=3)开盘高开 +7%，在 max_open_gap_pct=3.0 下必须弃单
    df_gap = _make_ohlcv("000001.SZ", n_days=10, open_gap_idx=3, open_gap_ratio=0.07)
    monkeypatch.setattr("engine.backtester.get_st_lookup", lambda: {})

    bt_gap = Backtester(
        strategy_cls=_Day2BuyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20260101", end_date="20260110",
        execution_mode="next_open",
        max_open_gap_pct=3.0,
        preloaded_data={"000001.SZ": df_gap},
    )
    res_gap = bt_gap.run(save=False)
    assert res_gap.trade_count == 0

    bt_nogap = Backtester(
        strategy_cls=_Day2BuyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20260101", end_date="20260110",
        execution_mode="next_open",
        max_open_gap_pct=0.0,
        preloaded_data={"000001.SZ": df_gap},
    )
    res_nogap = bt_nogap.run(save=False)
    assert any(t.direction == "BUY" for t in res_nogap.trades)

    # 2. 同申万行业最大持仓上限 + 因果 60 日相关性拦截
    rng = np.random.default_rng(42)
    rets = rng.normal(0.001, 0.015, 75)
    df_a = _make_ohlcv("000001.SZ", n_days=75, pct_returns=rets)
    df_b = _make_ohlcv("000002.SZ", n_days=75, pct_returns=rets + 0.0001)  # corr ~ 1.0
    df_c = _make_ohlcv("000003.SZ", n_days=75, pct_returns=-rets)          # corr ~ -1.0

    class _Day70Buy(BaseStrategy):
        name = "day70_buy"
        description = "Buy on bar 70"

        def on_bar(self, trade_date, data, portfolio=None):
            sigs = []
            for code, df in data.items():
                if len(df) == 70:
                    sigs.append(Signal(
                        ts_code=code, trade_date=trade_date, strategy=self.name,
                        direction="BUY", score=0.95 if code == "000001.SZ" else 0.85,
                        price_ref=float(df.iloc[-1]["close"]), reason="d70",
                    ))
            return sigs

    # 场景 A：同一行业("银行")且相关性放开 -> max_sector_positions=1 仅允许打开 1 只
    monkeypatch.setattr(
        "engine.backtester.get_industry_lookup",
        lambda: {"000001.SZ": "银行", "000002.SZ": "银行", "000003.SZ": "医药"},
    )
    bt_sec = Backtester(
        strategy_cls=_Day70Buy,
        params={},
        universe=["000001.SZ", "000002.SZ"],
        start_date="20260101", end_date="20260320",
        execution_mode="next_open",
        max_active_positions=4,
        max_sector_positions=1,
        max_correlation=0.0,
        preloaded_data={"000001.SZ": df_a, "000002.SZ": df_b},
    )
    res_sec = bt_sec.run(save=False)
    assert len({t.ts_code for t in res_sec.trades if t.direction == "BUY"}) == 1

    # 场景 B：不同行业("银行"/"房地产"/"医药") -> max_correlation=0.75 拦截高度正相关的 000002，放行负相关 000003
    monkeypatch.setattr(
        "engine.backtester.get_industry_lookup",
        lambda: {"000001.SZ": "银行", "000002.SZ": "房地产", "000003.SZ": "医药"},
    )
    bt_corr = Backtester(
        strategy_cls=_Day70Buy,
        params={},
        universe=["000001.SZ", "000002.SZ", "000003.SZ"],
        start_date="20260101", end_date="20260320",
        execution_mode="next_open",
        max_active_positions=4,
        max_sector_positions=2,
        max_correlation=0.75,
        preloaded_data={"000001.SZ": df_a, "000002.SZ": df_b, "000003.SZ": df_c},
    )
    res_corr = bt_corr.run(save=False)
    bought_corr = {t.ts_code for t in res_corr.trades if t.direction == "BUY"}
    assert "000001.SZ" in bought_corr
    assert "000002.SZ" not in bought_corr
    assert "000003.SZ" in bought_corr

    # 3. 统一组合 strategy_router（同时覆盖 (cls, params) 二元组与直接传类） + risk_manager="auto"
    class _StratOne(BaseStrategy):
        name = "kdj_cross"
        description = "strat 1"

        def on_bar(self, trade_date, data, portfolio=None):
            if "000001.SZ" in data and len(data["000001.SZ"]) == 3:
                return [Signal("000001.SZ", trade_date, "kdj_cross", "BUY", 0.9, float(data["000001.SZ"].iloc[-1]["close"]), "s1")]
            return []

    class _StratTwo(BaseStrategy):
        name = "ma_cross"
        description = "strat 2"

        def on_bar(self, trade_date, data, portfolio=None):
            if "000003.SZ" in data and len(data["000003.SZ"]) == 4:
                return [Signal("000003.SZ", trade_date, "ma_cross", "BUY", 0.9, float(data["000003.SZ"].iloc[-1]["close"]), "s2")]
            return []

    bt_router = Backtester(
        strategy_cls=None,
        universe=["000001.SZ", "000003.SZ"],
        start_date="20260101", end_date="20260115",
        execution_mode="next_open",
        risk_manager="auto",
        max_active_positions=2,
        strategy_router={
            "000001.SZ": (_StratOne, {}),
            "000003.SZ": _StratTwo,
        },
        preloaded_data={"000001.SZ": df_gap, "000003.SZ": df_c.iloc[:15].copy()},
    )
    res_router = bt_router.run(save=False)
    router_strats = {t.context_snapshot.get("strategy") for t in res_router.trades if t.direction == "BUY"}
    assert "kdj_cross" in router_strats and "ma_cross" in router_strats
    assert bt_router.auto_rm_groups["kdj_cross"][0].max_holding_days == RiskManager.for_strategy("kdj_cross").max_holding_days
    assert bt_router.auto_rm_groups["ma_cross"][0].enable_break_even is RiskManager.for_strategy("ma_cross").enable_break_even


def test_backtester_drawdown_brake_halves_allocation(monkeypatch):
    from engine.backtester import Backtester

    monkeypatch.setattr("engine.backtester.get_st_lookup", lambda: {})

    dates = [f"202601{i+1:02d}" for i in range(12)]
    c1 = [10.0, 10.0, 10.0, 7.5, 7.5, 7.5, 7.5, 7.5, 7.5, 7.5, 7.5, 7.5]
    c2 = [10.0] * 12
    df1 = pd.DataFrame({
        "ts_code": "000001.SZ", "trade_date": dates,
        "open": c1, "high": [x * 1.01 for x in c1], "low": [x * 0.99 for x in c1],
        "close": c1, "volume": [1e6] * 12, "amount": [1e7] * 12,
        "pct_chg": [0.0] * 12, "turnover": [2.0] * 12, "adj_factor": [1.0] * 12,
    })
    df2 = pd.DataFrame({
        "ts_code": "000002.SZ", "trade_date": dates,
        "open": c2, "high": [x * 1.01 for x in c2], "low": [x * 0.99 for x in c2],
        "close": c2, "volume": [1e6] * 12, "amount": [1e7] * 12,
        "pct_chg": [0.0] * 12, "turnover": [2.0] * 12, "adj_factor": [1.0] * 12,
    })

    class _BrakeSeqStrategy(BaseStrategy):
        name = "brake_seq"
        description = "seq"

        def on_bar(self, trade_date, data, portfolio=None):
            sigs = []
            if "000001.SZ" in data:
                n = len(data["000001.SZ"])
                if n == 2:
                    sigs.append(Signal("000001.SZ", trade_date, "brake_seq", "BUY", 0.9, 10.0, "b1"))
                elif n == 4:
                    sigs.append(Signal("000001.SZ", trade_date, "brake_seq", "SELL", 0.9, 7.5, "s1"))
            if "000002.SZ" in data and len(data["000002.SZ"]) == 7:
                sigs.append(Signal("000002.SZ", trade_date, "brake_seq", "BUY", 0.9, 10.0, "b2"))
            return sigs

    bt_no_brake = Backtester(
        strategy_cls=_BrakeSeqStrategy, params={}, universe=["000001.SZ", "000002.SZ"],
        start_date="20260101", end_date="20260112",
        execution_mode="next_open", max_active_positions=1, drawdown_brake_pct=0.0,
        preloaded_data={"000001.SZ": df1, "000002.SZ": df2},
    )
    res_no = bt_no_brake.run(save=False)

    bt_brake = Backtester(
        strategy_cls=_BrakeSeqStrategy, params={}, universe=["000001.SZ", "000002.SZ"],
        start_date="20260101", end_date="20260112",
        execution_mode="next_open", max_active_positions=1, drawdown_brake_pct=8.0,
        preloaded_data={"000001.SZ": df1, "000002.SZ": df2},
    )
    res_yes = bt_brake.run(save=False)

    b2_no = next(t for t in res_no.trades if t.ts_code == "000002.SZ" and t.direction == "BUY")
    b2_yes = next(t for t in res_yes.trades if t.ts_code == "000002.SZ" and t.direction == "BUY")
    assert b2_yes.volume <= b2_no.volume * 0.55
