"""UI 性能与缓存测试"""
import pytest
import pandas as pd
from app.st_utils import (
    cached_get_stocks_with_data,
    cached_check_data_freshness,
    cached_instrument_list,
)
from app.dashboard import _calc_watchlist_snapshot_rows


def test_cached_get_stocks_with_data():
    """测试缓存版有数据股票查询"""
    res = cached_get_stocks_with_data(min_days=1)
    assert isinstance(res, list)
    res2 = cached_get_stocks_with_data(min_days=1)
    assert res == res2


def test_cached_check_data_freshness():
    """测试缓存版数据时效检查"""
    freshness = cached_check_data_freshness()
    assert isinstance(freshness, dict)
    assert "latest_date" in freshness
    assert "active_stocks" in freshness
    assert "total_rows" in freshness


def test_cached_instrument_list():
    """测试缓存版标的列表"""
    df = cached_instrument_list()
    assert isinstance(df, pd.DataFrame)
    if not df.empty:
        assert "ts_code" in df.columns
        assert "name" in df.columns


def test_calc_watchlist_snapshot_rows():
    """测试自选股快照数据聚合计算"""
    rows = _calc_watchlist_snapshot_rows((), ())
    assert rows == []
