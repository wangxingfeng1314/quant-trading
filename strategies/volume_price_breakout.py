"""量价突破策略（动量）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class VolumePriceBreakoutStrategy(BaseStrategy):
    """量价突破策略

    买入: 放量突破关键阻力（均线或前高），量能配合确认趋势
    卖出: 缩量反弹至压力位，或放量跌破支撑

    核心逻辑：
      - 上涨需要放量确认（量价配合理想）
      - 下跌无需放量即可确认（缩量阴跌也是风险）
    """
    name = "volume_price_breakout"
    description = "量价突破策略（放量突破买、缩量反弹卖）"
    param_schema = {
        "ma_period": {"default": 20, "desc": "参考均线周期"},
        "vol_ratio": {"default": 1.5, "desc": "放量倍数（成交额/均量）"},
        "lookback": {"default": 10, "desc": "前高回溯周期"},
    }

    def __init__(self, ma_period: int = 20, vol_ratio: float = 1.5, lookback: int = 10):
        self.ma_period = ma_period
        self.vol_ratio = vol_ratio
        self.lookback = lookback
        self.ma_col = f"ma{ma_period}"

    def _is_volume_surge(self, df) -> bool:
        """判断当日是否放量"""
        vol_ma5 = df.get("vol_ma5")
        if vol_ma5 is None or df["volume"].iloc[-1] == 0:
            return False
        return df["volume"].iloc[-1] > vol_ma5.iloc[-1] * self.vol_ratio

    def _recent_high(self, df) -> float:
        """最近N日最高价"""
        return df["high"].iloc[-(self.lookback + 1):-1].max()

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < max(self.ma_period, self.lookback) + 2:
                continue
            if self.ma_col not in df.columns or "vol_ma5" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            ma_val = df[self.ma_col].iloc[-1]
            prev_ma = df[self.ma_col].iloc[-2]
            vol_surge = self._is_volume_surge(df)
            recent_high = self._recent_high(df)

            # 买入信号:
            # 条件1: 收盘站上MA20
            # 条件2: 放量确认（量能 > MA5量均线的1.5倍）
            # 条件3: 突破前N日高点更佳（加分）
            if prev_close <= ma_val and price > ma_val and vol_surge:
                # 基础分0.6，突破前高再加分
                base_score = 0.6
                bonus = 0.2 if price > recent_high else 0
                score = round(min(base_score + bonus, 1.0), 2)

                reason_parts = [f"放量{self.vol_ratio:.1f}倍突破MA{self.ma_period}"]
                if price > recent_high:
                    reason_parts.append(f"突破{self.lookback}日高点{recent_high:.2f}")
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score, reason="，".join(reason_parts),
                    price_ref=price,
                ))

            # 卖出信号:
            # 持有中且缩量反弹至均线附近受压，或放量跌破均线
            if portfolio and portfolio.get_position(ts_code):
                # 场景1: 放量跌破MA20
                if prev_close >= ma_val and price < ma_val and vol_surge:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.8,
                        reason=f"放量跌破MA{self.ma_period}（{ma_val:.2f}）",
                        price_ref=price,
                    ))
                # 场景2: 缩量反弹至前高附近受压（量价背离）
                elif not vol_surge and price >= recent_high * 0.98 and price <= recent_high * 1.02:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.6,
                        reason=f"缩量反弹至前高{recent_high:.2f}附近受压",
                        price_ref=price,
                    ))

        return signals
