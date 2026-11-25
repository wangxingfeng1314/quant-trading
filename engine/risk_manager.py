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

    # 按策略名映射默认推荐风控预设（反转类给足震荡缓冲+时间止损；趋势类窄止损+保本+移动止盈）
    STRATEGY_PRESETS = {
        "kdj_cross": {
            "stop_loss_pct": -10.0,
            "trailing_stop_activation": 12.0,
            "trailing_stop_callback": 3.5,
            "max_holding_days": 35,
            "enable_break_even": False,
            "atr_stop_mult": 3.5,
            "atr_min_sl_pct": 5.0,
            "atr_max_sl_pct": 11.0,
        },
        "bollinger_reversal": {
            "stop_loss_pct": -10.0,
            "trailing_stop_activation": 12.0,
            "trailing_stop_callback": 3.5,
            "max_holding_days": 35,
            "enable_break_even": False,
            "atr_stop_mult": 3.5,
            "atr_min_sl_pct": 5.0,
            "atr_max_sl_pct": 11.0,
        },
        "ma_cross": {
            "stop_loss_pct": -6.0,
            "trailing_stop_activation": 12.0,
            "trailing_stop_callback": 4.0,
            "max_holding_days": 0,
            "enable_break_even": True,
            "break_even_trigger_pct": 6.0,
            "break_even_buffer_pct": 0.3,
            "atr_stop_mult": 2.2,
            "atr_min_sl_pct": 3.5,
            "atr_max_sl_pct": 8.0,
        },
        "signal_combo": {
            "stop_loss_pct": -6.0,
            "trailing_stop_activation": 12.0,
            "trailing_stop_callback": 4.0,
            "max_holding_days": 0,
            "enable_break_even": True,
            "break_even_trigger_pct": 6.0,
            "break_even_buffer_pct": 0.3,
            "atr_stop_mult": 2.2,
            "atr_min_sl_pct": 3.5,
            "atr_max_sl_pct": 8.0,
        },
        "momentum_rotation": {
            "stop_loss_pct": -8.0,
            "trailing_stop_activation": 15.0,
            "trailing_stop_callback": 5.0,
            "max_holding_days": 0,
            "enable_break_even": False,
            "atr_stop_mult": 2.8,
            "atr_min_sl_pct": 4.0,
            "atr_max_sl_pct": 10.0,
        },
    }

    def __init__(self,
                 stop_loss_pct: float = -5.0,
                 trailing_stop_activation: float = 8.0,
                 trailing_stop_callback: float = 3.0,
                 max_holding_days: int = 0,
                 enable_break_even: bool = False,
                 break_even_trigger_pct: float = 5.0,
                 break_even_buffer_pct: float = 0.2,
                 trailing_stop_pct: Optional[float] = None,
                 trailing_callback_pct: Optional[float] = None,
                 use_atr_stop: bool = False,
                 atr_stop_mult: float = 2.5,
                 atr_min_sl_pct: float = 3.0,
                 atr_max_sl_pct: float = 12.0,
                 atr_trailing_mult: float = 0.0,
                 partial_tp_pct: float = 0.0,
                 partial_tp_ratio: float = 0.5):
        """
        Args:
            stop_loss_pct: 固定止损百分比，如 -5.0 代表亏损5%止损
            trailing_stop_activation: 移动跟踪止盈激活阈值百分比，如 8.0 代表涨幅达到8%激活
            trailing_stop_callback: 移动止盈回撤阈值百分比，如 3.0 代表从最高点回撤3%触发止盈
            max_holding_days: 最大持仓天数，0 表示不限制
            enable_break_even: 是否启用保本止损保护机制
            break_even_trigger_pct: 保本激活阈值百分比，如 5.0 代表浮盈达5%后开启成本保护
            break_even_buffer_pct: 保本保护缓冲百分比，如 0.2 代表回落至成本+0.2%即平仓锁定本金
            trailing_stop_pct: trailing_stop_activation 的参数别名
            trailing_callback_pct: trailing_stop_callback 的参数别名
            use_atr_stop: 是否按标的 ATR% 自适应计算止损线（回退值为 stop_loss_pct）
            atr_stop_mult: ATR 止损倍数
            atr_min_sl_pct: ATR 自适应止损下限（%）
            atr_max_sl_pct: ATR 自适应止损上限（%）
            atr_trailing_mult: ATR 移动止盈回撤倍数（>0 时替代固定 trailing_stop_callback）
            partial_tp_pct: 分批止盈触发浮盈（%），>0 且持仓>=200股时先兑现部分仓位
            partial_tp_ratio: 分批止盈减仓比例（默认 0.5 即减半）
        """
        if trailing_stop_pct is not None:
            trailing_stop_activation = trailing_stop_pct
        if trailing_callback_pct is not None:
            trailing_stop_callback = trailing_callback_pct

        # 智能归一化兼容各类输入（小数如 0.05/0.08、正数如 5.0 等）
        if 0 < abs(stop_loss_pct) < 1.0:
            stop_loss_pct = stop_loss_pct * 100.0
        if stop_loss_pct > 0:
            stop_loss_pct = -stop_loss_pct

        if 0 < trailing_stop_activation < 1.0:
            trailing_stop_activation = trailing_stop_activation * 100.0

        if 0 < trailing_stop_callback < 1.0:
            trailing_stop_callback = trailing_stop_callback * 100.0

        if 0 < abs(break_even_trigger_pct) < 1.0:
            break_even_trigger_pct = break_even_trigger_pct * 100.0

        if 0 < abs(break_even_buffer_pct) <= 0.02:
            break_even_buffer_pct = break_even_buffer_pct * 100.0

        if 0 < partial_tp_pct < 1.0:
            partial_tp_pct = partial_tp_pct * 100.0

        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_activation = trailing_stop_activation
        self.trailing_stop_callback = trailing_stop_callback
        self.max_holding_days = max_holding_days
        self.enable_break_even = enable_break_even
        self.break_even_trigger_pct = break_even_trigger_pct
        self.break_even_buffer_pct = break_even_buffer_pct
        self.use_atr_stop = bool(use_atr_stop)
        self.atr_stop_mult = max(0.5, float(atr_stop_mult))
        self.atr_min_sl_pct = max(0.5, float(atr_min_sl_pct))
        self.atr_max_sl_pct = max(self.atr_min_sl_pct, float(atr_max_sl_pct))
        self.atr_trailing_mult = max(0.0, float(atr_trailing_mult))
        self.partial_tp_pct = max(0.0, float(partial_tp_pct))
        self.partial_tp_ratio = min(max(float(partial_tp_ratio), 0.1), 1.0)

        # 跟踪每个持仓标的的最高价: {ts_code: highest_price}
        self.highest_prices: Dict[str, float] = {}
        # 跟踪已触发过一次分批止盈的持仓标的，避免连续交易日重复减仓
        self.partial_tp_triggered: set = set()

    @classmethod
    def for_strategy(cls, strategy_name: str, use_atr_stop: bool = True) -> "RiskManager":
        """按策略风格生成配套的默认风控实例。"""
        preset = cls.STRATEGY_PRESETS.get(
            strategy_name,
            {
                "stop_loss_pct": -6.0,
                "trailing_stop_activation": 12.0,
                "trailing_stop_callback": 3.5,
            },
        )
        return cls(use_atr_stop=use_atr_stop, **preset)

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
                self.partial_tp_triggered.discard(code)

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
                    current_prices: Dict[str, float],
                    current_atr_pct: Optional[Dict[str, float]] = None) -> List[Signal]:
        """对所有当前持仓进行风控合规检查，生成风控平仓信号

        Args:
            trade_date: 当前交易日 YYYYMMDD
            portfolio: Portfolio 组合对象
            current_prices: 当日最新价格字典 {ts_code: price}
            current_atr_pct: 可选当日 ATR% 字典 {ts_code: atr_pct}，开启 ATR 自适应止损/止盈时生效

        Returns:
            触发风控的 Signal 列表（direction='SELL'）
        """
        self.update_highs(current_prices, active_positions=portfolio.positions)
        risk_signals = []

        for ts_code, pos in portfolio.positions.items():
            if pos.is_empty:
                self.highest_prices.pop(ts_code, None)
                self.partial_tp_triggered.discard(ts_code)
                continue

            price = current_prices.get(ts_code)
            if price is None or price <= 0:
                continue

            cost = pos.avg_cost
            if cost <= 0:
                continue

            raw_atr = current_atr_pct.get(ts_code) if current_atr_pct else None
            atr_pct_val = None
            if raw_atr is not None and raw_atr == raw_atr and float(raw_atr) > 0:
                atr_pct_val = float(raw_atr)

            # 1. 计算浮动盈亏百分比
            pnl_pct = (price / cost - 1) * 100

            # --- 风控规则1: 固定 / ATR 自适应止损 ---
            eff_stop_loss = self.stop_loss_pct
            atr_sl_applied = False
            if self.use_atr_stop and atr_pct_val is not None:
                atr_sl = max(self.atr_min_sl_pct, min(self.atr_max_sl_pct, atr_pct_val * self.atr_stop_mult))
                eff_stop_loss = -abs(atr_sl)
                atr_sl_applied = True

            if eff_stop_loss < 0 and pnl_pct <= eff_stop_loss:
                sl_tag = (
                    f"ATR动态止损 (-{abs(eff_stop_loss):.1f}%, {self.atr_stop_mult:g}×ATR)"
                    if atr_sl_applied else f"风控固定止损 ({eff_stop_loss}%)"
                )
                snap = {
                    "risk_type": "stop_loss",
                    "cost": cost,
                    "current_price": price,
                    "pnl_pct": round(pnl_pct, 2),
                    "stop_loss_threshold": round(eff_stop_loss, 2),
                }
                if atr_pct_val is not None:
                    snap["atr_pct"] = round(atr_pct_val, 2)
                risk_signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy="risk_manager",
                    direction="SELL",
                    score=1.0,
                    reason=f"{sl_tag}: 浮亏 {pnl_pct:.2f}% 触及止损线",
                    price_ref=price,
                    created_at=trade_date,
                    context_snapshot=snap,
                ))
                continue

            # --- 风控规则2: 移动跟踪止盈 ---
            highest = self.highest_prices.get(ts_code, price)
            max_gain_pct = (highest / cost - 1) * 100
            eff_trail_cb = self.trailing_stop_callback
            if self.atr_trailing_mult > 0 and atr_pct_val is not None:
                eff_trail_cb = max(2.0, min(10.0, atr_pct_val * self.atr_trailing_mult))

            if self.trailing_stop_activation > 0 and max_gain_pct >= self.trailing_stop_activation and highest > 0:
                # 已经激活移动止盈，检测从最高点的回撤
                drawdown_from_high = (1 - price / highest) * 100
                if drawdown_from_high >= eff_trail_cb:
                    risk_signals.append(Signal(
                        ts_code=ts_code,
                        trade_date=trade_date,
                        strategy="risk_manager",
                        direction="SELL",
                        score=1.0,
                        reason=(f"风控跟踪止盈: 最高浮盈 {max_gain_pct:.2f}%, "
                                f"现自高点回撤 {drawdown_from_high:.2f}% (阈值{eff_trail_cb:.1f}%) 锁定利润"),
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

            # --- 风控规则3: 保本止损保护 (Break-even Stop) ---
            # 浮盈曾达到预设触发线（如 +5%），若后续回撤至保本保护线（成本 + 0.2%缓冲）触发平仓，锁定本金不转亏
            if self.enable_break_even and max_gain_pct >= self.break_even_trigger_pct and pnl_pct <= self.break_even_buffer_pct:
                risk_signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy="risk_manager",
                    direction="SELL",
                    score=1.0,
                    reason=(f"风控保本平仓: 最高浮盈曾达 +{max_gain_pct:.2f}%, "
                            f"现回落至成本保护线 ({pnl_pct:.2f}%) 锁定本金离场"),
                    price_ref=price,
                    created_at=trade_date,
                    context_snapshot={
                        "risk_type": "break_even_stop",
                        "cost": cost,
                        "highest_price": highest,
                        "current_price": price,
                        "max_gain_pct": round(max_gain_pct, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "break_even_buffer_pct": self.break_even_buffer_pct,
                    }
                ))
                continue

            # --- 风控规则4: 分批止盈 (Partial Take Profit) ---
            avail_shares = getattr(pos, "available_shares", pos.shares)
            if (
                self.partial_tp_pct > 0
                and ts_code not in self.partial_tp_triggered
                and pnl_pct >= self.partial_tp_pct
                and avail_shares >= 200
            ):
                self.partial_tp_triggered.add(ts_code)
                risk_signals.append(Signal(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    strategy="risk_manager",
                    direction="SELL",
                    score=0.95,
                    reason=(f"风控分批止盈: 浮盈 {pnl_pct:.2f}% 达标 (+{self.partial_tp_pct:.1f}%)，"
                            f"先兑现 {int(round(self.partial_tp_ratio * 100))}% 仓位锁利"),
                    price_ref=price,
                    created_at=trade_date,
                    context_snapshot={
                        "risk_type": "partial_take_profit",
                        "sell_ratio": self.partial_tp_ratio,
                        "cost": cost,
                        "current_price": price,
                        "pnl_pct": round(pnl_pct, 2),
                        "partial_tp_pct": self.partial_tp_pct,
                    }
                ))
                continue

            # --- 风控规则5: 最大持仓周期退出 ---
            if self.max_holding_days > 0 and pos.buy_date:
                try:
                    b_str = str(pos.buy_date).replace("-", "").replace("/", "").strip()[:8]
                    c_str = str(trade_date).replace("-", "").replace("/", "").strip()[:8]
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


def clone_risk_manager(risk_manager):
    """创建**配置完全相同、状态独立**的风控实例。

    网格搜索 / walk-forward 会跑几十次回测，若共用一个 RiskManager，
    ``highest_prices`` / ``partial_tp_triggered`` 等可变状态会跨组合、跨折污染。
    """
    if risk_manager is None:
        return None
    try:
        return RiskManager(
            stop_loss_pct=risk_manager.stop_loss_pct,
            trailing_stop_activation=risk_manager.trailing_stop_activation,
            trailing_stop_callback=risk_manager.trailing_stop_callback,
            max_holding_days=risk_manager.max_holding_days,
            enable_break_even=getattr(risk_manager, "enable_break_even", False),
            break_even_trigger_pct=getattr(risk_manager, "break_even_trigger_pct", 5.0),
            break_even_buffer_pct=getattr(risk_manager, "break_even_buffer_pct", 0.2),
            use_atr_stop=getattr(risk_manager, "use_atr_stop", False),
            atr_stop_mult=getattr(risk_manager, "atr_stop_mult", 2.5),
            atr_min_sl_pct=getattr(risk_manager, "atr_min_sl_pct", 3.0),
            atr_max_sl_pct=getattr(risk_manager, "atr_max_sl_pct", 12.0),
            atr_trailing_mult=getattr(risk_manager, "atr_trailing_mult", 0.0),
            partial_tp_pct=getattr(risk_manager, "partial_tp_pct", 0.0),
            partial_tp_ratio=getattr(risk_manager, "partial_tp_ratio", 0.5),
        )
    except Exception as e:  # pragma: no cover - 风控结构变化时的兜底
        logger.warning(f"风控实例克隆失败，退回共享实例: {e}")
        return risk_manager
