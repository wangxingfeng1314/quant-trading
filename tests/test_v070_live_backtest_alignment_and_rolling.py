"""v0.7.0 专项回归测试：策略可信度解锁 signal_combo、统一大盘择时单源、策略专属推送风控/组合护栏、推荐器全维度小样本惩罚、布林空头过滤、严格零前视滚动组合回测与数据健康监控"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from core.models import BacktestResult, Signal


def _make_ohlcv(
    ts_code: str,
    n_days: int = 80,
    start_price: float = 10.0,
    pct_returns: np.ndarray = None,
    start_date: str = "20260101",
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
        "amount": [35_000_000.0] * n_days,
        "pct_chg": [0.0] + [round((closes[i] / closes[i - 1] - 1.0) * 100.0, 2) for i in range(1, n_days)],
        "turnover": [2.5] * n_days,
        "adj_factor": [1.0] * n_days,
    })


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    import data.storage as st_mod

    db_file = tmp_path / "v070_test.db"
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
# P0-1: 清理活跃权重 & 解锁 signal_combo 实盘推送
# =====================================================================
def test_signal_combo_credibility_unlocks_live_push(monkeypatch):
    from services.signal_service import STRATEGY_CREDIBILITY_WEIGHTS
    from strategies.signal_combo import SignalComboStrategy
    import notifier.push as push_mod

    assert set(STRATEGY_CREDIBILITY_WEIGHTS.keys()) == {
        "signal_combo", "ma_cross", "momentum_rotation", "kdj_cross", "bollinger_reversal"
    }
    assert STRATEGY_CREDIBILITY_WEIGHTS["signal_combo"] >= 1.0

    # 构造 4 因子刚转强的边界触发行情：前一根未达 buy_threshold(3.0)，最后一根 4 因子全共振(4.0)
    dates = [(datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d") for i in range(35)]
    df = pd.DataFrame({
        "ts_code": "600660.SH",
        "trade_date": dates,
        "open": [49.5] * 34 + [50.0],
        "high": [50.2] * 34 + [51.8],
        "low": [49.2] * 34 + [49.9],
        "close": [49.8] * 34 + [51.5],
        "volume": [1_000_000.0] * 34 + [2_000_000.0],
        "vol_ma5": [1_000_000.0] * 35,
        "pct_chg": [0.1] * 34 + [3.4],
        "ma20": [50.0] * 34 + [50.1],
        "ma60": [49.0] * 35,
        "dif": [-0.1] * 34 + [0.2],
        "dea": [0.0] * 34 + [0.1],
        "macd_hist": [-0.2] * 34 + [0.3],
        "rsi14": [48.0] * 34 + [58.0],
    })
    strat = SignalComboStrategy()
    sigs = strat.on_bar(dates[-1], {"600660.SH": df})
    assert len(sigs) == 1 and sigs[0].direction == "BUY"

    # 模拟 scanner 权重缩放，校验分数顺利通过 0.60 推送门槛
    sig = sigs[0]
    w = STRATEGY_CREDIBILITY_WEIGHTS[sig.strategy]
    sig.score = round(min(max(sig.score * w, 0.10), 1.0), 2)
    assert sig.score >= 0.60

    captured = {}
    monkeypatch.setattr("data.storage.get_instrument_name", lambda c: "福耀玻璃")
    monkeypatch.setattr(
        "services.signal_service.get_dual_market_regime",
        lambda *a, **kw: {
            "large_cap": {"label": "🟢 多头进攻", "max_position_pct": 80, "regime": "BULL"},
            "growth": {"label": "🟢 多头进攻", "regime": "BULL"},
            "combined_regime": "BULL",
        },
    )
    monkeypatch.setattr("engine.scanner.annotate_portfolio_guardrails", lambda s, **kw: (s, {}))
    monkeypatch.setattr("services.signal_service.get_data_health_summary", lambda *a, **kw: {"has_issues": False})
    monkeypatch.setattr(push_mod, "send_notification", lambda title, content, **kw: captured.update({"content": content}) or True)

    assert push_mod.notify_signals([sig]) is True
    assert "福耀玻璃(600660.SH)" in captured["content"]
    assert "已自动拦截" not in captured["content"]


# =====================================================================
# P0-2: 回测与实盘大盘择时零分歧（单源 classify_index_regime）
# =====================================================================
def test_unified_regime_zero_divergence_between_backtest_and_live(isolated_db):
    from engine.backtester import _build_index_regime_map
    from services.signal_service import get_market_regime

    rng = np.random.default_rng(7)
    # 构造 130 天前涨→震荡→持续走熊行情
    rets = np.concatenate([
        np.full(50, 0.004),
        rng.normal(0.0, 0.008, 35),
        np.full(45, -0.006),
    ])
    df_idx = _make_ohlcv("000300.SH", n_days=130, start_price=3500.0, pct_returns=rets, start_date="20260101")
    with isolated_db.get_conn() as conn:
        for _, r in df_idx.iterrows():
            conn.execute(
                "INSERT OR REPLACE INTO index_daily VALUES (?,?,?,?,?,?,?,?)",
                ("000300.SH", r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["amount"]),
            )
        # 同步插入上证防止 fallback
        for _, r in df_idx.iterrows():
            conn.execute(
                "INSERT OR REPLACE INTO index_daily VALUES (?,?,?,?,?,?,?,?)",
                ("000001.SH", r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["amount"]),
            )

    dates = df_idx["trade_date"].tolist()
    bt_map = _build_index_regime_map("000300.SH", dates[30], dates[-1], fallback_sh=True)

    # 逐日校验实盘 get_market_regime 与回测 _build_index_regime_map 完全一致（0 分歧）
    disagree = 0
    for dt in dates[30:]:
        live_allow = get_market_regime(end_date=dt, index_code="000300.SH")["regime"] != "BEAR"
        bt_allow = bt_map[dt]
        if live_allow != bt_allow:
            disagree += 1
    assert disagree == 0


# =====================================================================
# P0-3: 策略专属止损/移动止盈 + 真实持仓组合护栏 + 数据健康提醒
# =====================================================================
def test_push_strategy_stops_and_portfolio_guardrails(isolated_db, monkeypatch):
    from engine.scanner import annotate_portfolio_guardrails
    from engine.risk_manager import RiskManager
    import notifier.push as push_mod

    # 1. 验证 strategy="" 兼容默认 2x ATR，而指定 strategy="kdj_cross" 精确匹配 RiskManager.for_strategy
    df_px = _make_ohlcv("600036.SH", n_days=40, pct_returns=np.full(40, 0.001), spread=0.012)
    monkeypatch.setattr("data.storage.get_daily", lambda *a, **kw: df_px)

    p_def = push_mod.calc_atr_stops_and_sizing("600036.SH", price=40.0, score=0.8)
    p_kdj = push_mod.calc_atr_stops_and_sizing("600036.SH", price=40.0, score=0.8, strategy="kdj_cross")
    p_ma = push_mod.calc_atr_stops_and_sizing("600036.SH", price=40.0, score=0.8, strategy="ma_cross")
    rm_kdj = RiskManager.for_strategy("kdj_cross", use_atr_stop=True)
    rm_ma = RiskManager.for_strategy("ma_cross", use_atr_stop=True)

    assert 3.0 <= p_def["sl_pct"] <= 8.0
    assert p_kdj["sl_pct"] >= rm_kdj.atr_min_sl_pct
    assert p_kdj["trail_activation"] == rm_kdj.trailing_stop_activation
    assert p_kdj["trail_callback"] == rm_kdj.trailing_stop_callback
    assert p_kdj["break_even_trigger"] == 0.0
    assert p_ma["break_even_trigger"] == rm_ma.break_even_trigger_pct

    # 2. 写入 2 只银行股持仓与高度相关行情，触发同业集中(银行>=2)、已持仓、高相关及满仓上限预警
    rng = np.random.default_rng(11)
    rets = rng.normal(0.001, 0.015, 70)
    df_held1 = _make_ohlcv("600036.SH", n_days=70, pct_returns=rets)
    df_held2 = _make_ohlcv("601166.SH", n_days=70, pct_returns=rets + 0.0002)
    df_cand = _make_ohlcv("601398.SH", n_days=70, pct_returns=rets - 0.0002)  # 高度相关(>0.95)且同为银行
    for d in (df_held1, df_held2, df_cand):
        isolated_db.save_daily(d)

    with isolated_db.get_conn() as conn:
        for code, nm, ind in [
            ("600036.SH", "招商银行", "银行"),
            ("601166.SH", "兴业银行", "银行"),
            ("601398.SH", "工商银行", "银行"),
        ]:
            conn.execute(
                "INSERT OR REPLACE INTO stock_basic (ts_code, symbol, name, market, industry, is_st) VALUES (?,?,?,?,?,0)",
                (code, code.split(".")[0], nm, "SH", ind),
            )

    isolated_db.upsert_position_buy("600036.SH", 38.0, 1000, "20261110", strategy="kdj_cross")
    isolated_db.upsert_position_buy("601166.SH", 18.0, 2000, "20261112", strategy="kdj_cross")

    monkeypatch.setattr("engine.scanner.get_daily", lambda code, *a, **kw: {"600036.SH": df_held1, "601166.SH": df_held2, "601398.SH": df_cand}[code])
    sigs = [
        Signal("600036.SH", "20261124", "kdj_cross", "BUY", 0.80, "金叉买点", 40.0),
        Signal("601398.SH", "20261124", "signal_combo", "BUY", 0.78, "共振买点", 6.2),
    ]
    annotated, meta = annotate_portfolio_guardrails(sigs, max_active_positions=2, max_sector_positions=2, max_correlation=0.75)
    assert meta["at_capacity"] is True
    assert "[📦已持仓·勿重复追买]" in annotated[0].reason
    assert "[⚠️同业集中:银行]" in annotated[1].reason
    assert "[⚠️高相关:" in annotated[1].reason

    # 3. 推送日报同时渲染双指数、满仓提醒、策略移动止盈提示与数据健康告警
    pushed = {}
    monkeypatch.setattr(
        "services.signal_service.get_dual_market_regime",
        lambda *a, **kw: {
            "large_cap": {"label": "🟡 震荡均衡", "max_position_pct": 50},
            "growth": {"label": "🔴 空头防守"},
            "combined_regime": "GROWTH_BEAR",
        },
    )
    monkeypatch.setattr(
        "services.signal_service.get_data_health_summary",
        lambda *a, **kw: {"has_issues": True, "summary_text": "行情滞后标的 1 只: 长江电力"},
    )
    monkeypatch.setattr(push_mod, "send_notification", lambda title, content, **kw: pushed.update({"content": content}) or True)

    # 补足至 5 只持仓以触发 notify_signals 默认 max_active_positions=5 满额提醒
    for extra_code in ("000001.SZ", "000002.SZ", "600519.SH"):
        isolated_db.upsert_position_buy(extra_code, 20.0, 500, "20261112", strategy="kdj_cross")

    push_mod.notify_signals([
        Signal("600036.SH", "20261124", "kdj_cross", "BUY", 0.80, "金叉买点", 40.0),
        Signal("601398.SH", "20261124", "signal_combo", "BUY", 0.78, "共振买点", 6.2),
    ])
    body = pushed["content"]
    assert "创业板走熊分化" in body
    assert "当前实盘/模拟持仓已达上限（5/5 只）" in body
    assert "[📦已持仓·勿重复追买]" in body
    assert "[⚠️同业集中:银行]" in body
    assert "[⚠️高相关:600036.SH" in body
    assert "回撤" in body and "止盈" in body
    assert "浮盈6%保本" in body
    assert "数据健康提醒" in body


# =====================================================================
# P0-4: 推荐器全维度收缩 + 低样本惩罚拦截“1笔满胜率神单”
# =====================================================================
def test_recommender_v070_rejects_one_trade_mirage(monkeypatch):
    import services.strategy_recommender as rec_mod

    df = _make_ohlcv("000725.SZ", n_days=380, start_date="20250101")
    end_dt = str(df["trade_date"].iloc[-1])
    monkeypatch.setattr(rec_mod, "get_daily", lambda *a, **kw: df)
    monkeypatch.setattr(rec_mod, "preload_backtest_data", lambda *a, **kw: {"000725.SZ": df})

    def _mk(s, ret, sh, win, pf, sells):
        r = BacktestResult(
            strategy=s, params="{}", start_date="", end_date="",
            initial_capital=100000.0, final_capital=100000.0 * (1.0 + ret / 100.0),
            total_return=ret, max_drawdown=4.0 if sells <= 2 else 12.0,
            sharpe_ratio=sh, win_rate=win, trade_count=sells * 2, sell_count=sells,
        )
        r.profit_factor = pf
        return r

    class _FakeBT:
        def __init__(self, strategy_cls=None, **kwargs):
            self.sname = getattr(strategy_cls, "name", "")
            self.start_date = kwargs.get("start_date", "")

        def run(self, save=False):
            is_oos = self.start_date > "20250601"
            # ma_cross 模仿京东方A在 v0.6 中的 1 笔满胜率低回撤虚假高分
            if self.sname == "ma_cross":
                return _mk("ma_cross", 11.7 if not is_oos else -7.1, 0.24, 100.0, 99.9, 1)
            # signal_combo 10 笔稳健大样本交易
            if self.sname == "signal_combo":
                return _mk("signal_combo", 8.5 if not is_oos else 20.0, 0.15, 60.0, 1.8, 10 if not is_oos else 6)
            return _mk(self.sname, -5.0, -0.5, 30.0, 0.6, 2)

    monkeypatch.setattr(rec_mod, "Backtester", _FakeBT)
    res = rec_mod.evaluate_stock_strategy("000725.SZ", name="京东方A", industry="元器件", end_date=end_dt)
    assert res["strategy"] == "signal_combo"
    assert res["oos_passed"] is True

    # 验证样本外交易不足 (<3笔) 且未明显亏损时：oos_passed 为 None（不误熔断，允许进组合）
    class _ShortOOSBT(_FakeBT):
        def run(self, save=False):
            is_oos = self.start_date > "20250601"
            if is_oos and self.sname == "signal_combo":
                return _mk("signal_combo", 1.0, 0.2, 100.0, 2.0, 1)
            return super().run(save=False)

    monkeypatch.setattr(rec_mod, "Backtester", _ShortOOSBT)
    res_insuff = rec_mod.evaluate_stock_strategy("000725.SZ", name="京东方A", industry="元器件", end_date=end_dt)
    assert res_insuff["oos_passed"] is None
    assert rec_mod.is_no_trade_recommendation(res_insuff["tag"]) is False
    assert "样本外交易不足" in res_insuff["tag"]


# =====================================================================
# P1-5: 布林中期空头过滤 & 动量轮动实盘已持有标注 & 严格零前视滚动组合回测
# =====================================================================
def test_bollinger_downtrend_knife_guard_and_rotation_live_held(monkeypatch):
    from strategies.bollinger_reversal import BollingerReversalStrategy
    from strategies.momentum_rotation import MomentumRotationStrategy

    boll = BollingerReversalStrategy(boll_period=20, boll_std=2.0)
    base = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 23)],
        "open": [10.0] * 21 + [9.60],
        "high": [10.2] * 21 + [9.95],
        "low": [9.9] * 21 + [9.40],
        "close": [10.0] * 21 + [9.85],
        "volume": [10000.0] * 22,
        "boll_mid": [10.3] * 22,
        "boll_upper": [10.8] * 22,
        "boll_lower": [9.5] * 22,
        "ma20": [10.5] * 22,
        "ma60": [11.0] * 22,
        "rsi14": [50.0] * 21 + [36.0],
    })
    # 中期空头排列 (9.85 < ma20=10.5 < ma60=11.0) 且 RSI=36 >= 30 -> 拒绝接飞刀
    assert boll.on_bar("20260122", {"000001.SZ": base}) == []

    # 同等空头排列下但 RSI=25 (< 30) 极端超卖阳线企稳 -> 放行低吸
    df_ext = base.copy()
    df_ext.loc[df_ext.index[-1], "rsi14"] = 25.0
    sigs = boll.on_bar("20260122", {"000001.SZ": df_ext})
    assert len(sigs) == 1 and sigs[0].direction == "BUY"

    # 动量轮动实盘模式：Top-1 已持有，应标注续持并降分；Top-2 新晋入选维持高分
    dates = pd.bdate_range("2026-01-01", periods=30).strftime("%Y%m%d").tolist()
    df_e1 = pd.DataFrame({"trade_date": dates, "close": np.linspace(1.0, 1.3, 30), "volume": [1e6] * 30})
    df_e2 = pd.DataFrame({"trade_date": dates, "close": np.linspace(1.0, 1.2, 30), "volume": [1e6] * 30})
    monkeypatch.setattr("data.storage.get_positions", lambda: [{"ts_code": "518880.SH", "shares": 1000}])

    rot = MomentumRotationStrategy(lookback=20, top_k=2, ma_filter_period=20)
    rot_sigs = rot.on_bar(dates[-1], {"518880.SH": df_e1, "513100.SH": df_e2}, portfolio=None)
    by_c = {s.ts_code: s for s in rot_sigs if s.direction == "BUY"}
    assert "持仓续持" in by_c["518880.SH"].reason
    assert by_c["518880.SH"].score < by_c["513100.SH"].score


def test_rolling_adaptive_backtest_strict_no_lookahead(monkeypatch):
    from engine.backtester import run_rolling_adaptive_backtest

    df_1 = _make_ohlcv("000001.SZ", n_days=1460, pct_returns=np.full(1460, 0.0005), start_date="20220101")
    df_2 = _make_ohlcv("000002.SZ", n_days=1460, pct_returns=np.full(1460, 0.0006), start_date="20220101")

    monkeypatch.setattr("engine.backtester.get_st_lookup", lambda: {})
    monkeypatch.setattr("engine.backtester.get_industry_lookup", lambda: {})
    monkeypatch.setattr("engine.backtester.preload_backtest_data", lambda u, s, e: {"000001.SZ": df_1, "000002.SZ": df_2})
    monkeypatch.setattr("data.storage.get_instrument_list", lambda: pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"], "name": ["平安银行", "万科A"], "industry": ["银行", "地产"],
    }))

    seen_evals = []

    def fake_eval(ts_code, name="", industry="", start_date="", end_date="", **kw):
        seen_evals.append((ts_code, start_date, end_date))
        # 验证训练窗绝不踩线进入 2024 年第 1 期（>= 20240101）或第 2 期（>= 20250101）
        if start_date == "20230101" and ts_code == "000001.SZ":
            return {"strategy": "kdj_cross", "tag": "⚡ KDJ", "is_oos": True, "oos_passed": True}
        return {"strategy": "kdj_cross", "tag": "⚡ KDJ", "is_oos": True, "oos_passed": True}

    monkeypatch.setattr("services.strategy_recommender.evaluate_stock_strategy", fake_eval)

    out = run_rolling_adaptive_backtest(
        universe=["000001.SZ", "000002.SZ"],
        start_date="20240101",
        end_date="20251231",
        rebalance_months=12,
        train_lookback_years=2,
        initial_capital=100000.0,
        market_regime_filter=False,
    )
    assert len(out["periods"]) == 2
    assert len(out["result"].equity_curve) > 100
    # 第 1 期仅允许看到 20231231 及以前数据，第 2 期仅允许看到 20241231 及以前数据
    for _code, tr_s, tr_e in seen_evals[:2]:
        assert tr_e == "20231231"
    for _code, tr_s, tr_e in seen_evals[2:4]:
        assert tr_e == "20241231"


# =====================================================================
# P2-6: 数据健康度摘要检查（滞后股 / 滞后指数 / 缺失估值）
# =====================================================================
def test_data_health_summary_detects_lag_and_missing_valuation(isolated_db):
    from services.signal_service import get_data_health_summary

    df_fresh = _make_ohlcv("600036.SH", n_days=10, start_date="20261115")
    df_lag = _make_ohlcv("600900.SH", n_days=9, start_date="20261115")
    isolated_db.save_daily(df_fresh)
    isolated_db.save_daily(df_lag)
    isolated_db.add_to_watchlist("600036.SH")
    isolated_db.add_to_watchlist("600900.SH")

    with isolated_db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stock_basic (ts_code, symbol, name, market, industry, is_st) VALUES ('600900.SH','600900','长江电力','SH','电力',0)"
        )
        for ic, dt in [("000001.SH", "20261124"), ("000300.SH", "20261123"), ("399001.SZ", "20261124"), ("399006.SZ", "20261124")]:
            conn.execute(
                "INSERT OR REPLACE INTO index_daily VALUES (?, ?, 3000, 3050, 2980, 3020, 1e6, 1e9)",
                (ic, dt),
            )

    summary = get_data_health_summary()
    assert summary["has_issues"] is True
    assert any(x["ts_code"] == "600900.SH" for x in summary["lagging_stocks"])
    assert any(x["ts_code"] == "000300.SH" for x in summary["lagging_indices"])
    assert summary["valuation_covered"] == 0 and summary["valuation_total"] == 2
    assert "长江电力" in summary["summary_text"] and "000300.SH" in summary["summary_text"]
