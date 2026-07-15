"""均线金叉/死叉策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MACrossStrategy(BaseStrategy):
    """双均线交叉策略

    买入: 短期均线上穿长期均线 (金叉)
    卖出: 短期均线下穿长期均线 (死叉)

    适合中长线波段操作。
    """
    name = "ma_cross"
    description = "双均线交叉策略（金叉买、死叉卖）"
    param_schema = {
        "fast_period": {"default": 5, "desc": "短期均线周期"},
        "slow_period": {"default": 20, "desc": "长期均线周期"},
    }

    def __init__(self, fast_period: int = 5, slow_period: int = 20):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.fast_col = f"ma{fast_period}"
        self.slow_col = f"ma{slow_period}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.slow_period + 1:
                continue
            if self.fast_col not in df.columns or self.slow_col not in df.columns:
                continue

            # 最近两天的均线值
            curr_fast = df[self.fast_col].iloc[-1]
            curr_slow = df[self.slow_col].iloc[-1]
            prev_fast = df[self.fast_col].iloc[-2]
            prev_slow = df[self.slow_col].iloc[-2]

            # 跳过NaN
            if any(v != v for v in [curr_fast, curr_slow, prev_fast, prev_slow]):
                continue

            price = df["close"].iloc[-1]

            # 金叉: 快线从下方穿越慢线
            if prev_fast <= prev_slow and curr_fast > curr_slow:
                # 跳过停牌
                if df["volume"].iloc[-1] == 0:
                    continue
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=round(min(curr_fast / curr_slow - 1, 0.1) * 10, 2),
                    reason=f"MA{self.fast_period}({curr_fast:.2f}) 上穿 "
                           f"MA{self.slow_period}({curr_slow:.2f})，金叉",
                    price_ref=price,
                ))

            # 死叉: 快线从上方穿越慢线
            elif prev_fast >= prev_slow and curr_fast < curr_slow:
                if portfolio and portfolio.get_position(ts_code):
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="SELL",
                        score=round(min(1 - curr_fast / curr_slow, 0.1) * 10, 2),
                        reason=f"MA{self.fast_period}({curr_fast:.2f}) 下穿 "
                               f"MA{self.slow_period}({curr_slow:.2f})，死叉",
                        price_ref=price,
                    ))

        return signals
