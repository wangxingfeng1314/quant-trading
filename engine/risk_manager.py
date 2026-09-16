"""统一风控管理器 (Risk Manager)

负责对组合中的持仓进行多维度出场风控检查：
  1. 固定比例止损 (Stop Loss)：亏损超过阈值（如 -5%）强制止损
  2. 动态跟踪止盈 (Trailing Stop)：最高浮盈达标后，从高点回撤超过阈值触发保盈平仓
  3. 最大持仓周期 (Max Holding Days)：防止资金长期沉淀于僵尸股
"""
from typing import Dict, List, Optional
from datetime import datetime
import logging

from core.models import Signal
from engine.portfolio import Portfolio

logger = logging.getLogger(__name__)


class RiskManager:
    """风控管理器"""

    def __init__(self,
                 stop_loss_pct: float = -5.0,
                 trailing_stop_activation: float = 8.0,
                 trailing_stop_callback: float = 3.0,
                 max_holding_days: int = 0,
                 trailing_stop_pct: Optional[float] = None,
                 trailing_callback_pct: Optional[float] = None):
        """
        Args:
            stop_loss_pct: 固定止损百分比，如 -5.0 代表亏损5%止损
            trailing_stop_activation: 移动跟踪止盈激活阈值百分比，如 8.0 代表涨幅达到8%激活
            trailing_stop_callback: 移动止盈回撤阈值百分比，如 3.0 代表从最高点回撤3%触发止盈
            max_holding_days: 最大持仓天数，0 表示不限制
            trailing_stop_pct: trailing_stop_activation 的参数别名
            trailing_callback_pct: trailing_stop_callback 的参数别名
        """
        if trailing_stop_pct is not None:
            trailing_stop_activation = trailing_stop_pct
        if trailing_callback_pct is not None:
            trailing_stop_callback = trailing_callback_pct

        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_activation = trailing_stop_activation
        self.trailing_stop_callback = trailing_stop_callback
        self.max_holding_days = max_holding_days

        # 跟踪每个持仓标的的最高价: {ts_code: highest_price}
        self.highest_prices: Dict[str, float] = {}

    def update_highs(self, current_prices: Dict[str, float], active_positions: Optional[dict] = None):
        """更新标的最新最高价（严格限定于当前持仓标的，防止未持仓股票的历史价格污染移动止盈基准）"""
        if active_positions is not None:
            active_codes = {c for c, p in active_positions.items() if not p.is_empty}
        else:
            active_codes = set(current_prices.keys())

        # 清理已平仓或非活跃的标的
        for code in list(self.highest_prices.keys()):
            if code not in active_codes:
                self.highest_prices.pop(code, None)

        for ts_code in active_codes:
            price = current_prices.get(ts_code)
            if price is None or price <= 0:
                continue

            pos = active_positions.get(ts_code) if active_positions else None
            cost = pos.avg_cost if (pos and pos.avg_cost > 0) else price

            if ts_code in self.highest_prices:
                if price > self.highest_prices[ts_code]:
                    self.highest_prices[ts_code] = price
            else:
                # 首次建仓跟踪，以建仓成本与当前价的较高者作为起始基准
                self.highest_prices[ts_code] = max(cost, price)

    def check_risks(self, trade_date: str, portfolio: Portfolio,
                    current_prices: Dict[str, float]) -> List[Signal]:
        """对所有当前持仓进行风控合规检查，生成风控平仓信号

        Args:
            trade_date: 当前交易日 YYYYMMDD
            portfolio: Portfolio 组合对象
            current_prices: 当日最新价格字典 {ts_code: price}

        Returns:
            触发风控的 Signal 列表（direction='SELL'）
        """
        self.update_highs(current_prices, active_positions=portfolio.positions)
        risk_signals = []

        for ts_code, pos in portfolio.positions.items():
            if pos.is_empty:
                self.highest_prices.pop(ts_code, None)
                continue

            price = current_prices.get(ts_code)
            if price is None or price <= 0:
                continue

            cost = pos.avg_cost
            if cost <= 0:
                continue

            # 1. 计算浮动盈亏百分比
            pnl_pct = (price / cost - 1) * 100

            # --- 风控规则1: 固定止损 ---
            if self.stop_loss_pct < 0 and pnl_pct <= self.stop_loss_pct:
                risk_signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy="risk_manager",
                    direction="SELL",
                    score=1.0,
                    reason=f"风控固定止损: 浮亏 {pnl_pct:.2f}% 触及止损线 ({self.stop_loss_pct}%)",
                    price_ref=price,
                    created_at=trade_date,
                    context_snapshot={
                        "risk_type": "stop_loss",
                        "cost": cost,
                        "current_price": price,
                        "pnl_pct": round(pnl_pct, 2),
                        "stop_loss_threshold": self.stop_loss_pct,
                    }
                ))
                continue

            # --- 风控规则2: 移动跟踪止盈 ---
            highest = self.highest_prices.get(ts_code, price)
            max_gain_pct = (highest / cost - 1) * 100

            if self.trailing_stop_activation > 0 and max_gain_pct >= self.trailing_stop_activation:
                # 已经激活移动止盈，检测从最高点的回撤
                drawdown_from_high = (1 - price / highest) * 100
                if drawdown_from_high >= self.trailing_stop_callback:
                    risk_signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy="risk_manager",
                        direction="SELL",
                        score=1.0,
                        reason=(f"风控跟踪止盈: 最高浮盈 {max_gain_pct:.2f}%, "
                                f"现自高点回撤 {drawdown_from_high:.2f}% 锁定利润"),
                        price_ref=price,
                        created_at=trade_date,
                        context_snapshot={
                            "risk_type": "trailing_stop",
                            "cost": cost,
                            "highest_price": highest,
                            "current_price": price,
                            "max_gain_pct": round(max_gain_pct, 2),
                            "drawdown_from_high": round(drawdown_from_high, 2),
                        }
                    ))
                    continue

            # --- 风控规则3: 最大持仓周期退出 ---
            if self.max_holding_days > 0 and pos.buy_date:
                try:
                    b_str = str(pos.buy_date).replace("-", "").replace("/", "")
                    c_str = str(trade_date).replace("-", "").replace("/", "")
                    b_dt = datetime.strptime(b_str, "%Y%m%d")
                    c_dt = datetime.strptime(c_str, "%Y%m%d")
                    holding_days = (c_dt - b_dt).days
                    if holding_days >= self.max_holding_days:
                        risk_signals.append(Signal(
                            ts_code=ts_code,
                            trade_date=trade_date,
                            strategy="risk_manager",
                            direction="SELL",
                            score=0.9,
                            reason=f"风控持仓到期: 已持有 {holding_days} 天超过上限 ({self.max_holding_days}天)",
                            price_ref=price,
                            created_at=trade_date,
                            context_snapshot={
                                "risk_type": "max_holding_days",
                                "buy_date": pos.buy_date,
                                "holding_days": holding_days,
                                "max_holding_days": self.max_holding_days,
                            }
                        ))
                        continue
                except Exception as e:
                    logger.warning(f"持仓周期计算失败: {e}")

        return risk_signals
