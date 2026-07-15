"""RSI超买超卖策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class RSIOversoldStrategy(BaseStrategy):
    """RSI超买超卖策略

    买入: RSI跌破超卖线后回升（RSI < 30 然后上穿30）
    卖出: RSI升破超买线后回落（RSI > 70 然后下穿70）

    适合震荡行情中的反转交易。
    """
    name = "rsi_oversold"
    description = "RSI超买超卖策略（超卖买入、超买卖出）"
    param_schema = {
        "rsi_period": {"default": 14, "desc": "RSI计算周期"},
        "oversold": {"default": 30, "desc": "超卖线"},
        "overbought": {"default": 70, "desc": "超买线"},
    }

    def __init__(self, rsi_period: int = 14, oversold: int = 30, overbought: int = 70):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.rsi_col = f"rsi{rsi_period}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < 3 or self.rsi_col not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            curr_rsi = df[self.rsi_col].iloc[-1]
            prev_rsi = df[self.rsi_col].iloc[-2]
            price = df["close"].iloc[-1]

            if any(v != v for v in [curr_rsi, prev_rsi]):
                continue

            # 买入: 前一日RSI < 超卖线，今日上穿超卖线
            if prev_rsi < self.oversold and curr_rsi > self.oversold:
                score = round(min((curr_rsi - self.oversold) / 30, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"RSI从{prev_rsi:.1f}上穿{self.oversold}至{curr_rsi:.1f}，超卖反弹",
                    price_ref=price,
                ))

            # 卖出: 前一日RSI > 超买线，今日下穿超买线
            elif prev_rsi > self.overbought and curr_rsi < self.overbought:
                if portfolio and portfolio.get_position(ts_code):
                    score = round(min((self.overbought - curr_rsi) / 30, 1.0), 2)
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=score,
                        reason=f"RSI从{prev_rsi:.1f}下穿{self.overbought}至{curr_rsi:.1f}，超买回落",
                        price_ref=price,
                    ))

        return signals
