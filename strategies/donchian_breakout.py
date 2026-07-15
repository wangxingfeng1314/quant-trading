"""唐奇安通道突破策略（趋势跟踪）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class DonchianBreakoutStrategy(BaseStrategy):
    """唐奇安通道突破策略（经典海龟体系简化版）

    买入: 价格突破N日唐奇安通道上轨
    卖出: 价格跌破M日唐奇安通道下轨

    与海龟策略的区别：
      - 海龟用前N日最高/低价作为触发
      - 唐奇安通道更强调通道中轨 + ATR止损，适合强趋势行情
    """
    name = "donchian_breakout"
    description = "唐奇安通道突破（趋势跟踪，通道上轨买、下轨卖）"
    param_schema = {
        "entry_period": {"default": 20, "desc": "入场通道周期"},
        "exit_period": {"default": 10, "desc": "出场通道周期"},
        "atr_multiplier": {"default": 2.0, "desc": "ATR止损倍数"},
    }

    def __init__(self, entry_period: int = 20, exit_period: int = 10, atr_multiplier: float = 2.0):
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.atr_multiplier = atr_multiplier

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.entry_period + 2:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]

            # 唐奇安通道上轨 = 最高价中的最高值
            upper = df["high"].iloc[-(self.entry_period + 1):-1].max()
            # 唐奇安通道下轨 = 最低价中的最低值
            lower = df["low"].iloc[-(self.exit_period + 1):-1].min()
            # 通道中轨
            mid = (upper + lower) / 2

            # ATR（如果没有atr列则简单用近期平均振幅，排除今日数据防未来函数）
            if "atr14" in df.columns:
                atr = df["atr14"].iloc[-1]
            else:
                recent_range = df["high"].iloc[-(20+1):-1] - df["low"].iloc[-(20+1):-1]
                atr = recent_range.mean()

            # 入场信号: 价格突破上轨，且从下方接近（确认突破有效）
            if prev_close <= upper and price > upper:
                # 突破强度：超出的比例 / ATR，越大越强
                strength = (price - upper) / max(atr, 0.01)
                score = round(min(strength * 0.5, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"突破唐奇安上轨{upper:.2f}（中轨{mid:.2f}，ATR={atr:.2f}）",
                    price_ref=price,
                ))

            # 出场信号: 价格跌破下轨
            if portfolio and portfolio.get_position(ts_code):
                if prev_close >= lower and price < lower:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.8,
                        reason=f"跌破唐奇安下轨{lower:.2f}（中轨{mid:.2f}）",
                        price_ref=price,
                    ))

        return signals
