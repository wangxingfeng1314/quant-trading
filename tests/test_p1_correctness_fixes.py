"""P0/P1 正确性修复的回归测试（2026-11-12）

覆盖六条会直接改写回测结论或造成不可逆数据丢失的缺陷：

1. 除权断层判定：``pct_chg`` 缺失/为 0 时不再把合法涨停误判成除权
2. 全量替换：先校验再删除、单事务、带备份；全量重拉失败绝不写回
3. 成交量单位：整批判定，前复权因子漂移不再制造 100 倍断层
4. 涨跌停：交易所 ROUND_HALF_UP 舍入 + 封板判定必须结合当日 low/high
5. ATR 仓位：不再读取执行日收盘价（未来函数）
6. 风控克隆与撮合规则：配置完整复制，并透传到网格搜索 / 样本外验证

所有涉及数据库的用例都在 tmp_path 临时库上运行，绝不触碰 data/quant.db。
"""
import threading
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from data.cleaner import clean_daily
from engine.commission import (
    get_price_limits, adjust_price, is_limit_up_locked, is_limit_down_locked,
)
from engine.portfolio import Portfolio
from engine.risk_manager import RiskManager, clone_risk_manager
from engine.backtester import (
    build_backtest_options, _options_kwargs, grid_search,
)
import engine.backtester as btmod
from engine import validation as V


# ============================================================
# 公共夹具
# ============================================================

@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """把 data.storage 指向临时库，隔离真实数据"""
    import data.storage as st
    path = tmp_path / "quant.db"
    monkeypatch.setattr(st, "DB_PATH", path)
    monkeypatch.setattr(st, "_thread_local", threading.local())
    st.init_db()
    return st, path


def _rows(code, items):
    """items: [(trade_date, close, pct_chg)] → 一份自洽的日线 DataFrame"""
    return pd.DataFrame([
        {
            "ts_code": code, "trade_date": d,
            "open": c, "high": c, "low": c, "close": c,
            "volume": 1e5, "amount": c * 1e5,
            "pct_chg": pct, "turnover": 1.0, "adj_factor": 1.0,
        }
        for d, c, pct in items
    ])


def _market_df(closes, volumes, amounts, dates=None):
    n = len(closes)
    return pd.DataFrame({
        "ts_code": ["000001.SZ"] * n,
        "trade_date": dates or [f"202401{i+1:02d}" for i in range(n)],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": volumes, "amount": amounts, "pct_chg": [0.0] * n,
    })


# ============================================================
# 1. 除权断层判定（P0-①）
# ============================================================

class TestSplitAnomaly:
    def test_legal_limit_up_with_zero_pct_chg_is_not_anomaly(self, temp_db):
        """旧实现会把 pct_chg=0 的合法涨停误判成除权 → 触发清库"""
        st, _ = temp_db
        code = "000001.SZ"
        st.save_daily(_rows(code, [("20240102", 10.0, 0.0)]))
        inc = _rows(code, [("20240103", 11.0, 0.0)])
        assert st.check_split_dividend_anomaly(code, inc) is False

    def test_overlap_date_price_shift_is_detected(self, temp_db):
        """真实现金分红导致前复权基准迁移：重叠交易日同日价格变化"""
        st, _ = temp_db
        code = "000001.SZ"
        st.save_daily(_rows(code, [("20240101", 10.5, 0.0),
                                   ("20240102", 10.5, 0.0)]))
        inc = _rows(code, [("20240102", 10.0, 0.0),
                           ("20240103", 10.1, 1.0)])
        assert st.check_split_dividend_anomaly(code, inc) is True

    def test_real_split_with_valid_pct_chg_is_detected(self, temp_db):
        st, _ = temp_db
        code = "000001.SZ"
        st.save_daily(_rows(code, [("20240531", 20.0, 0.0)]))
        inc = _rows(code, [("20240603", 10.1, 1.0)])
        assert st.check_split_dividend_anomaly(code, inc) is True

    def test_long_suspension_resume_is_not_anomaly(self, temp_db):
        """间隔数月的复牌大涨不是除权；宁可漏判，不可误判（误判会清库）"""
        st, _ = temp_db
        code = "000001.SZ"
        st.save_daily(_rows(code, [("20240102", 10.0, 0.0)]))
        inc = _rows(code, [("20240603", 14.0, 0.0)])
        assert st.check_split_dividend_anomaly(code, inc) is False

    def test_empty_or_no_history_is_safe(self, temp_db):
        st, _ = temp_db
        assert st.check_split_dividend_anomaly("000001.SZ", pd.DataFrame()) is False
        st.save_daily(_rows("000001.SZ", [("20240102", 10.0, 0.0)]))
        assert st.check_split_dividend_anomaly(
            "000001.SZ", _rows("000001.SZ", [("20240102", 10.0, 0.0)])) is False


# ============================================================
# 2. 全量替换的原子性与安全校验（P0-①）
# ============================================================

class TestSafeReplace:
    def _seed(self, st, code="000001.SZ"):
        hist = _rows(code, [(f"202{i}0101", 7.0 + i, 0.0) for i in range(1, 6)])
        st.save_daily(hist)
        return hist

    def test_rejects_short_history(self, temp_db):
        st, _ = temp_db
        self._seed(st)
        before = st.get_daily_count()
        short = _rows("000001.SZ", [("20240101", 10.0, 0.0),
                                    ("20240102", 10.1, 0.0)])
        assert st.replace_daily("000001.SZ", short) is False
        assert st.get_daily_count() == before

    def test_rejects_later_start(self, temp_db):
        st, _ = temp_db
        self._seed(st)
        before = st.get_daily_count()
        late = _rows("000001.SZ", [(f"202{i}0101", 7.0 + i, 0.0)
                                   for i in range(2, 6)])
        assert st.replace_daily("000001.SZ", late) is False
        assert st.get_daily_count() == before

    def test_accepts_full_history_and_creates_backup(self, temp_db):
        st, path = temp_db
        self._seed(st)
        full = _rows("000001.SZ", [(f"202{i}0101", 7.0 + i, 0.0)
                                   for i in range(1, 7)])
        assert st.replace_daily("000001.SZ", full) is True
        assert st.get_daily_count() == 6
        assert st.get_earliest_date("000001.SZ") == "20210101"
        assert list((path.parent / "backups").glob("daily_*.csv.gz"))

    def test_resync_uses_earliest_date_and_keeps_db_on_failure(self, temp_db, monkeypatch):
        """全量重拉失败时原库分毫不动，且起点用库内最早日而非固定 730 天"""
        import services.data_service as ds
        st, _ = temp_db
        self._seed(st)
        before = st.get_daily_count()
        calls = []

        def fake_fetch(ts_code, start_date=None, end_date=None, *a, **kw):
            calls.append(start_date)
            return pd.DataFrame()

        monkeypatch.setattr(ds, "fetch_instrument_daily", fake_fetch)
        assert ds.resync_full_history("000001.SZ", "20261112") is False
        assert calls == ["20210101"]
        assert st.get_daily_count() == before

    def test_update_keeps_history_when_anomaly_and_refetch_fails(self, temp_db, monkeypatch):
        """端到端：断层 + 重拉失败，绝不把断裂的增量写回"""
        import services.data_service as ds
        import data.fetcher as fetcher
        st, _ = temp_db
        code = "000001.SZ"
        st.save_daily(_rows(code, [("20210101", 7.0, 0.0),
                                   ("20220101", 8.0, 0.0),
                                   ("20230101", 9.0, 0.0),
                                   ("20240101", 10.0, 0.0),
                                   ("20260101", 12.0, 0.0)]))
        before = st.get_daily_count()

        inc = _rows(code, [("20260101", 12.0, 0.0),
                           ("20260102", 20.0, 0.0)])

        def fake_fetch(ts_code, start_date=None, end_date=None, *a, **kw):
            if start_date == "20260101":     # 增量请求
                return inc
            return pd.DataFrame()            # 全量重拉失败

        monkeypatch.setattr(fetcher, "fetch_instrument_daily", fake_fetch)
        monkeypatch.setattr(ds, "fetch_instrument_daily", fake_fetch)
        monkeypatch.setattr(ds, "align_volume_unit", lambda c, d: d)
        assert ds.DataService.update_stock_data(code) is False
        assert st.get_daily_count() == before
        assert st.get_earliest_date(code) == "20210101"


# ============================================================
# 3. 成交量单位整批判定（P0-②）
# ============================================================

class TestVolumeUnit:
    def test_forward_adjusted_factor_drift_never_mixes_units(self):
        """旧逐行阈值 1.5 会把 f>1.5 的行 ×100，制造 100 倍断层"""
        c = [10.0] * 10
        f = [1.0, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 2.9, 2.9, 2.9]
        v = [100000.0] * 10
        a = [x * y * ff for x, y, ff in zip(c, v, f)]
        out = clean_daily(_market_df(c, v, a))
        assert out["volume"].nunique() == 1
        assert out["volume"].iloc[0] == 100000.0

    def test_lots_unit_normalized_for_whole_batch(self):
        c, v = [10.0] * 10, [1000.0] * 10
        a = [x * y * 100 * 2.9 for x, y in zip(c, v)]
        out = clean_daily(_market_df(c, v, a))
        assert out["volume"].tolist() == [100000.0] * 10
        assert out["amount"].tolist() == pytest.approx(a)

    def test_explicit_shares_unit_skips_heuristic(self):
        c, v = [10.0] * 10, [1000.0] * 10
        a = [x * y * 100 * 2.9 for x, y in zip(c, v)]
        out = clean_daily(_market_df(c, v, a), volume_unit="shares")
        assert out["volume"].tolist() == v

    def test_explicit_lots_unit_forces_conversion(self):
        c, v = [10.0] * 10, [1000.0] * 10
        a = [x * y * 100 for x, y in zip(c, v)]
        out = clean_daily(_market_df(c, v, a), volume_unit="lots")
        assert out["volume"].iloc[0] == 100000.0

    def test_old_format_still_supported(self):
        c, v = [10.0] * 10, [1000.0] * 10
        a = [x * y * 0.1 for x, y in zip(c, v)]   # ratio ≈ 0.1
        out = clean_daily(_market_df(c, v, a))
        assert out["volume"].iloc[0] == 100000.0
        assert out["amount"].iloc[0] == pytest.approx(a[0] * 1000)


# ============================================================
# 4. 涨跌停价与封板判定（P1）
# ============================================================

class TestPriceLimits:
    def test_exchange_rounding_over_full_price_range(self):
        """Python round 是银行家舍入；交易所是 ROUND_HALF_UP，修复前有 769 个价位差 1 分"""
        def exch(x, m):
            return float((Decimal(str(x)) * Decimal(str(m)))
                         .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        bad = []
        for i in range(100, 20001):
            p = i / 100
            lo, up = get_price_limits(p)
            if abs(lo - exch(p, "0.90")) > 1e-9 or abs(up - exch(p, "1.10")) > 1e-9:
                bad.append(p)
        assert not bad
        assert get_price_limits(2.05) == (1.85, 2.26)

    def test_adjust_price_never_below_lower_limit(self):
        lo, _ = get_price_limits(2.05)
        assert adjust_price(1.83, 2.05) >= lo - 1e-9

    def test_st_and_gem_limits(self):
        assert get_price_limits(10.0, is_st=True) == (9.50, 10.50)
        assert get_price_limits(10.0, is_cy=True) == (8.00, 12.00)
        assert get_price_limits(10.0, is_bj=True) == (7.00, 13.00)

    def test_limit_up_uses_intraday_low(self):
        pc = 10.0
        upper = get_price_limits(pc)[1]
        assert is_limit_up_locked(upper, pc) is True                     # 无信息时保守拒单
        assert is_limit_up_locked(upper, pc, low_price=9.5) is False     # 盘中打开可买
        assert is_limit_up_locked(upper, pc, low_price=upper) is True    # 一字板拒单

    def test_limit_down_uses_intraday_high(self):
        pc = 10.0
        lower = get_price_limits(pc)[0]
        assert is_limit_down_locked(lower, pc) is True
        assert is_limit_down_locked(lower, pc, high_price=9.9) is False
        assert is_limit_down_locked(lower, pc, high_price=lower) is True

    def test_buy_on_opened_limit_up_succeeds_but_one_word_board_rejected(self):
        pc = 10.0
        upper = get_price_limits(pc)[1]

        p_open = Portfolio(100000)
        assert p_open.buy("000001.SZ", upper, 100, "20240102",
                          prev_close=pc, slippage=False,
                          bar_low=9.5, bar_high=upper) is not None

        p_locked = Portfolio(100000)
        assert p_locked.buy("000001.SZ", upper, 100, "20240102",
                            prev_close=pc, slippage=False,
                            bar_low=upper, bar_high=upper) is None

    def test_sell_on_opened_limit_down_succeeds_but_one_word_board_rejected(self):
        pc = 10.0
        lower = get_price_limits(pc)[0]

        p_open = Portfolio(100000)
        p_open.buy("000001.SZ", 10.0, 100, "20240102", slippage=False)
        assert p_open.sell("000001.SZ", lower, 100, "20240103",
                           prev_close=pc, slippage=False,
                           bar_low=lower, bar_high=9.9) is not None

        p_locked = Portfolio(100000)
        p_locked.buy("000001.SZ", 10.0, 100, "20240102", slippage=False)
        assert p_locked.sell("000001.SZ", lower, 100, "20240103",
                             prev_close=pc, slippage=False,
                             bar_low=lower, bar_high=lower) is None


# ============================================================
# 5. ATR 仓位未来函数（P1）
# ============================================================

class TestAtrNoLookahead:
    DATES = ["20240102", "20240103", "20240104", "20240105", "20240106", "20240107"]

    @staticmethod
    def _make(exec_close, exec_atr):
        close = [10.0, 10.0, exec_close, 10.0, 10.0, 10.0]
        atr = [0.30, 0.30, exec_atr, 0.30, 0.30, 0.30]
        cols = {
            "trade_date": TestAtrNoLookahead.DATES,
            "open": [10.0] * 6, "high": [10.6] * 6, "low": [9.4] * 6,
            "close": close, "volume": [1e6] * 6, "amount": [1e7] * 6,
            "atr14": atr,
        }
        for c, v in {"ma5": 10.0, "ma20": 10.0, "ma60": 10.0, "rsi14": 50.0,
                     "dif": 0.0, "dea": 0.0, "kdj_k": 50.0, "kdj_d": 50.0,
                     "kdj_j": 50.0, "boll_upper": 11.0, "boll_lower": 9.0,
                     "boll_mid": 10.0, "vol_ma5": 1e6, "vol_ma10": 1e6,
                     "vol_ma20": 1e6}.items():
            cols[c] = [v] * 6
        return pd.DataFrame(cols)

    @staticmethod
    def _run(exec_close, exec_atr):
        from core.models import Signal
        from strategies.base import BaseStrategy
        from engine.backtester import Backtester

        class BuyOnD(BaseStrategy):
            name = "probe"
            description = "probe"
            param_schema = {}

            def on_bar(self, td, data, portfolio=None):
                if td == "20240103" and (portfolio is None or portfolio.active_position_count == 0):
                    return [Signal(ts_code="X.SH", trade_date=td, strategy=self.name,
                                   direction="BUY", score=0.7, price_ref=10.0, reason="probe")]
                return []

        r = Backtester(BuyOnD, {}, ["X.SH"], "20240102", "20240107", 100000,
                       execution_mode="next_open", position_sizing="atr_risk",
                       preloaded_data={"X.SH": TestAtrNoLookahead._make(exec_close, exec_atr)}
                       ).run(save=False)
        buys = [t for t in r.trades if t.direction == "BUY"]
        return buys[0].volume if buys else None

    def test_execution_day_close_cannot_change_position_size(self):
        """只改 D+1 收盘价/ATR、开盘价不变；股数必须完全一致"""
        vols = [self._run(c, a) for c, a in
                [(10.0, 0.30), (11.0, 0.80), (9.5, 0.15), (12.0, 1.50)]]
        assert all(v is not None and v > 0 for v in vols)
        assert len(set(vols)) == 1, f"仓位被执行日收盘价改写：{vols}"


# ============================================================
# 6. 风控克隆与撮合规则透传（P1）
# ============================================================

class TestRiskCloneAndOptions:
    def test_clone_preserves_every_config_field(self):
        src = RiskManager(
            stop_loss_pct=-6.0, trailing_stop_activation=12.0,
            trailing_stop_callback=4.0, max_holding_days=30,
            enable_break_even=True, break_even_trigger_pct=8.0,
            break_even_buffer_pct=0.5,
        )
        c = clone_risk_manager(src)
        assert c is not src
        for k in ("stop_loss_pct", "trailing_stop_activation",
                  "trailing_stop_callback", "max_holding_days",
                  "enable_break_even", "break_even_trigger_pct",
                  "break_even_buffer_pct"):
            assert getattr(c, k) == getattr(src, k), k
        assert clone_risk_manager(None) is None

    def test_clone_has_independent_mutable_state(self):
        src = RiskManager()
        c = clone_risk_manager(src)
        c.highest_prices["000001.SZ"] = 99.0
        assert "000001.SZ" not in src.highest_prices

    def test_options_helper_filters_unknown_keys(self):
        opts = build_backtest_options(max_active_positions=3)
        assert _options_kwargs({"foo": 1, "max_active_positions": 5}) == {
            "max_active_positions": 5}
        assert _options_kwargs(None) == {}
        assert set(_options_kwargs(opts)) == set(btmod.BACKTEST_OPTION_KEYS)

    def test_grid_search_forwards_options_and_clones_risk(self):
        opts = build_backtest_options(
            max_active_positions=3, position_sizing="atr_risk",
            risk_per_trade=0.02, market_regime_filter=True,
            market_index="000001.SH")
        seen = []

        class FakeBT:
            def __init__(self, **kw):
                seen.append(kw)

            def run(self, save=False):
                return SimpleNamespace(total_return=1.0, max_drawdown=0.0,
                                       sharpe_ratio=0.0, win_rate=0.0,
                                       calmar_ratio=0.0, trade_count=0)

        rm = RiskManager(stop_loss_pct=-6.0, enable_break_even=True,
                         break_even_trigger_pct=8.0, break_even_buffer_pct=0.5)
        with patch.object(btmod, "Backtester", FakeBT):
            results = grid_search(
                object, ["X.SH"], "20230101", "20231231", 100000,
                {"p": [1, 2, 3]}, preloaded_data={}, risk_manager=rm,
                backtest_options=opts)

        assert len(results) == 3 and len(seen) == 3
        for kw in seen:
            assert kw["max_active_positions"] == 3
            assert kw["position_sizing"] == "atr_risk"
            assert kw["risk_per_trade"] == 0.02
            assert kw["market_regime_filter"] is True
            assert kw["market_index"] == "000001.SH"
        rms = [kw["risk_manager"] for kw in seen]
        assert len({id(x) for x in rms}) == 3          # 每组合独立状态
        assert all(x is not rm and x.enable_break_even is True
                   and x.break_even_trigger_pct == 8.0 for x in rms)

    def test_parameter_stability_forwards_options(self, monkeypatch):
        seen = []

        def fake_gs(**kw):
            seen.append(kw.get("backtest_options"))
            return [
                {"params": {"p": 1}, "result": None, "metric_value": 1.0},
                {"params": {"p": 2}, "result": None, "metric_value": 0.5},
            ]

        monkeypatch.setattr(V, "grid_search", fake_gs)
        opts = build_backtest_options(max_active_positions=4)
        out = V.parameter_stability(
            object, ["X.SH"], "20230101", "20231231", {"p": [1, 2]},
            backtest_options=opts)
        assert seen == [opts]
        assert out["best_params"] == {"p": 1}


@pytest.fixture
def wf_preloaded(monkeypatch):
    """合成行情，避免 walk_forward 测试依赖真实数据库"""
    from data.indicators import apply_indicators
    n = 260
    dates, d = [], pd.Timestamp("2023-01-02")
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d += pd.Timedelta(days=1)

    data = {}
    for code, seed in [("600000.SH", 7), ("600001.SH", 11)]:
        rng = np.random.default_rng(seed)
        close = np.maximum(10.0 + np.cumsum(rng.normal(0.02, 0.12, n)), 1.0)
        open_ = close + rng.normal(0, 0.04, n)
        raw = pd.DataFrame({
            "ts_code": code, "trade_date": dates,
            "open": open_, "high": np.maximum(open_, close) + 0.05,
            "low": np.minimum(open_, close) - 0.05, "close": close,
            "volume": rng.integers(500_000, 2_000_000, n).astype(float),
            "amount": close * 1_000_000,
            "pct_chg": pd.Series(close).pct_change().fillna(0.0).to_numpy() * 100,
            "turnover": 2.0, "adj_factor": 1.0,
        })
        data[code] = apply_indicators(
            raw, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])

    monkeypatch.setattr(V, "preload_backtest_data", lambda *a, **k: data)
    monkeypatch.setattr(V, "get_benchmark_returns", lambda *a, **k: {})
    return data


class TestWalkForwardOptions:
    def test_options_reach_both_training_and_test_stages(self, wf_preloaded, monkeypatch):
        from strategies import STRATEGY_REGISTRY
        opts = build_backtest_options(
            max_active_positions=2, position_sizing="atr_risk",
            risk_per_trade=0.025)
        gs_seen, bt_seen = [], []
        orig_gs = V.grid_search

        def spy_gs(**kw):
            gs_seen.append(kw.get("backtest_options"))
            return orig_gs(**kw)

        orig_bt = V.Backtester

        class SpyBT(orig_bt):
            def __init__(self, *a, **kw):
                bt_seen.append(kw)
                super().__init__(*a, **kw)

        monkeypatch.setattr(V, "grid_search", spy_gs)
        monkeypatch.setattr(V, "Backtester", SpyBT)

        rep = V.walk_forward(
            STRATEGY_REGISTRY["ma_cross"],
            {"fast_period": [5, 8], "slow_period": [20]},
            list(wf_preloaded), "20230101", "20240731",
            n_splits=2, train_ratio=0.6, backtest_options=opts)

        assert len(rep.windows) >= 1
        assert gs_seen and all(o == opts for o in gs_seen), "训练段/参数稳定性未透传"
        assert bt_seen and all(
            k["max_active_positions"] == 2
            and k["position_sizing"] == "atr_risk"
            and k["risk_per_trade"] == 0.025
            for k in bt_seen), "测试段未透传"
