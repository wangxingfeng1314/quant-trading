"""v0.8.0 核心优化测试套件:
1. Baostock 高速估值渠道及优先级
2. Portfolio 闲置现金日计息与 Backtester 参数透传
3. 熊市反转降仓系数 & 策略对齐 ATR 风险平价定仓
4. 真实连续跨期滚动回测 (跨年度调仓保留在途持仓并延续原策略出场信号)
5. Schema v8 position.strategy 持久化及持仓风控对齐
6. 委托汇总跳过已持仓买入防误超买 (skip_held_buys)
"""
from __future__ import annotations

import sys
import types
import pandas as pd
import pytest

from core import config as core_config
from core.models import Signal
from data import storage
from engine.portfolio import Portfolio
from engine.risk_manager import RiskManager
from engine.backtester import (
    Backtester,
    BACKTEST_OPTION_KEYS,
    build_backtest_options,
    run_rolling_adaptive_backtest,
)
from services import valuation_service
from services.position_service import build_position_risk_report
from app.signal import build_deduplicated_broker_orders
from strategies import STRATEGY_REGISTRY
from strategies.base import BaseStrategy


def _use_temp_db(monkeypatch, tmp_path, name: str = "test_v080.db"):
    """将 data.storage 的 DB 路径与线程本地连接完全隔离到临时目录"""
    old_conn = getattr(storage._thread_local, "conn", None)
    if old_conn is not None:
        try:
            old_conn.close()
        except Exception:
            pass
    storage._thread_local.conn = None
    db_path = tmp_path / name
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setattr(core_config, "DB_PATH", db_path)
    storage.init_db()
    return db_path


def _seed_stock(ts_code: str, df: pd.DataFrame, name: str = "测试标的", industry: str = "银行"):
    storage.save_stock_list(pd.DataFrame([{
        "ts_code": ts_code,
        "symbol": ts_code.split(".")[0],
        "name": name,
        "market": "主板",
        "list_date": "20100101",
        "delist_date": "",
        "industry": industry,
        "is_st": 0,
    }]))
    storage.save_daily(df)


def _make_daily_df(
    ts_code: str,
    start: str,
    periods: int,
    base: float = 20.0,
    drift: float = 0.001,
    spread: float = 0.02,
) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=periods).strftime("%Y%m%d").tolist()
    closes = [round(base * ((1.0 + drift) ** i), 2) for i in range(periods)]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "ts_code": ts_code,
        "trade_date": dates,
        "open": opens,
        "high": [round(max(o, c) * (1.0 + spread), 2) for o, c in zip(opens, closes)],
        "low": [round(min(o, c) * (1.0 - spread), 2) for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [2_000_000.0] * periods,
        "amount": [c * 200_000.0 for c in closes],
        "pct_chg": [0.0] + [round((closes[i] / closes[i - 1] - 1.0) * 100.0, 2) for i in range(1, periods)],
        "turnover": [1.5] * periods,
        "adj_factor": [1.0] * periods,
    })


def test_baostock_code_conversion_and_priority_over_akshare(monkeypatch, tmp_path):
    """验证 Baostock 代码转换、单股快速估值解析及优先于 AKShare 全市场爬取"""
    assert valuation_service._to_baostock_code("600036.SH") == "sh.600036"
    assert valuation_service._to_baostock_code("000001.SZ") == "sz.000001"
    assert valuation_service._to_baostock_code("688981.SH") == "sh.688981"

    _use_temp_db(monkeypatch, tmp_path, "val_test.db")
    monkeypatch.setattr(core_config, "TUSHARE_TOKEN", "")
    monkeypatch.setattr(valuation_service, "TUSHARE_TOKEN", "", raising=False)

    ak_called = {"flag": False}
    fake_ak = types.SimpleNamespace(
        stock_zh_a_spot_em=lambda: ak_called.__setitem__("flag", True) or pd.DataFrame()
    )
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)

    class _FakeRs:
        def __init__(self, rows):
            self.error_code = "0"
            self._rows = rows
            self._idx = -1

        def next(self):
            self._idx += 1
            return self._idx < len(self._rows)

        def get_row_data(self):
            return self._rows[self._idx]

    class _FakeLogin:
        error_code = "0"

    import baostock as bs
    monkeypatch.setattr(bs, "login", lambda: _FakeLogin())
    monkeypatch.setattr(bs, "logout", lambda: None)

    def _fake_query(bs_code, fields, start_date="", end_date="", frequency="d", **kwargs):
        if bs_code == "sh.600036":
            return _FakeRs([["2026-11-24", "43.52", "7.18", "0.93", "3.13"]])
        if bs_code == "sz.000001":
            return _FakeRs([["2026-11-24", "11.20", "-5.40", "0.62", "1.10"]])
        return _FakeRs([])

    monkeypatch.setattr(bs, "query_history_k_data_plus", _fake_query)

    vmap = valuation_service.fetch_and_cache_valuations(["600036.SH", "000001.SZ", "510310.SH"])
    assert ak_called["flag"] is False
    assert vmap["600036.SH"]["pe_ttm"] == pytest.approx(7.18, abs=1e-2)
    assert vmap["600036.SH"]["pb"] == pytest.approx(0.93, abs=1e-2)
    assert vmap["000001.SZ"]["pe_ttm"] == pytest.approx(-5.40, abs=1e-2)


def test_portfolio_cash_yield_and_backtest_options_keys():
    """验证闲置现金每日计息精确性与 Backtester option key 契约"""
    assert "bear_position_scale" in BACKTEST_OPTION_KEYS
    assert "cash_yield_annual" in BACKTEST_OPTION_KEYS
    opts = build_backtest_options(bear_position_scale=0.5, cash_yield_annual=2.52)
    assert opts["bear_position_scale"] == pytest.approx(0.5)
    assert opts["cash_yield_annual"] == pytest.approx(2.52)

    pf = Portfolio(initial_capital=100_000.0)
    pf.accrue_cash_yield(2.52)  # 日利率 = 0.0252 / 252 = 0.0001 (+10元)
    assert pf.cash == pytest.approx(100_010.0, abs=1e-6)
    pf.accrue_cash_yield(0.0)
    assert pf.cash == pytest.approx(100_010.0, abs=1e-6)


def test_backtester_cash_yield_accrues_on_idle_cash(monkeypatch, tmp_path):
    """验证回测引擎在无持仓期间每日自动按年化利率计提货基收益"""
    _use_temp_db(monkeypatch, tmp_path, "cash_yield.db")
    df = _make_daily_df("600036.SH", "20240102", 40, base=20.0, drift=0.0)
    _seed_stock("600036.SH", df, name="招商银行")

    class _NoTradeStrategy(BaseStrategy):
        name = "no_trade"
        description = "不交易仅测试现金计息"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            return []

    bt = Backtester(
        strategy_cls=_NoTradeStrategy,
        params={},
        universe=["600036.SH"],
        start_date=df["trade_date"].iloc[0],
        end_date=df["trade_date"].iloc[-1],
        initial_capital=100_000.0,
        market_regime_filter=False,
        cash_yield_annual=2.52,
    )
    res = bt.run(save=False)
    assert len(res.equity_curve) == 40
    assert res.total_return > 0.35
    assert res.final_capital > 100_350.0


def test_atr_risk_sizing_aligns_with_strategy_rm_multiplier(monkeypatch, tmp_path):
    """验证 atr_risk 模式根据目标策略专属 ATR 止损倍数动态校准买入手数"""
    _use_temp_db(monkeypatch, tmp_path, "atr_align.db")
    # spread=0.015 使 kdj_cross(3.5*ATR) 与 ma_cross(2.2*ATR) 均位于非饱和区间 (5%~40%)
    df = _make_daily_df("600036.SH", "20240102", 60, base=20.0, drift=0.001, spread=0.015)
    _seed_stock("600036.SH", df, name="招商银行")
    trigger_day = df["trade_date"].iloc[30]

    def _make_one_buy_strategy(strat_name: str):
        class _OneBuy(BaseStrategy):
            name = strat_name
            description = f"测试 {strat_name}"
            param_schema = {}

            def on_bar(self, trade_date, data, portfolio=None):
                if trade_date == trigger_day and "600036.SH" in data:
                    px = float(data["600036.SH"].iloc[-1]["close"])
                    return [Signal(
                        ts_code="600036.SH",
                        trade_date=trade_date,
                        strategy=strat_name,
                        direction="BUY",
                        score=0.9,
                        price_ref=px,
                    )]
                return []

        return _OneBuy

    res_kdj = Backtester(
        strategy_cls=_make_one_buy_strategy("kdj_cross"),
        params={},
        universe=["600036.SH"],
        start_date=df["trade_date"].iloc[0],
        end_date=df["trade_date"].iloc[-1],
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager="auto",
        max_active_positions=2,
        position_sizing="atr_risk",
        risk_per_trade=0.015,
        market_regime_filter=False,
    ).run(save=False)

    res_ma = Backtester(
        strategy_cls=_make_one_buy_strategy("ma_cross"),
        params={},
        universe=["600036.SH"],
        start_date=df["trade_date"].iloc[0],
        end_date=df["trade_date"].iloc[-1],
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager="auto",
        max_active_positions=2,
        position_sizing="atr_risk",
        risk_per_trade=0.015,
        market_regime_filter=False,
    ).run(save=False)

    buys_kdj = [t for t in res_kdj.trades if t.direction == "BUY"]
    buys_ma = [t for t in res_ma.trades if t.direction == "BUY"]
    assert len(buys_kdj) == 1 and len(buys_ma) == 1

    mult_kdj = RiskManager.for_strategy("kdj_cross", use_atr_stop=True).atr_stop_mult
    mult_ma = RiskManager.for_strategy("ma_cross", use_atr_stop=True).atr_stop_mult
    assert mult_kdj > mult_ma
    # kdj_cross 止损更宽 -> 风险平价买入手数显著少于 ma_cross
    assert buys_kdj[0].volume < buys_ma[0].volume


def test_bear_position_scale_halves_buy_volume_when_dual_bear(monkeypatch, tmp_path):
    """验证熊市空头期 allow_bear_buy 策略在 bear_position_scale=0.5 时严格减半买入股数"""
    _use_temp_db(monkeypatch, tmp_path, "bear_scale.db")
    idx_bear = _make_daily_df("000300.SH", "20230103", 280, base=4500.0, drift=-0.0015, spread=0.01)
    cyb_bear = idx_bear.copy()
    cyb_bear["ts_code"] = "399006.SZ"
    storage.save_index_daily(idx_bear)
    storage.save_index_daily(cyb_bear)

    stock_df = _make_daily_df("600036.SH", "20230103", 280, base=20.0, drift=0.0005, spread=0.01)
    _seed_stock("600036.SH", stock_df, name="招商银行")
    buy_day = stock_df["trade_date"].iloc[260]

    class _BuyOnDay(BaseStrategy):
        name = "kdj_cross"
        description = "指定日反转买入测试熊市降仓"
        param_schema = {}
        allow_bear_buy = True

        def on_bar(self, trade_date, data, portfolio=None):
            if trade_date == buy_day and "600036.SH" in data:
                px = float(data["600036.SH"].iloc[-1]["close"])
                return [Signal(
                    ts_code="600036.SH",
                    trade_date=trade_date,
                    strategy="kdj_cross",
                    direction="BUY",
                    score=0.9,
                    price_ref=px,
                )]
            return []

    res_full = Backtester(
        strategy_cls=_BuyOnDay,
        params={},
        universe=["600036.SH"],
        start_date=stock_df["trade_date"].iloc[245],
        end_date=stock_df["trade_date"].iloc[-1],
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager=None,
        max_active_positions=2,
        position_sizing="score",
        market_regime_filter=True,
        dual_index_regime=True,
        bear_position_scale=1.0,
    ).run(save=False)

    res_half = Backtester(
        strategy_cls=_BuyOnDay,
        params={},
        universe=["600036.SH"],
        start_date=stock_df["trade_date"].iloc[245],
        end_date=stock_df["trade_date"].iloc[-1],
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager=None,
        max_active_positions=2,
        position_sizing="score",
        market_regime_filter=True,
        dual_index_regime=True,
        bear_position_scale=0.5,
    ).run(save=False)

    v_full = sum(t.volume for t in res_full.trades if t.direction == "BUY")
    v_half = sum(t.volume for t in res_half.trades if t.direction == "BUY")
    assert v_full > 0
    assert 0 < v_half <= v_full * 0.6


def test_continuous_rolling_backtest_preserves_open_positions(monkeypatch, tmp_path):
    """验证真实连续滚动回测在年度换仓时不强平老持仓，并延续原策略出场信号"""
    _use_temp_db(monkeypatch, tmp_path, "rolling_cont.db")
    df_a = _make_daily_df("600036.SH", "20210104", 1250, base=20.0, drift=0.0005)
    _seed_stock("600036.SH", df_a, name="招商银行")

    dates = df_a["trade_date"].tolist()
    d_2024 = [d for d in dates if d.startswith("2024")]
    d_2025 = [d for d in dates if d.startswith("2025")]
    assert len(d_2024) > 50 and len(d_2025) > 50
    buy_day = d_2024[-10]
    sell_day = d_2025[15]

    class _StratPeriodOne(BaseStrategy):
        name = "strat_p1"
        description = "一期买并在二期卖"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            sigs = []
            for code, df_c in data.items():
                px = float(df_c.iloc[-1]["close"])
                if trade_date == buy_day:
                    sigs.append(Signal(
                        ts_code=code, trade_date=trade_date, strategy="strat_p1",
                        direction="BUY", score=0.9, price_ref=px,
                    ))
                elif trade_date == sell_day:
                    sigs.append(Signal(
                        ts_code=code, trade_date=trade_date, strategy="strat_p1",
                        direction="SELL", score=0.9, price_ref=px,
                    ))
            return sigs

    class _StratPeriodTwo(BaseStrategy):
        name = "strat_p2"
        description = "二期接管新信号但不干扰一期持仓卖出"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            return []

    monkeypatch.setitem(STRATEGY_REGISTRY, "strat_p1", _StratPeriodOne)
    monkeypatch.setitem(STRATEGY_REGISTRY, "strat_p2", _StratPeriodTwo)

    calls = {"n": 0}

    def _fake_eval(code, start_date, end_date, **kw):
        calls["n"] += 1
        cls = _StratPeriodOne if end_date.startswith("2023") else _StratPeriodTwo
        return {
            "strategy": cls.name,
            "tag": "推荐使用",
            "is_oos": True,
            "oos_passed": True,
        }

    import services.strategy_recommender as sr
    monkeypatch.setattr(sr, "evaluate_stock_strategy", _fake_eval)

    roll_out = run_rolling_adaptive_backtest(
        universe=["600036.SH"],
        start_date=d_2024[0],
        end_date=d_2025[-1],
        rebalance_months=12,
        train_lookback_years=3,
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager=None,
        market_regime_filter=False,
    )
    res = roll_out["result"]
    assert calls["n"] == 2
    buys = [t for t in res.trades if t.direction == "BUY"]
    sells = [t for t in res.trades if t.direction == "SELL"]
    assert len(buys) == 1 and buys[0].trade_date == buy_day
    # 不得在 2024 年底强平，必须在 2025 年 sell_day 由原归属策略触发卖出
    assert len(sells) == 1 and sells[0].trade_date == sell_day


def test_schema_v8_position_strategy_and_signal_dedup_skip_held(monkeypatch, tmp_path):
    """验证 Schema v8 position.strategy 持久化、风控透传以及信号汇总跳过已持仓 BUY"""
    _use_temp_db(monkeypatch, tmp_path, "pos_v8.db")

    with storage.get_conn() as conn:
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(position)").fetchall()]
    assert uv >= 8
    assert "strategy" in cols

    storage.upsert_position_buy("600036.SH", buy_price=35.0, shares=500, buy_date="20260110", note="建仓", strategy="ma_cross")
    pos_list = storage.get_positions()
    assert len(pos_list) == 1
    assert pos_list[0]["strategy"] == "ma_cross"

    df_px = _make_daily_df("600036.SH", "20251001", 80, base=35.0, drift=0.001)
    _seed_stock("600036.SH", df_px, name="招商银行")

    report = build_position_risk_report()
    assert len(report["items"]) == 1
    assert report["items"][0]["diag"]["strategy_hint"] == "ma_cross"

    # 若建仓策略为 kdj_cross，持仓诊断应切换到反转策略规则并体现为 kdj_cross
    storage.upsert_position_buy("000001.SZ", buy_price=12.0, shares=500, buy_date="20260110", note="反转仓", strategy="kdj_cross")
    df_px2 = _make_daily_df("000001.SZ", "20251001", 80, base=12.0, drift=0.001)
    _seed_stock("000001.SZ", df_px2, name="平安银行")
    report2 = build_position_risk_report()
    hint_by_code = {it["ts_code"]: it["diag"]["strategy_hint"] for it in report2["items"]}
    assert hint_by_code == {"600036.SH": "ma_cross", "000001.SZ": "kdj_cross"}

    sigs = [
        Signal(ts_code="000001.SZ", trade_date="20260110", strategy="ma_cross", direction="BUY", score=0.90, price_ref=12.5),
        Signal(ts_code="600519.SH", trade_date="20260110", strategy="signal_combo", direction="BUY", score=0.85, price_ref=1500.0),
        Signal(ts_code="600036.SH", trade_date="20260110", strategy="kdj_cross", direction="SELL", score=0.80, price_ref=36.0),
    ]
    held_map = {"600036.SH": 500, "000001.SZ": 500}

    rows_skip, _ = build_deduplicated_broker_orders(
        sigs, capital=200_000.0, max_pos_pct=25.0, held_map=held_map, skip_held_buys=True,
    )
    actions_skip = [(r["标的代码"], r["操作"]) for r in rows_skip]
    assert ("000001.SZ", "买入") not in actions_skip
    assert ("600519.SH", "买入") in actions_skip
    assert ("600036.SH", "卖出") in actions_skip

    rows_keep, _ = build_deduplicated_broker_orders(
        sigs, capital=200_000.0, max_pos_pct=25.0, held_map=held_map, skip_held_buys=False,
    )
    actions_keep = [(r["标的代码"], r["操作"]) for r in rows_keep]
    assert ("000001.SZ", "买入") in actions_keep
