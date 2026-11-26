"""v0.11.0 核心优化测试套件:
1. price_decimals / round_price / format_price 全链路 ETF 3位小数与个股 2位小数统一规范
2. 券商批量委托单 build_deduplicated_broker_orders 启用 ATR 风险平价、熊市半仓与 ETF 3位小数导出
3. diagnose_position_action 透传 strategy 预设 + momentum_rotation 轮动 ETF 专属 MA20 破位/防5%保本洗出
4. scheduler.scan_and_notify 自动补扫本地就绪轮动 ETF 预设池 & get_data_health_summary 监测滞后轮动 ETF
5. run_dual_sleeve_backtest(save=True) 写入 backtest_result 与 backtest_trade 历史归因库
"""
from __future__ import annotations

import pandas as pd
import pytest

from core import config as core_config
from core.models import Signal
from data import storage
from engine.backtester import run_dual_sleeve_backtest
from engine.commission import is_etf_code, price_decimals, round_price, format_price
from strategies import STRATEGY_REGISTRY
from strategies.base import BaseStrategy


def _use_temp_db(monkeypatch, tmp_path, name: str = "test_v0110.db"):
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


def test_price_helpers_and_etf_3_decimal_formatting(monkeypatch):
    """验证 ETF 3位小数 / 个股 2位小数统一辅助函数与 calc_atr_stops_and_sizing 精度输出"""
    import notifier.push as push_mod

    assert price_decimals("512890.SH") == 3
    assert price_decimals("159915.SZ") == 3
    assert price_decimals("600036.SH") == 2
    assert round_price(1.2484, "512890.SH") == pytest.approx(1.248)
    assert round_price(40.126, "600036.SH") == pytest.approx(40.13)
    assert format_price(1.25, "512890.SH") == "¥1.250"
    assert format_price(10.5, "600036.SH") == "¥10.50"
    assert format_price(None, "512890.SH") == "-"

    df_etf = _make_daily_df("512890.SH", "20261001", 30, base=1.248, drift=0.0, spread=0.016)
    monkeypatch.setattr("data.storage.get_daily", lambda code, limit=25: df_etf)
    plan = push_mod.calc_atr_stops_and_sizing("512890.SH", price=1.248, score=0.8, base_capital=100_000.0)
    assert abs(round(plan["stop_loss"], 3) - plan["stop_loss"]) < 1e-9
    assert abs(round(plan["take_profit"], 3) - plan["take_profit"]) < 1e-9
    assert plan["stop_loss"] < 1.248 < plan["take_profit"]


def test_broker_orders_use_atr_sizing_bear_half_scale_and_etf_3_decimals(monkeypatch):
    """验证券商委托单默认启用 ATR 风险平价、熊市半仓及 3 位小数 ETF 导出"""
    from app.signal import build_deduplicated_broker_orders

    def _fake_atr(code, px, score=0.7, base_cap=100000.0, strategy="", position_scale=1.0):
        # 低波动 600900 給较大股数（2000股），高波动 600460 給较小股数（800股），按 position_scale 缩放
        raw = 2000 if code == "600900.SH" else (1600 if code == "512890.SH" else 800)
        shares = int(raw * position_scale) // 100 * 100
        dec = 3 if is_etf_code(code) else 2
        return {
            "sl_pct": 5.0,
            "tp_pct": 10.0,
            "stop_loss": round(px * 0.95, dec),
            "take_profit": round(px * 1.10, dec),
            "shares": shares,
            "atr_pct": 1.8,
        }

    monkeypatch.setattr("notifier.push.calc_atr_stops_and_sizing", _fake_atr)
    monkeypatch.setattr("app.signal.get_valuation_map", lambda codes=None: {})

    sigs = [
        Signal(ts_code="600900.SH", trade_date="20261125", strategy="kdj_cross", direction="BUY", score=0.88, price_ref=26.50),
        Signal(ts_code="600460.SH", trade_date="20261125", strategy="ma_cross", direction="BUY", score=0.82, price_ref=28.00),
        Signal(ts_code="512890.SH", trade_date="20261125", strategy="momentum_rotation", direction="BUY", score=0.85, price_ref=1.248),
    ]

    # 震荡/牛市环境（max_pos_pct=80 放宽封顶以便检验 ATR 风险平价差异与 ETF 3位小数）
    monkeypatch.setattr("services.signal_service.get_dual_market_regime", lambda *a, **kw: {"combined_regime": "RANGE"})
    rows_range, txt_range = build_deduplicated_broker_orders(
        signals=sigs,
        capital=100_000.0,
        max_pos_pct=80.0,
        stock_map={"600900.SH": "长江电力", "600460.SH": "士兰微", "512890.SH": "红利低波ETF"},
    )
    by_code = {r["标的代码"]: r for r in rows_range}
    assert by_code["600900.SH"]["委托数量(股)"] > by_code["600460.SH"]["委托数量(股)"]
    assert by_code["512890.SH"]["委托价格"] == pytest.approx(1.248)
    assert "¥1.186" in by_code["512890.SH"]["建议止损价"]
    assert by_code["600900.SH"]["仓位模型"] == "ATR风险平价"
    assert any(line.startswith("512890\t") and "\t1.248\t买入" in line for line in txt_range)

    # 熊市环境：手数减半 + 仓位模型标记熊市半仓
    monkeypatch.setattr("services.signal_service.get_dual_market_regime", lambda *a, **kw: {"combined_regime": "BEAR"})
    rows_bear, _ = build_deduplicated_broker_orders(
        signals=sigs[:1],
        capital=100_000.0,
        max_pos_pct=80.0,
        stock_map={"600900.SH": "长江电力"},
    )
    assert rows_bear[0]["委托数量(股)"] == 1000
    assert rows_bear[0]["仓位模型"] == "ATR风险平价(熊市半仓)"


def test_position_diagnosis_passes_strategy_and_handles_momentum_rotation_etf():
    """验证持仓诊断向 ATR 函数透传 strategy，并为轮动 ETF 关闭 5% 机械保本、增加 MA20 破位提示"""
    from services.position_service import diagnose_position_action

    seen_strats = []

    def _mock_atr(code, px, score=0.7, strategy=""):
        seen_strats.append(strategy)
        return {"atr_pct": 1.6, "sl_pct": 4.0, "tp_pct": 8.0}

    # 构造 25 个交易日：前 20 日 ~7.200，第 21~24 日冲高 7.450 (+6.4%)，第 25 日回落至 7.010（< MA20 但仍在成本 7.000 之上）
    dates = pd.bdate_range("20261015", periods=25).strftime("%Y%m%d").tolist()
    closes = [7.200] * 20 + [7.350, 7.450, 7.400, 7.250, 7.010]
    highs = [c + 0.03 for c in closes]
    df_etf = pd.DataFrame({"trade_date": dates, "high": highs, "close": closes})

    diag = diagnose_position_action(
        ts_code="518880.SH",
        buy_price=7.000,
        buy_date=dates[18],
        strategy_hint="🔄 动量轮动 (多资产横截面轮动)",
        get_daily_fn=lambda *a, **kw: df_etf,
        get_signals_fn=lambda **kw: pd.DataFrame(),
        calc_atr_fn=_mock_atr,
    )
    assert seen_strats == ["momentum_rotation"]
    assert diag["strategy_hint"] == "momentum_rotation"
    assert abs(round(diag["stop_loss_price"], 3) - diag["stop_loss_price"]) < 1e-9
    assert "保本" not in diag["action"]
    assert "MA20" in diag["action"]
    assert diag["level"] == "warning"


def test_scheduler_auto_scans_ready_rotation_etfs_and_health_checks_lag(monkeypatch, tmp_path):
    """验证无显式分组时 scheduler 自动扫描本地就绪轮动 ETF，且健康检查监测滞后轮动 ETF"""
    import scheduler as sched_mod
    from services.signal_service import get_data_health_summary

    _use_temp_db(monkeypatch, tmp_path, "sched_rot.db")

    # 构造 1 只普通自选股（更新到 20261125）+ 3 只预设轮动 ETF（其中 512890.SH 滞后到 20261124）
    df_stk = _make_daily_df("600036.SH", "20260701", 80, base=40.0)
    max_dt = str(df_stk["trade_date"].iloc[-1])
    df_e1 = _make_daily_df("510310.SH", "20260701", 80, base=3.8)
    df_e2 = _make_daily_df("518880.SH", "20260701", 80, base=7.1)
    df_e3 = _make_daily_df("512890.SH", "20260701", 79, base=1.25)  # 少1天 -> 滞后

    _seed_stock("600036.SH", df_stk, name="招商银行")
    storage.save_daily(df_e1)
    storage.save_daily(df_e2)
    storage.save_daily(df_e3)
    storage.add_to_watchlist("600036.SH", group_name="", recommended_strategy="⚡ KDJ短线企稳")
    storage.add_to_watchlist("510310.SH", group_name="", recommended_strategy="")

    # 1. 健康检查应识别出 512890.SH 虽未加入 watchlist 但已本地下载且滞后于最新日线
    health = get_data_health_summary()
    assert health["has_issues"] is True
    assert any(x["ts_code"] == "512890.SH" and x["latest_date"] < max_dt for x in health["lagging_etfs"])
    assert "滞后轮动ETF" in health["summary_text"]

    # 2. scheduler.scan_and_notify 自动识别本地就绪预设 ETF 并独立跑 momentum_rotation（并把 510310 从个股扫描去重）
    scan_calls = []

    def _fake_scan(**kw):
        scan_calls.append(kw)
        return []

    monkeypatch.setattr("engine.scanner.scan_signals", _fake_scan)
    monkeypatch.setattr("notifier.push.notify_signals", lambda *a, **kw: True)
    monkeypatch.setattr("notifier.push.notify_position_summary", lambda *a, **kw: False)

    sched_mod.scan_and_notify(force=True)
    assert len(scan_calls) == 2
    reg_call = next(c for c in scan_calls if "600036.SH" in c["universe"])
    rot_call = next(c for c in scan_calls if c["strategy_names"] == ["momentum_rotation"])
    assert "510310.SH" not in reg_call["universe"]
    assert set(rot_call["universe"]) == {"510310.SH", "518880.SH", "512890.SH"}


def test_dual_sleeve_backtest_persists_to_db_when_save_true(monkeypatch, tmp_path):
    """验证 run_dual_sleeve_backtest(save=True) 将净值与分策略/分舱成交写入数据库并可回查"""
    _use_temp_db(monkeypatch, tmp_path, "dual_save.db")

    df_stk = _make_daily_df("600036.SH", "20210104", 1250, base=20.0, drift=0.0005)
    df_etf1 = _make_daily_df("510310.SH", "20230601", 650, base=3.5, drift=0.0008)
    df_etf2 = _make_daily_df("518880.SH", "20230601", 650, base=4.8, drift=0.0012)
    _seed_stock("600036.SH", df_stk, name="招商银行")
    storage.save_daily(df_etf1)
    storage.save_daily(df_etf2)

    d_2024 = [d for d in df_stk["trade_date"].tolist() if d.startswith("2024")]
    d_2025 = [d for d in df_stk["trade_date"].tolist() if d.startswith("2025")]
    buy_day = d_2024[30]

    class _MockSaveStrat(BaseStrategy):
        name = "mock_dual_save"
        description = "测试"
        param_schema = {}

        def on_bar(self, trade_date, data, portfolio=None):
            if trade_date == buy_day and "600036.SH" in data:
                px = float(data["600036.SH"].iloc[-1]["close"])
                return [Signal(ts_code="600036.SH", trade_date=trade_date, strategy="mock_dual_save", direction="BUY", score=0.9, price_ref=px)]
            return []

    monkeypatch.setitem(STRATEGY_REGISTRY, "mock_dual_save", _MockSaveStrat)
    import services.strategy_recommender as sr
    monkeypatch.setattr(sr, "evaluate_stock_strategy", lambda *a, **kw: {
        "strategy": "mock_dual_save", "tag": "推荐使用", "is_oos": True, "oos_passed": True,
    })

    out = run_dual_sleeve_backtest(
        stock_universe=["600036.SH"],
        etf_universe=["510310.SH", "518880.SH"],
        start_date=d_2024[0],
        end_date=d_2025[-1],
        initial_capital=100_000.0,
        save=True,
        execution_mode="current_close",
        risk_manager=None,
        market_regime_filter=False,
    )
    saved_id = getattr(out["result"], "id", None)
    assert isinstance(saved_id, int) and saved_id > 0

    rec = storage.get_backtest_result_by_id(saved_id)
    assert rec is not None
    assert rec["strategy"] == "dual_sleeve_core_satellite"
    assert len(rec["equity_curve"]) > 200

    trades_df = storage.get_backtest_trades(saved_id)
    assert not trades_df.empty
    assert len(trades_df) == len(out["result"].trades)
