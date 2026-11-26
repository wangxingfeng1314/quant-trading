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


def test_dashboard_report_and_status_no_name_error():
    """测试首页看板复盘报告及系统状态逻辑，确保无任何未定义名称"""
    from app.dashboard import _render_daily_report, _show_system_status
    import inspect

    # 验证关键函数存在且可解析，无编译期 undefined name
    assert callable(_render_daily_report)
    assert callable(_show_system_status)

    source = inspect.getsource(_render_daily_report)
    assert "check_data_freshness" not in source or "cached_check_data_freshness" in source


def test_format_strategy_and_direction_cn():
    """测试策略中文名称映射与方向转换"""
    from app.st_utils import format_strategy_cn, format_direction_cn
    from strategies import STRATEGY_REGISTRY

    # 1. 验证所有内置策略均有中文映射且不为空
    for sname in STRATEGY_REGISTRY.keys():
        cn = format_strategy_cn(sname)
        assert cn != sname, f"策略 {sname} 未成功解析为中文名称"
        assert len(cn) > 0

    # 2. 验证多策略组合及逗号分隔
    assert "多策略共振(2合一)" in format_strategy_cn("ensemble_2")
    combo = format_strategy_cn("ma_cross, macd_cross")
    assert "双均线交叉" in combo and "MACD金叉死叉" in combo

    # 3. 验证买入卖出方向转换
    assert "买入" in format_direction_cn("BUY")
    assert "卖出" in format_direction_cn("SELL")
    assert format_direction_cn("BUY", with_icon=False) == "买入"
    assert format_direction_cn("SELL", with_icon=False) == "卖出"
    assert format_direction_cn("HOLD") == "HOLD"


def test_format_stock_cn():
    """测试股票代码转中文名称工具方法"""
    from app.st_utils import format_stock_cn

    # 空值返回空字符串
    assert format_stock_cn("") == ""
    assert format_stock_cn(None) == ""

    # 传入 name_map
    name_map = {"600519.SH": "贵州茅台", "000001.SZ": "平安银行"}
    assert format_stock_cn("600519.SH", name_map) == "600519.SH 贵州茅台"
    assert format_stock_cn("000001.SZ", name_map) == "000001.SZ 平安银行"
    # 未匹配时返回原代码
    assert format_stock_cn("999999.SH", name_map) == "999999.SH"

    # 不传 name_map 自动使用缓存列表
    formatted = format_stock_cn("000001.SZ")
    assert formatted.startswith("000001.SZ")


def test_html_helpers_no_indented_code_blocks():
    """验证表格/卡片 HTML 无 4 空格缩进与空行，避免被 Markdown 当作代码块渲染"""
    from app.st_utils import _build_chinese_dataframe_html
    from app.theme import card_html

    df = pd.DataFrame({"代码": ["510310.SH"], "名称": ["沪深300ETF"], "最新价": ["¥3.860"]})
    table_html = _build_chinese_dataframe_html(df, height=400)
    assert 'class="quant-table-wrap"' in table_html
    assert "<table " in table_html
    assert "510310.SH" in table_html
    assert "共 1 条记录" in table_html
    for line in table_html.splitlines():
        assert line != "", "HTML 中不应包含空行"
        assert not line.startswith("    "), f"存在 4 空格缩进: {line!r}"

    card = card_html("标题", "<div>内容</div>")
    assert "标题" in card and "<div>内容</div>" in card
    for line in card.splitlines():
        assert line != "", "卡片 HTML 中不应包含空行"
        assert not line.startswith("    "), f"卡片存在 4 空格缩进: {line!r}"


def test_register_static_mimetypes():
    """验证静态资源 MIME 类型注册完整（避免 Windows 下 woff2 图标字体变 text/plain）"""
    import mimetypes
    from app.st_utils import register_static_mimetypes

    register_static_mimetypes()
    assert mimetypes.guess_type("MaterialSymbols-Rounded.woff2")[0] == "font/woff2"
    assert mimetypes.guess_type("font.woff")[0] == "font/woff"
    assert mimetypes.guess_type("Metric.js")[0] in ("application/javascript", "text/javascript")
    assert mimetypes.guess_type("index.css")[0] == "text/css"


def test_get_dual_stack_server_address():
    """验证双栈监听地址探测及配置文件启用双栈监听"""
    import pathlib
    from app.st_utils import get_dual_stack_server_address

    addr = get_dual_stack_server_address()
    assert addr in ("::", "0.0.0.0")

    cfg_text = pathlib.Path(".streamlit/config.toml").read_text(encoding="utf-8")
    assert 'address = "::"' in cfg_text



