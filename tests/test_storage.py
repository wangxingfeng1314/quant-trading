"""数据存储层（data.storage）CRUD 单元测试

使用临时 SQLite 数据库（:memory:），不依赖真实 quant.db 文件。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import pandas as pd
from core.models import Signal


# ============================================================
# 辅助函数：在内存数据库中创建表结构
# ============================================================

@pytest.fixture(autouse=True)
def _patch_db_path(monkeypatch):
    """将所有 storage 操作重定向到 :memory: 数据库"""
    import sqlite3
    from contextlib import contextmanager

    # 创建一个内存数据库连接（每个测试独立）
    conn = sqlite3.connect(":memory:")

    @contextmanager
    def mock_get_conn():
        """模拟原 get_conn 行为：自动提交/回滚，但使用内存数据库"""
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr("data.storage.get_conn", mock_get_conn)

    # 初始化表结构
    from data.storage import init_db
    init_db()

    yield

    conn.close()


@pytest.fixture
def sample_stock_df():
    """模拟股票列表"""
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "600519.SH", "300750.SZ"],
        "symbol": ["000001", "600519", "300750"],
        "name": ["平安银行", "贵州茅台", "宁德时代"],
        "market": ["SZ", "SH", "SZ"],
        "list_date": ["19910403", "20010731", "20180611"],
        "industry": ["银行", "白酒", "电池"],
        "is_st": [0, 0, 0],
        "delist_date": ["", "", ""],
    })


@pytest.fixture
def sample_daily_df():
    """模拟日线数据"""
    dates = [f"202607{str(i).zfill(2)}" for i in range(1, 6)]
    return pd.DataFrame({
        "ts_code": ["000001.SZ"] * 5,
        "trade_date": dates,
        "open": [10.0, 10.1, 10.2, 10.3, 10.4],
        "high": [10.5, 10.6, 10.7, 10.8, 10.9],
        "low": [9.8, 9.9, 10.0, 10.1, 10.2],
        "close": [10.2, 10.3, 10.4, 10.5, 10.6],
        "volume": [1000000] * 5,
        "amount": [10200000] * 5,
        "pct_chg": [0.5, 0.3, 0.2, 0.15, 0.1],
        "turnover": [1.5] * 5,
        "adj_factor": [1.0] * 5,
    })


class TestStockList:
    """股票列表 CRUD"""

    def test_save_and_get_stock_list(self, sample_stock_df):
        from data.storage import save_stock_list, get_stock_list

        save_stock_list(sample_stock_df)
        df = get_stock_list()

        assert len(df) == 3
        assert "000001.SZ" in df["ts_code"].values
        assert "600519.SH" in df["ts_code"].values

    def test_save_stock_list_idempotent(self, sample_stock_df):
        """验证幂等：重复保存不会产生重复记录"""
        from data.storage import save_stock_list, get_stock_list

        save_stock_list(sample_stock_df)
        save_stock_list(sample_stock_df)  # 重复保存
        df = get_stock_list()
        assert len(df) == 3  # 不产生重复记录

    def test_get_stock_name(self, sample_stock_df):
        from data.storage import save_stock_list, get_stock_name

        save_stock_list(sample_stock_df)
        name = get_stock_name("600519.SH")
        assert name == "贵州茅台"

    def test_get_stock_name_empty(self):
        from data.storage import get_stock_name
        name = get_stock_name("999999.SZ")
        assert name == "999999.SZ"  # 查不到返回代码本身


class TestDailyPrice:
    """日线数据 CRUD"""

    def test_save_and_get_daily(self, sample_daily_df):
        from data.storage import save_daily, get_daily

        save_daily(sample_daily_df)
        df = get_daily("000001.SZ")

        assert len(df) == 5
        assert df.iloc[0]["close"] == 10.2

    def test_get_daily_with_date_filter(self, sample_daily_df):
        from data.storage import save_daily, get_daily

        save_daily(sample_daily_df)
        df = get_daily("000001.SZ", start_date="20260703")
        assert len(df) >= 3  # 20260703, 04, 05

    def test_get_daily_count(self, sample_daily_df):
        from data.storage import save_daily, get_daily_count

        save_daily(sample_daily_df)
        count = get_daily_count()
        assert count == 5

    def test_get_latest_date(self, sample_daily_df):
        from data.storage import save_daily, get_latest_date

        save_daily(sample_daily_df)
        latest = get_latest_date("000001.SZ")
        assert latest == "20260705"

    def test_get_stocks_with_data(self, sample_daily_df):
        from data.storage import save_daily, get_stocks_with_data

        save_daily(sample_daily_df)
        stocks = get_stocks_with_data()
        assert "000001.SZ" in stocks


class TestWatchlist:
    """自选股 CRUD"""

    def test_add_and_get(self):
        from data.storage import add_to_watchlist, get_watchlist, remove_from_watchlist

        add_to_watchlist("000001.SZ", note="测试股票")
        wl = get_watchlist()
        assert len(wl) == 1
        assert wl.iloc[0]["ts_code"] == "000001.SZ"

    def test_remove(self):
        from data.storage import add_to_watchlist, get_watchlist, remove_from_watchlist

        add_to_watchlist("000001.SZ")
        add_to_watchlist("600519.SH")
        assert len(get_watchlist()) == 2

        remove_from_watchlist("000001.SZ")
        assert len(get_watchlist()) == 1

    def test_add_duplicate(self):
        """重复添加同一只股票不会报错"""
        from data.storage import add_to_watchlist, get_watchlist

        add_to_watchlist("000001.SZ")
        add_to_watchlist("000001.SZ")  # 重复添加
        wl = get_watchlist()
        assert len(wl) == 1  # 去重

    def test_update_group(self):
        from data.storage import add_to_watchlist, update_watchlist_group, get_watchlist

        add_to_watchlist("000001.SZ")
        update_watchlist_group("000001.SZ", "长线池")
        wl = get_watchlist()
        assert wl.iloc[0]["group_name"] == "长线池"

    def test_get_watchlist_groups(self):
        from data.storage import add_to_watchlist, update_watchlist_group, get_watchlist_groups

        add_to_watchlist("000001.SZ")
        update_watchlist_group("000001.SZ", "长线池")
        add_to_watchlist("600519.SH")
        update_watchlist_group("600519.SH", "短线池")

        groups = get_watchlist_groups()
        assert "长线池" in groups
        assert "短线池" in groups


class TestSignal:
    """信号 CRUD"""

    def test_save_and_get_signals(self):
        from data.storage import save_signal, get_signals

        sig = Signal(
            ts_code="000001.SZ", trade_date="20260715",
            strategy="ma_cross", direction="BUY",
            score=0.9, reason="金叉", price_ref=10.5,
        )
        save_signal(sig)

        signals = get_signals()
        assert len(signals) >= 1

    def test_save_duplicate_signal(self):
        """重复保存同一信号不会 UNIQUE 冲突"""
        from data.storage import save_signal, get_signals

        sig = Signal(
            ts_code="000001.SZ", trade_date="20260715",
            strategy="turtle", direction="SELL",
            score=0.8, reason="跌破",
        )
        save_signal(sig)
        save_signal(sig)  # 重复保存（INSERT OR REPLACE）

        signals = get_signals()
        assert len(signals) >= 1

    def test_get_signals_filtered(self):
        from data.storage import save_signal, get_signals

        for strategy in ["ma_cross", "turtle", "kdj_cross"]:
            sig = Signal(
                ts_code="000001.SZ", trade_date="20260715",
                strategy=strategy, direction="BUY",
            )
            save_signal(sig)

        ma_signals = get_signals(strategy="ma_cross")
        assert len(ma_signals) >= 1
        assert ma_signals.iloc[0]["strategy"] == "ma_cross"


class TestBacktestResults:
    """回测结果 CRUD"""

    def test_save_and_get(self):
        import json
        from data.storage import save_backtest_result, get_backtest_results
        from core.models import BacktestResult

        result = BacktestResult(
            strategy="ma_cross", params=json.dumps({"fast": 5, "slow": 20}),
            start_date="20260101", end_date="20260701",
            initial_capital=100000, final_capital=110000,
            total_return=10.0, annual_return=20.0,
            max_drawdown=5.0, sharpe_ratio=1.5,
            calmar_ratio=4.0, win_rate=60.0, trade_count=5,
        )

        bt_id = save_backtest_result(result)
        assert bt_id > 0  # 返回有效的自增ID

        results_df = get_backtest_results()
        assert len(results_df) >= 1
        assert results_df.iloc[0]["strategy"] == "ma_cross"

    def test_save_backtest_trades(self):
        import json
        from data.storage import save_backtest_result, save_backtest_trades, get_backtest_trades
        from core.models import BacktestResult, Trade

        result = BacktestResult(
            strategy="test", params="{}",
            start_date="20260101", end_date="20260131",
            initial_capital=100000, final_capital=100000,
        )
        bt_id = save_backtest_result(result)

        trades = [
            Trade(ts_code="000001.SZ", direction="BUY",
                  trade_date="20260105", price=10.0, volume=1000),
            Trade(ts_code="000001.SZ", direction="SELL",
                  trade_date="20260115", price=11.0, volume=1000,
                  pnl=950.0, holding_days=10),
        ]
        save_backtest_trades(bt_id, trades)

        trades_df = get_backtest_trades(bt_id)
        assert len(trades_df) == 2
        assert trades_df.iloc[0]["direction"] == "BUY"


class TestPosition:
    """模拟持仓 CRUD"""

    def test_add_and_get(self):
        from data.storage import add_position, get_positions, remove_position

        pid = add_position("000001.SZ", buy_price=10.0, shares=1000,
                           buy_date="20260701", note="测试持仓")
        assert pid > 0

        positions = get_positions()
        assert len(positions) >= 1
        assert positions[0]["ts_code"] == "000001.SZ"

    def test_remove_position(self):
        from data.storage import add_position, get_positions, remove_position

        pid = add_position("000001.SZ", 10.0, 1000, "20260701")
        remove_position(pid)

        positions = get_positions()
        assert all(p["ts_code"] != "000001.SZ" for p in positions)


class TestIndexDaily:
    """大盘指数 CRUD"""

    def test_save_and_get(self):
        from data.storage import save_index_daily, get_index_daily

        df = pd.DataFrame({
            "ts_code": ["000001.SH"] * 3,
            "trade_date": ["20260701", "20260702", "20260703"],
            "open": [3000, 3010, 3020],
            "high": [3020, 3030, 3040],
            "low": [2980, 2990, 3000],
            "close": [3010, 3020, 3030],
            "volume": [100] * 3,
            "pct_chg": [0.5, 0.3, 0.2],
        })
        save_index_daily(df)

        result = get_index_daily("000001.SH", limit=2)
        assert len(result) == 2

    def test_get_index_latest_date(self):
        from data.storage import save_index_daily, get_index_latest_date

        df = pd.DataFrame({
            "ts_code": ["000001.SH"],
            "trade_date": ["20260705"],
            "open": [3000], "high": [3020], "low": [2980],
            "close": [3010], "volume": [100], "pct_chg": [0.5],
        })
        save_index_daily(df)

        latest = get_index_latest_date()
        assert latest == "20260705"


class TestBatchGetLatest:
    """批量查询"""

    def test_batch_get_latest(self, sample_daily_df):
        from data.storage import save_daily, batch_get_latest, save_stock_list

        # 插入多只股票的数据
        df1 = sample_daily_df.copy()
        df2 = sample_daily_df.copy()
        df2["ts_code"] = "600519.SH"
        df2["close"] = [1500, 1510, 1520, 1530, 1540]
        df2["trade_date"] = [f"202607{str(i).zfill(2)}" for i in range(1, 6)]

        save_daily(df1)
        save_daily(df2)

        result = batch_get_latest(["000001.SZ", "600519.SH"], limit=2)
        assert len(result) == 4  # 每只股票 2 条 = 4 条
        codes = result["ts_code"].unique()
        assert "000001.SZ" in codes
        assert "600519.SH" in codes
