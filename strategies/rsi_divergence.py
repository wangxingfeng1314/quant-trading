"""RSI背离策略（震荡反转）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class RSIDivergenceStrategy(BaseStrategy):
    """RSI背离策略

    买入: 价格创新低但RSI高于前低（底背离）— 空头衰竭
    卖出: 价格创新高但RSI低于前高（顶背离）— 多头衰竭

    适合震荡行情中的反转交易，与MACD背离互补。
    """
    name = "rsi_divergence"
    description = "RSI背离策略（底背离买、顶背离卖）"
    style = "震荡"
    param_schema = {
        "lookback": {"default": 60, "desc": "回看周期（天）"},
        "rsi_period": {"default": 14, "desc": "RSI计算周期"},
    }

    def __init__(self, lookback: int = 60, rsi_period: int = 14):
        self.lookback = lookback
        self.rsi_period = rsi_period
        self.rsi_col = f"rsi{rsi_period}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.lookback or self.rsi_col not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            window = df.iloc[-self.lookback:]
            price = df["close"].iloc[-1]
            curr_rsi = df[self.rsi_col].iloc[-1]

            if curr_rsi != curr_rsi:
                continue

            prev_window = window.iloc[:-3]
            if len(prev_window) < 10:
                continue

            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 底背离: 历史区间最低点
            price_min_idx = prev_window["close"].idxmin()
            price_min = prev_window.loc[price_min_idx, "close"]
            rsi_at_price_min = prev_window.loc[price_min_idx, self.rsi_col]

            # 当前价格处于低位（接近或创新低），但RSI明显高于前低
            if (not has_position
                    and curr_rsi > rsi_at_price_min + 5
                    and price <= price_min * 1.05
                    and curr_rsi < 50):  # RSI在弱势区更有效
                raw_score = 0.5 + 0.5 * min((curr_rsi - rsi_at_price_min) / 30, 1.0)
                score = round(min(max(raw_score, 0.5), 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"RSI底背离: 价格近低点{price_min:.2f}, "
                           f"RSI从{rsi_at_price_min:.1f}回升至{curr_rsi:.1f}",
                    price_ref=price,
                ))

            # 顶背离: 历史区间最高点
            price_max_idx = prev_window["close"].idxmax()
            price_max = prev_window.loc[price_max_idx, "close"]
            rsi_at_price_max = prev_window.loc[price_max_idx, self.rsi_col]

            if (portfolio is None or has_position) and (
                    curr_rsi < rsi_at_price_max - 5
                    and price >= price_max * 0.95
                    and curr_rsi > 50):  # RSI在强势区更有效
                raw_score = 0.5 + 0.5 * min((rsi_at_price_max - curr_rsi) / 30, 1.0)
                score = round(min(max(raw_score, 0.5), 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="SELL",
                    score=score,
                    reason=f"RSI顶背离: 价格近高点{price_max:.2f}, "
                           f"RSI从{rsi_at_price_max:.1f}回落至{curr_rsi:.1f}",
                    price_ref=price,
                ))

        return signals
