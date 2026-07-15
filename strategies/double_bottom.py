"""双底形态识别策略（反转交易）"""
from typing import List
from strategies.base import BaseStrategy
from core.models import Signal


class DoubleBottomStrategy(BaseStrategy):
    """双底形态识别策略

    买入: 识别W底形态：两次探底后放量突破颈线
    卖出: 跌破颈线或反弹至压力位回落

    形态特征：
      - 第一个底（左底）+ 反弹至颈线
      - 第二个底（右底）不低于左底 + 放量突破颈线
      - 颈线 = 两个底之间的反弹高点
    """
    name = "double_bottom"
    description = "双底形态识别（W底突破颈线买入）"
    param_schema = {
        "lookback": {"default": 30, "desc": "形态搜索周期"},
        "neck_break_vol_ratio": {"default": 1.3, "desc": "突破颈线放量倍数"},
        "min_distance": {"default": 3, "desc": "双底最小间距（交易日）"},
    }

    def __init__(self, lookback: int = 30, neck_break_vol_ratio: float = 1.3, min_distance: int = 3):
        self.lookback = lookback
        self.neck_break_vol_ratio = neck_break_vol_ratio
        self.min_distance = min_distance

    def _find_double_bottom(self, df) -> dict:
        """在最近 lookback 日内寻找 W 底形态

        Returns:
            {"found": bool, "neck_line": float, "left_bottom": float, "right_bottom": float}
        """
        if len(df) < self.lookback + 2:
            return {"found": False}

        # 取最近 lookback 日的数据
        segment = df.iloc[-(self.lookback + 1):-1].copy()
        if len(segment) < 10:
            return {"found": False}

        lows = segment["low"].values
        highs = segment["high"].values
        closes = segment["close"].values

        # 寻找最低点集合（局部极小值）
        bottoms = []
        for i in range(2, len(lows) - 2):
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] \
               and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                bottoms.append((i, lows[i]))

        if len(bottoms) < 2:
            return {"found": False}

        # 找两个最低的点作为双底候选
        bottoms.sort(key=lambda x: x[1])
        left = bottoms[0]
        # 找第二个底：在第一个底之后 min_distance 天以外
        right_candidates = [b for b in bottoms if abs(b[0] - left[0]) >= self.min_distance]
        if not right_candidates:
            return {"found": False}
        right_candidates.sort(key=lambda x: x[1])
        right = right_candidates[0]

        # 确定左右（按时间先后）
        if left[0] > right[0]:
            left, right = right, left

        # 双底之间的最高点为颈线
        between = segment.iloc[left[0]:right[0]+1]
        neck_line = between["high"].max()

        # 两个底不能相差太大（右底不低于左底的5%范围）
        bottom_diff = abs(right[1] - left[1]) / max(left[1], 0.01)
        if bottom_diff > 0.05:
            return {"found": False}

        return {
            "found": True,
            "neck_line": neck_line,
            "left_bottom": left[1],
            "right_bottom": right[1],
            "right_idx": right[0],
        }

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []
        for ts_code, df in data.items():
            if len(df) < self.lookback + 2:
                continue
            if df["volume"].iloc[-1] == 0:
                continue

            price = df["close"].iloc[-1]
            prev_close = df["close"].iloc[-2]

            # 用前 lookback+1 天数据检测形态（不含今天）
            seg_df = df.iloc[-(self.lookback + 2):]
            pattern = self._find_double_bottom(seg_df)

            if not pattern["found"]:
                continue

            neck_line = pattern["neck_line"]

            # 计算量比
            vol_ma5 = df["vol_ma5"].iloc[-1] if "vol_ma5" in df.columns else df["volume"].iloc[-10:].mean()
            vol_ratio = df["volume"].iloc[-1] / max(vol_ma5, 1)

            # 买入: 价格突破颈线 + 放量确认
            if prev_close <= neck_line and price > neck_line and vol_ratio >= self.neck_break_vol_ratio:
                # 评分：放量越大分越高，突破越远分越高
                vol_score = min((vol_ratio - 1) / 2, 0.5)
                dist_score = min((price / neck_line - 1) * 10, 0.5)
                score = round(min(0.5 + vol_score + dist_score, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code, trade_date=trade_date,
                    strategy=self.name, direction="BUY",
                    score=score,
                    reason=f"W底突破颈线{neck_line:.2f}（左底{pattern['left_bottom']:.2f}，"
                           f"右底{pattern['right_bottom']:.2f}）",
                    price_ref=price,
                ))

            # 卖出: 持有中且跌破颈线
            if portfolio and portfolio.get_position(ts_code):
                if prev_close >= neck_line and price < neck_line:
                    signals.append(Signal(
                        ts_code=ts_code, trade_date=trade_date,
                        strategy=self.name, direction="SELL",
                        score=0.7,
                        reason=f"跌破双底颈线{neck_line:.2f}，形态失败",
                        price_ref=price,
                    ))

        return signals
