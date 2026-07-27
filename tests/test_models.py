"""数据模型单元测试"""
import json
import pytest
from datetime import datetime
from core.models import Signal, Trade, PositionInfo, BacktestResult, StockInfo


class TestSignal:
    """交易信号模型"""

    def test_basic_signal(self):
        sig = Signal(ts_code="000001.SZ", trade_date="20260701",
                     strategy="ma_cross", direction="BUY")
        assert sig.ts_code == "000001.SZ"
        assert sig.direction == "BUY"
        assert sig.score == 0.0    # 默认值
        assert sig.reason == ""      # 默认值

    def test_signal_default_created_at(self):
        sig = Signal(ts_code="000001.SZ", trade_date="20260701",
                     strategy="turtle", direction="SELL")
        # created_at 应该在当前时间的 1 秒内
        now = datetime.now().isoformat()
        assert abs(len(sig.created_at) - len(now)) <= 1

    def test_signal_full_fields(self):
        sig = Signal(
            ts_code="600519.SH", trade_date="20260715",
            strategy="macd_divergence", direction="BUY",
            score=0.95, reason="MACD底背离", price_ref=185.50,
        )
        assert sig.score == 0.95
        assert "MACD底背离" in sig.reason
        assert sig.price_ref == 185.50


class TestTrade:
    """交易记录模型"""

    def test_basic_trade(self):
        t = Trade(ts_code="000001.SZ", direction="BUY",
                  trade_date="20260701", price=10.0, volume=1000)
        assert t.ts_code == "000001.SZ"
        assert t.pnl == 0.0       # 买入时 pnl=0
        assert t.holding_days == 0

    def test_sell_trade(self):
        t = Trade(ts_code="000001.SZ", direction="SELL",
                  trade_date="20260715", price=11.0, volume=1000,
                  commission=5.0, tax=5.5, pnl=950.0, holding_days=14)
        assert t.pnl == 950.0
        assert t.holding_days == 14
        assert t.commission == 5.0
        assert t.tax == 5.5


class TestBacktestResult:
    """回测结果模型"""

    def test_basic_result(self):
        r = BacktestResult(
            strategy="ma_cross", params=json.dumps({"fast": 5, "slow": 20}),
            start_date="20260101", end_date="20260701",
            initial_capital=100000, final_capital=120000,
            total_return=20.0, annual_return=40.0,
            max_drawdown=10.0, sharpe_ratio=1.5,
            calmar_ratio=4.0, win_rate=60.0, trade_count=10,
        )
        assert r.strategy == "ma_cross"
        assert r.total_return == 20.0
        assert r.sharpe_ratio == 1.5
        assert r.calmar_ratio == 4.0

    def test_default_result_values(self):
        r = BacktestResult(
            strategy="turtle", params="{}",
            start_date="20260101", end_date="20260701",
            initial_capital=100000, final_capital=100000,
        )
        assert r.total_return == 0.0
        assert r.trade_count == 0
        assert r.equity_curve == []

    def test_result_with_trades(self):
        trades = [
            Trade(ts_code="000001.SZ", direction="BUY",
                  trade_date="20260105", price=10.0, volume=1000),
            Trade(ts_code="000001.SZ", direction="SELL",
                  trade_date="20260115", price=11.0, volume=1000,
                  pnl=950.0, holding_days=10),
        ]
        r = BacktestResult(
            strategy="test", params="{}",
            start_date="20260101", end_date="20260701",
            initial_capital=100000, final_capital=110000,
            trades=trades,
        )
        assert len(r.trades) == 2
        assert r.trades[1].pnl == 950.0


class TestStockInfo:
    """股票基本信息模型"""

    def test_normal_stock(self):
        s = StockInfo(ts_code="000001.SZ", symbol="000001",
                      name="平安银行", market="SZ")
        assert s.name == "平安银行"
        assert s.is_st is False   # 默认值

    def test_st_stock(self):
        s = StockInfo(ts_code="600123.SH", symbol="600123",
                      name="*ST兰石", market="SH", is_st=True)
        assert s.is_st is True

    def test_with_industry(self):
        s = StockInfo(ts_code="600519.SH", symbol="600519",
                      name="贵州茅台", market="SH",
                      list_date="20010731", industry="白酒")
        assert s.industry == "白酒"
        assert s.list_date == "20010731"
