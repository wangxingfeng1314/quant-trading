"""URL 导航与参数双向同步测试 (F5 刷新与直链访问)"""
import pytest
from streamlit.testing.v1 import AppTest


def test_url_navigation_default():
    """测试默认首次访问，自动解析至 dashboard 并设置 URL 参数"""
    at = AppTest.from_file("app/main.py", default_timeout=15)
    at.run()
    assert not at.exception
    # 验证 URL 包含 page=dashboard
    assert "dashboard" in at.query_params.get("page", []) or at.query_params.get("page") == "dashboard"


def test_url_navigation_direct_backtest():
    """测试直接通过 ?page=backtest 访问或 F5 刷新回测页面"""
    at = AppTest.from_file("app/main.py", default_timeout=15)
    at.query_params["page"] = "backtest"
    at.run()
    assert not at.exception
    # 验证单选框选中的是回测中心
    radio_vals = [r.value for r in at.sidebar.radio if r.key == "main_nav_radio"]
    assert radio_vals == ["🔬 回测中心"]
    assert "backtest" in at.query_params.get("page", []) or at.query_params.get("page") == "backtest"


def test_url_navigation_alias_bt():
    """测试别名 ?page=bt 自动规范化并加载回测中心"""
    at = AppTest.from_file("app/main.py", default_timeout=15)
    at.query_params["page"] = "bt"
    at.run()
    assert not at.exception
    radio_vals = [r.value for r in at.sidebar.radio if r.key == "main_nav_radio"]
    assert radio_vals == ["🔬 回测中心"]
    # 规范化后 query_params 应为 backtest
    assert "backtest" in at.query_params.get("page", []) or at.query_params.get("page") == "backtest"


def test_url_navigation_direct_signal():
    """测试直接通过 ?page=signal 访问信号中心"""
    at = AppTest.from_file("app/main.py", default_timeout=15)
    at.query_params["page"] = "signal"
    at.run()
    assert not at.exception
    radio_vals = [r.value for r in at.sidebar.radio if r.key == "main_nav_radio"]
    assert radio_vals == ["📡 信号中心"]


def test_url_navigation_chinese_alias():
    """测试中文别名 ?page=选股"""
    at = AppTest.from_file("app/main.py", default_timeout=15)
    at.query_params["page"] = "选股"
    at.run()
    assert not at.exception
    radio_vals = [r.value for r in at.sidebar.radio if r.key == "main_nav_radio"]
    assert radio_vals == ["🔍 选股筛选"]
    assert "screener" in at.query_params.get("page", []) or at.query_params.get("page") == "screener"


def test_url_navigation_switch_page():
    """测试侧边栏切换页面后，URL 参数自动同步更新"""
    at = AppTest.from_file("app/main.py", default_timeout=15)
    at.run()
    # 切换到 💼 持仓管理
    nav_radio = next(r for r in at.sidebar.radio if r.key == "main_nav_radio")
    nav_radio.set_value("💼 持仓管理").run()
    assert not at.exception
    assert "portfolio" in at.query_params.get("page", []) or at.query_params.get("page") == "portfolio"
