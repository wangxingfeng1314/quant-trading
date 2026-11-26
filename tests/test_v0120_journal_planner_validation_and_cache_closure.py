"""v0.12.0 收口优化测试套件:
1. reduce_position 平仓账本自动继承 position.strategy（手工平仓不丢分策略归因）
2. build_core_satellite_plan 批量行情加载、深低估低波蓝筹豁免（无股息率时基于 PE/PB）与 ETF 3位小数精度
3. STRATEGY_CHINESE_NAMES 补齐双舱/滚动/路由/风控中文映射 + calc_signal_forward_return 扣费精算
4. 信号/看板/回测页面清除残余未缓存 get_instrument_list() 及统一 _get_benchmark_curve 级联
"""
from __future__ import annotations

import inspect
import pandas as pd
import pytest

from core import config as core_config
from data import storage
from engine.commission import is_etf_code


def _use_temp_db(monkeypatch, tmp_path, name: str = "test_v0120.db"):
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


def _make_daily_df(
    ts_code: str,
    start: str,
    periods: int,
    base: float = 20.0,
    spread: float = 0.015,
) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=periods).strftime("%Y%m%d").tolist()
    dec = 3 if is_etf_code(ts_code) else 2
    closes = [round(base, dec)] * periods
    return pd.DataFrame({
        "ts_code": [ts_code] * periods,
        "trade_date": dates,
        "open": closes,
        "high": [round(base * (1.0 + spread * 0.5), dec)] * periods,
        "low": [round(base * (1.0 - spread * 0.5), dec)] * periods,
        "close": closes,
        "volume": [1_000_000.0] * periods,
        "amount": [base * 1_000_000.0] * periods,
        "pct_chg": [0.0] * periods,
    })


def test_reduce_position_inherits_strategy_when_not_specified(monkeypatch, tmp_path):
    """验证平仓不传 strategy 时自动继承建仓策略并持久化到 real_trade_journal"""
    _use_temp_db(monkeypatch, tmp_path, "reduce_inherit.db")

    pid = storage.upsert_position_buy(
        ts_code="600036.SH",
        buy_price=40.0,
        shares=500,
        buy_date="20261110",
        strategy="kdj_cross",
    )
    res_inherit = storage.reduce_position(
        ts_code="600036.SH",
        sell_shares=200,
        sell_price=42.0,
        sell_date="20261120",
        strategy="",
        position_id=pid,
    )
    assert res_inherit["strategy"] == "kdj_cross"

    res_override = storage.reduce_position(
        ts_code="600036.SH",
        sell_shares=300,
        sell_price=43.0,
        sell_date="20261122",
        strategy="risk_manager",
        position_id=pid,
    )
    assert res_override["strategy"] == "risk_manager"

    j_df = storage.get_trade_journal(limit=10)
    sells = j_df[j_df["direction"] == "SELL"].reset_index(drop=True)
    assert len(sells) == 2
    assert sells.loc[0, "strategy"] == "risk_manager"
    assert sells.loc[1, "strategy"] == "kdj_cross"


def test_core_satellite_plan_batch_load_deep_value_and_etf_precision(monkeypatch, tmp_path):
    """验证核心-卫星规划器批量查询、无股息深低估破净豁免及 ETF 3位小数输出"""
    from services import valuation_service

    _use_temp_db(monkeypatch, tmp_path, "cs_plan.db")

    df_stk = _make_daily_df("601318.SH", "20260701", 80, base=50.0, spread=0.012)
    df_etf = _make_daily_df("512890.SH", "20260701", 80, base=1.248, spread=0.010)
    storage.save_daily(df_stk)
    storage.save_daily(df_etf)

    # Mock batch_get_latest & spy get_daily to confirm zero N+1 SQL roundtrips
    combined = pd.concat([df_stk, df_etf], ignore_index=True)
    calls = {"batch": 0, "daily": 0}

    def _spy_batch(codes, limit=90, chunk_size=500):
        calls["batch"] += 1
        return combined[combined["ts_code"].isin(codes)].copy()

    def _spy_daily(*a, **kw):
        calls["daily"] += 1
        return pd.DataFrame()

    monkeypatch.setattr("data.storage.batch_get_latest", _spy_batch)
    monkeypatch.setattr(valuation_service, "get_daily", _spy_daily)

    wl_df = pd.DataFrame([
        # 无 dividend_yield，但 0<PE<=12 且 0<PB<=1.2 且 ATR<2.2% -> 即使暂不适配也归入 核心底仓
        {"ts_code": "601318.SH", "recommended_strategy": "⛔ 暂不适配·观望"},
        {"ts_code": "512890.SH", "recommended_strategy": "🔄 动量轮动"},
    ])
    val_map = {
        "601318.SH": {"pe_ttm": 10.1, "pb": 0.95, "dividend_yield": None, "pe_percentile": 97.5},
    }
    inst_df = pd.DataFrame([
        {"ts_code": "601318.SH", "name": "中国平安", "type": "股票"},
        {"ts_code": "512890.SH", "name": "红利低波ETF", "type": "ETF"},
    ])

    plan = valuation_service.build_core_satellite_plan(
        watchlist_df=wl_df,
        capital=100_000.0,
        core_ratio=0.70,
        val_map=val_map,
        inst_df=inst_df,
    )
    assert calls["batch"] == 1
    assert calls["daily"] == 0

    by_code = {r["代码"]: r for r in plan["rows"]}
    assert by_code["601318.SH"]["资产角色"] == "🧱 核心底仓"
    assert by_code["512890.SH"]["现价"] == pytest.approx(1.248)
    assert abs(round(by_code["512890.SH"]["ATR止损价"], 3) - by_code["512890.SH"]["ATR止损价"]) < 1e-9


def test_strategy_cn_names_and_signal_forward_net_return():
    """验证新增策略中文名映射与信号前向净收益扣费逻辑"""
    from app.st_utils import format_strategy_cn
    from app.signal import calc_signal_forward_return

    for code_key in (
        "dual_sleeve_core_satellite",
        "rolling_adaptive_portfolio",
        "adaptive_router",
        "risk_manager",
    ):
        cn = format_strategy_cn(code_key)
        assert cn != code_key and len(cn) > 0

    gross_stk = calc_signal_forward_return(10.0, 10.01, "BUY", ts_code="600036.SH", deduct_cost=False)
    net_stk = calc_signal_forward_return(10.0, 10.01, "BUY", ts_code="600036.SH", deduct_cost=True)
    net_etf = calc_signal_forward_return(10.0, 10.01, "BUY", ts_code="512890.SH", deduct_cost=True)
    assert gross_stk == pytest.approx(0.10)
    assert net_stk < 0.0 < gross_stk   # 微涨 +0.10% 扣掉股票双边费+滑点+印花税后为负
    assert net_etf > net_stk           # ETF 免印花税与过户费，净收益严格高于同涨幅个股


def test_ui_pages_use_cached_instrument_list_and_unified_benchmark_cascade():
    """验证首页/信号/回测已消除未缓存 get_instrument_list() 且基准曲线统一多源级联"""
    import app.dashboard as dash
    import app.signal as sig_page
    import app.backtest as bt_page

    assert "get_instrument_list()" not in inspect.getsource(dash._show_today_signals)
    assert "get_instrument_list()" not in inspect.getsource(sig_page._show_signal_validation)
    assert "get_instrument_list()" not in inspect.getsource(sig_page._show_composite)

    bm_src = inspect.getsource(bt_page._get_benchmark_curve)
    assert "fetch_index_daily" in bm_src
    assert "akshare" not in bm_src
