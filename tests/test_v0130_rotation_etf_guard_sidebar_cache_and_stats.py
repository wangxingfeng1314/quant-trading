"""v0.13.0 安全护栏、侧边栏全局提速与收口完善测试套件:
1. get_non_watchlist_stocks_with_data / clean_non_watchlist_data 默认保护 ROTATION_ETF_PRESETS（防误删双舱核心资产）
2. 策略百科 _get_strategy_stats 单 SQL GROUP BY 聚合 + 纳入双舱/滚动组合历史成绩
3. 侧边栏 app/main.py 改用 cached_instrument_list + 更新后刷新过期估值，app/data_viewer.py 顶部卡片支持 ETF 3 位小数
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd
import pytest

from core import config as core_config
from core.models import BacktestResult
from data import storage


def _use_temp_db(monkeypatch, tmp_path, name: str = "test_v0130.db"):
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


def _make_daily_df(ts_code: str, base: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame([{
        "ts_code": ts_code,
        "trade_date": "20261125",
        "open": base,
        "high": base * 1.01,
        "low": base * 0.99,
        "close": base,
        "volume": 1_000_000.0,
        "amount": base * 1_000_000.0,
        "pct_chg": 0.0,
    }])


def test_non_watchlist_cleanup_protects_core_rotation_etfs_by_default(monkeypatch, tmp_path):
    """验证未进自选表的 7 只核心轮动 ETF 默认受保护，不会被当成非自选废缓存误删"""
    _use_temp_db(monkeypatch, tmp_path, "protect_etfs.db")

    storage.save_daily(_make_daily_df("600036.SH", 40.0))
    storage.save_daily(_make_daily_df("512890.SH", 1.25))
    storage.save_daily(_make_daily_df("518880.SH", 7.17))
    storage.save_daily(_make_daily_df("300750.SZ", 220.0))
    storage.add_to_watchlist("600036.SH")

    # 1. 默认行为：512890 / 518880 受轮动预设保护，只返回真正孤儿 300750.SZ
    assert storage.get_non_watchlist_stocks_with_data() == ["300750.SZ"]
    assert storage.get_non_watchlist_stocks_with_data(exclude_rotation_etfs=False) == [
        "300750.SZ", "512890.SH", "518880.SH",
    ]

    # 2. 尝试清理指定轮动 ETF 默认拦截；显式清理全库只删孤儿 300750.SZ
    guard_res = storage.clean_non_watchlist_data(["512890.SH"])
    assert guard_res["deleted_codes"] == []
    assert not storage.get_daily("512890.SH").empty

    clean_res = storage.clean_non_watchlist_data()
    assert clean_res["deleted_codes"] == ["300750.SZ"]
    assert storage.get_daily("300750.SZ").empty
    assert not storage.get_daily("512890.SH").empty
    assert not storage.get_daily("518880.SH").empty

    # 3. 显式传入 protect_rotation_etfs=False 时仍可手动清理
    force_res = storage.clean_non_watchlist_data(["512890.SH"], protect_rotation_etfs=False)
    assert force_res["deleted_codes"] == ["512890.SH"]
    assert storage.get_daily("512890.SH").empty


def test_strategy_intro_stats_single_group_by_and_includes_dual_sleeve(monkeypatch, tmp_path):
    """验证策略百科 _get_strategy_stats 聚合准确并包含双舱组合与单策略"""
    import app.strategy_intro as si

    _use_temp_db(monkeypatch, tmp_path, "si_stats.db")
    for strat_name, tot, ann, sh, dd, wr, tc in (
        ("kdj_cross", 12.0, 6.0, 0.9, 8.0, 65.0, 20),
        ("kdj_cross", 16.0, 8.0, 1.1, 6.0, 75.0, 30),
        ("dual_sleeve_core_satellite", 30.5, 10.0, 0.86, 6.5, 62.0, 245),
    ):
        storage.save_backtest_result(BacktestResult(
            strategy=strat_name, params="{}", start_date="20240101", end_date="20261124",
            initial_capital=100000.0, final_capital=100000.0 * (1 + tot / 100.0),
            total_return=tot, annual_return=ann, max_drawdown=dd,
            sharpe_ratio=sh, win_rate=wr, trade_count=tc,
        ))

    stats = si._get_strategy_stats.__wrapped__()
    assert stats["kdj_cross"]["回测次数"] == 2
    assert stats["kdj_cross"]["平均总收益%"] == pytest.approx(14.0)
    assert stats["kdj_cross"]["总交易次数"] == 50
    assert stats["dual_sleeve_core_satellite"]["平均夏普"] == pytest.approx(0.86)


def test_main_sidebar_cache_valuation_refresh_and_data_viewer_etf_precision():
    """验证侧边栏使用 cached_instrument_list / 更新估值联动，以及数据浏览卡片 ETF 3 位小数"""
    main_src = Path("app/main.py").read_text(encoding="utf-8")
    assert "get_instrument_list" not in main_src
    assert "cached_instrument_list()" in main_src
    assert "refresh_watchlist_valuations_if_stale" in main_src

    dv_src = Path("app/data_viewer.py").read_text(encoding="utf-8")
    assert "format_price" in dv_src
    assert "price_decimals" in dv_src
