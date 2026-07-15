"""A股交易费用模型 + 滑点模型"""
from core.config import COMMISSION_RATE, MIN_COMMISSION, STAMP_TAX_RATE, TRANSFER_FEE_RATE, SLIPPAGE_RATE


def calc_cost(price: float, volume: int, direction: str,
              commission_rate: float = None,
              stamp_tax_rate: float = None) -> dict:
    """计算A股交易费用

    Args:
        price: 成交价格
        volume: 成交数量（股）
        direction: 'BUY' 或 'SELL'
        commission_rate: 佣金费率，默认从配置读取
        stamp_tax_rate: 印花税费率，默认从配置读取

    Returns:
        {'commission': 佣金, 'tax': 印花税, 'transfer_fee': 过户费, 'total': 总费用}
    """
    if commission_rate is None:
        commission_rate = COMMISSION_RATE
    if stamp_tax_rate is None:
        stamp_tax_rate = STAMP_TAX_RATE

    amount = price * volume

    # 佣金：双向收取，最低5元
    commission = max(amount * commission_rate, MIN_COMMISSION)

    # 印花税：仅卖出收取
    tax = amount * stamp_tax_rate if direction == "SELL" else 0.0

    # 过户费：双向收取
    transfer_fee = amount * TRANSFER_FEE_RATE

    total = commission + tax + transfer_fee

    return {
        "commission": round(commission, 2),
        "tax": round(tax, 2),
        "transfer_fee": round(transfer_fee, 2),
        "total": round(total, 2),
    }


def apply_slippage(price: float, direction: str, slippage_rate: float = None) -> float:
    """应用滑点：买入按更高价成交，卖出按更低价成交

    Args:
        price: 信号触发价
        direction: 'BUY' 或 'SELL'
        slippage_rate: 滑点比率，默认从配置读取

    Returns:
        滑点调整后的实际成交价
    """
    if slippage_rate is None:
        slippage_rate = SLIPPAGE_RATE

    if direction == "BUY":
        # 买入：实际成交价 = 触发价 * (1 + 滑点率)
        return round(price * (1 + slippage_rate), 2)
    else:
        # 卖出：实际成交价 = 触发价 * (1 - 滑点率)
        return round(price * (1 - slippage_rate), 2)


def adjust_price(price: float, prev_close: float, is_st: bool = False,
                 is_cy: bool = False) -> float:
    """价格涨跌停限制

    Args:
        price: 委托价格
        prev_close: 前收盘价
        is_st: 是否ST股
        is_cy: 是否创业板/科创板（20%涨跌停）

    Returns:
        限制后的价格
    """
    if prev_close <= 0:
        return price

    if is_st:
        upper = prev_close * 1.05
        lower = prev_close * 0.95
    elif is_cy:
        upper = prev_close * 1.20
        lower = prev_close * 0.80
    else:
        upper = prev_close * 1.10
        lower = prev_close * 0.90

    return max(lower, min(upper, price))


def round_lot(volume: int, direction: str) -> int:
    """取整手（买入必须100股整数倍，卖出可以任意数量）"""
    if direction == "BUY":
        return (volume // 100) * 100
    return volume
