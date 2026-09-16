"""信号共振策略（综合多指标确认）"""
from typing import List
import pandas as pd
from strategies.base import BaseStrategy
from core.models import Signal


class SignalComboStrategy(BaseStrategy):
    """信号共振策略

    综合 4 个维度的多空判断，只有当多个维度同时确认时才产生信号：
      - 趋势: 收盘价 vs MA20（站上/跌破）
      - 动量: MACD柱（放大/缩小）与DIF方向
      - 量能: 放量（>均量1.3倍）/ 缩量
      - 强度: RSI 强弱（>50 / <50）

    评分: 每个维度 ±1 分，总分 +4~-4
    买入: 总分 ≥ +2（至少3个维度看多）
    卖出: 总分 ≤ -2（至少3个维度看空）

    适合作为过滤信号，提高信号质量的综合参考策略。
    """
    name = "signal_combo"
    description = "信号共振策略（趋势+动量+量能+强度多维度确认）"
    style = "综合"
    param_schema = {
        "ma_period": {"default": 20, "desc": "趋势参考均线"},
        "buy_threshold": {"default": 2, "desc": "买入阈值（总分4分）"},
        "sell_threshold": {"default": -2, "desc": "卖出阈值（总分-4分）"},
        "vol_ratio": {"default": 1.3, "desc": "放量判定倍数"},
    }

    def __init__(self, ma_period: int = 20, buy_threshold: float = 2,
                 sell_threshold: float = -2, vol_ratio: float = 1.3):
        self.ma_period = ma_period
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.vol_ratio = vol_ratio
        self.ma_col = f"ma{ma_period}"

    def _score(self, df) -> float:
        """返回 -4 ~ +4 的综合评分"""
        score = 0.0
        details = []

        # 1. 趋势: 价格 vs MA
        if self.ma_col in df.columns:
            ma = df[self.ma_col].iloc[-1]
            price = df["close"].iloc[-1]
            if price > ma:
                score += 1
                details.append("站上均线+1")
            elif price < ma:
                score -= 1
                details.append("跌破均线-1")

        # 2. 动量: MACD柱放大 + DIF>DEA
        if "dif" in df.columns and "dea" in df.columns and "macd_hist" in df.columns:
            dif = df["dif"].iloc[-1]
            dea = df["dea"].iloc[-1]
            hist = df["macd_hist"].iloc[-1]
            prev_hist = df["macd_hist"].iloc[-2] if len(df) >= 2 else 0
            if dif > dea and hist > prev_hist:
                score += 1
                details.append("MACD多+1")
            elif dif < dea and hist < prev_hist:
                score -= 1
                details.append("MACD空-1")

        # 3. 量能: 放量配合方向
        if "vol_ma5" in df.columns:
            vol = df["volume"].iloc[-1]
            vol_ma = df["vol_ma5"].iloc[-1]
            pct_chg = df["pct_chg"].iloc[-1] if ("pct_chg" in df.columns and pd.notna(df["pct_chg"].iloc[-1])) else (
                (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100 if len(df) >= 2 else 0
            )
            if vol_ma > 0 and vol > vol_ma * self.vol_ratio:
                if pct_chg > 0:
                    score += 1
                    details.append("放量上涨+1")
                elif pct_chg < 0:
                    score -= 1
                    details.append("放量下跌-1")

        # 4. 强度: RSI 强弱
        if "rsi14" in df.columns:
            rsi = df["rsi14"].iloc[-1]
            if rsi >= 50:
                score += 1
                details.append(f"RSI偏多+1({rsi:.0f})")
            else:
                score -= 1
                details.append(f"RSI偏空-1({rsi:.0f})")

        return score, "; ".join(details) if details else ""

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < max(self.ma_period, 30):
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            total, detail = self._score(df)

            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 买入: 总分达到买入阈值（多维度共振看多）
            if not has_position and total >= self.buy_threshold:
                score = round(min(total / 4.0, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"信号共振看多({total:+.0f}分): {detail}",
                    price_ref=price,
                ))

            # 卖出: 总分达到卖出阈值（多维度共振看空）
            elif (portfolio is None or has_position) and total <= self.sell_threshold:
                score = round(min(abs(total) / 4.0, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="SELL",
                    score=score,
                    reason=f"信号共振看空({total:+.0f}分): {detail}",
                    price_ref=price,
                ))

        return signals
