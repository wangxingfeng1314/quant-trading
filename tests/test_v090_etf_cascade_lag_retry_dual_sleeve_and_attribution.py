"""v0.9.0 核心优化测试套件:
1. Baostock 线程安全会话 + ETF(3位小数)/指数三级级联兜底
2. Baostock 3年 PE-TTM 历史分位计算 & 低分位估值守卫纠偏
3. init_data 增量更新「非空但日期滞后」检测与绕开 TickFlow 备用源补拉
4. position.strategy 中文展示名归一化 + 持仓诊断反转风控对齐 + notify_signals 已持仓拦截/熊市半仓/估值徽章
5. Position / Portfolio / Backtester 保留 entry_strategy 与 exit_strategy 真实归因
6. 双舱位核心-卫星组合回测 run_dual_sleeve_backtest 净值与成交合并
"""
from __future__ import annotations

import pandas as pd
import pytest

from core import config as core_config
from core.models import Signal
from data import fetcher, storage
from data.cleaner import clean_daily
from engine.backtester import Backtester, run_dual_sleeve_backtest
from engine.portfolio import Portfolio
from engine.risk_manager import RiskManager
from notifier import push as push_mod
from services import position_service, valuation_service
from strategies import STRATEGY_REGISTRY
from strategies.base import BaseStrategy


def _use_temp_db(monkeypatch, tmp_path, name: str = "test_v090.db"):
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
    closes = [round(base * ((1.0 + drift) ** i), 3 if ts_code.startswith(("51", "15")) else 2) for i in range(periods)]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "ts_code": ts_code,
        "trade_date": dates,
        "open": opens,
        "high": [round(max(o, c) * (1.0 + spread), 3 if ts_code.startswith(("51", "15")) else 2) for o, c in zip(opens, closes)],
        "low": [round(min(o, c) * (1.0 - spread), 3 if ts_code.startswith(("51", "15")) else 2) for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [2_000_000.0] * periods,
        "amount": [round(c * 200_000.0, 2) for c in closes],
        "pct_chg": [0.0] + [round((closes[i] / closes[i - 1] - 1.0) * 100.0, 2) for i in range(1, periods)],
        "turnover": [1.5] * periods,
        "adj_factor": [1.0] * periods,
    })


def test_etf_and_index_baostock_cascade_and_shared_session(monkeypatch):
    """验证 ETF 3位小数精度级联兜底、指数 Baostock 兜底以及共享会话不互相 logout"""
    import baostock as bs

    monkeypatch.setattr(fetcher, "_fetch_daily_tickflow", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(fetcher, "_fetch_index_daily_tickflow", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(fetcher, "_fetch_etf_daily_akshare", lambda *a, **kw: pd.DataFrame())

    stats = {"login": 0, "logout": 0, "calls": 0}

    class _FakeRs:
        def __init__(self, rows, fields, err="0"):
            self.error_code = err
            self.fields = fields
            self._rows = rows
            self._idx = -1

        def next(self):
            self._idx += 1
            return self._idx < len(self._rows)

        def get_row_data(self):
            return self._rows[self._idx]

    class _FakeLogin:
        error_code = "0"

    def _fake_login():
        stats["login"] += 1
        return _FakeLogin()

    def _fake_logout():
        stats["logout"] += 1

    def _fake_query(bs_code, fields, start_date="", end_date="", frequency="d", adjustflag="2", **kw):
        stats["calls"] += 1
        # 第2次请求模拟 session 10001001 过期，验证自动重登恢复
        if stats["calls"] == 2:
            return _FakeRs([], fields.split(","), err="10001001")
        if bs_code == "sh.512890":
            return _FakeRs(
                [["2026-11-24", "1.249", "1.257", "1.242", "1.2554", "708770901", "886457786.0", "", "-0.08"]],
                fields.split(","),
            )
        if bs_code == "sh.000300":
            return _FakeRs(
                [["2026-11-24", "4000.0", "4020.0", "3980.0", "4010.5", "1000000", "0.25"]],
                fields.split(","),
            )
        return _FakeRs([], fields.split(","))

    monkeypatch.setattr(bs, "login", _fake_login)
    monkeypatch.setattr(bs, "logout", _fake_logout)
    monkeypatch.setattr(bs, "query_history_k_data_plus", _fake_query)

    df_etf = fetcher.fetch_etf_daily("512890.SH", "20261120", "20261124")
    assert len(df_etf) == 1
    assert df_etf.iloc[0]["close"] == pytest.approx(1.255, abs=1e-4)  # ETF保留3位小数
    assert df_etf.iloc[0]["turnover"] == 0.0

    # 触发第2次查询(10001001) + 重登成功拉回指数
    import types, sys
    fake_ak = types.SimpleNamespace(stock_zh_index_daily=lambda **kw: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "akshare", fake_ak)
    df_idx = fetcher.fetch_index_daily("000300.SH", "20261120", "20261124")
    assert len(df_idx) == 1
    assert df_idx.iloc[0]["close"] == pytest.approx(4010.5)
    assert stats["logout"] == 0  # 共享会话绝不互相 logout
    assert stats["login"] == 2  # 初次登录1次 + 10001001过期恢复1次


def test_baostock_valuation_3y_pe_percentile_and_guard_cyclical_exception(monkeypatch, tmp_path):
    """验证 3年 PE-TTM 历史分位计算，以及低分位(<=20%)周期股不被 PE>60 误判高估"""
    import baostock as bs

    _use_temp_db(monkeypatch, tmp_path, "val_pct_v090.db")

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

    monkeypatch.setattr(bs, "login", lambda: _FakeLogin())
    monkeypatch.setattr(bs, "logout", lambda: None)

    def _fake_query(bs_code, fields, start_date="", end_date="", frequency="d", **kw):
        if bs_code == "sh.600460":
            # 前29天高PE(70..98)，最新一天PE=63.03(仍>60但处于3年极低3.3%分位)
            rows = [[f"2025-01-{i+1:02d}", "30.0", str(70.0 + i), "3.0", "2.0"] for i in range(29)]
            rows.append(["2026-11-24", "30.0", "63.03", "2.97", "2.0"])
            return _FakeRs(rows)
        if bs_code == "sz.000538":
            # PE从25降到18.95 -> 最低分位
            rows = [[f"2025-01-{i+1:02d}", "55.0", str(30.0 - i * 0.2), "2.5", "2.0"] for i in range(29)]
            rows.append(["2026-11-24", "55.0", "18.95", "2.45", "2.0"])
            return _FakeRs(rows)
        return _FakeRs([])

    monkeypatch.setattr(bs, "query_history_k_data_plus", _fake_query)

    now_iso = "2026-11-25T15:30:00"
    res = valuation_service._fetch_valuations_baostock(["600460.SH", "000538.SZ"], now_iso)
    assert res["600460.SH"]["pe_percentile"] == pytest.approx(3.3, abs=0.2)
    assert res["000538.SZ"]["pe_percentile"] == pytest.approx(3.3, abs=0.2)

    guard_cyc = valuation_service.evaluate_valuation_guard("600460.SH", res["600460.SH"])
    assert guard_cyc["zone"] == "低估高息"
    assert guard_cyc["allow_left_buy"] is True
    assert "3%" in guard_cyc["detail"] or "分位" in guard_cyc["detail"]

    guard_high = valuation_service.evaluate_valuation_guard(
        "600999.SH", {"pe_ttm": 75.0, "pb": 5.0, "pe_percentile": 90.0}
    )
    assert guard_high["zone"] == "估值偏高"
    assert guard_high["allow_left_buy"] is False


def test_init_data_detects_non_empty_lagging_date_and_retries_fallback(monkeypatch, tmp_path):
    """验证增量更新能识别非空但停在昨日的数据，并绕开 TickFlow 用备用源补齐当日K线"""
    import scripts.init_data as init_data

    _use_temp_db(monkeypatch, tmp_path, "lag_retry.db")
    df_base_1 = _make_daily_df("600036.SH", "20261110", 10, base=40.0)
    df_base_2 = _make_daily_df("600887.SH", "20261110", 10, base=28.0)
    _seed_stock("600036.SH", df_base_1, name="招商银行")
    _seed_stock("600887.SH", df_base_2, name="伊利股份")
    storage.add_to_watchlist("600036.SH")
    storage.add_to_watchlist("600887.SH")

    with storage.get_conn() as conn:
        old_max = conn.execute("SELECT MAX(trade_date) FROM daily_price WHERE ts_code='600887.SH'").fetchone()[0]
    next_dt = (pd.Timestamp(old_max) + pd.offsets.BDay(2)).strftime("%Y%m%d")
    lag_dt = (pd.Timestamp(old_max) + pd.offsets.BDay(1)).strftime("%Y%m%d")

    monkeypatch.setattr("time.sleep", lambda *a: None)
    monkeypatch.setattr(init_data, "update_all_indices", lambda *a, **kw: 0)
    monkeypatch.setattr(init_data, "fetch_etf_list", lambda: pd.DataFrame())
    monkeypatch.setattr(init_data, "save_index_daily", lambda *a, **kw: None)

    seen_skip_calls = []

    def _fake_fetch(ts_code, start_date, end_date, skip_tickflow=False):
        if skip_tickflow:
            seen_skip_calls.append(ts_code)
        base_px = 40.0 if ts_code == "600036.SH" else 28.0
        dt = next_dt if (ts_code == "600036.SH" or skip_tickflow) else lag_dt
        return pd.DataFrame([{
            "ts_code": ts_code,
            "trade_date": dt,
            "open": base_px,
            "high": base_px * 1.01,
            "low": base_px * 0.99,
            "close": base_px,
            "volume": 2_000_000.0,
            "amount": base_px * 2_000_000.0,
            "pct_chg": 0.5,
            "turnover": 1.2,
            "adj_factor": 1.0,
        }])

    monkeypatch.setattr(init_data, "fetch_instrument_daily", _fake_fetch)
    monkeypatch.setattr(init_data, "check_split_dividend_anomaly", lambda *a, **kw: False)
    monkeypatch.setattr(init_data, "align_volume_unit", lambda tc, df: df)

    rc = init_data.run_update(days=5, watchlist=True)
    assert rc in (0, None)
    assert "600887.SH" in seen_skip_calls
    assert storage.get_latest_date("600887.SH") == next_dt


def test_position_chinese_strategy_normalization_and_notify_guardrails(monkeypatch, tmp_path):
    """验证中文策略名归一化触发反转止损，以及日报跳过已持仓、熊市半仓与估值徽章"""
    _use_temp_db(monkeypatch, tmp_path, "pos_notify_v090.db")

    # 1. 中文策略名 'KDJ短线' 必须精确解析为 'kdj_cross' 并走反转风控分支 (max_holding_days=60, BE禁用)
    assert position_service._resolve_strategy_hint("600036.SH", "KDJ短线") == "kdj_cross"
    assert position_service._resolve_strategy_hint("600900.SH", "布林反转") == "bollinger_reversal"

    df_px = _make_daily_df("600036.SH", "20250901", 90, base=40.0, drift=0.001)
    _seed_stock("600036.SH", df_px, name="招商银行")
    _seed_stock("600660.SH", _make_daily_df("600660.SH", "20250901", 90, base=50.0, drift=0.001), name="福耀玻璃")

    storage.upsert_position_buy("600036.SH", buy_price=40.0, shares=500, buy_date=df_px["trade_date"].iloc[-5], strategy="KDJ短线")
    report = position_service.build_position_risk_report()
    assert report["items"][0]["diag"]["strategy_hint"] == "kdj_cross"

    # 2. 缓存估值快照
    storage.save_valuation_batch([{
        "ts_code": "600660.SH", "pe_ttm": 13.5, "pb": 3.2,
        "dividend_yield": 3.8, "roe": None, "market_cap_yi": 1200.0,
        "pe_percentile": 22.0, "next_report_date": "", "updated_at": "2026-11-25T15:00:00",
    }])

    # 3. 构造双空头 BEAR 环境下的买入信号（600036 已持仓应跳过，600660 应按熊市半仓减半建议股数并带低估高息徽章）
    captured = {}
    monkeypatch.setattr("data.storage.get_instrument_name", lambda c: {"600036.SH": "招商银行", "600660.SH": "福耀玻璃"}.get(c, c))
    monkeypatch.setattr(
        "services.signal_service.get_dual_market_regime",
        lambda *a, **kw: {
            "large_cap": {"label": "🔴 空头防守", "max_position_pct": 25, "regime": "BEAR"},
            "growth": {"label": "🔴 空头防守", "regime": "BEAR"},
            "combined_regime": "BEAR",
        },
    )
    monkeypatch.setattr("engine.scanner.annotate_portfolio_guardrails", lambda s, **kw: (s, {}))
    monkeypatch.setattr("services.signal_service.get_data_health_summary", lambda *a, **kw: {"has_issues": False})
    monkeypatch.setattr(push_mod, "send_notification", lambda title, content, **kw: captured.update({"content": content}) or True)

    sig_held = Signal(ts_code="600036.SH", trade_date="20261125", strategy="kdj_cross", direction="BUY", score=0.85, price_ref=41.0, reason="金叉")
    sig_new = Signal(ts_code="600660.SH", trade_date="20261125", strategy="ma_cross", direction="BUY", score=0.82, price_ref=50.0, reason="放量站上MA20")

    ok = push_mod.notify_signals([sig_held, sig_new], skip_held_buys=True)
    assert ok is True
    md = captured["content"]
    assert "已自动跳过 1 条当前已持仓标的的重复买入推荐" in md
    assert "招商银行(600036.SH)" not in md.split("### 🔴 卖出信号")[0]
    assert "福耀玻璃(600660.SH)" in md
    assert "(熊市半仓)" in md
    assert "💎低估高息" in md


def test_portfolio_and_backtester_preserve_entry_strategy_on_rm_exit(monkeypatch, tmp_path):
    """验证 RiskManager 强制平仓后 Trade.context_snapshot 仍精确保留 entry_strategy"""
    _use_temp_db(monkeypatch, tmp_path, "entry_attr.db")
    dates = pd.bdate_range("20250102", periods=65).strftime("%Y%m%d").tolist()
    closes = [20.0] * 35 + [20.0 * (0.97 ** i) for i in range(1, 31)]
    opens = [closes[0]] + closes[:-1]
    df = pd.DataFrame({
        "ts_code": "600036.SH",
        "trade_date": dates,
        "open": opens,
        "high": [round(max(o, c) * 1.01, 2) for o, c in zip(opens, closes)],
        "low": [round(min(o, c) * 0.99, 2) for o, c in zip(opens, closes)],
        "close": [round(c, 2) for c in closes],
        "volume": [2_000_000.0] * len(dates),
        "amount": [c * 200_000.0 for c in closes],
        "pct_chg": [0.0] + [round((closes[i] / closes[i - 1] - 1.0) * 100.0, 2) for i in range(1, len(dates))],
        "turnover": [1.5] * len(dates),
        "adj_factor": [1.0] * len(dates),
    })
    _seed_stock("600036.SH", df, name="招商银行")

    buy_day = dates[34]

    class _EntryStrat(BaseStrategy):
        name = "signal_combo"
        description = "买后触发风控止损测试归因保留"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            if trade_date == buy_day and "600036.SH" in data:
                px = float(data["600036.SH"].iloc[-1]["close"])
                return [Signal(ts_code="600036.SH", trade_date=trade_date, strategy="signal_combo", direction="BUY", score=0.85, price_ref=px)]
            return []

    rm = RiskManager(stop_loss_pct=-5.0, use_atr_stop=False)
    res = Backtester(
        strategy_cls=_EntryStrat,
        params={},
        universe=["600036.SH"],
        start_date=dates[0],
        end_date=dates[-1],
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager=rm,
        market_regime_filter=False,
    ).run(save=False)

    sells = [t for t in res.trades if t.direction == "SELL"]
    assert len(sells) == 1
    snap = sells[0].context_snapshot
    assert snap["strategy"] == "risk_manager"
    assert snap["exit_strategy"] == "risk_manager"
    assert snap["entry_strategy"] == "signal_combo"


def test_dual_sleeve_backtest_merges_stock_and_etf_sleeves(monkeypatch, tmp_path):
    """验证 run_dual_sleeve_backtest 对齐合并个股滚动与ETF轮动双舱位净值及成交记录"""
    _use_temp_db(monkeypatch, tmp_path, "dual_sleeve.db")

    df_stk = _make_daily_df("600036.SH", "20210104", 1250, base=20.0, drift=0.0005)
    df_etf1 = _make_daily_df("510310.SH", "20230601", 650, base=3.5, drift=0.0008)
    df_etf2 = _make_daily_df("518880.SH", "20230601", 650, base=4.8, drift=0.0012)
    df_etf3 = _make_daily_df("511010.SH", "20230601", 650, base=130.0, drift=0.0001)
    _seed_stock("600036.SH", df_stk, name="招商银行")
    storage.save_daily(df_etf1)
    storage.save_daily(df_etf2)
    storage.save_daily(df_etf3)

    d_2024 = [d for d in df_stk["trade_date"].tolist() if d.startswith("2024")]
    d_2025 = [d for d in df_stk["trade_date"].tolist() if d.startswith("2025")]
    buy_day = d_2024[30]
    sell_day = d_2025[20]

    class _FixedStockStrat(BaseStrategy):
        name = "mock_dual_stock"
        description = "双舱测试个股策略"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            if "600036.SH" not in data:
                return []
            px = float(data["600036.SH"].iloc[-1]["close"])
            if trade_date == buy_day:
                return [Signal(ts_code="600036.SH", trade_date=trade_date, strategy="mock_dual_stock", direction="BUY", score=0.9, price_ref=px)]
            if trade_date == sell_day:
                return [Signal(ts_code="600036.SH", trade_date=trade_date, strategy="mock_dual_stock", direction="SELL", score=0.9, price_ref=px)]
            return []

    monkeypatch.setitem(STRATEGY_REGISTRY, "mock_dual_stock", _FixedStockStrat)
    import services.strategy_recommender as sr
    monkeypatch.setattr(sr, "evaluate_stock_strategy", lambda *a, **kw: {
        "strategy": "mock_dual_stock", "tag": "推荐使用", "is_oos": True, "oos_passed": True,
    })

    out = run_dual_sleeve_backtest(
        stock_universe=["600036.SH"],
        etf_universe=["510310.SH", "518880.SH", "511010.SH"],
        start_date=d_2024[0],
        end_date=d_2025[-1],
        stock_weight=0.6,
        rebalance_months=12,
        train_lookback_years=3,
        etf_rebalance_days=10,
        etf_top_k=2,
        initial_capital=100_000.0,
        execution_mode="current_close",
        risk_manager=None,
        market_regime_filter=False,
        cash_yield_annual=1.8,
    )
    comb = out["result"]
    s_res = out["stock_result"]
    e_res = out["etf_result"]
    assert out["weights"] == {"stock": 0.6, "etf": 0.4}
    assert comb.initial_capital == pytest.approx(100_000.0)
    assert len(comb.equity_curve) > 200
    assert comb.final_capital == pytest.approx(s_res.final_capital + e_res.final_capital, abs=5.0)
    sleeves = {t.context_snapshot.get("sleeve") for t in comb.trades}
    assert "个股滚动" in sleeves
    assert "ETF轮动" in sleeves
