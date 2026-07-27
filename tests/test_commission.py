"""交易费用模型单元测试"""
import pytest
from engine.commission import calc_cost, apply_slippage, adjust_price, round_lot


class TestCalcCost:
    """交易费用计算"""

    def test_buy_commission(self):
        """买入：只收佣金（最低5元）+ 过户费"""
        cost = calc_cost(price=10.0, volume=1000, direction="BUY")
        # 金额=10000, 佣金万2.5=2.5 → 最低5元, 过户费万0.1=0.1
        assert cost["commission"] == 5.0
        assert cost["tax"] == 0.0                # 买入不交印花税
        assert cost["transfer_fee"] > 0
        assert cost["total"] == cost["commission"] + cost["transfer_fee"]

    def test_sell_commission(self):
        """卖出：佣金 + 印花税（万5）+ 过户费"""
        cost = calc_cost(price=10.0, volume=1000, direction="SELL")
        assert cost["commission"] == 5.0          # 最低5元
        assert cost["tax"] > 0                     # 万5 = 5元
        assert cost["total"] > cost["commission"]

    def test_large_trade_commission(self):
        """大额交易：佣金超过最低5元限制"""
        cost = calc_cost(price=100.0, volume=10000, direction="BUY")
        # 金额=1000000, 佣金万2.5=250
        assert cost["commission"] == 250.0
        assert cost["commission"] > 5.0

    def test_custom_commission_rate(self):
        """自定义佣金费率"""
        cost = calc_cost(price=10.0, volume=1000, direction="BUY",
                         commission_rate=0.0005)  # 万5
        # 金额=10000, 佣金万5=5, 最低5元
        assert cost["commission"] == 5.0

    def test_custom_stamp_tax(self):
        """自定义印花税率"""
        cost = calc_cost(price=10.0, volume=1000, direction="SELL",
                         stamp_tax_rate=0.001)    # 千1
        # 金额=10000, 印花税千1=10
        assert cost["tax"] == 10.0


class TestApplySlippage:
    """滑点模型"""

    def test_buy_slippage(self):
        """买入滑点：价格上浮"""
        result = apply_slippage(price=10.0, direction="BUY", slippage_rate=0.001)
        assert result == 10.01  # 10 * 1.001 = 10.01

    def test_sell_slippage(self):
        """卖出滑点：价格下浮"""
        result = apply_slippage(price=10.0, direction="SELL", slippage_rate=0.001)
        assert result == 9.99   # 10 * 0.999 = 9.99

    def test_default_slippage(self):
        """使用默认滑点率"""
        buy = apply_slippage(price=100.0, direction="BUY")
        assert buy == 100.1    # 默认千分之一

    def test_zero_slippage(self):
        """滑点为0"""
        result = apply_slippage(price=10.0, direction="BUY", slippage_rate=0.0)
        assert result == 10.0


class TestAdjustPrice:
    """涨跌停价格限制"""

    def test_normal_stock(self):
        """主板股票：±10%"""
        price = adjust_price(price=11.0, prev_close=10.0, is_st=False)
        assert price == 11.0         # 10*1.1=11，未超限

    def test_normal_stock_limit_up(self):
        """主板股票涨停限制"""
        price = adjust_price(price=12.0, prev_close=10.0, is_st=False)
        assert price == 11.0         # 被限制在10*1.1=11

    def test_normal_stock_limit_down(self):
        """主板股票跌停限制"""
        price = adjust_price(price=8.0, prev_close=10.0, is_st=False)
        assert price == 9.0          # 被限制在10*0.9=9

    def test_st_stock(self):
        """ST股票：±5%"""
        price = adjust_price(price=10.5, prev_close=10.0, is_st=True)
        assert price == 10.5         # 10*1.05=10.5
        price = adjust_price(price=11.0, prev_close=10.0, is_st=True)
        assert price == 10.5         # 被限制

    def test_cy_stock(self):
        """创业板/科创板：±20%"""
        price = adjust_price(price=12.0, prev_close=10.0, is_cy=True)
        assert price == 12.0         # 10*1.2=12
        price = adjust_price(price=13.0, prev_close=10.0, is_cy=True)
        assert price == 12.0         # 被限制

    def test_zero_prev_close(self):
        """前收盘价为0（停牌等）"""
        price = adjust_price(price=10.0, prev_close=0)
        assert price == 10.0         # 不限


class TestRoundLot:
    """取整手"""

    def test_buy_round_down(self):
        """买入向下取整到100股"""
        assert round_lot(volume=150, direction="BUY") == 100
        assert round_lot(volume=1090, direction="BUY") == 1000
        assert round_lot(volume=100, direction="BUY") == 100

    def test_buy_small_volume(self):
        """买入不足一手归零"""
        assert round_lot(volume=50, direction="BUY") == 0

    def test_sell_keep_any(self):
        """卖出不限整手"""
        assert round_lot(volume=150, direction="SELL") == 150
        assert round_lot(volume=1, direction="SELL") == 1
