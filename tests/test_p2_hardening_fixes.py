"""v0.4.6 加固回归测试

覆盖：标的元数据透传 / point-in-time 股票池 / 迁移事务 / 更新锁竞态 /
Tushare 复权因子 / Webhook SSRF 与日志脱敏 / 认证默认口令移除 /
调度幂等与日志 handler / Wilder 指标 / 封板最终价判定 /
current_close 跌停顺延 / 流动性参与率与冲击成本 / 策略推荐样本外。
"""
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 storage 指向临时库，避免测试污染 data/quant.db"""
    import data.storage as st

    monkeypatch.setattr(st, "DB_PATH", tmp_path / "quant.db")
    monkeypatch.setattr(st, "_LOCK_FILE", tmp_path / ".update.lock")
    if hasattr(st._thread_local, "conn"):
        st._thread_local.conn.close()
        del st._thread_local.conn
    st._LOCK_OWNER_META = None
    st.init_db()
    yield st
    if hasattr(st._thread_local, "conn"):
        st._thread_local.conn.close()
        del st._thread_local.conn
    st._LOCK_OWNER_META = None


# ============================================================
# 数据层
# ============================================================

def test_instrument_list_keeps_metadata_columns(isolated_db):
    """get_instrument_list 必须带出 industry/is_st/list_date/delist_date，
    否则 ST 判定与行业启发式推荐都会静默失效。"""
    st = isolated_db
    st.save_stock_list(pd.DataFrame([{
        "ts_code": "600000.SH", "symbol": "600000", "name": "*ST浦发",
        "market": "SH", "list_date": "19991110", "industry": "银行",
        "is_st": 1, "delist_date": "",
    }]))
    df = st.get_instrument_list()
    for col in ("industry", "is_st", "list_date", "delist_date", "type"):
        assert col in df.columns
    row = df[df["ts_code"] == "600000.SH"].iloc[0]
    assert row["is_st"] == 1 and row["industry"] == "银行"


def test_point_in_time_universe_filters_delisted(isolated_db):
    st = isolated_db
    st.save_stock_list(pd.DataFrame([
        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "market": "SZ",
         "list_date": "19910403", "industry": "银行", "is_st": 0, "delist_date": ""},
        {"ts_code": "600001.SH", "symbol": "600001", "name": "已退市", "market": "SH",
         "list_date": "19980101", "industry": "钢铁", "is_st": 0, "delist_date": "20201231"},
        {"ts_code": "300001.SZ", "symbol": "300001", "name": "后来上市", "market": "SZ",
         "list_date": "20250101", "industry": "电气", "is_st": 0, "delist_date": ""},
    ]))
    u2015 = st.get_point_in_time_universe("20150601")
    u2026 = st.get_point_in_time_universe("20260601")
    assert "600001.SH" in u2015 and "600001.SH" not in u2026
    assert "300001.SZ" not in u2015 and "300001.SZ" in u2026
    assert "000001.SZ" in u2015 and "000001.SZ" in u2026


def test_migrations_are_idempotent(isolated_db):
    st = isolated_db
    st.init_db()
    st.init_db()
    with st.get_conn() as conn:
        assert st._get_db_version(conn) == 5


def test_migration_failure_does_not_advance_user_version(isolated_db, monkeypatch):
    st = isolated_db
    with st.get_conn() as conn:
        conn.execute("PRAGMA user_version = 1")

    original = st._column_exists

    def broken(conn, table, column):
        if table == "backtest_result" and column == "calmar_ratio":
            raise RuntimeError("模拟 DDL 失败")
        return original(conn, table, column)

    monkeypatch.setattr(st, "_column_exists", broken)
    with pytest.raises(RuntimeError):
        with st.get_conn() as conn:
            st._run_migrations(conn)
    with st.get_conn() as conn:
        assert st._get_db_version(conn) == 1, "失败后版本号绝不能推进，否则迁移永不重试"


def test_partially_written_lock_meta_is_not_stale(isolated_db):
    """半截 JSON 不能当成孤儿锁强删（会误杀刚建好目录的持有者）"""
    st = isolated_db
    lock = Path(st._LOCK_FILE)
    lock.mkdir()
    (lock / "lock_info.json").write_text('{"pid":', encoding="utf-8")
    assert st.acquire_update_lock(timeout=1, max_stale_seconds=600) is False
    assert lock.exists()
    st.force_release_update_lock()


def test_release_only_removes_own_lock(isolated_db):
    st = isolated_db
    assert st.acquire_update_lock(timeout=1) is True
    # 模拟另一个进程接管了该锁目录
    st._write_lock_meta(Path(st._LOCK_FILE), {
        "pid": os.getpid() + 1, "created_at": time.time(), "owner_token": "other",
    })
    st.release_update_lock()
    assert Path(st._LOCK_FILE).exists(), "所有权不匹配时不能删掉别人的有效锁"
    st.force_release_update_lock()


def test_tushare_empty_adj_factor_is_rejected(monkeypatch):
    """复权因子接口返回空时必须放弃该源，不能把未复权价当前复权价写入"""
    import data.fetcher as f

    class FakePro:
        def daily(self, **kwargs):
            return pd.DataFrame([{
                "ts_code": "000001.SZ", "trade_date": "20240102", "open": 10.0,
                "high": 10.5, "low": 9.8, "close": 10.2, "vol": 100000,
                "amount": 1020000.0, "pct_chg": 2.0,
            }])

        def adj_factor(self, **kwargs):
            return pd.DataFrame()  # 接口正常但返回空

    monkeypatch.setattr(f, "_get_pro", lambda: FakePro())
    monkeypatch.setattr(f, "_rate_limit", lambda: None)
    assert f._fetch_daily_tushare("000001.SZ", "20240101", "20240110").empty


def test_tushare_partial_adj_factor_is_rejected(monkeypatch):
    import data.fetcher as f

    class FakePro:
        def daily(self, **kwargs):
            return pd.DataFrame([{
                "ts_code": "000001.SZ", "trade_date": d, "open": 10.0,
                "high": 10.5, "low": 9.8, "close": 10.2, "vol": 100000,
                "amount": 1020000.0, "pct_chg": 0.0,
            } for d in ("20240102", "20240103")])

        def adj_factor(self, **kwargs):
            return pd.DataFrame([{"trade_date": "20240102", "adj_factor": 1.2}])

    monkeypatch.setattr(f, "_get_pro", lambda: FakePro())
    monkeypatch.setattr(f, "_rate_limit", lambda: None)
    assert f._fetch_daily_tushare("000001.SZ", "20240101", "20240110").empty


# ============================================================
# 安全与调度
# ============================================================

def test_webhook_config_validation():
    from notifier.push import _validate_webhook_config
    good = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    assert _validate_webhook_config(good, "wecom") == good
    assert _validate_webhook_config("http://qyapi.weixin.qq.com/x", "wecom") == ""
    assert _validate_webhook_config("https://evil.example.com/x", "wecom") == ""
    assert _validate_webhook_config("https://127.0.0.1/x", "wecom", True) == ""
    assert _validate_webhook_config("https://169.254.169.254/x", "wecom", True) == ""
    assert _validate_webhook_config("https://user:pw@qyapi.weixin.qq.com/x", "wecom") == ""


def test_webhook_post_disables_redirects_and_redacts_logs(monkeypatch):
    import notifier.push as p
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(json=lambda: {"errcode": 0})

    monkeypatch.setattr(p.requests, "post", fake_post)
    assert p._post_webhook("https://qyapi.weixin.qq.com/x", "企业微信", payload={"a": 1}) is not None
    assert captured["allow_redirects"] is False
    assert p._post_webhook("http://127.0.0.1/x", "企业微信") is None
    assert "SECRET" not in p._redact("https://host/x?key=SECRET")
    assert "abc123" not in p._redact("Invalid token: abc123")


def test_no_hardcoded_default_password():
    src = Path("app/main.py").read_text(encoding="utf-8")
    assert "quant123" not in src
    assert "compare_digest" in src and "APP_AUTH_SESSION_TTL" in src


def test_scan_and_notify_is_idempotent(monkeypatch):
    import scheduler
    sent = []
    monkeypatch.setattr("data.storage.notification_already_sent", lambda key: key in sent)
    monkeypatch.setattr("data.storage.mark_notification_sent", lambda key: sent.append(key))
    monkeypatch.setattr("data.storage.get_watchlist", lambda: pd.DataFrame())
    monkeypatch.setattr("notifier.push.notify_position_summary", lambda: True)
    scheduler.scan_and_notify()
    scheduler.scan_and_notify()
    assert len(sent) == 1, "Windows 计划任务与 APScheduler 双入口当日只应推一次"


def test_scheduler_log_handler_not_duplicated(monkeypatch):
    import scheduler

    class FakeScheduler:
        running = True

        def __init__(self, **kwargs):
            pass

        def add_job(self, *args, **kwargs):
            pass

        def add_listener(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "SCHEDULER_ENABLED", True)
    root = logging.getLogger()
    before = len(root.handlers)
    scheduler.start()
    scheduler.stop()
    scheduler.start()
    scheduler.stop()
    assert len(root.handlers) == before


# ============================================================
# 指标与撮合
# ============================================================

def test_wilder_rsi_matches_reference():
    from data.indicators import add_rsi
    close = np.array([10, 12, 11, 13, 12, 14, 13, 15, 14, 16,
                      15, 17, 16, 18, 17, 20, 19, 21, 20, 22], dtype=float)
    rsi = add_rsi(pd.DataFrame({"close": close}), 14)["rsi14"]
    delta = np.diff(close)
    gain, loss = np.clip(delta, 0, None), np.clip(-delta, 0, None)
    ref_first = 100 - 100 / (1 + gain[:14].mean() / loss[:14].mean())
    assert rsi.iloc[:14].isna().all(), "预热期不能填中性 50 冒充 RSI"
    assert abs(rsi.iloc[14] - ref_first) < 1e-8
    # 旧 ewm(alpha=1/14, adjust=False) 的种子是第一个 delta，与标准 Wilder 明显不同
    legacy = pd.Series(gain).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    legacy_loss = pd.Series(loss).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    legacy_rsi = 100 - 100 / (1 + legacy.iloc[14] / legacy_loss.iloc[14])
    assert abs(legacy_rsi - ref_first) > 1.0


def test_wilder_atr_has_full_warmup():
    from data.indicators import add_atr
    n = 20
    close = np.linspace(10.0, 13.0, n)
    df = pd.DataFrame({"close": close, "high": close * 1.02, "low": close * 0.98})
    res = add_atr(df, 5)
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    assert res["atr5"].iloc[:4].isna().all()
    assert abs(res["atr5"].iloc[4] - tr.iloc[:5].mean()) < 1e-9


def test_slippage_cannot_bypass_limit_up_lock():
    """委托价略低于涨停价、滑点上浮后被裁到涨停价，仍必须按一字板拒单"""
    from engine.portfolio import Portfolio
    p = Portfolio(100000.0)
    trade = p.buy("000001.SZ", 10.995, 1000, "20240102", prev_close=10.0,
                  bar_low=11.0, bar_high=11.0)
    assert trade is None


def test_current_close_limit_down_sell_is_retried_next_day():
    """current_close 模式下一字跌停卖单失败后必须在下一交易日重试"""
    from core.models import Signal
    from engine.backtester import Backtester
    from strategies.base import BaseStrategy

    code = "000001.SZ"
    rows = [
        # D1 正常日，收盘 10.0（D2 跌停价 9.0）
        {"trade_date": "20240101", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0},
        # D2 一字跌停
        {"trade_date": "20240102", "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0},
        # D3 打开跌停
        {"trade_date": "20240103", "open": 9.2, "high": 9.5, "low": 9.0, "close": 9.3},
        {"trade_date": "20240104", "open": 9.3, "high": 9.6, "low": 9.1, "close": 9.4},
    ]
    df = pd.DataFrame(rows)
    df["ts_code"] = code
    df["volume"] = 1_000_000.0
    df["amount"] = df["close"] * df["volume"]
    df["pct_chg"] = 0.0

    class OneBuyThenSell(BaseStrategy):
        name = "one_buy_then_sell"

        def on_bar(self, trade_date, data, portfolio=None):
            if trade_date == "20240101":
                return [Signal(ts_code=code, trade_date=trade_date, strategy=self.name,
                               direction="BUY", score=0.9, price_ref=10.0, reason="test")]
            if trade_date == "20240102":
                return [Signal(ts_code=code, trade_date=trade_date, strategy=self.name,
                               direction="SELL", score=0.9, price_ref=9.0, reason="test")]
            return []

    bt = Backtester(strategy_cls=OneBuyThenSell, params={}, universe=[code],
                    start_date="20240101", end_date="20240104",
                    execution_mode="current_close", preloaded_data={code: df})
    res = bt.run(save=False)
    sells = [t for t in res.trades if t.direction == "SELL"]
    assert len(sells) == 1
    assert sells[0].trade_date == "20240103", "一字跌停当日不能成交，应在次日重试"


def test_cap_order_volume_and_impact_cost(monkeypatch):
    from engine import commission as c
    assert c.cap_order_volume(10000, 1000, max_participation_rate=0.1) == 100
    assert c.cap_order_volume(10000, 0, max_participation_rate=0.1) == 10000
    assert c.cap_order_volume(10000, 1000, lot_size=1, max_participation_rate=0.1) == 100

    monkeypatch.setattr(c, "SLIPPAGE_IMPACT_K", 1.0)
    base = c.apply_slippage(10.0, "BUY", slippage_rate=0.001)
    small = c.apply_slippage(10.0, "BUY", slippage_rate=0.001,
                             volume=100, bar_volume=10000, volatility=0.02)
    big = c.apply_slippage(10.0, "BUY", slippage_rate=0.001,
                           volume=9000, bar_volume=10000, volatility=0.02)
    assert base <= small < big
    # 默认 k=0 时与旧行为完全一致
    monkeypatch.setattr(c, "SLIPPAGE_IMPACT_K", 0.0)
    assert c.apply_slippage(10.0, "BUY", slippage_rate=0.001,
                            volume=9000, bar_volume=10000) == base


# ============================================================
# 策略推荐样本外
# ============================================================

def test_strategy_recommendation_uses_oos_metrics(monkeypatch):
    """训练段择优、测试段冻结策略；展示业绩只能来自测试段"""
    import services.strategy_recommender as sr

    dates = pd.bdate_range("2023-01-02", periods=160).strftime("%Y%m%d").tolist()
    df = pd.DataFrame({"trade_date": dates, "close": np.linspace(10, 20, len(dates))})
    monkeypatch.setattr(sr, "get_daily", lambda *a, **kw: df)
    monkeypatch.setattr(sr, "preload_backtest_data", lambda *a, **kw: {})

    train_start = dates[0]
    seen_windows = []

    class FakeBacktester:
        def __init__(self, strategy_cls, params, universe, start_date, end_date, **kwargs):
            self.name = strategy_cls.name
            self.start_date = start_date
            self.end_date = end_date

        def run(self, save=False):
            is_train = self.start_date == train_start
            seen_windows.append((self.name, self.start_date, self.end_date, is_train))
            if is_train:
                ret = 50.0 if self.name == "kdj_cross" else -5.0
            else:
                ret = -12.5  # 样本外一律明显更差
            return SimpleNamespace(total_return=ret, win_rate=60.0, trade_count=6,
                                   sell_count=3, max_drawdown=10.0, sharpe_ratio=1.0)

    monkeypatch.setattr(sr, "Backtester", FakeBacktester)
    out = sr.evaluate_stock_strategy("000001.SZ", start_date=dates[0], end_date=dates[-1])

    assert out["is_oos"] is True
    assert out["strategy"] == "kdj_cross"
    assert out["train_window"][0] == train_start
    assert out["test_window"][1] == dates[-1]
    assert out["test_window"][0] > out["train_window"][1]
    assert out["details"]["kdj_cross"]["total_return"] == 50.0  # 样本内
    assert out["oos_total_return"] == -12.5
    assert "-12.5" in out["tag"] and "+50.0" not in out["tag"]
    assert "样本外" in out["tag"] and "样本外" in out["reason"]
    # 测试段只用训练段选出的那一个策略，不再把 4 个策略都跑一遍
    assert sum(1 for w in seen_windows if not w[3]) == 1
