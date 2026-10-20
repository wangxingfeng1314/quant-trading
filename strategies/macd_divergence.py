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
    style = "中长线"
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

            prev_window = window.iloc[:-3]
            if len(prev_window) < 10:
                continue

            # 底背离: 历史区间最低点
            price_min_idx = prev_window["close"].idxmin()
            price_min = prev_window.loc[price_min_idx, "close"]
            macd_at_price_min = prev_window.loc[price_min_idx, "macd_hist"]

            # 计算前低距今间隔与中间反弹幅度（确保存在双谷探底结构，而非单边阴跌）
            min_pos = prev_window.index.get_loc(price_min_idx)
            bars_since_min = (len(window) - 1) - min_pos
            between_high = window["close"].iloc[min_pos + 1:-1].max() if bars_since_min > 1 else curr_close
            has_bounce = (between_high >= price_min * 1.02) if bars_since_min >= 4 else True

            # 右侧企稳确认（当具备完整OHLC指标时，要求当日收阳或MACD柱边际改善，且前一日未重复触发）
            right_confirm_buy = True
            if "open" in df.columns and len(df) >= 3:
                curr_open = df["open"].iloc[-1]
                prev_macd = df["macd_hist"].iloc[-2]
                prev_close = df["close"].iloc[-2]
                # 当日收阳或MACD柱回升，且二次探底价格紧贴或跌破前低（1.5%以内）
                right_confirm_buy = (
                    (curr_close >= curr_open or curr_macd > prev_macd)
                    and prev_close > price_min * 1.012
                )

            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 当前价格创出新低或二次探底紧贴前低（<=1.015），且MACD柱明显高于前低
            if (not has_position
                    and bars_since_min >= 4
                    and has_bounce
                    and right_confirm_buy
                    and curr_close <= price_min * 1.015
                    and curr_macd > macd_at_price_min * 0.85
                    and curr_macd > macd_at_price_min
                    and macd_at_price_min < 0
                    and curr_macd < 0):  # 都在零轴下方更有效
                raw_score = 0.52 + 0.22 * min((curr_macd - macd_at_price_min) / (abs(macd_at_price_min) + 0.001), 1.0)
                score = round(min(max(raw_score, 0.52), 0.74), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=score,
                    reason=f"MACD底背离确认: 二次探底{curr_close:.2f}(前低{price_min:.2f}), "
                           f"MACD柱从{macd_at_price_min:.3f}抬升至{curr_macd:.3f}",
                    price_ref=price,
                ))

            # 顶背离: 历史区间最高点
            price_max_idx = prev_window["close"].idxmax()
            price_max = prev_window.loc[price_max_idx, "close"]
            macd_at_price_max = prev_window.loc[price_max_idx, "macd_hist"]
            max_pos = prev_window.index.get_loc(price_max_idx)
            bars_since_max = (len(window) - 1) - max_pos

            right_confirm_sell = True
            if "open" in df.columns and len(df) >= 3:
                curr_open = df["open"].iloc[-1]
                prev_macd = df["macd_hist"].iloc[-2]
                prev_close = df["close"].iloc[-2]
                right_confirm_sell = (
                    (curr_close <= curr_open or curr_macd < prev_macd)
                    and prev_close < price_max * 0.988
                )

            if (portfolio is None or has_position) and (
                    bars_since_max >= 4
                    and right_confirm_sell
                    and curr_close >= price_max * 0.985
                    and curr_macd < macd_at_price_max * 0.85
                    and curr_macd < macd_at_price_max
                    and macd_at_price_max > 0
                    and curr_macd > 0):
                raw_score = 0.55 + 0.22 * min((macd_at_price_max - curr_macd) / (abs(macd_at_price_max) + 0.001), 1.0)
                score = round(min(max(raw_score, 0.55), 0.76), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="SELL",
                    score=score,
                    reason=f"MACD顶背离确认: 二次冲高{curr_close:.2f}(前高{price_max:.2f}), "
                           f"MACD柱从{macd_at_price_max:.3f}衰减至{curr_macd:.3f}",
                    price_ref=price,
                ))

        return signals
