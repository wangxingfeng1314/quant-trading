"""智能策略推荐引擎单元测试"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
import pandas as pd
from services.strategy_recommender import evaluate_stock_strategy, PREFIX_MAP


def test_heuristic_recommendation_growth():
    """测试成长科技类冷启动启发式推荐"""
    res = evaluate_stock_strategy("688981.SH", name="中芯国际", industry="半导体")
    assert res["strategy"] == "ma_cross"
    assert "成长弹性" in res["tag"]
    assert res["is_backtested"] is False


def test_heuristic_recommendation_defensive():
    """测试高股息防御类冷启动启发式推荐"""
    res = evaluate_stock_strategy("510300.SH", name="300ETF", industry="ETF基金")
    assert res["strategy"] == "bollinger_reversal"
    assert "高股息防御" in res["tag"]
    assert res["is_backtested"] is False


def test_heuristic_recommendation_default():
    """测试通用标的默认短线企稳推荐"""
    res = evaluate_stock_strategy("000001.SZ", name="平安银行", industry="商业银行")
    # 商业银行含银行，匹配高股息防御或KDJ
    assert res["strategy"] in ("bollinger_reversal", "kdj_cross")
    assert res["is_backtested"] is False
