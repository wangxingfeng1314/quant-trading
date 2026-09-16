"""均线回踩策略（短线强势股回调买入）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MAPullbackStrategy(BaseStrategy):
    """均线回踩策略

    买入: 短期多头排列（MA5>MA10）下，价格回踩MA10不破且重新站上
    卖出: 跌破MA10且MA5下穿MA10（短期趋势走坏）

    适合短线强势股回调后的二次介入。
    """
    name = "ma_pullback"
    description = "均线回踩策略（强势股回踩MA10不破买入）"
    style = "短线"
    param_schema = {
        "ma_period": {"default": 10, "desc": "回踩均线周期"},
        "tolerance": {"default": 0.005, "desc": "回踩容差（跌破均线幅度）"},
    }

    def __init__(self, ma_period: int = 10, tolerance: float = 0.005):
        self.ma_period = ma_period
        self.tolerance = tolerance
        self.ma_col = f"ma{ma_period}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.ma_period + 3:
                continue
            if self.ma_col not in df.columns or "ma5" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            ma = df[self.ma_col].iloc[-1]
            prev_ma = df[self.ma_col].iloc[-2]
            ma5 = df["ma5"].iloc[-1]
            prev_ma5 = df["ma5"].iloc[-2]

            if any(v != v for v in [ma, prev_ma, ma5, prev_ma5]):
                continue

            # 短期多头: MA5 > MA10 且 MA10 走平或向上
            is_bullish = ma5 > ma and ma >= prev_ma
            # 回踩: 前日收盘在昨日MA10附近或跌破（不超过容差），今日站回均线上方
            pulled_back = (prev_close <= prev_ma * (1 + self.tolerance))
            rebounded = price > ma

            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 买入: 未持仓 + 多头排列 + 回踩企稳
            if not has_position and is_bullish and pulled_back and rebounded:
                # 回踩越深、站回越强分越高
                depth_score = min((prev_ma - prev_close) / max(prev_ma, 0.01) / 0.05, 0.4) if prev_close < prev_ma else 0.1
                strength_score = min((price - ma) / max(ma, 0.01) / 0.02, 0.4)
                score = round(min(0.3 + max(depth_score, 0) + strength_score, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"回踩MA{self.ma_period}={ma:.2f}企稳反弹（{prev_close:.2f}→{price:.2f}）",
                    price_ref=price,
                ))

            # 卖出: 持有中（或无组合扫描）+ 跌破MA10 且 MA5 下穿 MA10
            elif portfolio is None or has_position:
                if price < ma and prev_ma5 >= prev_ma and ma5 < ma:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.7,
                        reason=f"跌破MA{self.ma_period}={ma:.2f}且MA5下穿，短线走坏",
                        price_ref=price,
                    ))

        return signals
