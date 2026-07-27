"""投资组合（Portfolio）单元测试"""
import pytest
from engine.portfolio import Portfolio
from core.models import Trade, BacktestResult


class TestPortfolioBasic:
    """组合基础操作"""

    def test_init(self):
        p = Portfolio(100000)
        assert p.cash == 100000
        assert p.initial_capital == 100000
        assert p.positions == {}
        assert p.trades == []
        assert p.equity_curve == []

    def test_buy_one_stock(self):
        p = Portfolio(100000)
        t = p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
                  trade_date="20260701", slippage=False)
        assert t is not None
        assert t.direction == "BUY"
        assert t.ts_code == "000001.SZ"
        assert t.volume == 1000
        # 现金减少：100000 - (10*1000 + 佣金5 + 过户费0.1) ≈ 89994.9
        assert p.cash < 100000
        assert "000001.SZ" in p.positions

    def test_buy_and_sell(self):
        p = Portfolio(100000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
              trade_date="20260701", slippage=False)
        buy_cash = p.cash

        # 当天不能卖（T+1）
        t = p.sell(ts_code="000001.SZ", price=11.0, volume=1000,
                   trade_date="20260701", slippage=False)
        assert t is None  # T+1 限制

        # 第二天可以卖
        t = p.sell(ts_code="000001.SZ", price=11.0, volume=1000,
                   trade_date="20260702", slippage=False)
        assert t is not None
        assert t.direction == "SELL"
        assert t.pnl > 0    # 10→11 有盈利
        # 现金恢复并加上利润（扣除费用）
        assert p.cash > buy_cash

    def test_buy_insufficient_funds(self):
        """资金不足时自动减少数量"""
        p = Portfolio(1000)  # 只有1000元
        t = p.buy(ts_code="000001.SZ", price=100.0, volume=1000,
                  trade_date="20260701", slippage=False)
        # 资金不足，应该买入更少的股数或None
        if t is not None:
            assert t.volume < 1000
        else:
            # 也有可能完全买不起
            pos = p.get_position("000001.SZ")
            assert pos is None or pos.is_empty

    def test_market_value(self):
        p = Portfolio(100000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
              trade_date="20260701", slippage=False)
        mv = p.market_value({"000001.SZ": 12.0})
        assert mv == 12000.0  # 1000股 × 12元

    def test_total_equity(self):
        p = Portfolio(100000)
        cost_total = 100000 - p.cash  # 记录初始现金变化前
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
              trade_date="20260701", slippage=False)
        te = p.total_equity({"000001.SZ": 10.0})
        # 总权益应略小于初始资金（因为交易费用）
        assert te < 100000
        assert te > 100000 - 100


class TestPortfolioMetrics:
    """组合绩效指标计算"""

    def test_no_trades(self):
        p = Portfolio(100000)
        metrics = p.calc_metrics()
        assert metrics == {}  # 无权益曲线时返回空

    def test_profitable_trade(self):
        p = Portfolio(100000)
        # 买入
        t = p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
                  trade_date="20260701", slippage=False)
        # 记录当日权益（持仓市值=10000）
        p.record_equity("20260701", {"000001.SZ": 10.0})

        # 卖出（涨价到11元）
        t = p.sell(ts_code="000001.SZ", price=11.0, volume=1000,
                   trade_date="20260702", slippage=False)
        # 记录卖出后权益
        p.record_equity("20260702", {"000001.SZ": 11.0})
        # 再记录一天
        p.record_equity("20260703", {"000001.SZ": 11.0})

        metrics = p.calc_metrics()
        assert metrics["trade_count"] == 2   # 1笔买入+1笔卖出
        assert metrics["sell_count"] == 1
        assert metrics["win_rate"] == 100.0  # 唯一卖出是盈利
        assert metrics["total_return"] > 0
        assert metrics["final_capital"] > p.initial_capital

    def test_loss_trade(self):
        p = Portfolio(100000)
        # 高价买入，低价卖出（亏损）
        t = p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
                  trade_date="20260701", slippage=False)
        p.record_equity("20260701", {"000001.SZ": 10.0})

        t = p.sell(ts_code="000001.SZ", price=9.0, volume=1000,
                   trade_date="20260702", slippage=False)
        p.record_equity("20260702", {"000001.SZ": 9.0})

        metrics = p.calc_metrics()
        assert metrics["win_rate"] == 0.0    # 亏损
        assert metrics["total_return"] < 0

    def test_max_drawdown(self):
        p = Portfolio(100000)
        # 构造先涨后跌的权益曲线
        p.record_equity("20260701", {})
        # 手动设置权益曲线模拟回撤
        p.equity_curve = [
            {"date": "20260701", "equity": 100000, "cash": 100000, "market_value": 0},
            {"date": "20260702", "equity": 110000, "cash": 110000, "market_value": 0},
            {"date": "20260703", "equity": 105000, "cash": 105000, "market_value": 0},
            {"date": "20260704", "equity": 95000, "cash": 95000, "market_value": 0},
            {"date": "20260705", "equity": 102000, "cash": 102000, "market_value": 0},
        ]
        metrics = p.calc_metrics()
        # 最高点110000，最低点95000，回撤=(110000-95000)/110000=13.64%
        assert abs(metrics["max_drawdown"] - 13.64) < 0.1
        assert metrics["trade_count"] == 0


class TestPortfolioEdgeCases:
    """边界情况"""

    def test_sell_nonexistent_stock(self):
        p = Portfolio(100000)
        t = p.sell(ts_code="999999.SZ", price=10.0, volume=1000,
                   trade_date="20260701", slippage=False)
        assert t is None

    def test_sell_more_than_owned(self):
        p = Portfolio(100000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=500,
              trade_date="20260701", slippage=False)
        t = p.sell(ts_code="000001.SZ", price=10.0, volume=1000,
                   trade_date="20260702", slippage=False)
        assert t is not None
        # 实际卖出应该只有500股
        assert t.volume == 500

    def test_multiple_stocks(self):
        p = Portfolio(200000)
        p.buy(ts_code="000001.SZ", price=10.0, volume=1000,
              trade_date="20260701", slippage=False)
        p.buy(ts_code="600519.SH", price=100.0, volume=500,
              trade_date="20260701", slippage=False)

        assert len(p.positions) == 2

        prices = {"000001.SZ": 12.0, "600519.SH": 110.0}
        mv = p.market_value(prices)
        assert mv == 12000 + 55000  # 1000*12 + 500*110
