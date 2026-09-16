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
    style = "中长线"
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
            prev_close = df["close"].iloc[-2]

            # 跳过停牌
            if df["volume"].iloc[-1] == 0:
                continue

            # 入场信号: 昨日收在N日最高下方，今日突破N日最高
            high_n = df["high"].iloc[-(self.entry_period + 1):-1].max()
            prev_high_n = df["high"].iloc[-(self.entry_period + 2):-2].max() if len(df) >= self.entry_period + 2 else df["high"].iloc[:-2].max()
            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty
            if not has_position and prev_close <= prev_high_n and price > high_n:
                score = round(min(0.6 + (price / high_n - 1) * 10, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=score,
                    reason=f"突破{self.entry_period}日高点{high_n:.2f}",
                    price_ref=price,
                ))

            # 出场信号: 昨日收在M日最低上方，今日跌破M日最低
            if portfolio is None or has_position:
                low_m = df["low"].iloc[-(self.exit_period + 1):-1].min()
                prev_low_m = df["low"].iloc[-(self.exit_period + 2):-2].min() if len(df) >= self.exit_period + 2 else df["low"].iloc[:-2].min()
                if prev_close >= prev_low_m and price < low_m:
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
