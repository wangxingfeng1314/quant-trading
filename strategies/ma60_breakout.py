"""长期均线突破策略（中长线趋势转折）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MA60BreakoutStrategy(BaseStrategy):
    """长期均线突破策略

    买入: 价格放量突破MA60（年线级别），且MA60走平或向上（趋势转折）
    卖出: 价格跌破MA60 且 MA60拐头向下（长期趋势走坏）

    适合中长线趋势跟踪，捕捉大级别行情启动。
    """
    name = "ma60_breakout"
    description = "长期均线突破（放量突破MA60且均线走平向上）"
    style = "中长线"
    param_schema = {
        "ma_period": {"default": 60, "desc": "长期均线周期"},
        "vol_ratio": {"default": 1.2, "desc": "突破放量倍数"},
        "slope_days": {"default": 5, "desc": "均线斜率回看天数"},
    }

    def __init__(self, ma_period: int = 60, vol_ratio: float = 1.2,
                 slope_days: int = 5):
        self.ma_period = ma_period
        self.vol_ratio = vol_ratio
        self.slope_days = slope_days
        self.ma_col = f"ma{ma_period}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.ma_period + self.slope_days + 2:
                continue
            if self.ma_col not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            ma = df[self.ma_col].iloc[-1]
            prev_ma = df[self.ma_col].iloc[-2]
            ma_slope = ma - df[self.ma_col].iloc[-self.slope_days - 1] if len(df) >= self.slope_days + 1 else 0

            if ma != ma or prev_ma != prev_ma:
                continue

            # 量比（放量确认）
            vol_ma5 = df["vol_ma5"].iloc[-1] if "vol_ma5" in df.columns else df["volume"].iloc[-5:].mean()
            vol_ratio = df["volume"].iloc[-1] / max(vol_ma5, 1)

            # 买入: 突破MA60 + 均线走平或向上 + 放量
            if (prev_close <= ma and price > ma
                    and ma >= prev_ma and vol_ratio >= self.vol_ratio):
                # 均线斜率越陡、放量越大分越高
                slope_score = min(max(ma_slope / max(ma, 0.01) * 50, 0), 0.5)
                vol_score = min((vol_ratio - 1) / 1.5, 0.3)
                score = round(min(0.4 + slope_score + vol_score, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"放量{vol_ratio:.1f}倍突破MA{self.ma_period}={ma:.2f}"
                           f"{'（均线走平/向上）' if ma >= prev_ma else ''}",
                    price_ref=price,
                ))

            # 卖出: 跌破MA60 且 MA60 拐头向下
            if portfolio and portfolio.get_position(ts_code):
                if price < ma and ma < prev_ma:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.8,
                        reason=f"跌破MA{self.ma_period}={ma:.2f}且均线拐头向下",
                        price_ref=price,
                    ))

        return signals
