"""v0.4.9 实盘扫描/推送/防洗盘/信号存储回归测试

覆盖：
1. parse_recommended_strategy_key 中英文推荐标签与未知/空值解析；
2. 过期滞后交易日信号过滤与精准清理（绝不跨日期误删其他标的昨日历史信号）；
3. 大盘空头防守期与回测对齐，硬拦截趋势追涨类 BUY，保留 SELL 与超卖 BUY；
4. 自选股按适配策略过滤，拦截低胜率错配单策略信号，放行适配策略、双策略共振及非自选股；
5. 跨日反手冷却拦截「昨日低位卖、今日高价追回」并放行超 5% 深跌吸点，同时抑制高位卖后滞后重复 SELL；
6. notify_signals 最低买入评分门槛（<0.60 不进推荐表并提示观望）、策略名展示及同股卖单合并去重。
"""
import numpy as np
import pandas as pd
import pytest

from core.models import Signal


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 storage 指向临时库，防止测试污染 data/quant.db。"""
    import data.storage as st

    monkeypatch.setattr(st, "DB_PATH", tmp_path / "quant.db")
    monkeypatch.setattr(st, "_LOCK_FILE", tmp_path / ".update.lock")
    if hasattr(st._thread_local, "conn"):
        st._thread_local.conn.close()
        del st._thread_local.conn
    st.init_db()
    yield st
    if hasattr(st._thread_local, "conn"):
        st._thread_local.conn.close()
        del st._thread_local.conn


def _seed_daily(st, ts_code: str, end_date: str, n_days: int = 70):
    dates = pd.bdate_range(end=pd.to_datetime(end_date), periods=n_days).strftime("%Y%m%d").tolist()
    df = pd.DataFrame({
        "ts_code": [ts_code] * n_days,
        "trade_date": dates,
        "open": np.full(n_days, 54.0),
        "high": np.full(n_days, 55.0),
        "low": np.full(n_days, 53.0),
        "close": np.full(n_days, 54.5),
        "volume": np.full(n_days, 1_000_000.0),
        "amount": np.full(n_days, 54_500_000.0),
        "pct_chg": np.full(n_days, 0.5),
    })
    st.save_daily(df)
    return dates


def test_parse_recommended_strategy_key():
    from services.strategy_recommender import parse_recommended_strategy_key

    assert parse_recommended_strategy_key("↔️ 布林反转 (样本外胜率75.0%/+1.2%)") == "bollinger_reversal"
    assert parse_recommended_strategy_key("📈 双均线趋势 (样本外+1.6%/回撤3.8%)") == "ma_cross"
    assert parse_recommended_strategy_key("⚡ KDJ短线企稳 (样本外胜率75.0%/+0.2%)") == "kdj_cross"
    assert parse_recommended_strategy_key("🧩 多因子共振") == "signal_combo"
    assert parse_recommended_strategy_key("bollinger_reversal") == "bollinger_reversal"
    assert parse_recommended_strategy_key("ma_cross") == "ma_cross"
    assert parse_recommended_strategy_key("kdj_cross") == "kdj_cross"
    assert parse_recommended_strategy_key("signal_combo") == "signal_combo"
    assert parse_recommended_strategy_key("") == ""
    assert parse_recommended_strategy_key(None) == ""
    assert parse_recommended_strategy_key("未知策略") == ""


def test_scan_signals_stale_date_filtered_and_no_cross_date_delete(isolated_db, monkeypatch):
    """滞后标的旧日期信号不得在当日重复推送，且清理当日旧信号时绝不能误删其他标的昨日信号。"""
    import engine.scanner as sc

    st = isolated_db
    _seed_daily(st, "601318.SH", "20261124", n_days=70)
    _seed_daily(st, "600900.SH", "20261123", n_days=70)

    # 预置 20261123 历史信号（来自 601318.SH）
    st.save_signals_batch([
        Signal("601318.SH", "20261123", "signal_combo", "SELL", 0.55, "昨日卖出", 53.70),
    ])

    def fake_single(ts_code, end_date, strategy_instances, *args, **kwargs):
        if ts_code == "601318.SH":
            return [Signal("601318.SH", "20261124", "bollinger_reversal", "BUY", 0.72, "布林买点", 52.50)]
        if ts_code == "600900.SH":
            # 停在 20261123 的滞后旧信号
            return [Signal("600900.SH", "20261123", "kdj_cross", "SELL", 0.78, "滞后旧卖出", 31.35)]
        return []

    monkeypatch.setattr(sc, "_scan_single_stock", fake_single)
    monkeypatch.setattr("services.signal_service.get_market_regime", lambda *a, **kw: {"regime": "RANGE"})

    res = sc.scan_signals(
        universe=["601318.SH", "600900.SH"],
        strategy_names=["bollinger_reversal", "kdj_cross", "signal_combo"],
        end_date="20261124",
        save=True,
        parallel=False,
        anti_whipsaw=False,
    )

    assert len(res) == 1
    assert res[0].ts_code == "601318.SH" and res[0].trade_date == "20261124"

    # 库中 20261123 历史信号必须完好保留，绝不可因 600900.SH 滞后日期而被误删
    hist_23 = st.get_signals(trade_date="20261123")
    assert len(hist_23) == 1
    assert hist_23.iloc[0]["ts_code"] == "601318.SH"
    assert hist_23.iloc[0]["direction"] == "SELL"


def test_scan_signals_bear_regime_blocks_trend_buy(isolated_db, monkeypatch):
    """空头防守期应与回测对齐，硬拦截趋势追涨买入，保留卖出与超卖抄底。"""
    import engine.scanner as sc

    st = isolated_db
    _seed_daily(st, "601318.SH", "20261124", n_days=70)
    _seed_daily(st, "600036.SH", "20261124", n_days=70)

    def fake_single(ts_code, end_date, strategy_instances, *args, **kwargs):
        if ts_code == "601318.SH":
            return [
                Signal("601318.SH", "20261124", "signal_combo", "BUY", 0.65, "共振买入", 54.88),
                Signal("601318.SH", "20261124", "ma_cross", "BUY", 0.70, "均线金叉", 54.88),
                Signal("601318.SH", "20261124", "ma_cross", "SELL", 0.75, "死叉卖出", 54.88),
            ]
        return [Signal("600036.SH", "20261124", "kdj_cross", "BUY", 0.70, "KDJ超卖", 38.0)]

    monkeypatch.setattr(sc, "_scan_single_stock", fake_single)
    monkeypatch.setattr("services.signal_service.get_market_regime", lambda *a, **kw: {"regime": "BEAR"})

    res = sc.scan_signals(
        universe=["601318.SH", "600036.SH"],
        end_date="20261124",
        save=False,
        parallel=False,
        respect_market_regime=True,
        anti_whipsaw=False,
    )

    pairs = {(s.ts_code, s.strategy, s.direction) for s in res}
    assert ("601318.SH", "signal_combo", "BUY") not in pairs
    assert ("601318.SH", "ma_cross", "BUY") not in pairs
    assert ("601318.SH", "ma_cross", "SELL") in pairs
    assert ("600036.SH", "kdj_cross", "BUY") in pairs


def test_scan_signals_respect_watchlist_strategy(isolated_db, monkeypatch):
    """自选股优先按各自适配策略过滤：单策略错配拦截，适配策略、双策略共振及非自选股放行。"""
    import engine.scanner as sc

    st = isolated_db
    for code in ("601318.SH", "600036.SH", "000001.SZ"):
        _seed_daily(st, code, "20261124", n_days=70)
    st.add_to_watchlist("601318.SH", recommended_strategy="↔️ 布林反转 (样本外胜率75.0%/+1.2%)")
    st.add_to_watchlist("600036.SH", recommended_strategy="⚡ KDJ短线企稳 (样本外胜率75.0%/+0.7%)")

    def fake_single(ts_code, end_date, strategy_instances, *args, **kwargs):
        if ts_code == "601318.SH":
            # 单策略非适配 BUY -> 拦截；适配 SELL -> 保留
            return [
                Signal("601318.SH", "20261124", "signal_combo", "BUY", 0.75, "共振买", 54.88),
                Signal("601318.SH", "20261124", "bollinger_reversal", "SELL", 0.72, "触上轨卖", 55.60),
            ]
        if ts_code == "600036.SH":
            # 双策略同向共振 -> 即使含非适配策略也放行
            return [
                Signal("600036.SH", "20261124", "ma_cross", "BUY", 0.74, "均线金叉", 38.5),
                Signal("600036.SH", "20261124", "signal_combo", "BUY", 0.70, "多因子共振", 38.5),
            ]
        # 非自选股 -> 原样放行
        return [Signal("000001.SZ", "20261124", "ma_cross", "BUY", 0.72, "普通买单", 12.0)]

    monkeypatch.setattr(sc, "_scan_single_stock", fake_single)
    monkeypatch.setattr("services.signal_service.get_market_regime", lambda *a, **kw: {"regime": "RANGE"})

    res = sc.scan_signals(
        universe=["601318.SH", "600036.SH", "000001.SZ"],
        end_date="20261124",
        save=False,
        parallel=False,
        respect_market_regime=False,
        respect_watchlist_strategy=True,
        anti_whipsaw=False,
    )

    kept_buy_601318 = [s for s in res if s.ts_code == "601318.SH" and s.direction == "BUY"]
    kept_sell_601318 = [s for s in res if s.ts_code == "601318.SH" and s.direction == "SELL"]
    kept_600036 = [s for s in res if s.ts_code == "600036.SH" and s.direction == "BUY"]
    kept_000001 = [s for s in res if s.ts_code == "000001.SZ" and s.direction == "BUY"]

    assert len(kept_buy_601318) == 0
    assert len(kept_sell_601318) == 1 and "[⭐适配]" in kept_sell_601318[0].reason
    assert len(kept_600036) == 2
    assert len(kept_000001) == 1


def test_cross_day_whipsaw_blocks_high_buyback_and_lagging_sell(isolated_db):
    """跨日防洗盘应拦截昨日卖今高买，允许深跌>5%吸筹，并抑制高位卖后滞后补刀卖。"""
    import engine.scanner as sc

    st = isolated_db
    _seed_daily(st, "601318.SH", "20261124", n_days=70)
    # 11-20 高位 55.46 KDJ 卖出(0.77)；11-23 低位 53.70 signal_combo 卖出(0.48)
    st.save_signals_batch([
        Signal("601318.SH", "20261120", "kdj_cross", "SELL", 0.77, "高位落袋", 55.46),
        Signal("601318.SH", "20261123", "signal_combo", "SELL", 0.48, "回调看空", 53.70),
    ])

    # 1. 昨日 53.70 刚卖，今日 54.88 单策略追买 -> 拦截
    buy_high = Signal("601318.SH", "20261124", "signal_combo", "BUY", 0.65, "追买", 54.88)
    assert sc._apply_cross_day_whipsaw_guard([buy_high], cooldown_days=3, sell_dedup_days=2) == []

    # 2. 若跌超 5% 至 50.50 (< 53.70 * 0.95 = 51.015) -> 允许低吸买回
    buy_dip = Signal("601318.SH", "20261124", "bollinger_reversal", "BUY", 0.75, "深跌低吸", 50.50)
    assert len(sc._apply_cross_day_whipsaw_guard([buy_dip], cooldown_days=3, sell_dedup_days=2)) == 1

    # 3. 11-20 已于 55.46 提示 KDJ 卖出(0.77)，11-23 跌回 53.70 再出 KDJ 死叉(0.67) -> 抑制滞后补刀卖
    lag_sell = Signal("601318.SH", "20261123", "kdj_cross", "SELL", 0.67, "滞后死叉", 53.70)
    assert sc._apply_cross_day_whipsaw_guard([lag_sell], cooldown_days=3, sell_dedup_days=2) == []


def test_notify_signals_min_buy_score_and_sell_dedup(monkeypatch):
    """低分买单(<0.60)不进推荐表且提示观望；同股多策略卖单应合并去重为单行。"""
    from notifier.push import notify_signals

    captured = []
    monkeypatch.setattr("notifier.push.send_notification", lambda title, content: captured.append((title, content)) or True)
    monkeypatch.setattr("data.storage.get_instrument_name", lambda code: {"601318.SH": "中国平安", "600900.SH": "长江电力"}.get(code, code))

    signals = [
        Signal("601318.SH", "20261124", "signal_combo", "BUY", 0.47, "低分弱买点", 54.88),
        Signal("600900.SH", "20261124", "kdj_cross", "SELL", 0.78, "KDJ超买落袋", 31.35),
        Signal("600900.SH", "20261124", "bollinger_reversal", "SELL", 0.68, "触及布林上轨", 31.35),
    ]
    assert notify_signals(signals, min_buy_score=0.60) is True
    assert len(captured) == 1
    body = captured[0][1]

    # 0.47 低分买单不得进买入表，且需展示拦截观望提示
    assert "中国平安(601318.SH)" not in body
    assert "无满足高置信度门槛" in body
    assert "已自动拦截 1 条低分弱信号" in body

    # 卖出表同股合并去重，只出现 1 行长江电力且合并展示策略
    sell_lines = [ln for ln in body.splitlines() if "长江电力(600900.SH)" in ln]
    assert len(sell_lines) == 1
    assert "KDJ短线+布林反转" in sell_lines[0]
