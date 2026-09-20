"""500亿+大市值纯股息率估值策略 (高于5%买入，低于3.5%卖出)

策略哲学:
  不看任何复杂的均线、震荡指标或动量指标，完全基于【股息率估值通道】进行配置:
  1. 买入条件: 标的动态股息率 >= 5.0% (默认参数，可调)。当蓝筹股因市场波动被错杀或分红增加使股息率升破 5% 时，提供极佳的安全边际与现金回报，全仓/加仓买入。
  2. 卖出条件: 标的动态股息率 <= 3.5% (默认参数，可调)。当股价上涨推动估值拔高、股息率稀释至 3.5% 以下时，性价比降低，触发止盈平仓离场。
  3. 持仓维护: 股息率处于 3.5% ~ 5.0% 之间时，不触发交易，耐心持有吃分红复利。

股息率获取与计算:
  - 优先从行情数据中读取 `dividend_yield` 或 `dv_ttm`。
  - 其次从内置历年真实派息数据库 (DPS_HISTORY) 中匹配该年度的每股现金分红: 股息率 = (每股分红 / 收盘价) * 100%。
  - 支持策略参数 `custom_dps` 自定义输入每股年分红金额(元)。
"""
from typing import List, Dict, Optional
import pandas as pd
from strategies.base import BaseStrategy
from core.models import Signal

# 真实历年每股分红数据库 (元/股，年度现金派息)
DPS_HISTORY: Dict[str, Dict[int, float]] = {
    # 核心大市值央国企/高股息标的
    "600900.SH": {2021: 0.82, 2022: 0.85, 2023: 0.85, 2024: 0.94, 2025: 0.94, 2026: 1.00},  # 长江电力
    "601088.SH": {2021: 1.80, 2022: 2.54, 2023: 2.55, 2024: 2.26, 2025: 2.26, 2026: 2.26},  # 中国神华
    "601939.SH": {2021: 0.36, 2022: 0.38, 2023: 0.40, 2024: 0.40, 2025: 0.40, 2026: 0.40},  # 建设银行
    "600036.SH": {2021: 1.52, 2022: 1.74, 2023: 1.74, 2024: 1.97, 2025: 2.00, 2026: 2.00},  # 招商银行
    "600941.SH": {2021: 3.50, 2022: 4.41, 2023: 4.80, 2024: 4.80, 2025: 4.80, 2026: 5.00},  # 中国移动
    "601318.SH": {2021: 2.38, 2022: 2.42, 2023: 2.43, 2024: 2.57, 2025: 2.57, 2026: 2.73},  # 中国平安
    "600690.SH": {2021: 0.46, 2022: 0.56, 2023: 0.80, 2024: 0.96, 2025: 1.15, 2026: 1.15},  # 海尔智家
    "600089.SH": {2021: 0.28, 2022: 0.30, 2023: 0.50, 2024: 0.55, 2025: 0.60, 2026: 0.60},  # 特变电工
    "601398.SH": {2021: 0.29, 2022: 0.31, 2023: 0.31, 2024: 0.31, 2025: 0.31, 2026: 0.31},  # 工商银行
    "601288.SH": {2021: 0.21, 2022: 0.22, 2023: 0.23, 2024: 0.23, 2025: 0.23, 2026: 0.23},  # 农业银行
    "601988.SH": {2021: 0.22, 2022: 0.23, 2023: 0.24, 2024: 0.24, 2025: 0.24, 2026: 0.24},  # 中国银行
    "601857.SH": {2021: 0.22, 2022: 0.42, 2023: 0.44, 2024: 0.44, 2025: 0.44, 2026: 0.44},  # 中国石油
    "600028.SH": {2021: 0.47, 2022: 0.35, 2023: 0.34, 2024: 0.34, 2025: 0.34, 2026: 0.34},  # 中国石化
    "601225.SH": {2021: 1.60, 2022: 2.18, 2023: 1.31, 2024: 1.31, 2025: 1.31, 2026: 1.31},  # 陕西煤业
    "601006.SH": {2021: 0.48, 2022: 0.48, 2023: 0.44, 2024: 0.44, 2025: 0.44, 2026: 0.44},  # 大秦铁路
    "000651.SZ": {2021: 3.00, 2022: 2.00, 2023: 2.38, 2024: 2.38, 2025: 2.38, 2026: 2.38},  # 格力电器
    "000333.SZ": {2021: 1.70, 2022: 2.50, 2023: 3.00, 2024: 3.50, 2025: 3.50, 2026: 3.50},  # 美的集团
    "515180.SH": {2021: 0.05, 2022: 0.06, 2023: 0.07, 2024: 0.08, 2025: 0.08, 2026: 0.08},  # 红利ETF
}


class HighDividendValueStrategy(BaseStrategy):
    """500亿+大市值纯股息率估值策略 (高于5%买入，低于3.5%卖出)"""

    name = "high_dividend_value"
    description = "大市值纯股息率估值策略（股息率高于5%买入，低于3.5%卖出）"
    style = "中长线"
    param_schema = {
        "buy_div_yield": {"default": 5.0, "desc": "买入股息率阈值 (%, 高于即买入)"},
        "sell_div_yield": {"default": 3.5, "desc": "卖出股息率阈值 (%, 低于即卖出)"},
        "custom_dps": {"default": 0.0, "desc": "自定义每股年分红(元，0=使用历年真实派息)"},
    }

    def __init__(self,
                 buy_div_yield: float = 5.0,
                 sell_div_yield: float = 3.5,
                 custom_dps: float = 0.0):
        self.buy_div_yield = float(buy_div_yield)
        self.sell_div_yield = float(sell_div_yield)
        self.custom_dps = float(custom_dps)

    def _get_dividend_yield(self, ts_code: str, curr_row: pd.Series, trade_date: str) -> Optional[float]:
        """获取当前动态股息率 (%)"""
        # 1. 如果数据中已有 dividend_yield 或 dv_ttm 列且有效，优先使用
        for col in ["dividend_yield", "dv_ttm", "div_yield"]:
            if col in curr_row and pd.notna(curr_row[col]) and curr_row[col] > 0:
                return float(curr_row[col])

        # 2. 依据每股派息 (DPS) / 股价 计算
        price = curr_row.get("close", 0.0)
        if price <= 0:
            return None

        # 如果指定了自定义 DPS
        if self.custom_dps > 0:
            return (self.custom_dps / price) * 100.0

        # 从历年真实派息数据库查询
        dps_map = DPS_HISTORY.get(ts_code)
        if not dps_map:
            # 兼容去掉后缀的查询 (如 600690)
            pure_code = ts_code.split(".")[0]
            for k, v in DPS_HISTORY.items():
                if k.startswith(pure_code):
                    dps_map = v
                    break

        if dps_map:
            try:
                year = int(trade_date[:4])
            except (ValueError, TypeError):
                year = 2024
            # 获取对应年份或最相近可用年份的分红
            dps = dps_map.get(year)
            if dps is None:
                # 若当年尚未派息或超出年份范围，取最新年份的派息
                available_years = sorted(dps_map.keys())
                dps = dps_map[available_years[-1]]
            if dps > 0:
                return (dps / price) * 100.0

        return None

    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        signals = []

        for ts_code, df in data.items():
            if df.empty or "close" not in df.columns:
                continue

            # 停牌过滤 (兼容 volume 与 vol 列名)
            vol_col = "volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
            if vol_col and df[vol_col].iloc[-1] == 0:
                continue

            curr = df.iloc[-1]
            price = curr["close"]
            if pd.isna(price) or price <= 0:
                continue

            # 计算当前标的的股息率
            div_yield = self._get_dividend_yield(ts_code, curr, trade_date)
            if div_yield is None:
                continue

            has_position = (portfolio is not None
                            and portfolio.get_position(ts_code) is not None
                            and not portfolio.get_position(ts_code).is_empty)

            # ----------------------------------------------------
            # 1. 买入判定：仅看股息率，高于 5% 就买 (或 >= buy_div_yield)
            # ----------------------------------------------------
            if not has_position and div_yield >= self.buy_div_yield:
                # 股息率越高，评分越高 (0.6 ~ 1.0)
                score = round(min(0.6 + (div_yield - self.buy_div_yield) * 0.1, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="BUY",
                    score=score,
                    reason=(f"纯股息率买入: 股息率达 {div_yield:.2f}% (>= {self.buy_div_yield:.1f}%), "
                            f"具备高分红配置价值 (现价={price:.2f}元)"),
                    price_ref=price,
                ))

            # ----------------------------------------------------
            # 2. 卖出判定：仅看股息率，低于 3.5% 就卖 (或 <= sell_div_yield)
            # ----------------------------------------------------
            elif (portfolio is None or has_position) and div_yield <= self.sell_div_yield:
                # 股息率越低，止盈平仓置信度越高 (0.6 ~ 1.0)
                score = round(min(0.6 + (self.sell_div_yield - div_yield) * 0.1, 1.0), 2)
                signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy=self.name,
                    direction="SELL",
                    score=score,
                    reason=(f"纯股息率卖出: 股息率降至 {div_yield:.2f}% (<= {self.sell_div_yield:.1f}%), "
                            f"估值偏高分红性价比稀释，触发止盈 (现价={price:.2f}元)"),
                    price_ref=price,
                ))

        return signals
