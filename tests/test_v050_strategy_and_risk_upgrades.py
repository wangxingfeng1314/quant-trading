"""v0.5.0 全栈策略、风控、选策、估值与实盘闭环回归测试"""
import contextlib
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


def _isolated_sqlite_conn(tmp_path):
    """创建隔离的内存/临时文件 SQLite 数据库并初始化 schema。"""
    db_file = tmp_path / "test_v050.db"
    import data.storage as storage

    def _connect():
        c = sqlite3.connect(str(db_file))
        c.execute("PRAGMA journal_mode=WAL")
        return c

    return db_file, storage


# ============================================================
# 1. RiskManager ATR 自适应止损、分批止盈与策略预设
# ============================================================

def test_risk_manager_atr_stop_partial_tp_and_clone():
    from engine.risk_manager import RiskManager, clone_risk_manager
    from engine.portfolio import Portfolio

    # 验证预设差异：反转类允许更宽止损与时间止损，趋势类启用保本与较紧止损
    rm_rev = RiskManager.for_strategy("bollinger_reversal", use_atr_stop=True)
    rm_trend = RiskManager.for_strategy("ma_cross", use_atr_stop=True)
    assert rm_rev.stop_loss_pct == -10.0 and rm_rev.max_holding_days == 60 and rm_rev.max_holding_pnl_cap == 2.0
    assert rm_trend.stop_loss_pct == -6.0 and rm_trend.enable_break_even is True

    # 验证 ATR 动态止损：ATR%=4.0, atr_stop_mult=2.5 => 止损线 = -10.0%
    rm_atr = RiskManager(
        stop_loss_pct=-5.0, use_atr_stop=True, atr_stop_mult=2.5,
        atr_min_sl_pct=3.0, atr_max_sl_pct=12.0,
        partial_tp_pct=8.0, partial_tp_ratio=0.5,
        trailing_stop_activation=20.0, trailing_stop_callback=4.0,
    )
    cloned = clone_risk_manager(rm_atr)
    assert cloned is not rm_atr
    assert cloned.use_atr_stop is True
    assert cloned.atr_stop_mult == 2.5
    assert cloned.partial_tp_pct == 8.0
    assert cloned.partial_tp_ratio == 0.5

    p = Portfolio(initial_capital=100000.0)
    p.buy("000001.SZ", price=10.0, volume=1000, trade_date="20260105", slippage=False)
    p.on_new_day("20260106")

    # 跌 -7.0%：固定止损(-5%)本该割肉，但 ATR%=4% -> 动态止损 -10%，不误止损
    sigs_hold = rm_atr.check_risks("20260106", p, {"000001.SZ": 9.30}, current_atr_pct={"000001.SZ": 4.0})
    assert sigs_hold == []

    # 跌 -10.5%：触及 ATR 动态止损线(-10%)
    sigs_sl = rm_atr.check_risks("20260107", p, {"000001.SZ": 8.95}, current_atr_pct={"000001.SZ": 4.0})
    assert len(sigs_sl) == 1 and sigs_sl[0].context_snapshot["risk_type"] == "stop_loss"

    # 分批止盈只触发一次（sell_ratio=0.5），次日更高价不再重复减仓
    p2 = Portfolio(initial_capital=100000.0)
    p2.buy("000002.SZ", price=10.0, volume=1000, trade_date="20260105", slippage=False)
    p2.on_new_day("20260106")
    sigs_ptp1 = cloned.check_risks("20260106", p2, {"000002.SZ": 10.90}, current_atr_pct={"000002.SZ": 2.0})
    assert len(sigs_ptp1) == 1
    assert sigs_ptp1[0].context_snapshot["risk_type"] == "partial_take_profit"
    assert sigs_ptp1[0].context_snapshot["sell_ratio"] == 0.5
    sigs_ptp2 = cloned.check_risks("20260107", p2, {"000002.SZ": 11.10}, current_atr_pct={"000002.SZ": 2.0})
    assert sigs_ptp2 == []


# ============================================================
# 2. Backtester 策略感知大盘择时 & sell_ratio 部分平仓不误顺延
# ============================================================

def test_backtester_allow_bear_buy_and_partial_sell(monkeypatch):
    from engine.backtester import Backtester
    from engine.risk_manager import RiskManager
    from strategies.base import BaseStrategy
    from core.models import Signal

    dates = pd.bdate_range("2025-01-01", periods=50).strftime("%Y%m%d").tolist()
    # 大盘持续下跌，全部处于 BEAR 状态
    idx_df = pd.DataFrame({
        "ts_code": ["000300.SH"] * 50,
        "trade_date": dates,
        "open": np.linspace(4000, 3200, 50),
        "high": np.linspace(4020, 3220, 50),
        "low": np.linspace(3980, 3180, 50),
        "close": np.linspace(4000, 3200, 50),
        "volume": [1e7] * 50,
        "pct_chg": [-0.5] * 50,
    })
    # 标的先横盘后稳步上涨（可触发 +8% 分批止盈）
    closes = [10.0] * 10 + list(np.linspace(10.0, 12.5, 40))
    stock_df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 50,
        "trade_date": dates,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1000000.0] * 50,
        "amount": [10000000.0] * 50,
        "pct_chg": [0.5] * 50,
    })

    monkeypatch.setattr("data.storage.get_index_daily_range", lambda *a, **kw: idx_df)
    monkeypatch.setattr("engine.backtester.get_daily", lambda *a, **kw: stock_df)
    monkeypatch.setattr(
        "data.storage.get_instrument_list",
        lambda: pd.DataFrame({"ts_code": ["000001.SZ"], "is_st": [0], "name": ["平安银行"]}),
    )

    class BearAllowedStrategy(BaseStrategy):
        name = "bear_allowed_buy"
        allow_bear_buy = True
        def on_bar(self, trade_date, data, portfolio=None):
            if portfolio is not None and not portfolio.get_position("000001.SZ"):
                if trade_date == dates[25]:
                    return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="BUY", score=0.8, price_ref=closes[25])]
            return []

    rm = RiskManager(stop_loss_pct=-10.0, trailing_stop_activation=0.0, partial_tp_pct=8.0, partial_tp_ratio=0.5)
    bt = Backtester(
        strategy_cls=BearAllowedStrategy, params={}, universe=["000001.SZ"],
        start_date=dates[20], end_date=dates[-1], initial_capital=100000.0,
        execution_mode="next_open", risk_manager=rm,
        max_active_positions=1, market_regime_filter=True,
    )
    res = bt.run(save=False)
    buys = [t for t in res.trades if t.direction == "BUY"]
    sells = [t for t in res.trades if t.direction == "SELL"]
    assert len(buys) == 1, "allow_bear_buy=True 策略在熊市期间应允许建仓"
    # 分批止盈只应触发 1 笔减半，绝不能因剩余仓位被误当成未成交而连续次日全部抛光
    assert len(sells) == 1
    assert sells[0].volume < buys[0].volume


# ============================================================
# 3. 布林反转 V2 & 多资产动量轮动策略
# ============================================================

def test_bollinger_reversal_v2_candle_and_pullback_exit():
    from strategies.bollinger_reversal import BollingerReversalStrategy

    strat = BollingerReversalStrategy(boll_period=20, boll_std=2.0)
    base = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 23)],
        "open": [10.0] * 21 + [9.60],
        "high": [10.2] * 21 + [9.95],
        "low": [9.9] * 21 + [9.40],
        "close": [10.0] * 21 + [9.85],
        "volume": [10000.0] * 22,
        "boll_mid": [10.0] * 22,
        "boll_upper": [10.5] * 22,
        "boll_lower": [9.5] * 22,
        "rsi14": [50.0] * 21 + [36.0],
    })
    # 收盘 > 开盘 (9.85 >= 9.60)，RSI=36 < 42，触下轨反弹 -> 允许买
    sigs_buy = strat.on_bar("20260122", {"000001.SZ": base})
    assert len(sigs_buy) == 1 and sigs_buy[0].direction == "BUY"

    # 若当收阴线（open=9.90 > close=9.85）接飞刀 -> 拦截买入
    df_knife = base.copy()
    df_knife.loc[df_knife.index[-1], "open"] = 9.95
    assert strat.on_bar("20260122", {"000001.SZ": df_knife}) == []

    # 强势实体大阳放量突破上轨 (close >= boll_upper and close >= open) -> 继续持有不卖飞
    df_breakout = base.copy()
    df_breakout.loc[df_breakout.index[-1], "open"] = 10.30
    df_breakout.loc[df_breakout.index[-1], "high"] = 10.80
    df_breakout.loc[df_breakout.index[-1], "close"] = 10.75
    assert not any(s.direction == "SELL" for s in strat.on_bar("20260122", {"000001.SZ": df_breakout}))

    # 触上轨后冲高回落收在上轨下方 -> 触发止盈 SELL
    df_reject = base.copy()
    df_reject.loc[df_reject.index[-1], "open"] = 10.40
    df_reject.loc[df_reject.index[-1], "high"] = 10.65
    df_reject.loc[df_reject.index[-1], "close"] = 10.35
    sigs_sell = strat.on_bar("20260122", {"000001.SZ": df_reject})
    assert len(sigs_sell) == 1 and sigs_sell[0].direction == "SELL"


def test_momentum_rotation_ranking_and_cash_defense():
    from strategies.momentum_rotation import MomentumRotationStrategy

    dates = pd.bdate_range("2026-01-01", periods=30).strftime("%Y%m%d").tolist()

    def _make_df(closes):
        return pd.DataFrame({
            "trade_date": dates,
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.full(len(closes), 100000.0),
        })

    # 黄金稳定上涨，红利温和上涨，纳指下跌
    data_panel = {
        "518880.SH": _make_df(np.linspace(5.0, 5.8, 30)),
        "512890.SH": _make_df(np.linspace(1.0, 1.06, 30)),
        "513100.SH": _make_df(np.linspace(1.5, 1.30, 30)),
    }
    strat = MomentumRotationStrategy(lookback=20, rebalance_days=5, top_k=1, ma_filter_period=20)
    sigs = strat.on_bar(dates[-1], data_panel, portfolio=None)
    buys = [s for s in sigs if s.direction == "BUY"]
    sells = [s for s in sigs if s.direction == "SELL"]
    assert len(buys) == 1 and buys[0].ts_code == "518880.SH"
    assert any(s.ts_code == "513100.SH" for s in sells)

    # 全体走弱：所有标的负动量且破 MA20 -> 零买入（自动空仓防御）
    bear_panel = {
        "518880.SH": _make_df(np.linspace(6.0, 5.2, 30)),
        "512890.SH": _make_df(np.linspace(1.2, 1.0, 30)),
    }
    sigs_bear = strat.on_bar(dates[-1], bear_panel, portfolio=None)
    assert not any(s.direction == "BUY" for s in sigs_bear)


# ============================================================
# 4. 实盘持仓风控诊断与持仓日报推送闭环
# ============================================================

def test_position_risk_service_and_push_alert(monkeypatch):
    from services.position_service import diagnose_position_action
    import notifier.push as push_mod

    # 保本回撤测试：买价10，中间冲高至10.6 (+6% 激活保本)，现跌回 10.02 (+0.2% <= 0.5%)
    df_be = pd.DataFrame([
        {"trade_date": "20261118", "open": 10.0, "high": 10.60, "low": 9.95, "close": 10.50},
        {"trade_date": "20261119", "open": 10.2, "high": 10.25, "low": 10.00, "close": 10.02},
    ])
    diag_be = diagnose_position_action(
        "600460.SH", buy_price=10.0, buy_date="20261118", strategy_hint="ma_cross",
        get_daily_fn=lambda *a, **kw: df_be,
        get_signals_fn=lambda **kw: pd.DataFrame(),
        calc_atr_fn=lambda c, p, score=0.7: {"sl_pct": 6.0, "tp_pct": 12.0, "stop_loss": 9.4, "take_profit": 11.2, "atr_pct": 3.0},
    )
    assert diag_be["level"] == "warning"
    assert "保本" in diag_be["action"]

    # 持仓日报推送集成测试：应带有紧急风控预警区块与移动止盈信息
    monkeypatch.setattr(
        "data.storage.get_positions",
        lambda: [{"ts_code": "600460.SH", "buy_price": 10.0, "shares": 1000, "buy_date": "20261118", "note": ""}],
    )
    monkeypatch.setattr("data.storage.get_daily", lambda *a, **kw: df_be)
    monkeypatch.setattr("data.storage.get_signals", lambda **kw: pd.DataFrame())
    monkeypatch.setattr("data.storage.get_instrument_name", lambda c: "士兰微")
    monkeypatch.setattr("data.storage.get_watchlist", lambda: pd.DataFrame([{"ts_code": "600460.SH", "recommended_strategy": "📈 双均线趋势"}]))

    captured = {}
    monkeypatch.setattr(push_mod, "send_notification", lambda t, c: captured.update({"title": t, "content": c}) or True)
    assert push_mod.notify_position_summary() is True
    assert "🚨" in captured["title"]
    assert "持仓风控预警" in captured["content"]
    assert "保本" in captured["content"]


# ============================================================
# 5. 选策 max_active_positions=1 修复 + OOS 熔断与扫描器联动
# ============================================================

def test_recommender_uses_single_pos_and_blocks_oos_loser_in_scanner(monkeypatch, tmp_path):
    import services.strategy_recommender as sr
    import engine.scanner as scanner
    from core.models import Signal

    dates = pd.bdate_range("2024-01-01", periods=160).strftime("%Y%m%d").tolist()
    df = pd.DataFrame({"trade_date": dates, "close": np.linspace(10, 15, len(dates))})
    monkeypatch.setattr(sr, "get_daily", lambda *a, **kw: df)
    monkeypatch.setattr(sr, "preload_backtest_data", lambda *a, **kw: {})

    captured_max_pos = []

    class RecordingBacktester:
        def __init__(self, strategy_cls, params, universe, start_date, end_date, **kwargs):
            captured_max_pos.append(kwargs.get("max_active_positions"))
            self.name = strategy_cls.name
            self.start_date = start_date

        def run(self, save=False):
            is_train = self.start_date == dates[0]
            if is_train:
                ret = 30.0 if self.name == "ma_cross" else 5.0
                return SimpleNamespace(total_return=ret, win_rate=55.0, trade_count=10, sell_count=5, max_drawdown=5.0, sharpe_ratio=1.2, profit_factor=1.8)
            return SimpleNamespace(total_return=-4.5, win_rate=40.0, trade_count=8, sell_count=4, max_drawdown=9.0, sharpe_ratio=-0.3, profit_factor=0.7)

    monkeypatch.setattr(sr, "Backtester", RecordingBacktester)
    out = sr.evaluate_stock_strategy("600660.SH", start_date=dates[0], end_date=dates[-1])
    assert all(x == 1 for x in captured_max_pos), "单股选策回测必须满仓(max_active_positions=1)消除稀释"
    assert out["oos_passed"] is False
    assert sr.is_no_trade_recommendation(out["tag"]) is True
    assert sr.parse_recommended_strategy_key(out["tag"]) == ""

    # 验证扫描器过滤：不适配标的拦截单策略 BUY，但保留 SELL 护盘
    monkeypatch.setattr(
        scanner, "get_watchlist",
        lambda: pd.DataFrame([{"ts_code": "600660.SH", "recommended_strategy": out["tag"]}]),
    )
    s_buy = Signal(ts_code="600660.SH", trade_date="20261124", strategy="bollinger_reversal", direction="BUY", score=0.80, price_ref=50.0, reason="买")
    s_sell = Signal(ts_code="600660.SH", trade_date="20261124", strategy="bollinger_reversal", direction="SELL", score=0.75, price_ref=52.0, reason="卖")
    filtered = scanner._filter_by_watchlist_strategy([s_buy, s_sell])
    assert len(filtered) == 1 and filtered[0].direction == "SELL"


# ============================================================
# 6. 估值表 CRUD、估值守卫与核心-卫星配置服务
# ============================================================

def test_valuation_guard_and_core_satellite_plan(tmp_path, monkeypatch):
    import data.storage as storage
    from services.valuation_service import evaluate_valuation_guard, build_core_satellite_plan

    db_file = tmp_path / "val_test.db"
    @contextlib.contextmanager
    def _tmp_conn():
        conn = sqlite3.connect(str(db_file))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(storage, "get_conn", _tmp_conn)
    storage.init_db()

    storage.save_valuation_batch([
        {"ts_code": "600036.SH", "pe_ttm": 6.5, "pb": 0.9, "dividend_yield": 5.1, "market_cap_yi": 9500.0},
        {"ts_code": "600460.SH", "pe_ttm": -25.0, "pb": 3.5, "dividend_yield": 0.0, "market_cap_yi": 450.0},
        {"ts_code": "002241.SZ", "pe_ttm": 32.0, "pb": 2.8, "dividend_yield": 0.5, "market_cap_yi": 750.0},
    ])
    vmap = storage.get_valuation_map(["600036.SH", "600460.SH", "002241.SZ"])
    assert len(vmap) == 3

    vg_bank = evaluate_valuation_guard("600036.SH", vmap["600036.SH"])
    assert vg_bank["zone"] == "低估高息" and vg_bank["allow_left_buy"] is True

    vg_loss = evaluate_valuation_guard("600460.SH", vmap["600460.SH"])
    assert vg_loss["zone"] == "亏损预警" and vg_loss["allow_left_buy"] is False and vg_loss["score_adj"] < 0

    dates = pd.bdate_range("2026-06-01", periods=70).strftime("%Y%m%d").tolist()
    def _mock_daily(code, limit=90):
        step = 0.006 if code == "600036.SH" else 0.032
        close = np.full(70, 20.0)
        return pd.DataFrame({
            "trade_date": dates,
            "open": close,
            "high": close * (1 + step),
            "low": close * (1 - step),
            "close": close,
        })

    wl_df = pd.DataFrame([
        {"ts_code": "600036.SH", "recommended_strategy": "⚡ KDJ短线企稳 (样本外胜率75.0%/+2.0%)"},
        {"ts_code": "002241.SZ", "recommended_strategy": "📈 双均线趋势 (样本外+8.0%/回撤4.0%)"},
        {"ts_code": "600460.SH", "recommended_strategy": "⛔ 暂不适配·观望 (📈 双均线趋势 样本外-3.0%/回撤6.0%)"},
    ])
    inst_df = pd.DataFrame([
        {"ts_code": "600036.SH", "name": "招商银行", "type": "股票"},
        {"ts_code": "002241.SZ", "name": "歌尔股份", "type": "股票"},
        {"ts_code": "600460.SH", "name": "士兰微", "type": "股票"},
    ])
    plan = build_core_satellite_plan(
        wl_df, capital=100000.0, core_ratio=0.70,
        val_map=vmap, inst_df=inst_df, daily_loader=_mock_daily,
    )
    by_code = {r["代码"]: r for r in plan["rows"]}
    assert by_code["600036.SH"]["资产角色"] == "🧱 核心底仓"
    assert by_code["600036.SH"]["分配资金(元)"] == 70000.0
    assert by_code["002241.SZ"]["资产角色"] == "⚡ 卫星趋势"
    assert by_code["002241.SZ"]["分配资金(元)"] == 30000.0
    assert by_code["600460.SH"]["资产角色"] == "⛔ 观望/不配"
    assert by_code["600460.SH"]["分配资金(元)"] == 0.0


def test_scanner_cross_sectional_and_valuation_guard(tmp_path, monkeypatch):
    """验证 scanner.scan_signals 支持横截面动量轮动扫描与估值守卫打标/调权。"""
    import data.storage as storage
    import engine.scanner as scanner

    db_file = tmp_path / "scan_v050.db"
    conn = sqlite3.connect(str(db_file))
    @contextlib.contextmanager
    def _tmp_conn():
        try:
            yield conn
            conn.commit()
        finally:
            pass

    monkeypatch.setattr(storage, "get_conn", _tmp_conn)
    monkeypatch.setattr(scanner, "get_conn", _tmp_conn)
    storage.init_db()

    dates = pd.bdate_range("2026-01-01", periods=70).strftime("%Y%m%d").tolist()
    # 构造2只标的：600001.SH 强劲上升但负PE亏损预警；600002.SH 温和下跌
    rows = []
    for code, closes in [("600001.SH", np.linspace(10.0, 14.0, 70)), ("600002.SH", np.linspace(12.0, 9.5, 70))]:
        for d, c in zip(dates, closes):
            rows.append((code, d, c, c * 1.01, c * 0.99, c, 200000.0, c * 200000.0, 0.5, 1.0, 1.0))
    conn.executemany(
        "INSERT INTO daily_price VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()

    storage.save_valuation_batch([
        {"ts_code": "600001.SH", "pe_ttm": -15.0, "pb": 2.0, "dividend_yield": 0.0, "market_cap_yi": 200.0},
    ])

    sigs = scanner.scan_signals(
        universe=["600001.SH", "600002.SH"],
        strategy_names=["momentum_rotation"],
        end_date=dates[-1],
        save=False,
        min_daily_amount=0.0,
        force_refresh=True,
        respect_market_regime=False,
    )
    conn.close()

    buys = [s for s in sigs if s.direction == "BUY"]
    sells = [s for s in sigs if s.direction == "SELL"]
    assert len(buys) == 1 and buys[0].ts_code == "600001.SH"
    assert "亏损" in buys[0].reason and "警示" in buys[0].reason
    assert any(s.ts_code == "600002.SH" for s in sells)

