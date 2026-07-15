"""KDJ金叉死叉策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class KDJCrossStrategy(BaseStrategy):
    """KDJ金叉死叉策略

    买入: K线上穿D线（金叉），且K/D值在超卖区（<30）
    卖出: K线下穿D线（死叉），且K/D值在超买区（>70）

    适合短线交易。
    """
    name = "kdj_cross"
    description = "KDJ金叉死叉策略（低位金叉买、高位死叉卖）"
    param_schema = {
        "k_period": {"default": 3, "desc": "K线平滑周期"},
        "d_period": {"default": 3, "desc": "D线平滑周期"},
    }

    def __init__(self, k_period: int = 3, d_period: int = 3):
        self.k_period = k_period
        self.d_period = d_period

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < 3:
                continue
            if "kdj_k" not in df.columns or "kdj_d" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            curr_k = df["kdj_k"].iloc[-1]
            curr_d = df["kdj_d"].iloc[-1]
            prev_k = df["kdj_k"].iloc[-2]
            prev_d = df["kdj_d"].iloc[-2]
            price = df["close"].iloc[-1]

            if any(v != v for v in [curr_k, curr_d, prev_k, prev_d]):
                continue

            # 金叉买入: K上穿D 且 K在低位(<40)
            if prev_k <= prev_d and curr_k > curr_d and curr_k < 40:
                score = round(min((40 - curr_k) / 40 + (curr_k - curr_d) / 100, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"KDJ低位金叉: K={curr_k:.1f}上穿D={curr_d:.1f}",
                    price_ref=price,
                ))

            # 死叉卖出: K下穿D 且 K在高位(>60)
            elif prev_k >= prev_d and curr_k < curr_d and curr_k > 60:
                if portfolio and portfolio.get_position(ts_code):
                    score = round(min((curr_k - 60) / 40 + (curr_d - curr_k) / 100, 1.0), 2)
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=score,
                        reason=f"KDJ高位死叉: K={curr_k:.1f}下穿D={curr_d:.1f}",
                        price_ref=price,
                    ))

        return signals
