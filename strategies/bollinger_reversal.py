"""布林带反转策略"""
from typing import List
import pandas as pd
from strategies.base import BaseStrategy
from core.models import Signal


class BollingerReversalStrategy(BaseStrategy):
    """布林带反转策略

    买入: 价格触及布林带下轨后反弹
    卖出: 价格触及布林带上轨后回落

    适合震荡行情中的波段操作。
    """
    name = "bollinger_reversal"
    description = "布林带反转策略（触下轨阳线企稳买、触上轨遇阻回落卖）"
    style = "震荡"
    allow_bear_buy = True
    param_schema = {
        "boll_period": {"default": 20, "desc": "布林带周期"},
        "boll_std": {"default": 2.0, "desc": "标准差倍数"},
    }

    def __init__(self, boll_period: int = 20, boll_std: float = 2.0):
        self.boll_period = boll_period
        self.boll_std = boll_std

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        is_default_boll = (self.boll_period == 20 and abs(float(self.boll_std) - 2.0) < 1e-6)
        for ts_code, df in data.items():
            if len(df) < self.boll_period + 2:
                continue
            if any(c not in df.columns for c in ["close", "low", "high", "volume"]):
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            if is_default_boll and "boll_upper" in df.columns and "boll_lower" in df.columns and "boll_mid" in df.columns:
                boll_lower = df["boll_lower"].iloc[-1]
                boll_upper = df["boll_upper"].iloc[-1]
                boll_mid = df["boll_mid"].iloc[-1]
                prev_boll_lower = df["boll_lower"].iloc[-2]
                prev_boll_upper = df["boll_upper"].iloc[-2]
            else:
                # 动态按自定义 boll_period 与 boll_std 计算布林带，确保网格搜索调参真实生效
                tail_s = df["close"].iloc[-(self.boll_period + 2):]
                mid_s = tail_s.rolling(window=self.boll_period, min_periods=self.boll_period).mean()
                std_s = tail_s.rolling(window=self.boll_period, min_periods=self.boll_period).std()
                upper_s = mid_s + float(self.boll_std) * std_s
                lower_s = mid_s - float(self.boll_std) * std_s
                boll_mid = mid_s.iloc[-1]
                boll_upper = upper_s.iloc[-1]
                boll_lower = lower_s.iloc[-1]
                prev_boll_upper = upper_s.iloc[-2]
                prev_boll_lower = lower_s.iloc[-2]

            if any(v != v for v in [boll_lower, boll_upper, boll_mid, prev_boll_lower, prev_boll_upper]):
                continue

            curr_low = df["low"].iloc[-1]
            curr_high = df["high"].iloc[-1]
            curr_close = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            curr_open = df["open"].iloc[-1] if "open" in df.columns else curr_close

            pos = portfolio.get_position(ts_code) if portfolio is not None else None
            has_position = pos is not None and not pos.is_empty

            # 胜率增强过滤：RSI<42超卖探底 + 收盘收阳/平盘企稳；中期空头下行(close<ma20<ma60)收紧至RSI<30防接飞刀
            rsi = df["rsi14"].iloc[-1] if "rsi14" in df.columns else 50.0
            rsi_limit = 42.0
            if "ma20" in df.columns and "ma60" in df.columns:
                ma20_val = df["ma20"].iloc[-1]
                ma60_val = df["ma60"].iloc[-1]
                if pd.notna(ma20_val) and pd.notna(ma60_val) and curr_close < float(ma20_val) < float(ma60_val):
                    rsi_limit = 30.0
            rsi_oversold = pd.isna(rsi) or float(rsi) < rsi_limit
            candle_ok = curr_close >= curr_open

            # 买入: 前一日在轨内或触轨，今日低点触及下轨后收盘阳线企稳回升 + RSI超卖支撑
            if not has_position and rsi_oversold and candle_ok and prev_close >= prev_boll_lower and curr_low <= boll_lower and curr_close > boll_lower:
                score = round(min(0.55 + (curr_close - boll_lower) / (boll_mid - boll_lower + 0.01) * 0.45, 0.95), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"触及布林下轨{boll_lower:.2f}阳线企稳反弹至{curr_close:.2f}",
                    price_ref=curr_close,
                ))

            # 卖出: 触及布林上轨后遇阻回落（收在上轨下方或收阴）才止盈；强势实体阳线突破上轨允许继续持有
            elif (portfolio is None or has_position) and curr_high >= boll_upper and (curr_close < boll_upper or curr_close < curr_open):
                score = round(min(0.6 + max(0.0, (curr_high - boll_upper) / (boll_upper - boll_mid + 0.01)) * 0.35, 0.95), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="SELL",
                    score=score,
                    reason=f"触及布林上轨{boll_upper:.2f}遇阻回落止盈",
                    price_ref=curr_close,
                ))

        return signals
