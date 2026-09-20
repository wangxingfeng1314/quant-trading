"""500亿+大市值高股息价值趋势复合策略 (含分红复利增强)

策略适用标的:
  - A股总市值大于 500 亿的大盘蓝筹、央企国企、高分红核心资产（如中国神华、长江电力、建设银行、招商银行、中国移动、红利ETF等）
  - 长期股息率稳定（通常在 3.5%~8% 之间）

核心哲学:
  1. 股息打底 (Dividend Floor):
     依托前复权（QFQ）历史数据内含的稳定高额现金派息与分红再投资机制，建立年均 4%~6% 的稳健基础收益底仓。
  2. 均线滤波与顺势加仓 (Trend Enhancement):
     大市值高股息股通常呈现多月甚至跨年的慢牛上升通道。
     当基准均线 (MA60) 走平或向上、价格在 MA60 上方稳步站上或回踩短期趋势均线 (MA10/MA20) 企稳时，触发顺势加仓买入。
  3. 估值透支乖离止盈 (Overheat Valuation Take-Profit):
     当股价在短期内非理性加速暴涨、远离 60 日均线超过设定阈值（如乖离率 >= 22%）时，
     意味着短期静态股息率被严重稀释透支，性价比较低，策略主动分批触发止盈离场，锁定资本利得。
  4. 趋势破位防守 (Trend Breakdown Stop-Loss):
     当快线死叉慢线且价格跌破 60 日基准线时，说明中长期抱团筹码松动，及时止损/减仓离场，规避深幅回撤。
"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class HighDividendValueStrategy(BaseStrategy):
    """500亿+大市值高股息价值趋势策略"""

    name = "high_dividend_value"
    description = "500亿+大市值高股息价值趋势策略（含分红复利+过热止盈）"
    style = "中长线"
    param_schema = {
        "fast_ma": {"default": 10, "desc": "短期趋势跟踪周期 (日)"},
        "slow_ma": {"default": 60, "desc": "长期价值基准周期 (日)"},
        "bias_entry_max": {"default": 15.0, "desc": "最大入场偏离度 (%，防追高)"},
        "bias_exit_pct": {"default": 22.0, "desc": "估值过热止盈偏离度 (%)"},
        "rsi_filter": {"default": 42, "desc": "RSI 动量过滤下限"},
    }

    def __init__(self,
                 fast_ma: int = 10,
                 slow_ma: int = 60,
                 bias_entry_max: float = 15.0,
                 bias_exit_pct: float = 22.0,
                 rsi_filter: float = 42):
        self.fast_ma = fast_ma
        self.slow_ma = slow_ma
        self.bias_entry_max = bias_entry_max
        self.bias_exit_pct = bias_exit_pct
        self.rsi_filter = rsi_filter

        self.fast_col = f"ma{fast_ma}"
        self.slow_col = f"ma{slow_ma}"

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []

        for ts_code, df in data.items():
            # 最小数据长度保护（确保慢均线及前序周期已计算完整）
            if len(df) < self.slow_ma + 5:
                continue

            if self.fast_col not in df.columns or self.slow_col not in df.columns:
                continue

            if df["volume"].iloc[-1] == 0:
                continue

            curr = df.iloc[-1]
            prev = df.iloc[-2]

            price = curr["close"]
            prev_price = prev["close"]
            ma_fast = curr[self.fast_col]
            prev_fast = prev[self.fast_col]
            ma_slow = curr[self.slow_col]
            prev_slow = prev[self.slow_col]

            # 过滤 NaN 无效指标值
            if any(v != v for v in [price, prev_price, ma_fast, prev_fast, ma_slow, prev_slow]):
                continue

            rsi = curr.get("rsi14", 50)
            if rsi != rsi:
                rsi = 50.0

            # 偏离度 (Bias) = (现价 / 60日基准线 - 1) * 100%
            bias = (price / ma_slow - 1) * 100 if ma_slow > 0 else 0.0

            has_position = (portfolio is not None
                            and portfolio.get_position(ts_code) is not None
                            and not portfolio.get_position(ts_code).is_empty)

            # ----------------------------------------------------
            # 1. 买入判定：中长期基准健康 + 站稳趋势均线 + 估值未透支
            # ----------------------------------------------------
            # 基准均线走平或上扬（允许微小日度波动，斜率 >= -0.1%）
            is_slow_healthy = ma_slow >= prev_slow * 0.999

            # 模式 A: 顺势突破或回踩企稳 (均线多头金叉 或 站稳趋势均线)
            golden_cross = (prev_fast <= prev_slow and ma_fast > ma_slow)
            trend_rebound = (prev_price <= prev_fast * 1.008 and price > ma_fast and price > ma_slow)

            # 模式 B: 深度超跌左侧/右侧拐点 (当优质高股息蓝筹被错杀导致股息率处于历史极值时)
            kdj_k = curr.get("kdj_k", 50)
            kdj_d = curr.get("kdj_d", 50)
            prev_k = prev.get("kdj_k", 50)
            prev_d = prev.get("kdj_d", 50)
            kdj_gold = (prev_k <= prev_d and kdj_k > kdj_d and kdj_k < 35)
            oversold_value = (rsi < 36 or kdj_gold) and price > curr["open"] and bias < -5.0

            if not has_position and bias <= self.bias_entry_max:
                if is_slow_healthy and (golden_cross or trend_rebound) and rsi >= self.rsi_filter:
                    score = 0.9 if golden_cross else 0.8
                    entry_type = "金叉突破启动" if golden_cross else "回踩企稳再起"
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="BUY",
                        score=score,
                        reason=(f"大市值高股息{entry_type}: 站上MA{self.fast_ma}({price:.2f}>{ma_fast:.2f}), "
                                f"偏离MA{self.slow_ma}={bias:.1f}%, RSI={rsi:.0f}"),
                        price_ref=price,
                    ))
                elif oversold_value:
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="BUY",
                        score=0.82,
                        reason=(f"大市值高股息超跌价值底: 偏离MA{self.slow_ma}={bias:.1f}%, "
                                f"RSI={rsi:.0f}, KDJ={kdj_k:.0f}, 股息率凸显极高配置价值"),
                        price_ref=price,
                    ))

            # ----------------------------------------------------
            # 2. 卖出/减仓判定：高位过热止盈 或 趋势破位防守
            # ----------------------------------------------------
            if portfolio is None or has_position:
                # 卖出条件 1: 股价短期加速远离60日线（严重偏离），股息率被严重稀释，获利了结
                if bias >= self.bias_exit_pct:
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="SELL",
                        score=0.9,
                        reason=(f"高股息估值过热止盈: 现价偏离MA{self.slow_ma}达 {bias:.1f}% "
                                f"(>= {self.bias_exit_pct}%), 静态股息率被稀释，锁定资本利得"),
                        price_ref=price,
                    ))
                # 卖出条件 2: 快慢线死叉 且 跌破长期均线下方（趋势反转防守）
                elif (prev_fast >= prev_slow and ma_fast < ma_slow) or (price < ma_slow * 0.98):
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="SELL",
                        score=0.8,
                        reason=(f"高股息中长趋势破位: 跌破MA{self.slow_ma}基准线"
                                f"({price:.2f} < {ma_slow:.2f})，防守避险"),
                        price_ref=price,
                    ))

        return signals
