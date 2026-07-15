"""均线多头排列策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MABullishStrategy(BaseStrategy):
    """均线多头排列策略

    买入: 短期/中期/长期均线形成多头排列（MA5 > MA20 > MA60），
          且价格位于所有均线上方，趋势确立后入场
    卖出: 均线多头排列被破坏（MA5下穿MA20）

    适合中长线趋势跟踪。
    """
    name = "ma_bullish"
    description = "均线多头排列策略（多头排列买入、死叉卖出）"
    param_schema = {
        "fast_period": {"default": 5, "desc": "短期均线"},
        "mid_period": {"default": 20, "desc": "中期均线"},
        "slow_period": {"default": 60, "desc": "长期均线"},
    }

    def __init__(self, fast_period: int = 5, mid_period: int = 20, slow_period: int = 60):
        self.fast_period = fast_period
        self.mid_period = mid_period
        self.slow_period = slow_period
        self.fast_col = f"ma{fast_period}"
        self.mid_col = f"ma{mid_period}"
        self.slow_col = f"ma{slow_period}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.slow_period + 2:
                continue
            cols = [self.fast_col, self.mid_col, self.slow_col]
            if any(c not in df.columns for c in cols):
                continue

            price = df["close"].iloc[-1]
            f, m, s = df[self.fast_col].iloc[-1], df[self.mid_col].iloc[-1], df[self.slow_col].iloc[-1]
            pf, pm, ps = df[self.fast_col].iloc[-2], df[self.mid_col].iloc[-2], df[self.slow_col].iloc[-2]
            if any(v != v for v in [f, m, s, pf, pm, ps]):
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            # 买入: 今日多头排列，且之前不是多头排列（刚形成）
            is_bullish = f > m > s and price > f
            was_bullish = pf > pm > ps

            if is_bullish and not was_bullish:
                score = round(min((f / s - 1) * 10, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"均线多头排列 MA{self.fast_period}={f:.2f} > "
                           f"MA{self.mid_period}={m:.2f} > MA{self.slow_period}={s:.2f}",
                    price_ref=price,
                ))

            # 卖出: 多头排列被破坏（快线下穿中线）
            elif pf > pm and f < m:
                if portfolio and portfolio.get_position(ts_code):
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.7,
                        reason=f"多头排列破坏: MA{self.fast_period}={f:.2f} "
                               f"下穿MA{self.mid_period}={m:.2f}",
                        price_ref=price,
                    ))

        return signals
