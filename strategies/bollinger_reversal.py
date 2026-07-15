"""布林带反转策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class BollingerReversalStrategy(BaseStrategy):
    """布林带反转策略

    买入: 价格触及布林带下轨后反弹
    卖出: 价格触及布林带上轨后回落

    适合震荡行情中的波段操作。
    """
    name = "bollinger_reversal"
    description = "布林带反转策略（触下轨买、触上轨卖）"
    param_schema = {
        "boll_period": {"default": 20, "desc": "布林带周期"},
        "boll_std": {"default": 2.0, "desc": "标准差倍数"},
    }

    def __init__(self, boll_period: int = 20, boll_std: float = 2.0):
        self.boll_period = boll_period
        self.boll_std = boll_std

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.boll_period + 2:
                continue
            if "boll_upper" not in df.columns or "boll_lower" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            curr_low = df["low"].iloc[-1]
            curr_high = df["high"].iloc[-1]
            curr_close = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            boll_lower = df["boll_lower"].iloc[-1]
            boll_upper = df["boll_upper"].iloc[-1]
            boll_mid = df["boll_mid"].iloc[-1]

            # 买入: 前一日在轨内或触轨，今日低点触及下轨后收盘回升
            if prev_close >= boll_lower and curr_low <= boll_lower and curr_close > boll_lower:
                score = round(min((curr_close - boll_lower) / (boll_mid - boll_lower + 0.01), 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"触及布林下轨{boll_lower:.2f}后反弹至{curr_close:.2f}",
                    price_ref=curr_close,
                ))

            # 卖出: 前一日在轨内或触轨，今日高点触及上轨后收盘回落
            elif prev_close <= boll_upper and curr_high >= boll_upper and curr_close < boll_upper:
                if portfolio and portfolio.get_position(ts_code):
                    score = round(min((boll_upper - curr_close) / (boll_upper - boll_mid + 0.01), 1.0), 2)
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=score,
                        reason=f"触及布林上轨{boll_upper:.2f}后回落至{curr_close:.2f}",
                        price_ref=curr_close,
                    ))

        return signals
