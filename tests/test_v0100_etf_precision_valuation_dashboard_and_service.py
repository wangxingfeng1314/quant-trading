"""v0.10.0 核心优化测试套件:
1. 场内 ETF 0.001 元最小变动价位 (tick precision) 与中登零过户费 (commission + portfolio)
2. 双舱位核心-卫星组合 ETF 子舱默认 ATR 风险平价仓位模型 (run_dual_sleeve_backtest)
3. 低估值破净标的 3 年 PE 高分位误杀防护 (evaluate_valuation_guard)
4. BacktestService 全量引擎选项透传与 run_dual_sleeve 入口
5. 首页驾驶舱批量 SQL 查询 (消除 N+1)、估值分位展示与多源指数级联
"""
from __future__ import annotations

import inspect
import json
import pandas as pd
import pytest

from core import config as core_config
from core.exceptions import ValidationError
from core.models import Signal
from data import storage
from engine.backtester import Backtester, build_backtest_options, run_dual_sleeve_backtest
from engine.commission import (
    is_etf_code, calc_cost, apply_slippage, get_price_limits, adjust_price,
)
from engine.portfolio import Portfolio
from services.backtest_service import BacktestService
from services.valuation_service import evaluate_valuation_guard
from strategies import STRATEGY_REGISTRY
from strategies.base import BaseStrategy


def _use_temp_db(monkeypatch, tmp_path, name: str = "test_v0100.db"):
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
    dec = 3 if is_etf_code(ts_code) else 2
    closes = [round(base * ((1.0 + drift) ** i), dec) for i in range(periods)]
    opens = [closes[0]] + [closes[i - 1] for i in range(1, periods)]
    highs = [round(max(opens[i], closes[i]) * (1.0 + spread * 0.5), dec) for i in range(periods)]
    lows = [round(min(opens[i], closes[i]) * (1.0 - spread * 0.5), dec) for i in range(periods)]
    pcts = [0.0] + [round((closes[i] - closes[i - 1]) / closes[i - 1] * 100.0, 2) for i in range(1, periods)]
    return pd.DataFrame({
        "ts_code": [ts_code] * periods,
        "trade_date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1_000_000.0] * periods,
        "amount": [closes[i] * 1_000_000.0 for i in range(periods)],
        "pct_chg": pcts,
    })


def test_etf_precision_3_decimals_and_zero_transfer_fee():
    """验证场内 ETF 使用 0.001 元最小变动价位且免征中登过户费，个股保持 0.01 元"""
    assert is_etf_code("512890.SH") is True
    assert is_etf_code("159915.SZ") is True
    assert is_etf_code("588000.SH") is True
    assert is_etf_code("560010.SH") is True
    assert is_etf_code("161725.SZ") is True
    assert is_etf_code("600036.SH") is False
    assert is_etf_code("113050.SH") is False
    assert is_etf_code("") is False

    # 1.234 * 1.0005 = 1.234617 -> ETF 舍入到 0.001 为 1.235，而非 0.01 的 1.23
    assert apply_slippage(1.234, "BUY", slippage_rate=0.0005, ts_code="512890.SH") == pytest.approx(1.235)
    assert apply_slippage(1.234, "SELL", slippage_rate=0.0005, ts_code="512890.SH") == pytest.approx(1.233)
    assert apply_slippage(1.234, "BUY", slippage_rate=0.0005, ts_code="600036.SH") == pytest.approx(1.23)

    lo, hi = get_price_limits(1.234, ts_code="512890.SH")
    assert (lo, hi) == pytest.approx((1.111, 1.357))
    assert adjust_price(1.2345, 1.234, ts_code="512890.SH") == pytest.approx(1.235)

    etf_cost = calc_cost(1.234, 10000, "SELL", ts_code="512890.SH")
    assert etf_cost["transfer_fee"] == 0.0
    assert etf_cost["tax"] == 0.0
    assert etf_cost["commission"] == pytest.approx(max(12340.0 * core_config.COMMISSION_RATE, core_config.MIN_COMMISSION), abs=0.01)

    stk_cost = calc_cost(10.0, 10000, "BUY", ts_code="600036.SH")
    assert stk_cost["transfer_fee"] > 0.0

    # Portfolio.buy / Portfolio.sell 端到端透传 ts_code
    pf = Portfolio(initial_capital=50_000.0)
    t_buy = pf.buy("512890.SH", price=1.234, volume=10000, trade_date="20261124", prev_close=1.230, slippage=True)
    assert t_buy is not None
    assert t_buy.price == pytest.approx(1.235)  # 0.001 精度（默认 0.0005 滑点 -> 1.235）
    pf.on_new_day("20261125")
    t_sell = pf.sell("512890.SH", price=1.240, volume=10000, trade_date="20261125", prev_close=1.235, slippage=True)
    assert t_sell is not None
    assert t_sell.price == pytest.approx(1.239)
    assert t_sell.tax == 0.0


def test_dual_sleeve_default_etf_position_sizing_atr_risk(monkeypatch, tmp_path):
    """验证 run_dual_sleeve_backtest 默认启用 etf_position_sizing='atr_risk' 并准确记录参数"""
    sig = inspect.signature(run_dual_sleeve_backtest)
    assert sig.parameters["etf_position_sizing"].default == "atr_risk"

    _use_temp_db(monkeypatch, tmp_path, "dual_atr.db")
    df_stk = _make_daily_df("600036.SH", "20210104", 1250, base=20.0, drift=0.0005)
    df_etf1 = _make_daily_df("510310.SH", "20230601", 650, base=3.5, drift=0.0008, spread=0.025)
    df_etf2 = _make_daily_df("518880.SH", "20230601", 650, base=4.8, drift=0.0011, spread=0.015)
    _seed_stock("600036.SH", df_stk, name="招商银行")
    storage.save_daily(df_etf1)
    storage.save_daily(df_etf2)

    d_2024 = [d for d in df_stk["trade_date"].tolist() if d.startswith("2024")]
    d_2025 = [d for d in df_stk["trade_date"].tolist() if d.startswith("2025")]
    buy_day = d_2024[30]

    class _SimpleStockStrat(BaseStrategy):
        name = "mock_dual_stock_atr"
        description = "测试"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            if trade_date == buy_day and "600036.SH" in data:
                px = float(data["600036.SH"].iloc[-1]["close"])
                return [Signal(ts_code="600036.SH", trade_date=trade_date, strategy="mock_dual_stock_atr", direction="BUY", score=0.9, price_ref=px)]
            return []

    monkeypatch.setitem(STRATEGY_REGISTRY, "mock_dual_stock_atr", _SimpleStockStrat)
    import services.strategy_recommender as sr
    monkeypatch.setattr(sr, "evaluate_stock_strategy", lambda *a, **kw: {
        "strategy": "mock_dual_stock_atr", "tag": "推荐使用", "is_oos": True, "oos_passed": True,
    })

    out_atr = run_dual_sleeve_backtest(
        stock_universe=["600036.SH"],
        etf_universe=["510310.SH", "518880.SH"],
        start_date=d_2024[0],
        end_date=d_2025[-1],
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager=None,
        market_regime_filter=False,
    )
    meta = json.loads(out_atr["result"].params)
    assert meta["etf_position_sizing"] == "atr_risk"
    assert len(out_atr["etf_result"].trades) > 0


def test_valuation_guard_deep_value_sub_book_not_flagged_high():
    """验证绝对深低估破净标的（如 601318.SH PE=10.1, PB=0.95）不因 3 年低基数高分位被误判高估"""
    guard_pingan = evaluate_valuation_guard(
        "601318.SH",
        {"pe_ttm": 10.08, "pb": 0.95, "dividend_yield": 0.0, "pe_percentile": 97.5},
    )
    assert guard_pingan["zone"] == "估值合理"
    assert guard_pingan["allow_left_buy"] is True
    assert guard_pingan["score_adj"] == 0.0
    assert "PE分位=98%" in guard_pingan["detail"]

    # 真正的较高估值 + 高分位仍然严格拦截
    guard_high = evaluate_valuation_guard(
        "600999.SH",
        {"pe_ttm": 28.0, "pb": 3.2, "pe_percentile": 92.0},
    )
    assert guard_high["zone"] == "估值偏高"
    assert guard_high["allow_left_buy"] is False
    assert guard_high["score_adj"] == -0.08

    # 低估高息 + 非拥挤分位保持 💎低估高息
    guard_bank = evaluate_valuation_guard(
        "600036.SH",
        {"pe_ttm": 6.5, "pb": 0.75, "dividend_yield": 4.8, "pe_percentile": 35.0},
    )
    assert guard_bank["zone"] == "低估高息"
    assert guard_bank["allow_left_buy"] is True
    assert guard_bank["score_adj"] == 0.03

    # 亏损标的不受破净豁免影响，保持 ⚠️亏损(负PE)
    guard_loss = evaluate_valuation_guard(
        "600460.SH",
        {"pe_ttm": -12.0, "pb": 1.0, "pe_percentile": 95.0},
    )
    assert guard_loss["zone"] == "亏损预警"
    assert guard_loss["allow_left_buy"] is False


def test_backtest_service_options_forwarding_and_dual_sleeve(monkeypatch, tmp_path):
    """验证 BacktestService.run_backtest 选项透传及 run_dual_sleeve 校验与调用"""
    _use_temp_db(monkeypatch, tmp_path, "bt_service.db")
    df_stk = _make_daily_df("000001.SZ", "20240101", 80, base=12.0)
    _seed_stock("000001.SZ", df_stk, name="平安银行")

    captured = {}

    class _DummyBT:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, save=True):
            captured["save"] = save
            return "ok"

    monkeypatch.setattr("services.backtest_service.Backtester", _DummyBT)
    opts = build_backtest_options(
        max_active_positions=4,
        position_sizing="atr_risk",
        dual_index_regime=True,
        bear_position_scale=0.35,
        cash_yield_annual=1.8,
        max_open_gap_pct=3.0,
    )
    res = BacktestService.run_backtest(
        strategy_name="ma_cross",
        params={"fast": 5, "slow": 20},
        universe=["000001.SZ"],
        start_date="20240101",
        end_date="20240415",
        capital=200_000.0,
        save=False,
        backtest_options=opts,
        drawdown_brake_pct=10.0,
    )
    assert res == "ok"
    assert captured["position_sizing"] == "atr_risk"
    assert captured["max_active_positions"] == 4
    assert captured["dual_index_regime"] is True
    assert captured["bear_position_scale"] == 0.35
    assert captured["cash_yield_annual"] == 1.8
    assert captured["max_open_gap_pct"] == 3.0
    assert captured["drawdown_brake_pct"] == 10.0

    # run_dual_sleeve 输入校验与参数透传
    with pytest.raises(ValidationError):
        BacktestService.run_dual_sleeve([], [], "20240101", "20241231")

    dual_calls = {}
    monkeypatch.setattr(
        "services.backtest_service.run_dual_sleeve_backtest",
        lambda **kw: dual_calls.update(kw) or {"mock": True},
    )
    out = BacktestService.run_dual_sleeve(
        stock_universe=["000001.SZ"],
        etf_universe=["510310.SH"],
        start_date="20240101",
        end_date="20241231",
        capital=150_000.0,
        backtest_options={"cash_yield_annual": 1.8, "execution_mode": "next_open"},
    )
    assert out == {"mock": True}
    assert dual_calls["etf_position_sizing"] == "atr_risk"
    assert dual_calls["cash_yield_annual"] == 1.8
    assert dual_calls["execution_mode"] == "next_open"
    assert dual_calls["initial_capital"] == 150_000.0


def test_dashboard_watchlist_snapshot_uses_batch_query_and_valuation(monkeypatch):
    """验证驾驶舱自选股快照用 batch_get_latest 消除 N+1 查询，并正确展示 PE-TTM 与 PE分位%"""
    import app.dashboard as dash

    df1 = _make_daily_df("600036.SH", "20260601", 60, base=40.0)
    df2 = _make_daily_df("601318.SH", "20260601", 60, base=50.0)
    combined = pd.concat([df1, df2], ignore_index=True)

    calls = {"batch": 0, "daily": 0}

    def _fake_batch(codes, limit=2, chunk_size=500):
        calls["batch"] += 1
        return combined[combined["ts_code"].isin(codes)].copy()

    def _fake_daily(*args, **kwargs):
        calls["daily"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(dash, "batch_get_latest", _fake_batch)
    monkeypatch.setattr(dash, "get_daily", _fake_daily)
    monkeypatch.setattr(dash, "get_valuation_map", lambda codes=None: {
        "600036.SH": {"pe_ttm": 6.8, "pe_percentile": 42.0},
        "601318.SH": {"pe_ttm": 10.1, "pe_percentile": 97.5},
    })

    rows = dash._calc_watchlist_snapshot_rows.__wrapped__(
        (("600036.SH", "银行"), ("601318.SH", "保险")),
        (("600036.SH", "招商银行"), ("601318.SH", "中国平安")),
    )
    assert calls["batch"] == 1
    assert calls["daily"] == 0
    assert len(rows) == 2
    assert rows[0]["PE-TTM"] == "6.8"
    assert rows[0]["PE分位%"] == "42%"
    assert rows[1]["PE-TTM"] == "10.1"
    assert rows[1]["PE分位%"] == "98%"

    # 指数级联拉取复用 data.fetcher.fetch_index_daily
    monkeypatch.setattr(
        "data.fetcher.fetch_index_daily",
        lambda code, start_date="", end_date="": _make_daily_df(code, "20261001", 40, base=3800.0),
    )
    idx_df = dash._fetch_index_realtime.__wrapped__("000300.SH")
    assert len(idx_df) == 30
    assert idx_df["trade_date"].is_monotonic_increasing
