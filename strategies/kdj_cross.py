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
    style = "短线"
    param_schema = {
        "k_period": {"default": 3, "desc": "K线平滑周期"},
        "d_period": {"default": 3, "desc": "D线平滑周期"},
    }

    def __init__(self, k_period: int = 3, d_period: int = 3):
        self.k_period = k_period
        self.d_period = d_period

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        from data.indicators import add_kdj
        signals = []
        is_default_kdj = (self.k_period == 3 and self.d_period == 3)
        for ts_code, df in data.items():
            if len(df) < 3:
                continue
            if "volume" not in df.columns or df["volume"].iloc[-1] == 0:
                continue

            if is_default_kdj and "kdj_k" in df.columns and "kdj_d" in df.columns:
                k_series = df["kdj_k"]
                d_series = df["kdj_d"]
            elif all(c in df.columns for c in ["high", "low", "close"]):
                kdj_df = add_kdj(df, n=9, m1=max(1, int(self.k_period)), m2=max(1, int(self.d_period)))
                k_series = kdj_df["kdj_k"]
                d_series = kdj_df["kdj_d"]
            else:
                continue

            curr_k = k_series.iloc[-1]
            curr_d = d_series.iloc[-1]
            prev_k = k_series.iloc[-2]
            prev_d = d_series.iloc[-2]
            price = df["close"].iloc[-1]

            if any(v != v for v in [curr_k, curr_d, prev_k, prev_d]):
                continue

            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 中期趋势过滤（防止在空头主跌浪中因K=35普通金叉盲目抄底）
            downtrend_penalty = False
            if "ma20" in df.columns and "ma60" in df.columns:
                ma20_val = df["ma20"].iloc[-1]
                ma60_val = df["ma60"].iloc[-1]
                if ma20_val == ma20_val and ma60_val == ma60_val and price < ma20_val < ma60_val:
                    downtrend_penalty = True

            k_buy_limit = 22 if downtrend_penalty else 35
            candle_ok = (price >= df["open"].iloc[-1]) if "open" in df.columns else True

            # 金叉买入: K上穿D 且 K在超卖低位 + 当日收阳企稳
            if not has_position and candle_ok and prev_k <= prev_d and curr_k > curr_d and curr_k < k_buy_limit:
                score = round(min(0.55 + (k_buy_limit - curr_k) / 40 * 0.3 + (curr_k - curr_d) / 100, 0.85), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"KDJ低位金叉: K={curr_k:.1f}上穿D={curr_d:.1f}",
                    price_ref=price,
                ))

            # 死叉卖出: K下穿D 且 K在高位(>60)
            elif (portfolio is None or has_position) and prev_k >= prev_d and curr_k < curr_d and curr_k > 60:
                score = round(min(0.5 + (curr_k - 60) / 40 * 0.4 + (curr_d - curr_k) / 100, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="SELL",
                    score=score,
                    reason=f"KDJ高位死叉: K={curr_k:.1f}下穿D={curr_d:.1f}",
                    price_ref=price,
                ))

        return signals
