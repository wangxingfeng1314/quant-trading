"""多资产动量轮动策略（横截面强弱轮动 + 绝对动量空仓防御）"""
from typing import List
import numpy as np
import pandas as pd

from strategies.base import BaseStrategy
from core.models import Signal
from engine.commission import format_price


# 低相关多资产 ETF 推荐轮动池（宽基 + 红利防御 + 黄金避险 + 境外科技 + 国债防御）
ROTATION_ETF_PRESETS = [
    ("510310.SH", "沪深300ETF"),
    ("510500.SH", "中证500ETF"),
    ("159915.SZ", "创业板ETF"),
    ("512890.SH", "红利低波ETF"),
    ("518880.SH", "黄金ETF"),
    ("513100.SH", "纳指ETF"),
    ("511010.SH", "国债ETF"),
]


class MomentumRotationStrategy(BaseStrategy):
    """多资产风险调整动量轮动策略

    机制:
      1. 对标的池内所有资产计算 N 日动量与 20 日实现波动率，得到夏普式动量得分 ``mom / max(vol, 0.005)``；
      2. 叠加绝对动量与趋势双过滤：仅当资产 N 日动量 > 0 且收盘价站在均线上方时具备入选资格；
      3. 定期调仓（默认每 5 个交易日）只持有得分最高的 Top-K 资产，其余清仓腾出资金；
         若全体资产均走弱（如单边股债双杀或 A 股普跌时切换现金），自动空仓防御。
    """
    name = "momentum_rotation"
    description = "多资产动量轮动策略（风险调整动量排名，持强弃弱，熊市自动防御/空仓）"
    style = "综合"
    allow_bear_buy = True
    is_cross_sectional = True
    param_schema = {
        "lookback": {"default": 20, "min_value": 5, "max_value": 120, "desc": "动量回看周期（交易日）"},
        "rebalance_days": {"default": 5, "min_value": 1, "max_value": 60, "desc": "调仓间隔（交易日）"},
        "top_k": {"default": 2, "min_value": 1, "max_value": 10, "desc": "最多轮动持仓只数"},
        "ma_filter_period": {"default": 20, "min_value": 5, "max_value": 120, "desc": "趋势过滤均线周期"},
    }

    def __init__(self, lookback: int = 20, rebalance_days: int = 5,
                 top_k: int = 2, ma_filter_period: int = 20):
        self.lookback = max(2, int(lookback))
        self.rebalance_days = max(1, int(rebalance_days))
        self.top_k = max(1, int(top_k))
        self.ma_filter_period = max(2, int(ma_filter_period))
        self._bar_count = 0

    def _calc_momentum_snapshot(self, df) -> dict | None:
        """提取单只标的最新的收盘价、动量、均线与风险调整动量得分（兼容 BarFrame 与 DataFrame）。"""
        min_len = max(self.lookback + 1, self.ma_filter_period, 3)
        if df is None or len(df) < min_len:
            return None
        if "close" not in df.columns or "volume" not in df.columns:
            return None

        last_vol = df["volume"].iloc[-1]
        if last_vol is None or pd.isna(last_vol) or float(last_vol) <= 0:
            return None

        closes = np.asarray(df["close"], dtype=float)
        if len(closes) < min_len:
            return None
        curr_close = float(closes[-1])
        base_close = float(closes[-1 - self.lookback])
        if not np.isfinite(curr_close) or not np.isfinite(base_close) or curr_close <= 0 or base_close <= 0:
            return None

        ma_slice = closes[-self.ma_filter_period:]
        if not np.all(np.isfinite(ma_slice)):
            return None
        ma_val = float(np.mean(ma_slice))
        if ma_val <= 0:
            return None

        mom = curr_close / base_close - 1.0
        vol_win = closes[-min(len(closes), 21):]
        if len(vol_win) >= 3 and np.all(vol_win[:-1] > 0) and np.all(np.isfinite(vol_win)):
            rets = np.diff(vol_win) / vol_win[:-1]
            vol20 = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.02
        else:
            vol20 = 0.02
        if not np.isfinite(vol20):
            vol20 = 0.02

        rs = mom / max(vol20, 0.005)
        return {
            "close": curr_close,
            "ma": ma_val,
            "mom": mom,
            "vol": vol20,
            "rs": rs,
        }

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        if not data:
            return []

        if portfolio is not None:
            bar_idx = self._bar_count
            self._bar_count += 1
            if bar_idx % self.rebalance_days != 0:
                return []

        snapshots = {}
        candidates = []
        for ts_code, df in data.items():
            snap = self._calc_momentum_snapshot(df)
            if snap is None:
                continue
            snapshots[ts_code] = snap
            # 绝对动量 > 0 且收盘站在均线之上，剔除空头下跌标的
            if snap["mom"] > 0 and snap["close"] >= snap["ma"]:
                candidates.append((snap["rs"], ts_code, snap))

        if not snapshots:
            return []

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = candidates[:self.top_k]
        target_codes = [c for _, c, _ in selected]
        target_set = set(target_codes)

        signals: List[Signal] = []

        if portfolio is not None:
            # 回测模式：对非入选已有持仓先平仓，再买入新入选强势标的
            for ts_code, pos in portfolio.positions.items():
                if pos.is_empty or ts_code in target_set:
                    continue
                snap = snapshots.get(ts_code)
                if snap is None:
                    continue
                mom_pct = snap["mom"] * 100
                sell_score = round(min(max(0.75 + max(0.0, -snap["mom"]) * 1.5, 0.72), 0.92), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="SELL",
                    score=sell_score,
                    reason=(
                        f"动量轮动调出: {self.lookback}日涨幅{mom_pct:+.1f}%，"
                        f"相对强度未进前{self.top_k}名或跌破MA{self.ma_filter_period}"
                    ),
                    price_ref=snap["close"],
                ))

            for rank, (_, ts_code, snap) in enumerate(selected, start=1):
                pos = portfolio.get_position(ts_code)
                if pos is not None and not pos.is_empty:
                    continue
                buy_score = round(max(0.68, 0.92 - (rank - 1) * 0.06), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=buy_score,
                    reason=(
                        f"动量轮动第{rank}名入选: {self.lookback}日涨{snap['mom'] * 100:+.1f}%，"
                        f"风险调整动量{snap['rs']:.2f}，站上MA{self.ma_filter_period}({format_price(snap['ma'], ts_code, prefix='')})"
                    ),
                    price_ref=snap["close"],
                ))
        else:
            # 实盘扫描模式（portfolio=None）：查询真实持仓防已持仓标的每日重复高分提醒买入
            held_set = set()
            try:
                from data.storage import get_positions
                for p in (get_positions() or []):
                    if p.get("ts_code") and int(p.get("shares", 0) or 0) > 0:
                        held_set.add(str(p["ts_code"]).strip())
            except Exception:
                held_set = set()

            for rank, (_, ts_code, snap) in enumerate(selected, start=1):
                buy_score = round(max(0.68, 0.92 - (rank - 1) * 0.06), 2)
                is_held = ts_code in held_set
                prefix = "[持仓续持·无需加仓] " if is_held else ""
                if is_held:
                    buy_score = round(max(0.45, buy_score - 0.15), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=buy_score,
                    reason=(
                        f"{prefix}动量轮动第{rank}名优选: {self.lookback}日涨{snap['mom'] * 100:+.1f}%，"
                        f"夏普动量{snap['rs']:.2f}，强于MA{self.ma_filter_period}"
                    ),
                    price_ref=snap["close"],
                ))

            for ts_code, snap in snapshots.items():
                if ts_code in target_set:
                    continue
                if snap["mom"] < -0.02 and snap["close"] < snap["ma"]:
                    sell_score = round(min(max(0.68 + abs(snap["mom"]) * 2.0, 0.68), 0.92), 2)
                    signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy=self.name,
                        direction="SELL",
                        score=sell_score,
                        reason=(
                            f"动量走弱调出: {self.lookback}日跌{snap['mom'] * 100:+.1f}%，"
                            f"位于MA{self.ma_filter_period}({format_price(snap['ma'], ts_code, prefix='')})下方"
                        ),
                        price_ref=snap["close"],
                    ))

        return signals
