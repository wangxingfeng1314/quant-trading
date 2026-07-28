"""模型-数据库结构对齐测试

验证 core/models.py 的 dataclass 字段与 SQLite 表结构一致，
防止模型和数据库不同步。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import sqlite3
import pytest
from dataclasses import fields


# ============================================================
# 数据库表结构（从 storage.py 中的 CREATE TABLE 还原）
# 如果 storage.py 的表结构发生变更，这里需要同步更新
# ============================================================

EXPECTED_TABLES = {
    "stock_basic": {
        "columns": {
            "ts_code": "TEXT",
            "symbol": "TEXT",
            "name": "TEXT",
            "market": "TEXT",
            "list_date": "TEXT",
            "delist_date": "TEXT",
            "industry": "TEXT",
            "is_st": "INTEGER",
            "updated_at": "TEXT",
        },
        "pk": "ts_code",
    },
    "daily_price": {
        "columns": {
            "ts_code": "TEXT",
            "trade_date": "TEXT",
            "open": "REAL",
            "high": "REAL",
            "low": "REAL",
            "close": "REAL",
            "volume": "REAL",
            "amount": "REAL",
            "pct_chg": "REAL",
            "turnover": "REAL",
            "adj_factor": "REAL",
        },
        "pk": "(ts_code, trade_date)",
    },
    "signal": {
        "columns": {
            "id": "INTEGER",
            "ts_code": "TEXT",
            "trade_date": "TEXT",
            "strategy": "TEXT",
            "direction": "TEXT",
            "score": "REAL",
            "reason": "TEXT",
            "price_ref": "REAL",
            "created_at": "TEXT",
        },
        "unique": "(ts_code, trade_date, strategy, direction)",
    },
    "watchlist": {
        "columns": {
            "ts_code": "TEXT",
            "added_date": "TEXT",
            "note": "TEXT",
            "group_name": "TEXT",
        },
        "pk": "ts_code",
    },
    "index_daily": {
        "columns": {
            "ts_code": "TEXT",
            "trade_date": "TEXT",
            "open": "REAL",
            "high": "REAL",
            "low": "REAL",
            "close": "REAL",
            "volume": "REAL",
            "pct_chg": "REAL",
        },
        "pk": "(ts_code, trade_date)",
    },
    "position": {
        "columns": {
            "id": "INTEGER",
            "ts_code": "TEXT",
            "direction": "TEXT",
            "buy_price": "REAL",
            "shares": "INTEGER",
            "buy_date": "TEXT",
            "note": "TEXT",
        },
        "pk": "id",
    },
}


# ============================================================
# 模型字段定义（从 core/models.py 中的 dataclass 提取验证规则）
# ============================================================

def test_stock_info_fields():
    """StockInfo 模型的字段与 stock_basic 表一致"""
    from core.models import StockInfo
    model_fields = {f.name for f in fields(StockInfo)}
    # StockInfo 没有 updated_at，这没问题（updated_at 是表的管理字段）
    expected = {"ts_code", "symbol", "name", "market", "list_date",
                "delist_date", "industry", "is_st"}
    assert model_fields == expected, f"StockInfo 字段不匹配: {model_fields ^ expected}"


def test_signal_fields():
    """Signal 模型字段与 signal 表一致"""
    from core.models import Signal
    model_fields = {f.name for f in fields(Signal)}
    expected = {"ts_code", "trade_date", "strategy", "direction",
                "score", "reason", "price_ref", "created_at"}
    assert model_fields == expected, f"Signal 字段不匹配: {model_fields ^ expected}"


def test_backtest_result_fields():
    """BacktestResult 模型字段与 backtest_result 表一致"""
    from core.models import BacktestResult
    model_fields = {f.name for f in fields(BacktestResult)}
    # BacktestResult 有 equity_curve 和 trades（JSON序列化存储）
    expected = {"strategy", "params", "start_date", "end_date",
                "initial_capital", "final_capital", "total_return",
                "annual_return", "max_drawdown", "sharpe_ratio",
                "calmar_ratio", "win_rate", "trade_count", "sell_count",
                "equity_curve", "trades"}
    assert model_fields == expected, f"BacktestResult 字段不匹配: {model_fields ^ expected}"


def test_trade_fields():
    """Trade 模型字段与 backtest_trade 表一致"""
    from core.models import Trade
    model_fields = {f.name for f in fields(Trade)}
    expected = {"ts_code", "direction", "trade_date", "price", "volume",
                "commission", "tax", "pnl", "holding_days"}
    assert model_fields == expected, f"Trade 字段不匹配: {model_fields ^ expected}"


def test_position_info_fields():
    """PositionInfo 模型字段（注意 storage.position 表结构不同）"""
    from core.models import PositionInfo
    model_fields = {f.name for f in fields(PositionInfo)}
    expected = {"ts_code", "name", "shares", "avg_cost", "current_price",
                "buy_date", "market_value", "pnl", "pnl_pct"}
    assert model_fields == expected, f"PositionInfo 字段不匹配: {model_fields ^ expected}"


# ============================================================
# 运行时表结构验证（用内存数据库建表后检查列）
# ============================================================

def _create_test_db():
    """创建一个内存数据库，执行 init_db() 初始化"""
    from contextlib import contextmanager
    from data.storage import init_db

    conn = sqlite3.connect(":memory:")
    original_get_conn = None

    # 临时替换 get_conn
    import data.storage as storage_mod

    @contextmanager
    def mock_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    original = storage_mod.get_conn
    storage_mod.get_conn = mock_conn
    init_db()
    storage_mod.get_conn = original
    return conn


@pytest.fixture(scope="module")
def db_conn():
    """创建一个模块级别的内存数据库用于表结构验证"""
    conn = _create_test_db()
    yield conn
    conn.close()


@pytest.mark.parametrize("table_name, expected", list(EXPECTED_TABLES.items()))
def test_table_columns_exist(db_conn, table_name, expected):
    """验证每张表的列名和类型与期望一致"""
    cursor = db_conn.execute(f"PRAGMA table_info({table_name})")
    actual_cols = {row[1]: row[2] for row in cursor.fetchall()}

    expected_cols = expected["columns"]
    for col_name, col_type in expected_cols.items():
        assert col_name in actual_cols, f"{table_name} 缺少列: {col_name}"
        # 类型检查（SQLite 类型是 TEXT/REAL/INTEGER）
        actual_type = actual_cols[col_name].upper()
        expected_type = col_type.upper()
        # REAL 和 FLOAT 等价
        if expected_type in ("REAL", "FLOAT"):
            assert actual_type in ("REAL", "FLOAT", "NUMERIC"), \
                f"{table_name}.{col_name} 类型应为 REAL, 实际为 {actual_type}"
        else:
            assert actual_type == expected_type, \
                f"{table_name}.{col_name} 类型应为 {expected_type}, 实际为 {actual_type}"

    # 验证没有意外多余的列（允许多出的管理字段如 updated_at）
    extra = set(actual_cols.keys()) - set(expected_cols.keys())
    # 允许的管理字段
    allowed_extra = {"id", "created_at", "updated_at",
                     "backtest_id", "delist_date"}
    unexpected = extra - allowed_extra
    assert not unexpected, f"{table_name} 有多余列: {unexpected}"
