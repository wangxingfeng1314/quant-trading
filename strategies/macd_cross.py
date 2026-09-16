"""MACD金叉死叉策略（短线）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class MACDCrossStrategy(BaseStrategy):
    """MACD金叉死叉策略

    买入: DIF上穿DEA（金叉），低位金叉（DIF<0）更强
    卖出: DIF下穿DEA（死叉），高位死叉（DIF>0）更强

    适合短线交易，灵敏度高。
    """
    name = "macd_cross"
    description = "MACD金叉死叉策略（DIF上穿DEA买、下穿卖）"
    style = "短线"
    param_schema = {
        "lookback": {"default": 30, "desc": "回看周期（判断低位/高位）"},
    }

    def __init__(self, lookback: int = 30):
        self.lookback = lookback

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < 3:
                continue
            if "dif" not in df.columns or "dea" not in df.columns:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            curr_dif = df["dif"].iloc[-1]
            curr_dea = df["dea"].iloc[-1]
            prev_dif = df["dif"].iloc[-2]
            prev_dea = df["dea"].iloc[-2]
            price = df["close"].iloc[-1]

            if any(v != v for v in [curr_dif, curr_dea, prev_dif, prev_dea]):
                continue

            has_position = portfolio is not None and portfolio.get_position(ts_code) is not None and not portfolio.get_position(ts_code).is_empty

            # 金叉: DIF从下方上穿DEA（未持仓）
            if not has_position and prev_dif <= prev_dea and curr_dif > curr_dea:
                # 低位金叉（DIF<0）更可靠，加分
                score = 0.5 + (0.4 if curr_dif < 0 else 0.0)
                score += min(abs(curr_dif - curr_dea) / 0.2, 0.1)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=round(min(score, 1.0), 2),
                    reason=f"MACD金叉: DIF={curr_dif:.3f}上穿DEA={curr_dea:.3f}"
                           f"{'（低位金叉）' if curr_dif < 0 else ''}",
                    price_ref=price,
                ))

            # 死叉: DIF从上方下穿DEA（持有中或无组合扫描模式）
            elif (portfolio is None or has_position) and prev_dif >= prev_dea and curr_dif < curr_dea:
                score = 0.5 + (0.4 if curr_dif > 0 else 0.0)
                score += min(abs(curr_dif - curr_dea) / 0.2, 0.1)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="SELL",
                    score=round(min(score, 1.0), 2),
                    reason=f"MACD死叉: DIF={curr_dif:.3f}下穿DEA={curr_dea:.3f}"
                           f"{'（高位死叉）' if curr_dif > 0 else ''}",
                    price_ref=price,
                ))

        return signals
