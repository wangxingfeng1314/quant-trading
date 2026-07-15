"""MACD背离策略"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MACDDivergenceStrategy(BaseStrategy):
    """MACD底背离/顶背离策略

    买入: 价格创新低但MACD柱不创新低（底背离）
    卖出: 价格创新高但MACD柱不创新高（顶背离）
    """
    name = "macd_divergence"
    description = "MACD背离策略（底背离买、顶背离卖）"
    param_schema = {
        "lookback": {"default": 60, "desc": "回看周期（天）"},
    }

    def __init__(self, lookback: int = 60):
        self.lookback = lookback

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.lookback or "macd_hist" not in df.columns:
                continue

            window = df.iloc[-self.lookback:]
            price = df["close"].iloc[-1]
            curr_macd = df["macd_hist"].iloc[-1]
            curr_close = df["close"].iloc[-1]

            # 跳过停牌
            if df["volume"].iloc[-1] == 0:
                continue

            # 底背离: 当前价格接近区间最低，但MACD柱高于之前最低点
            price_min_idx = window["close"].idxmin()
            price_min = window.loc[price_min_idx, "close"]
            macd_at_price_min = window.loc[price_min_idx, "macd_hist"]

            # 当前价格比最低价高不超过5%，且MACD柱比那时高
            if (curr_close <= price_min * 1.05
                    and curr_macd > macd_at_price_min
                    and macd_at_price_min < 0
                    and curr_macd < 0):  # 都在零轴下方更有效
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=round(min((curr_macd - macd_at_price_min) / abs(macd_at_price_min + 0.001), 1.0), 2),
                    reason=f"MACD底背离: 价格接近低点{price_min:.2f}, "
                           f"MACD柱从{macd_at_price_min:.3f}回升至{curr_macd:.3f}",
                    price_ref=price,
                ))

            # 顶背离: 当前价格接近区间最高，但MACD柱低于之前最高点
            price_max_idx = window["close"].idxmax()
            price_max = window.loc[price_max_idx, "close"]
            macd_at_price_max = window.loc[price_max_idx, "macd_hist"]

            if (curr_close >= price_max * 0.95
                    and curr_macd < macd_at_price_max
                    and macd_at_price_max > 0
                    and curr_macd > 0):
                if portfolio and portfolio.get_position(ts_code):
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="SELL",
                        score=round(min((macd_at_price_max - curr_macd) / abs(macd_at_price_max + 0.001), 1.0), 2),
                        reason=f"MACD顶背离: 价格接近高点{price_max:.2f}, "
                               f"MACD柱从{macd_at_price_max:.3f}回落至{curr_macd:.3f}",
                        price_ref=price,
                    ))

        return signals
