"""布林带收口策略（震荡转趋势变盘）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class BollSqueezeStrategy(BaseStrategy):
    """布林带收口策略

    逻辑: 布林带宽（上轨-下轨）/中轨 压缩至历史低位 → 变盘前兆
    买入: 收口后价格放量突破上轨（向上变盘）
    卖出: 收口后价格跌破下轨（向下变盘）

    适合震荡末期捕捉方向突破，属震荡转趋势的综合型策略。
    """
    name = "boll_squeeze"
    description = "布林带收口策略（带宽压缩后突破方向跟随）"
    style = "震荡"
    param_schema = {
        "lookback": {"default": 50, "desc": "带宽分位回看周期"},
        "squeeze_quantile": {"default": 0.3, "desc": "收口阈值（带宽分位）"},
        "vol_ratio": {"default": 1.3, "desc": "突破放量倍数"},
    }

    def __init__(self, lookback: int = 50, squeeze_quantile: float = 0.3,
                 vol_ratio: float = 1.3):
        self.lookback = lookback
        self.squeeze_quantile = squeeze_quantile
        self.vol_ratio = vol_ratio

    def _is_squeezed(self, df) -> bool:
        """判断布林带宽是否处于历史低位（收口状态）"""
        if len(df) < self.lookback + 2:
            return False
        if "boll_upper" not in df.columns or "boll_lower" not in df.columns:
            return False

        mid = df["boll_mid"].iloc[-1] if "boll_mid" in df.columns else df["close"].iloc[-1]
        if mid <= 0:
            return False

        # 当前带宽
        curr_bw = (df["boll_upper"].iloc[-1] - df["boll_lower"].iloc[-1]) / mid
        # 昨日带宽（变盘前夕的蓄势收口状态，防止今日突破导致带宽瞬间扩张误杀）
        prev_mid = df["boll_mid"].iloc[-2] if "boll_mid" in df.columns else df["close"].iloc[-2]
        prev_bw = (df["boll_upper"].iloc[-2] - df["boll_lower"].iloc[-2]) / prev_mid if prev_mid > 0 else curr_bw

        # 历史带宽序列（不含今日与昨日，防未来函数与自相关）
        hist_mid = df["boll_mid"].iloc[-self.lookback - 1:-2] if "boll_mid" in df.columns else df["close"].iloc[-self.lookback - 1:-2]
        hist_mid = hist_mid.replace(0, float("nan"))
        hist_bw = (df["boll_upper"].iloc[-self.lookback - 1:-2]
                   - df["boll_lower"].iloc[-self.lookback - 1:-2]) / hist_mid
        hist_bw = hist_bw.dropna()
        if len(hist_bw) < 20:
            return False

        # 当前带宽或昨日变盘前带宽低于阈值分位 → 均判定有效收口
        quantile_val = hist_bw.quantile(self.squeeze_quantile)
        return (curr_bw <= quantile_val) or (prev_bw <= quantile_val)

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.lookback + 2:
                continue
            if "boll_upper" not in df.columns or "boll_lower" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]
            boll_upper = df["boll_upper"].iloc[-1]
            boll_lower = df["boll_lower"].iloc[-1]

            # 量比（放量确认）
            vol_ma5 = df["vol_ma5"].iloc[-1] if "vol_ma5" in df.columns else df["volume"].iloc[-5:].mean()
            vol_ratio = df["volume"].iloc[-1] / max(vol_ma5, 1)

            if not self._is_squeezed(df):
                continue

            # 买入: 收口后放量突破上轨
            if prev_close <= boll_upper and price > boll_upper and vol_ratio >= self.vol_ratio:
                # 收口越久、放量越大分越高
                score = round(min(0.5 + (vol_ratio - 1) / 2, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"布林带收口后放量{vol_ratio:.1f}倍突破上轨{boll_upper:.2f}",
                    price_ref=price,
                ))

            # 卖出: 收口后放量跌破下轨
            if portfolio is None or portfolio.get_position(ts_code):
                if prev_close >= boll_lower and price < boll_lower and vol_ratio >= self.vol_ratio:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.8,
                        reason=f"布林带收口后放量跌破下轨{boll_lower:.2f}",
                        price_ref=price,
                    ))

        return signals
