"""海龟突破策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class TurtleStrategy(BaseStrategy):
    """海龟突破策略（简化版）

    买入: 价格突破N日最高价
    卖出: 价格跌破N日最低价
    适合中长线趋势跟踪。
    """
    name = "turtle"
    description = "海龟突破策略（突破N日高点买、跌破N日低点卖）"
    param_schema = {
        "entry_period": {"default": 20, "desc": "入场突破周期"},
        "exit_period": {"default": 10, "desc": "出场突破周期"},
    }

    def __init__(self, entry_period: int = 20, exit_period: int = 10):
        self.entry_period = entry_period
        self.exit_period = exit_period

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.entry_period + 1:
                continue

            price = df["close"].iloc[-1]

            # 跳过停牌
            if df["volume"].iloc[-1] == 0:
                continue

            # 入场信号: 价格突破N日最高
            high_n = df["high"].iloc[-(self.entry_period + 1):-1].max()
            if price > high_n:
                score = min((price / high_n - 1) * 10, 1.0)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=round(score, 2),
                    reason=f"突破{self.entry_period}日高点{high_n:.2f}",
                    price_ref=price,
                ))

            # 出场信号: 价格跌破M日最低
            if portfolio and portfolio.get_position(ts_code):
                low_m = df["low"].iloc[-(self.exit_period + 1):-1].min()
                if price < low_m:
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="SELL",
                        score=0.8,
                        reason=f"跌破{self.exit_period}日低点{low_m:.2f}",
                        price_ref=price,
                    ))

        return signals
