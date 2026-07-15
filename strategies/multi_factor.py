"""多因子综合评分策略（组合策略）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MultiFactorStrategy(BaseStrategy):
    """多因子综合评分策略

    综合多个技术因子打分，当总分超过阈值时产生信号：
      - 均线趋势因子（MA排列）
      - MACD动量因子
      - RSI超买卖因子
      - 量能因子
      - 布林带位置因子

    适合作为参考策略，帮助用户快速了解多因子综合情况。
    """
    name = "multi_factor"
    description = "多因子综合评分（均线+MACD+RSI+量能+布林带）"
    param_schema = {
        "buy_threshold": {"default": 3.0, "desc": "买入阈值（总分5分）"},
        "sell_threshold": {"default": -3.0, "desc": "卖出阈值（总分-5分）"},
        "ma_period": {"default": 20, "desc": "参考均线周期"},
    }

    def __init__(self, buy_threshold: float = 3.0, sell_threshold: float = -3.0, ma_period: int = 20):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.ma_period = ma_period
        self.ma_col = f"ma{ma_period}"

    def _score_ma_trend(self, df) -> float:
        """均线趋势评分: -1 ~ +1"""
        if self.ma_col not in df.columns:
            return 0
        price = df["close"].iloc[-1]
        ma = df[self.ma_col].iloc[-1]
        ma_slope = (df[self.ma_col].iloc[-1] - df[self.ma_col].iloc[-5]) / max(ma, 0.01) * 100 \
            if len(df) >= 5 else 0

        score = 0
        # 价格在均线上方
        if price > ma:
            score += 0.5
        else:
            score -= 0.5
        # 均线斜率
        score += min(max(ma_slope * 5, -0.5), 0.5)
        return round(score, 2)

    def _score_macd(self, df) -> float:
        """MACD评分: -1 ~ +1"""
        if "dif" not in df.columns or "dea" not in df.columns or "macd_hist" not in df.columns:
            return 0
        dif = df["dif"].iloc[-1]
        dea = df["dea"].iloc[-1]
        hist = df["macd_hist"].iloc[-1]
        prev_hist = df["macd_hist"].iloc[-2] if len(df) >= 2 else 0

        score = 0
        # DIF > DEA（金叉状态）
        if dif > dea:
            score += 0.5
        else:
            score -= 0.5
        # MACD柱放大/缩小
        if hist > prev_hist:
            score += 0.5
        elif hist < prev_hist:
            score -= 0.3
        return round(score, 2)

    def _score_rsi(self, df) -> float:
        """RSI评分: -1 ~ +1"""
        rsi_col = "rsi14"
        if rsi_col not in df.columns:
            return 0
        rsi = df[rsi_col].iloc[-1]
        prev_rsi = df[rsi_col].iloc[-2] if len(df) >= 2 else 50

        score = 0
        if rsi < 30:
            # 超卖区间 → 看多
            score = 0.5 + (30 - rsi) / 30 * 0.5
        elif rsi > 70:
            # 超买区间 → 看空
            score = -0.5 - (rsi - 70) / 30 * 0.5
        else:
            # 中性区间，看方向
            score = (rsi - 50) / 20  # -1 ~ +1
        # RSI方向变化加分
        if rsi > prev_rsi and rsi < 70:
            score += 0.3
        elif rsi < prev_rsi and rsi > 30:
            score -= 0.3
        return round(max(min(score, 1.0), -1.0), 2)

    def _score_volume(self, df) -> float:
        """量能评分: -1 ~ +1"""
        if "vol_ma5" not in df.columns:
            return 0
        vol = df["volume"].iloc[-1]
        vol_ma = df["vol_ma5"].iloc[-1]
        if vol_ma <= 0:
            return 0

        vol_ratio = vol / vol_ma
        # 放量且上涨 → 看多
        pct_chg = df["pct_chg"].iloc[-1] if "pct_chg" in df.columns else 0
        if vol_ratio > 1.3 and pct_chg > 0:
            return round(min((vol_ratio - 1) * 0.8, 1.0), 2)
        # 放量且下跌 → 看空
        elif vol_ratio > 1.3 and pct_chg < 0:
            return round(max(-(vol_ratio - 1) * 0.8, -1.0), 2)
        # 缩量
        elif vol_ratio < 0.7:
            return -0.3
        return 0.1

    def _score_bollinger(self, df) -> float:
        """布林带评分: -1 ~ +1"""
        if "boll_upper" not in df.columns or "boll_lower" not in df.columns:
            return 0
        price = df["close"].iloc[-1]
        upper = df["boll_upper"].iloc[-1]
        lower = df["boll_lower"].iloc[-1]
        mid = df["boll_mid"].iloc[-1]

        if price <= lower:
            return 1.0  # 触及下轨，看反弹
        elif price >= upper:
            return -1.0  # 触及上轨，看回落
        elif price > mid:
            return round(-(price - mid) / (upper - mid + 0.01), 2)
        else:
            return round((mid - price) / (mid - lower + 0.01), 2)

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < 30:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]

            # 计算各因子评分
            s_ma = self._score_ma_trend(df)
            s_macd = self._score_macd(df)
            s_rsi = self._score_rsi(df)
            s_vol = self._score_volume(df)
            s_boll = self._score_bollinger(df)

            total = round(s_ma + s_macd + s_rsi + s_vol + s_boll, 2)

            # 生成评分明细
            detail = (f"均线={s_ma:+.1f} MACD={s_macd:+.1f} "
                      f"RSI={s_rsi:+.1f} 量能={s_vol:+.1f} 布林={s_boll:+.1f} "
                      f"总分={total:+.1f}")

            # 买入信号
            if total >= self.buy_threshold:
                score = round(min(total / 5.0, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score, reason=detail,
                    price_ref=price,
                ))

            # 卖出信号（持仓中或总分极低）
            elif total <= self.sell_threshold:
                if portfolio and portfolio.get_position(ts_code):
                    score = round(min(abs(total) / 5.0, 1.0), 2)
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=score, reason=detail,
                        price_ref=price,
                    ))

        return signals
