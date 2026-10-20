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
    style = "中长线"
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
            if "close" not in df.columns or "volume" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            # 支持自定义均线周期（网格搜索非默认周期时按需计算，避免因列缺失静默返回0信号）
            fast_series = df[self.fast_col] if self.fast_col in df.columns else df["close"].rolling(window=self.fast_period, min_periods=1).mean()
            slow_series = df[self.slow_col] if self.slow_col in df.columns else df["close"].rolling(window=self.slow_period, min_periods=1).mean()

            # 最近两天的均线值
            curr_fast = fast_series.iloc[-1]
            curr_slow = slow_series.iloc[-1]
            prev_fast = fast_series.iloc[-2]
            prev_slow = slow_series.iloc[-2]

            # 跳过NaN
            if any(v != v for v in [curr_fast, curr_slow, prev_fast, prev_slow]):
                continue

            price = df["close"].iloc[-1]
            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 金叉: 快线从下方穿越慢线
            if not has_position and prev_fast <= prev_slow and curr_fast > curr_slow:
                # 慢线方向与量能配合加分（历史胜率63.6%，校准合理高分位 0.65~0.90）
                slope_bonus = 0.12 if curr_slow >= prev_slow else -0.05
                vol_bonus = 0.08 if ("vol_ma5" in df.columns and df["vol_ma5"].iloc[-1] > 0 and df["volume"].iloc[-1] > df["vol_ma5"].iloc[-1] * 1.1) else 0.0
                score = round(min(max(0.62 + (curr_fast / curr_slow - 1) * 12 + slope_bonus + vol_bonus, 0.55), 0.92), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=score,
                    reason=f"MA{self.fast_period}({curr_fast:.2f}) 上穿 "
                           f"MA{self.slow_period}({curr_slow:.2f})，金叉"
                           f"{'（均线向上）' if curr_slow >= prev_slow else ''}",
                    price_ref=price,
                ))

            # 死叉: 快线从上方穿越慢线
            elif (portfolio is None or has_position) and prev_fast >= prev_slow and curr_fast < curr_slow:
                score = round(min(0.5 + (1 - curr_fast / curr_slow) * 10, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="SELL",
                    score=score,
                    reason=f"MA{self.fast_period}({curr_fast:.2f}) 下穿 "
                           f"MA{self.slow_period}({curr_slow:.2f})，死叉",
                    price_ref=price,
                ))

        return signals
